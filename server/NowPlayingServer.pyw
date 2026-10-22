"""
XE Visualizer companion server v2
=================================
Captures system audio (WASAPI loopback), turns it into a small set of
log-spaced, dB-scaled bands, reads track info from Windows media controls
(SMTC), and pushes everything to the iCUE widget over a WebSocket.

The heavy lifting (FFT, band mapping, dB scaling) happens here so the widget
only has to draw: each audio frame is 96 bytes, sent base64-encoded.

Messages (server -> widget):
    {"b": "<base64 of BANDS uint8 levels>"}                 every audio frame
    {"type": "media", "title", "artist", "album", "playing", "pos", "dur"}  ~1/s
    {"type": "art", "artUrl": "data:image/...;base64,..."}  on track change
Messages (widget -> server):
    {"type": "control", "action": "playpause"|"next"|"previous"|"seek", "position": seconds}

Dependencies:
    pip install PyAudioWPatch numpy websockets pystray Pillow winrt-runtime winrt-Windows.Media.Control winrt-Windows.Storage.Streams winrt-Windows.Foundation.Collections
"""

import argparse
import asyncio
import base64
import collections
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("XEVisualizer")

BANDS = 96
F_LO, F_HI = 30.0, 16000.0
BASS_SPLIT_HZ = 250.0   # below this, use the high-resolution bass FFT
DB_FLOOR = -48.0
PEAK_WINDOW_SECS = 10.0


def _detect_image_type(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/png"


class _RollingPeak:
    """Max over the last PEAK_WINDOW_SECS, so levels adapt slowly to volume."""

    def __init__(self):
        self._hist = collections.deque()

    def update(self, value):
        now = time.monotonic()
        self._hist.append((now, value))
        while self._hist and self._hist[0][0] < now - PEAK_WINDOW_SECS:
            self._hist.popleft()
        return max(v for _, v in self._hist)

    def reset(self):
        self._hist.clear()


# ---------------------------------------------------------------------------
# Audio -> bands
# ---------------------------------------------------------------------------

# Widget "Audio Source" keys -> loopback device name fragment. SteelSeries Sonar
# splits audio into virtual devices; music players land on "Media" by default,
# while the Windows default output is usually "Gaming" (i.e. the game).
SOURCES = {
    "media": "Sonar - Media", "gaming": "Sonar - Gaming", "chat": "Sonar - Chat",
    "aux": "Sonar - Aux", "stream": "Sonar - Stream",
}


class AudioCapture:
    CHUNK = 1024
    BASS_RATE = 4000
    BASS_CHUNK = 1024

    def __init__(self):
        self._lock = threading.Lock()
        self._bands = bytes(BANDS)
        self._version = 0
        self._running = False
        self._streams = []
        self._pa = None
        self._peak = _RollingPeak()
        self._bass_peak = _RollingPeak()
        self._low_smoothed = 0.0
        self.on_frame = None  # called from the capture thread after each new frame
        self.source = "auto"
        self._restart = False

    def set_source(self, source):
        # A source may combine channels, e.g. "media+aux" (YouTube + Spotify).
        parts = str(source).split("+")
        if all(p in SOURCES for p in parts) or source in ("auto", "default"):
            if source != self.source:
                log.info(f"Audio source -> {source}")
                self.source = source
                self._restart = True

    def _pick_devices(self, pa):
        loopbacks = [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
        loopbacks = [d for d in loopbacks if d.get("isLoopbackDevice")]
        src = "media" if self.source == "auto" else self.source
        found = []
        if src != "default":
            for key in src.split("+"):
                found += [d for d in loopbacks if SOURCES[key].lower() in d["name"].lower()][:1]
        if found:
            return found
        try:
            return [pa.get_default_wasapi_loopback()]
        except Exception:
            return loopbacks[:1]

    def snapshot(self):
        with self._lock:
            return self._version, self._bands

    def reset_peaks(self):
        self._peak.reset()
        self._bass_peak.reset()

    def start(self):
        self._running = True
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _capture_loop(self):
        while self._running:
            try:
                self._run_capture()
            except Exception as e:
                log.warning(f"Audio capture error: {e}")
            if self._running and not self._restart:
                time.sleep(2)
            self._restart = False

    def _band_plan(self, rate, bass_rate):
        """Per band: (use_bass, lo_bin, hi_bin, center_bin) for the chosen FFT."""
        centers = np.geomspace(F_LO, F_HI, BANDS)
        edges = np.sqrt(centers[:-1] * centers[1:])
        lo_f = np.concatenate([[centers[0] / 1.05], edges])
        hi_f = np.concatenate([edges, [centers[-1] * 1.05]])
        main_hz = rate / self.CHUNK
        bass_hz = bass_rate / self.BASS_CHUNK
        plan = []
        for c, lo, hi in zip(centers, lo_f, hi_f):
            use_bass = hi <= BASS_SPLIT_HZ
            hz = bass_hz if use_bass else main_hz
            plan.append((use_bass, int(np.floor(lo / hz)), int(np.ceil(hi / hz)), c / hz))
        # Mild treble lift: music has far less energy up top, so without it
        # the right half of the visualizer barely moves.
        tilt = np.clip((centers / 200.0) ** 0.3, 1.0, None)
        return plan, tilt

    def _run_capture(self):
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()
        try:
            self._capture_from(pyaudio, self._pick_devices(self._pa))
        finally:
            self.stop_stream()

    def stop_stream(self):
        for st, _ in self._streams:
            try:
                st.close()
            except Exception:
                pass
        try:
            self._pa.terminate()
        except Exception:
            pass
        self._streams = []

    def _publish(self, packed):
        with self._lock:
            self._bands = packed
            self._version += 1
        if self.on_frame:
            self.on_frame()

    def _capture_from(self, pyaudio, devices):
        if not devices:
            log.warning("No WASAPI loopback device found; retrying")
            return

        rate = int(devices[0]["defaultSampleRate"])
        decimate = max(1, rate // self.BASS_RATE)
        bass_rate = rate / decimate
        bass_needed = self.BASS_CHUNK * decimate
        plan, tilt = self._band_plan(rate, bass_rate)
        low_bin = max(1, int(BASS_SPLIT_HZ / (rate / self.CHUNK)))

        # Hanning x exponential ramp: recency-biased for snappy response.
        main_window = np.hanning(self.CHUNK) * np.exp(np.linspace(-8, 0, self.CHUNK))
        bass_window = np.hanning(self.BASS_CHUNK)
        bass_buf = np.zeros(0, dtype=np.float32)
        bass_spec = np.zeros(self.BASS_CHUNK // 2 + 1)
        out = np.zeros(BANDS)

        for d in devices:
            if int(d["defaultSampleRate"]) != rate:
                # shortcut: no resampling; set both Sonar devices to the same rate to mix them.
                log.warning(f"Skipping {d['name']}: {int(d['defaultSampleRate'])}Hz differs from {rate}Hz")
                continue
            ch = int(d["maxInputChannels"])
            st = self._pa.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                               input_device_index=d["index"], frames_per_buffer=self.CHUNK)
            self._streams.append((st, ch))
            log.info(f"Audio: {d['name']} @ {rate}Hz, {ch}ch")

        idle_since = time.monotonic()
        while self._running and not self._restart:
            # A loopback device sends nothing at all while it's silent, so a
            # blocking read would hang (and block source switches). Poll instead,
            # and mix whichever devices currently have audio.
            samples = None
            try:
                for st, ch in self._streams:
                    if st.get_read_available() < self.CHUNK:
                        continue
                    x = np.frombuffer(st.read(self.CHUNK, exception_on_overflow=False), dtype=np.int16).astype(np.float32)
                    if ch >= 2:
                        x = x.reshape(-1, ch).mean(axis=1)
                    samples = x if samples is None else samples + x
            except Exception:
                break
            if samples is None:
                if time.monotonic() - idle_since > 0.15 and any(self._bands):
                    self._publish(bytes(BANDS))  # went silent: drop the bars now
                time.sleep(0.004)
                continue
            idle_since = time.monotonic()

            spec = np.abs(np.fft.rfft(samples * main_window))
            peak = self._peak.update(float(spec.max()))
            if peak > 0:
                spec /= peak

            bass_buf = np.concatenate([bass_buf, samples])[-bass_needed:]
            if len(bass_buf) == bass_needed:
                dec = bass_buf.reshape(self.BASS_CHUNK, decimate).mean(axis=1)
                bass_spec = np.abs(np.fft.rfft(dec * bass_window))
                bpeak = self._bass_peak.update(float(bass_spec.max()))
                if bpeak > 0:
                    bass_spec /= bpeak

            # The bass FFT spans ~256 ms, so it reacts slowly. Scale it by how
            # the fast main FFT's low end compares to its recent average.
            low_now = float(spec[:low_bin].mean())
            self._low_smoothed = self._low_smoothed * 0.93 + low_now * 0.07
            env = min(2.5, low_now / self._low_smoothed) if self._low_smoothed > 1e-4 else 1.0

            for i, (use_bass, lo, hi, center) in enumerate(plan):
                src = bass_spec if use_bass else spec
                hi = min(hi, len(src) - 1)
                if hi - lo >= 2:
                    v = src[lo:hi].max()
                else:
                    j = min(int(center), len(src) - 2)
                    f = center - j
                    v = src[j] * (1 - f) + src[j + 1] * f
                out[i] = v * env if use_bass else v
            out *= tilt

            db = 20.0 * np.log10(np.maximum(out, 1e-6))
            levels = np.clip((db - DB_FLOOR) / -DB_FLOOR, 0.0, 1.0)
            self._publish((levels * 255.0 + 0.5).astype(np.uint8).tobytes())


# ---------------------------------------------------------------------------
# Track info + playback control (Windows SMTC)
# ---------------------------------------------------------------------------

class MediaInfo:
    def __init__(self):
        self._session = None
        self._last_title = None
        self._raw = None              # last (title, position, playing) the app reported
        self._anchor = (0.0, 0.0)     # (position, monotonic time) our clock counts from
        self.data = {"type": "media", "title": "", "artist": "", "album": "", "playing": False, "pos": 0.0, "dur": 0.0,
                     "live": False, "platform": ""}
        self._art_aspect = 0.0   # width / height of the current thumbnail, 0 = none
        self._art_retries = 0    # browsers attach the thumbnail after the title; keep re-checking briefly
        self.art_url = ""
        self.art_version = 0
        self.track_changed = False
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
            )
            self._Manager, self._Status = Manager, Status
        except ImportError as e:
            self._Manager = None
            log.warning(f"SMTC unavailable ({e}). Install winrt-Windows.Media.Control for track info.")

    async def poll(self):
        if not self._Manager:
            return
        try:
            manager = await asyncio.wait_for(self._Manager.request_async(), 5.0)
            s = self._pick_session(manager)
            self._session = s
            if not s:
                self.data.update(title="", artist="", album="", playing=False, pos=0.0, dur=0.0, live=False, platform="")
                if self._last_title:
                    self._last_title = ""
                    self.art_url = ""
                    self.art_version += 1
                return
            info = await asyncio.wait_for(s.try_get_media_properties_async(), 5.0)
            playing = s.get_playback_info().playback_status == self._Status.PLAYING
            title = info.title or ""
            pos = dur = 0.0
            live = False
            try:
                tl = s.get_timeline_properties()
                raw_dur = (tl.end_time - tl.start_time).total_seconds()
                live = raw_dur < 0          # live streams (Twitch) report an end before the start
                dur = max(0.0, raw_dur)
                pos = self._track_position(title, playing, tl)
                pos = max(0.0, min(dur, pos)) if dur else 0.0
            except Exception:
                pass
            if title != self._last_title:
                self._last_title = title
                self.track_changed = True
                self._art_retries = 6
                self.art_url, self._art_aspect = await self._read_art(info.thumbnail)
                self.art_version += 1
            elif self._art_retries > 0:
                self._art_retries -= 1
                url, aspect = await self._read_art(info.thumbnail)
                if url and url != self.art_url:
                    self.art_url, self._art_aspect = url, aspect
                    self.art_version += 1
            self.data.update(title=title, artist=info.artist or "", album=info.album_title or "",
                             playing=playing, pos=round(pos, 2), dur=round(dur, 2), live=live,
                             platform=self._platform(s.source_app_user_model_id, live, info.album_title or ""))
        except Exception as e:
            log.debug(f"SMTC poll failed: {type(e).__name__}: {e}")

    def _pick_session(self, manager):
        """Prefer a session that is actually playing; Windows' "current" one may be paused."""
        try:
            sessions = manager.get_sessions()
            for i in range(sessions.size):
                cand = sessions.get_at(i)
                if cand.get_playback_info().playback_status == self._Status.PLAYING:
                    return cand
        except Exception:
            pass  # needs winrt-Windows.Foundation.Collections
        return manager.get_current_session()

    def _track_position(self, title, playing, tl):
        """Our own steady clock, re-synced only when the app reports something new.

        Spotify refreshes last_updated_time every few seconds without updating
        position, so "position + time since last_updated" jumps backwards on
        every refresh. Instead: anchor when the reported position, track or
        play state changes, and count forward with a monotonic clock."""
        raw_pos = (tl.position - tl.start_time).total_seconds()
        now = time.monotonic()
        prev = self._raw
        self._raw = (title, round(raw_pos, 2), playing)
        if prev is None or prev[0] != title or prev[1] != self._raw[1]:
            # Fresh report: add the time since the app took it, if that's believable.
            age = (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
            self._anchor = (raw_pos + (age if playing and 0 <= age < 5 else 0.0), now)
        elif prev[2] != playing:
            # Only play/pause changed; the reported position is stale, so keep our count.
            p0, t0 = self._anchor
            self._anchor = (p0 + (now - t0 if prev[2] else 0.0), now)
        p0, t0 = self._anchor
        return p0 + (now - t0 if playing else 0.0)

    BROWSERS = ("chrome", "msedge", "firefox", "308046b0af4a39cb", "comet", "opera", "brave", "vivaldi", "arc", "zen")

    def _platform(self, app_id, live, album):
        """Best guess at the service. Browsers don't expose the site, so infer it:
        YouTube sends 16:9 video thumbnails, Twitch sends the streamer's square avatar
        (and live streams report no end). YouTube Music is square too but sets an album."""
        a = (app_id or "").lower()
        if "spotify" in a:
            return "spotify"
        if not any(b in a for b in self.BROWSERS):
            return ""
        if self._art_aspect > 1.25:
            return "youtube"
        if 0.8 < self._art_aspect <= 1.25:
            return "youtube" if album else "twitch"
        return "twitch" if live else ""

    async def _read_art(self, thumb):
        """Returns (data URL, width/height); ("", 0.0) when there's no usable image."""
        if not thumb:
            return "", 0.0
        try:
            from winrt.windows.storage.streams import DataReader
            stream = await asyncio.wait_for(thumb.open_read_async(), 5.0)
            reader = DataReader(stream)
            await asyncio.wait_for(reader.load_async(stream.size), 5.0)
            try:
                art = bytes(reader.read_buffer(stream.size))
            except (TypeError, ValueError):  # older winrt builds
                data = bytearray(stream.size)
                reader.read_bytes(data)
                art = bytes(data)
            aspect = 0.0
            try:
                from PIL import Image
                import io
                w, h = Image.open(io.BytesIO(art)).size
                aspect = w / h if h else 0.0
            except Exception:
                pass
            return f"data:{_detect_image_type(art)};base64,{base64.b64encode(art).decode()}", aspect
        except Exception as e:
            log.warning(f"Art read failed: {type(e).__name__}: {e}")
            return "", 0.0

    async def control(self, action, position=None):
        s = self._session
        if not s:
            return
        try:
            if action == "playpause":
                op = s.try_toggle_play_pause_async()
            elif action == "next":
                op = s.try_skip_next_async()
            elif action == "previous":
                op = s.try_skip_previous_async()
            elif action == "seek" and isinstance(position, (int, float)) and position >= 0:
                op = s.try_change_playback_position_async(int(position * 10_000_000))  # 100 ns ticks
                self.data["pos"] = float(position)
                self._anchor = (float(position), time.monotonic())
            else:
                return
            await asyncio.wait_for(op, 3.0)
        except Exception as e:
            log.warning(f"Control '{action}' failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# WebSocket server
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, port, fps):
        self.port = port
        self.fps = fps
        self.audio = AudioCapture()
        self.media = MediaInfo()
        self.clients = set()
        self.no_bands = set()
        self.pv_audio = set()   # preview sockets that want sound
        self._img_cache = {}   # clients (e.g. the chat widget) that only want track info
        self._last_push = 0.0
        self._was_silent = False

    def _broadcast(self, msg):
        import websockets
        if self.clients:
            websockets.broadcast(self.clients, msg)  # never blocks on a slow client

    IMG_HOSTS = ("static-cdn.jtvnw.net", "cdn.7tv.app", "cdn.betterttv.net", "cdn.frankerfacez.com")

    async def _http(self, connection, request):
        """GET /img?u=<url>: fetch an emote/badge image for the chat widget. Some CDNs
        (BetterTTV) don't allow cross-origin fetches from a widget page; only the
        known image hosts are allowed, so this can't be used as an open proxy."""
        from urllib.parse import urlsplit, parse_qs
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        parts = urlsplit(request.path)
        if parts.path == "/tw":
            return await self._twitch_proxy(parse_qs(parts.query), Headers, Response)
        if parts.path != "/img":
            return None  # normal WebSocket upgrade
        url = (parse_qs(parts.query).get("u") or [""])[0]
        u = urlsplit(url)
        if u.scheme != "https" or u.hostname not in self.IMG_HOSTS:
            return Response(403, "Forbidden", Headers({"Access-Control-Allow-Origin": "*"}), b"")
        hit = self._img_cache.get(url)
        if not hit:
            def get():
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "XEVisualizer"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.headers.get("Content-Type", "image/png"), r.read(2_000_000)
            try:
                hit = await asyncio.to_thread(get)
            except Exception:
                return Response(502, "Bad Gateway", Headers({"Access-Control-Allow-Origin": "*"}), b"")
            if len(self._img_cache) > 800:
                self._img_cache.clear()
            self._img_cache[url] = hit
        return Response(200, "OK", Headers({"Content-Type": hit[0], "Access-Control-Allow-Origin": "*",
                                            "Cache-Control": "max-age=86400"}), hit[1])

    TW_HOSTS = ("id.twitch.tv", "api.twitch.tv", "gql.twitch.tv")

    async def _twitch_proxy(self, q, Headers, Response):
        """GET /tw?m=&u=&b=&a=&c=: forward a Twitch sign-in/API call. iCUE's browser
        blocks the widget's own POSTs to Twitch. Only Twitch hosts are allowed."""
        from urllib.parse import urlsplit
        import urllib.request
        import urllib.error
        g = lambda k: (q.get(k) or [""])[0]
        url, method = g("u"), (g("m") or "GET").upper()
        cors = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json", "Cache-Control": "no-store"}
        u = urlsplit(url)
        host = u.hostname or ""
        # Stream preview: playlists refuse the widget's "null" origin, so they come through here
        ok_host = host in self.TW_HOSTS or host.endswith(".ttvnw.net")
        if u.scheme != "https" or not ok_host or method not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
            return Response(403, "Forbidden", Headers(cors), b"")
        hdrs = {"User-Agent": "XEChat"}
        if g("a"):
            hdrs["Authorization"] = g("a")
        if g("c"):
            hdrs["Client-Id"] = g("c")
        body = g("b").encode() if g("b") else None
        if body is not None:
            hdrs["Content-Type"] = g("t") or "application/x-www-form-urlencoded"

        def go():
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read(4_000_000)
            except urllib.error.HTTPError as e:
                return e.code, e.read(100_000)
        try:
            status, data = await asyncio.to_thread(go)
        except Exception:
            return Response(502, "Bad Gateway", Headers(cors), b"")
        return Response(status, "OK" if status < 400 else "Error", Headers(cors), data)

    _pv_q = None
    pv_device = None   # preview sound output (WASAPI device name); None = Windows default
    pv_volume = 0.8

    @staticmethod
    def _wasapi(pa):
        import pyaudiowpatch as pyaudio
        w = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        devs = [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
        return w, [d for d in devs if d["hostApi"] == w["index"] and d["maxOutputChannels"] > 0 and not d.get("isLoopbackDevice")]

    def _out_devices(self):
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        try:
            return [d["name"] for d in self._wasapi(pa)[1]]
        finally:
            pa.terminate()

    def _pv_play(self, pcm):
        """Play preview sound (48 kHz stereo 16-bit) on the chosen Windows output."""
        import queue
        if self._pv_q is None:
            self._pv_q = queue.Queue()

            def run():
                try:
                    play()
                except Exception:
                    log.exception("preview audio stopped")
                    self._pv_q = None

            def play():
                import pyaudiowpatch as pyaudio
                pa, st, opened, rate, chans = pyaudio.PyAudio(), None, object(), 48000, 2
                while True:
                    data = self._pv_q.get()
                    while self._pv_q.qsize() > 15:   # >1.5 s behind: skip ahead to stay in sync with the picture
                        data = self._pv_q.get()
                    if opened != self.pv_device or st is None:
                        if st:
                            st.close()
                        w, outs = self._wasapi(pa)
                        dev = next((d for d in outs if d["name"] == self.pv_device), None) or pa.get_device_info_by_index(w["defaultOutputDevice"])
                        # WASAPI shared mode only takes the device's own rate and channel count (Sonar: 8)
                        rate, chans = int(dev["defaultSampleRate"]), max(2, int(dev["maxOutputChannels"]))
                        try:
                            st = pa.open(format=pyaudio.paInt16, channels=chans, rate=rate, output=True,
                                         output_device_index=dev["index"], frames_per_buffer=2048)
                        except Exception as e:
                            log.warning("preview audio: can't open %s (%s)", dev["name"], e)
                            st = None
                        else:
                            log.info("preview audio on %s at %d Hz", dev["name"], rate)
                        opened = self.pv_device
                        if st is None:
                            continue
                    a = np.frombuffer(data, np.int16).reshape(-1, 2).astype(np.float32) * self.pv_volume
                    if rate != 48000:
                        x = np.linspace(0, len(a) - 1, int(len(a) * rate / 48000))
                        a = np.stack([np.interp(x, np.arange(len(a)), a[:, ch]) for ch in (0, 1)], 1)
                    if chans > 2:   # stereo into front left/right, other speakers silent
                        a = np.hstack([a, np.zeros((len(a), chans - 2), np.float32)])
                    st.write(np.clip(a, -32768, 32767).astype(np.int16).tobytes())
            threading.Thread(target=run, daemon=True, name="preview-audio").start()
        self._pv_q.put(pcm)

    async def _preview_relay(self, ws, channel):
        """Stream preview for the chat widget. iCUE's browser can't play Twitch's H.264
        video, so the stream is decoded here and sent as JPEG frames (720p, up to 60 fps), using Twitch's low-latency playlist."""
        import io
        import random
        import urllib.parse
        import urllib.request

        def get(url, data=None, headers=None):
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()

        def variant():
            q = json.dumps({"query": 'query{streamPlaybackAccessToken(channelName:"%s",params:{platform:"web",'
                            'playerBackend:"mediaplayer",playerType:"embed"}){value signature}}' % channel}).encode()
            t = json.loads(get("https://gql.twitch.tv/gql", q, {"Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko"}))
            t = (t.get("data") or {}).get("streamPlaybackAccessToken")
            if not t:
                raise RuntimeError("offline")
            qs = urllib.parse.urlencode({"sig": t["signature"], "token": t["value"], "allow_source": "true",
                                         "fast_bread": "true", "p": random.randint(0, 999999)})
            lines = get("https://usher.ttvnw.net/api/channel/hls/%s.m3u8?%s" % (channel, qs)).decode().splitlines()
            best = None
            for i, line in enumerate(lines[:-1]):
                m = re.search(r"RESOLUTION=\d+x(\d+)", line)
                if m and lines[i + 1].startswith("https"):
                    score = abs(int(m.group(1)) - 720)
                    if best is None or score < best[0]:
                        best = (score, lines[i + 1].strip())
            if not best:
                raise RuntimeError("no playable quality")
            return best[1]

        def decode(url, with_audio, emit):
            """Decode a chunk while it downloads (Twitch's prefetch chunks arrive as they're
            broadcast), handing out frames and ~0.1 s audio pieces as soon as they exist."""
            import av
            last, pcm = -1.0, []
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r, av.open(r, format="mpegts") as c:
                vs = c.streams.video[0]
                vs.thread_type = "AUTO"
                streams = [vs] + (list(c.streams.audio[:1]) if with_audio else [])
                rs = av.AudioResampler(format="s16", layout="stereo", rate=48000) if with_audio else None
                for f in (fr for pk in c.demux(*streams) for fr in pk.decode()):
                    if isinstance(f, av.AudioFrame):
                        pcm.extend(x.to_ndarray().tobytes() for x in rs.resample(f))
                        if sum(map(len, pcm)) >= 19200:   # 0.1 s of 48 kHz stereo
                            emit(None, b"".join(pcm)); pcm = []
                        continue
                    t = float(f.time or 0)
                    if t - last < 0.015:   # cap at ~60 fps
                        continue
                    last = t
                    im = f.to_image()
                    if im.width > 1280:
                        im = im.resize((1280, im.height * 1280 // im.width))
                    b = io.BytesIO()
                    im.save(b, "JPEG", quality=78)
                    emit(t, b.getvalue())
            if pcm:
                emit(None, b"".join(pcm))

        def parse(text):
            """[(segment url, wall-clock epoch when it was broadcast)]; Twitch's prefetch
            entries are upcoming segments, which is what keeps the delay low."""
            from datetime import datetime as dt
            out, pdt, dur = [], None, 2.0
            for line in text.splitlines():
                if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                    try:
                        pdt = dt.fromisoformat(line.split(":", 1)[1].strip().replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pdt = None
                elif line.startswith("#EXTINF:"):
                    try:
                        dur = float(line[8:].split(",")[0])
                    except ValueError:
                        pass
                elif line.startswith("https"):
                    out.append((line.strip(), pdt)); pdt = pdt + dur if pdt else None
                elif line.startswith("#EXT-X-TWITCH-PREFETCH:"):
                    out.append((line.split(":", 1)[1].strip(), pdt)); pdt = pdt + dur if pdt else None
            return out

        loop = asyncio.get_running_loop()
        frames_q = asyncio.Queue(maxsize=480)

        async def produce():
            while True:
                try:
                    url, seen = await asyncio.to_thread(variant), collections.deque(maxlen=60)
                    while True:
                        segs = parse((await asyncio.to_thread(get, url)).decode())
                        new = [x for x in segs if x[0] not in seen] if seen else segs[-1:]   # start at the live edge
                        seen.extend(x[0] for x in segs)
                        for seg, pdt in new:
                            first = []

                            def emit(t, data, pdt=pdt, first=first):   # runs on the decode thread
                                if t is not None and not first:
                                    first.append(t)
                                aired = pdt + (t - first[0]) if (pdt and t is not None) else None
                                asyncio.run_coroutine_threadsafe(frames_q.put((t, data, aired)), loop).result()
                            try:
                                await asyncio.to_thread(decode, seg, ws in self.pv_audio, emit)
                            except Exception:
                                continue   # one bad chunk (ad splice, cut-off prefetch): skip it, keep playing
                        if not new:
                            await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    try:
                        await ws.send(json.dumps({"type": "pverr", "msg": str(e)[:80] or "error"}))
                    except Exception:
                        return
                    await asyncio.sleep(10)

        producer = asyncio.create_task(produce())
        try:
            import struct
            LEAD = 0.25   # frames go out this early; the widget buffers them and shows each on time
            base = None   # (stream time, wall time) that frame timing is measured from
            sent = 0
            while True:
                t, jpg, aired = await frames_q.get()
                if t is None:
                    if ws in self.pv_audio:   # iCUE mutes widget audio, so the server plays it (in step with the picture)
                        loop.call_later(LEAD, self._pv_play, jpg)
                    continue
                now = loop.time()
                due = base and base[1] + (t - base[0])
                if base is None or due is None or due - now > 2.0 or now - due > 0.1:   # start, jump, or a late chunk: re-anchor
                    base, due = (t, now + LEAD), now + LEAD
                if due - LEAD > now:
                    await asyncio.sleep(due - LEAD - now)
                await ws.send(b"" + struct.pack("<d", t) + jpg)   # stream time + JPEG frame
                sent += 1
                if aired and sent % 30 == 0:   # how far behind the streamer this frame is
                    await ws.send(json.dumps({"type": "pvlat", "s": round(time.time() + LEAD - aired, 1)}))
        finally:
            producer.cancel()

    async def _irc_relay(self, ws, channel):
        """Bridge Twitch chat for the chat widget. iCUE's embedded browser can't open
        WebSockets to the internet (they fail with 1006), but localhost works, so the
        widget connects here and we hold the read-only (anonymous) Twitch connection."""
        import random
        import websockets
        while ws.close_code is None:
            try:
                async with websockets.connect("wss://irc-ws.chat.twitch.tv:443", max_size=2 ** 20) as up:
                    await up.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                    await up.send(f"NICK justinfan{random.randint(10000, 89999)}")
                    await up.send(f"JOIN #{channel}")
                    log.info(f"Chat relay: joined #{channel}")
                    async for frame in up:
                        if frame.startswith("PING"):
                            await up.send("PONG :tmi.twitch.tv")
                            continue
                        await ws.send(frame)
            except Exception as e:
                if ws.close_code is not None:
                    return
                log.warning(f"Chat relay for #{channel} dropped ({type(e).__name__}); reconnecting")
                await asyncio.sleep(3)

    async def _handler(self, ws):
        self.clients.add(ws)
        relay = None
        try:
            await ws.send(json.dumps(self.media.data))
            await ws.send(json.dumps({"type": "art", "artUrl": self.media.art_url}))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(msg, dict) and msg.get("type") == "irc":
                    channel = str(msg.get("channel", "")).lower()
                    if re.fullmatch(r"[a-z0-9_]{1,25}", channel) and relay is None:
                        self.clients.discard(ws)   # chat-only socket: no audio or track info
                        relay = asyncio.create_task(self._irc_relay(ws, channel))
                elif isinstance(msg, dict) and msg.get("type") == "preview":
                    channel = str(msg.get("channel", "")).lower()
                    if re.fullmatch(r"[a-z0-9_]{1,25}", channel) and relay is None:
                        self.clients.discard(ws)   # preview-only socket
                        relay = asyncio.create_task(self._preview_relay(ws, channel))
                elif isinstance(msg, dict) and msg.get("type") == "pvaudio":
                    # one preview plays sound at a time (iCUE can run two copies of a widget)
                    if "device" in msg:
                        self.pv_device = str(msg.get("device") or "") or None
                    if "volume" in msg:
                        try:
                            self.pv_volume = max(0.0, min(1.0, float(msg.get("volume")) / 100))
                        except (TypeError, ValueError):
                            pass
                    if msg.get("on"):
                        self.pv_audio.clear(); self.pv_audio.add(ws)
                    else:
                        self.pv_audio.discard(ws)
                elif isinstance(msg, dict) and msg.get("type") == "pvdevices":
                    names = await asyncio.to_thread(self._out_devices)
                    await ws.send(json.dumps({"type": "pvdevices", "list": names, "current": self.pv_device or ""}))
                elif isinstance(msg, dict) and msg.get("type") == "config":
                    if msg.get("bands") is False:
                        self.no_bands.add(ws)
                    if "source" in msg:
                        self.audio.set_source(msg.get("source"))
                elif isinstance(msg, dict) and msg.get("type") == "control":
                    await self.media.control(msg.get("action"), msg.get("position"))
        except Exception:
            pass
        finally:
            if relay:
                relay.cancel()
            self.clients.discard(ws)
            self.no_bands.discard(ws)
            self.pv_audio.discard(ws)

    def _push_frame(self):
        """Runs on the event loop each time the capture thread has a new frame.
        Event-driven because Windows asyncio sleeps are ~15 ms granular."""
        now = time.monotonic()
        if not self.clients or now - self._last_push < 1.0 / self.fps - 0.002:
            return
        _, bands = self.audio.snapshot()
        silent = not any(bands)
        # While silent, a keep-alive every 0.5 s is enough.
        if silent and self._was_silent and now - self._last_push < 0.5:
            return
        self._was_silent = silent
        self._last_push = now
        import websockets
        targets = self.clients - self.no_bands
        if targets:
            websockets.broadcast(targets, '{"b":"' + base64.b64encode(bands).decode() + '"}')

    async def _media_loop(self):
        art_version = -1
        while True:
            await self.media.poll()
            if self.media.track_changed:
                self.media.track_changed = False
                self.audio.reset_peaks()
            if art_version != self.media.art_version:
                art_version = self.media.art_version
                self._broadcast(json.dumps({"type": "art", "artUrl": self.media.art_url}))
            self._broadcast(json.dumps(self.media.data))
            await asyncio.sleep(1.0)

    async def run(self):
        import websockets
        loop = asyncio.get_running_loop()
        self.audio.on_frame = lambda: loop.call_soon_threadsafe(self._push_frame)
        self.audio.start()
        async with websockets.serve(self._handler, "localhost", self.port, max_size=2 ** 16, process_request=self._http):
            log.info(f"Listening on ws://localhost:{self.port} ({BANDS} bands, {self.fps} fps max)")
            await self._media_loop()


def _run_with_tray(server, port):
    import pystray
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 61, 61], fill=(0, 200, 255, 255))
    for i, h in enumerate((14, 26, 36, 22, 12)):
        d.rectangle([14 + i * 8, 46 - h, 18 + i * 8, 46], fill=(255, 255, 255, 255))

    loop = asyncio.new_event_loop()

    def server_thread():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.run())
        except Exception:
            pass
        finally:
            server.audio.stop()

    def on_quit(icon, _item):
        loop.call_soon_threadsafe(loop.stop)
        icon.stop()

    menu = pystray.Menu(pystray.MenuItem(f"XE Visualizer server - port {port}", None, enabled=False),
                        pystray.Menu.SEPARATOR, pystray.MenuItem("Quit", on_quit))
    threading.Thread(target=server_thread, daemon=True).start()
    pystray.Icon("XEVisualizerServer", img, "XE Visualizer server", menu).run()


def main():
    p = argparse.ArgumentParser(description="XE Visualizer companion server")
    p.add_argument("--port", type=int, default=16329)
    p.add_argument("--fps", type=int, default=60, help="max audio frames pushed per second")
    p.add_argument("--no-tray", action="store_true", help="console mode, no tray icon")
    args = p.parse_args()

    server = Server(args.port, max(10, min(120, args.fps)))
    if not args.no_tray:
        try:
            _run_with_tray(server, args.port)
            return
        except ImportError:
            pass
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        server.audio.stop()


if __name__ == "__main__":
    main()
