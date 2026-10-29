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
import os
import re
import sys
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
PEAK_HALF_LIFE_SECS = 3.0   # how quickly the loudness reference eases down after loud parts


def _detect_image_type(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "image/png"


class _RollingPeak:
    """Loudness reference: jumps up at once for louder audio, then eases down smoothly.
    (A hard 10 s max window made every bar jump at once when a loud hit dropped out of it.)"""

    def __init__(self):
        self._p = 0.0
        self._t = time.monotonic()

    def update(self, value):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        self._p = max(value, self._p * 0.5 ** (dt / PEAK_HALF_LIFE_SECS))
        return self._p

    def reset(self):
        self._p = 0.0


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
        self._fallback = src != "default" and not found   # wanted devices missing (e.g. Sonar still starting)
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
        self._started = time.time()
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._rescan_watch, daemon=True).start()

    def _rescan_watch(self):
        """Right after Windows starts, virtual devices such as SteelSeries Sonar can appear after the
        server does. PortAudio only lists devices when it is (re)initialised, so while the wanted
        devices are missing, restart capture every 15 s for the first 5 minutes."""
        while self._running and time.time() - self._started < 300:
            time.sleep(15)
            if getattr(self, "_fallback", False):
                log.info("Audio devices not ready yet, rescanning")
                self._restart = True

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
                if time.monotonic() - idle_since > 0.6 and any(self._bands):
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
            env = min(1.6, low_now / self._low_smoothed) ** 0.7 if self._low_smoothed > 1e-4 else 1.0

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

# ── Website lookup (browser history) ──────────────────────────────────────────
# Windows only says "a browser is playing". To show which website, find the page in that
# browser's own history by its title, and take the site's icon from the browser's icon cache.
_LOCAL, _ROAMING = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
# app-id fragment -> (kind, profile roots). Checked in order, so "comet" wins over "chrome".
HISTORY_DIRS = [
    ("comet", "chromium", [_LOCAL + "/Perplexity/Comet/User Data"]),
    ("msedge", "chromium", [_LOCAL + "/Microsoft/Edge/User Data"]),
    ("brave", "chromium", [_LOCAL + "/BraveSoftware/Brave-Browser/User Data"]),
    ("vivaldi", "chromium", [_LOCAL + "/Vivaldi/User Data"]),
    ("opera", "chromium", [_ROAMING + "/Opera Software"]),   # Opera, Opera GX, Opera Neon
    ("chrome", "chromium", [_LOCAL + "/Google/Chrome/User Data"]),
    ("firefox", "firefox", [_ROAMING + "/Mozilla/Firefox/Profiles"]),
    ("308046b0af4a39cb", "firefox", [_ROAMING + "/Mozilla/Firefox/Profiles"]),
]
SITE_PLATFORMS = {"youtube.com": "youtube", "m.youtube.com": "youtube", "music.youtube.com": "youtube",
                  "youtu.be": "youtube", "twitch.tv": "twitch", "open.spotify.com": "spotify"}
PLACEHOLDER_TITLES = {"a site is playing media"}
_CHROME_EPOCH_US =11644473600 * 1_000_000   # Chromium counts from 1601, Firefox from 1970


def _open_db(path, kind):
    """Read-only view of a browser database that the browser may have open."""
    import sqlite3
    uri = "file:///" + path.replace("\\", "/")
    if kind == "chromium":   # Chromium locks its databases; immutable reads still work
        return sqlite3.connect(uri + "?immutable=1", uri=True)
    try:   # Firefox keeps recent writes in a -wal file, which a normal read-only open includes
        c = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=0.3)
        c.execute("select 1 from sqlite_master limit 1")
        return c
    except sqlite3.Error:   # locked by Firefox: read a copy instead
        import shutil
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "xe-nowplaying")
        os.makedirs(d, exist_ok=True)
        name = os.path.basename(path)
        for ext in ("", "-wal"):
            dst = os.path.join(d, name + ext)
            if os.path.exists(path + ext):
                shutil.copyfile(path + ext, dst)
            elif os.path.exists(dst):
                os.remove(dst)
        return sqlite3.connect(os.path.join(d, name))


def find_site(app_id, title):
    """Newest history entry whose page title contains the playing title, from the browser that's
    playing (every known browser if the app isn't recognised). Returns {host, name, icon} or None."""
    import glob
    from urllib.parse import urlsplit
    a = app_id.lower()
    dirs = next(([(k, r) for r in roots] for frag, k, roots in HISTORY_DIRS if frag in a), None) \
        or [(k, r) for _, k, roots in HISTORY_DIRS for r in roots]
    like = "%" + (title or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    best = None   # (unix microseconds, url, page title, kind, profile dir)
    for kind, root in dict.fromkeys(dirs):
        name = "History" if kind == "chromium" else "places.sqlite"
        for db in glob.glob(f"{root}/*/{name}") + glob.glob(f"{root}/*/*/{name}"):
            try:
                c = _open_db(db, kind)
                if kind == "chromium":
                    row = c.execute("select last_visit_time - ?, url, title from urls where url like 'http%' and title like ? escape '\\' "
                                    "order by last_visit_time desc limit 1", (_CHROME_EPOCH_US, like)).fetchone()
                else:
                    row = c.execute("select last_visit_date, url, title from moz_places where url like 'http%' and title like ? escape '\\' "
                                    "order by last_visit_date desc limit 1", (like,)).fetchone()
                c.close()
            except Exception as e:
                log.debug(f"history read failed for {db}: {e}")
                continue
            if row and row[0] and (not best or row[0] > best[0]):
                best = (row[0], row[1], row[2] or "", kind, os.path.dirname(db))
    if not best:
        return None
    visited, url, page_title, kind, prof = best
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    if not host or parts.scheme not in ("http", "https"):
        return None
    # site name: the end of the page title ("Video - YouTube"), else the domain
    name = host
    page = re.sub(r"^\(\d+\)\s*", "", page_title)   # drop "(3) " notification counts
    for sep in (" - ", " | ", " – ", " — ", " • ", " · "):
        head, _, tail = page.rpartition(sep)
        if head and title in head and 0 < len(tail.strip()) <= 30:
            name, page = tail.strip(), head.strip()
            break
    return {"host": host, "name": name, "page": page or name, "url": url, "visited": visited / 1e6, "icon": _site_icon(kind, prof, url, f"{parts.scheme}://{parts.netloc}/")}


def _site_icon(kind, prof, url, origin):
    """The site's favicon from the browser's icon cache, as a small data URL ("" if none)."""
    import io
    from PIL import Image
    try:
        if kind == "chromium":
            c = _open_db(os.path.join(prof, "Favicons"), kind)
            q = ("select b.image_data from icon_mapping m join favicon_bitmaps b on b.icon_id = m.icon_id "
                 "where m.page_url {} ? order by b.width desc limit 1")
        else:
            c = _open_db(os.path.join(prof, "favicons.sqlite"), kind)
            q = ("select i.data from moz_pages_w_icons p join moz_icons_to_pages ip on ip.page_id = p.id "
                 "join moz_icons i on i.id = ip.icon_id where p.page_url {} ? order by i.width desc limit 1")
        row = c.execute(q.format("="), (url,)).fetchone() or c.execute(q.format("like"), (origin + "%",)).fetchone()
        c.close()
        if not row or not row[0]:
            return ""
        data = bytes(row[0])
        if data.lstrip()[:1] == b"<":   # SVG icon: the widget can draw it as is
            return "data:image/svg+xml;base64," + base64.b64encode(data).decode()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        if max(img.size) > 256:
            img = img.resize((256, 256), Image.LANCZOS)
        b = io.BytesIO()
        img.save(b, "PNG")
        return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()
    except Exception as e:
        log.debug(f"favicon read failed: {e}")
        return ""


def icon_art(icon):
    """Square cover art made from a site icon: the icon centred on a dark tint of its own colour,
    with a soft glow of itself behind it."""
    import io
    from PIL import Image, ImageFilter
    if not icon.startswith("data:image/png"):
        return icon   # SVG: draws sharp at any size as it is
    try:
        ic = Image.open(io.BytesIO(base64.b64decode(icon.split(",", 1)[1]))).convert("RGBA")
        a = np.asarray(ic, dtype=np.float32)
        w = a[..., 3:] / 255
        colour = (a[..., :3] * w).sum((0, 1)) / max(float(w.sum()), 1.0)   # average of the visible pixels
        size = 384
        card = Image.new("RGBA", (size, size), tuple(int(c * 0.28) for c in colour) + (255,))
        glow = ic.resize((size, size), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
        glow.putalpha(glow.getchannel("A").point(lambda v: v * 0.45))
        card.alpha_composite(glow)
        big = ic.resize((size * 2 // 5, size * 2 // 5), Image.LANCZOS)
        card.alpha_composite(big, ((size - big.width) // 2, (size - big.height) // 2))
        b = io.BytesIO()
        card.convert("RGB").save(b, "JPEG", quality=90)
        return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
    except Exception as e:
        log.debug(f"icon art failed: {e}")
        return ""


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
                self.data.update(title="", artist="", album="", playing=False, pos=0.0, dur=0.0, live=False, platform="", site=None)
                if self._last_title:
                    self._last_title = ""
                    self.art_url = ""
                    self.art_version += 1
                return
            info = await asyncio.wait_for(s.try_get_media_properties_async(), 5.0)
            playing = s.get_playback_info().playback_status == self._Status.PLAYING
            title = info.title or ""
            # Opera and others send a stand-in title and picture when the page gives no media details
            generic = title.casefold() in PLACEHOLDER_TITLES
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
                self._hd_title = ""
                self._site, self._site_tries, self._site_next, self._icon_art_src = None, 10, 0.0, ""
                self._started = time.time()
                self.art_url, self._art_aspect = ("", 0.0) if generic else await self._read_art(info.thumbnail)
                self.art_version += 1
            elif self._art_retries > 0 and not generic:
                self._art_retries -= 1
                url, aspect = await self._read_art(info.thumbnail)
                if url and url != self.art_url and self._hd_title != title and not self._icon_art_src:
                    self.art_url, self._art_aspect = url, aspect
                    self.art_version += 1
            app_id = s.source_app_user_model_id or ""
            # which website a browser is playing from (history is written a little late, so retry a while;
            # with only a stand-in title, guess the newest page in that browser's history)
            if title and self._site is None and self._site_tries > 0 \
                    and time.monotonic() >= self._site_next and any(b in app_id.lower() for b in self.BROWSERS):
                self._site_tries -= 1
                self._site_next = time.monotonic() + 3
                found = await asyncio.to_thread(find_site, app_id, "" if generic else title)
                # A stand-in title can only be guessed from the newest page, so that page must have been
                # opened around when the media started. Private windows keep no history: the newest page
                # is then some older, unrelated one, and showing nothing beats showing the wrong site.
                if found and generic and found["visited"] < self._started - 120:
                    found = None
                if found and self._last_title == title:
                    self._site = found
                    log.info(f"Playing on {found['host']}")
            site = self._site
            # websites other than the known services get their icon as the art: the pictures browsers
            # pass on for them are often missing, stand-ins or unreadable
            if site and site["icon"] and site["host"] not in SITE_PLATFORMS and self._icon_art_src != site["icon"]:
                self._icon_art_src = site["icon"]
                art = await asyncio.to_thread(icon_art, site["icon"])
                if art:
                    self.art_url, self._art_aspect = art, 1.0
                    self.art_version += 1
            platform = SITE_PLATFORMS.get(site["host"], "web") if site else \
                self._platform(app_id, live, info.album_title or "")
            # stand-in title: show only the guessed site's name. The newest page could be anything
            # (a search, another tab), so its own title never goes on screen.
            shown = (site["name"] if site else "Media playing") if generic else title
            self.data.update(title=shown, artist=info.artist or "", album=info.album_title or "",
                             playing=playing, pos=round(pos, 2), dur=round(dur, 2), live=live, platform=platform,
                             site={"name": site["name"], "icon": site["icon"]} if site else None)
            self.data["yt"] = self._yt_meta if self._hd_title == title else None
            # YouTube: swap in the HD thumbnail whenever the low-res one is showing, including after
            # switching back to the video's tab (looked-up videos come from the cache instantly).
            # The video's ID from the browser history is exact; without it, search by title. Finding the ID
            # later (history is written a little late) starts a new lookup straight away.
            vid = re.search(r"[?&]v=([\w-]{11})", site["url"]) if site and site["host"].endswith("youtube.com") else None
            vid = vid.group(1) if vid else ""
            hd_key = f"{title}|{vid}"
            if self.data["platform"] == "youtube" and title and self._hd_title != title and self._hd_tried != hd_key \
                    and (vid or time.monotonic() >= self._hd_retry_at):
                self._hd_tried = hd_key
                asyncio.ensure_future(self._youtube_hd_art(title, info.artist or "", vid, hd_key))
        except Exception as e:
            log.debug(f"SMTC poll failed: {type(e).__name__}: {e}")

    _site = None       # {"host", "name", "icon"} of the website playing, from browser history
    _site_tries = 0
    _site_next = 0.0
    _started = 0.0     # when the current title first appeared
    _icon_art_src = ""  # site icon the current icon art was made from
    _hd_tried = ""     # last YouTube title we looked up an HD thumbnail for
    _hd_title = ""     # title whose art is currently the HD thumbnail
    _hd_cache = {}
    _hd_retry_at = 0.0  # after a failed lookup, wait before trying again
    _yt_meta = None    # channel name/avatar, views and age for the video whose HD art is showing

    async def _youtube_hd_art(self, title, channel, vid, hd_key):
        """Browsers hand Windows a tiny YouTube thumbnail. Look the video up (by its ID, or else by its
        exact title and channel) and use YouTube's own HD thumbnail instead, plus the channel avatar,
        views and age for the video card. Not found: keep what we have."""
        key = (title, vid or channel)
        found = self._hd_cache.get(key)
        if found is None:
            found = await asyncio.to_thread(self._find_youtube_thumb, title, channel, vid)
            if not found:
                # not cached, so switching back to the video later tries again (e.g. network was down)
                if self._hd_tried == hd_key:
                    self._hd_tried = ""
                    self._hd_retry_at = time.monotonic() + 30
                return
            if len(self._hd_cache) > 200:
                self._hd_cache.clear()
            self._hd_cache[key] = found
        self._hd_tried = ""   # done: allowed to run again for this title if its art gets replaced later
        url = found.get("art") if found else ""
        if url and self._last_title == title:   # still the same video
            self._yt_meta = {k: v for k, v in found.items() if k != "art"}
            self.art_url, self._art_aspect, self._hd_title = url, 16 / 9, title
            self.art_version += 1
            log.info(f"HD thumbnail for {title!r}")

    @staticmethod
    def _find_youtube_thumb(title, channel, vid=""):
        import io
        import json as _json
        import re as _re
        import urllib.parse
        import urllib.request
        from PIL import Image
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en-US,en;q=0.8"}

        def get(u):
            with urllib.request.urlopen(urllib.request.Request(u, headers=hdrs), timeout=8) as r:
                return r.read()

        def norm(t):
            return _re.sub(r"\s+", " ", t).strip().casefold()

        def first(o, k):   # first value stored under key k anywhere in o
            if isinstance(o, dict):
                if k in o:
                    return o[k]
                o = list(o.values())
            if isinstance(o, list):
                for x in o:
                    r = first(x, k)
                    if r is not None:
                        return r
            return None

        def short_age(t):   # "2 hours ago" -> "2h ago", so it fits the card
            m = _re.match(r"(?:Streamed )?(\d+) (second|minute|hour|day|week|month|year)s? ago", t)
            units = {"second": "s", "minute": "m", "hour": "h", "day": "d", "week": "w", "month": "mo", "year": "y"}
            return f"{m.group(1)}{units[m.group(2)]} ago" if m else t

        try:
            meta = {}
            if not vid:
                vid, meta = MediaInfo._search_youtube(title, channel, get, norm)
                if not vid:
                    return {}
            # the watch page has the subscriber and like counts, and everything else when we came with an ID
            try:
                w = get(f"https://www.youtube.com/watch?v={vid}").decode("utf-8", "replace")
                m = _re.search(r"var ytInitialData = (\{.*?\});</script>", w)
                wd = _json.loads(m.group(1)) if m else {}
                pri, own = first(wd, "videoPrimaryInfoRenderer") or {}, first(wd, "videoOwnerRenderer") or {}
                thumbs = own.get("thumbnail", {}).get("thumbnails", [])
                badges = [(b.get("metadataBadgeRenderer") or {}).get("style", "") for b in own.get("badges", [])]
                meta["views"] = meta.get("views") or ((first(pri, "extraShortViewCount") or {}).get("simpleText") or "")
                meta["age"] = meta.get("age") or (pri.get("relativeDateText") or {}).get("simpleText", "")
                meta["channel"] = meta.get("channel") or "".join(r.get("text", "") for r in own.get("title", {}).get("runs", []))
                meta["avatar"] = meta.get("avatar") or (thumbs[-1]["url"] if thumbs else "")
                meta["verified"] = meta.get("verified") or any("VERIFIED" in b or "ARTIST" in b for b in badges)
                subs = (own.get("subscriberCountText") or {}).get("simpleText", "")
                likes = _re.search(r'"factoidRenderer":\{"value":\{"simpleText":"([^"]+)"\},"label":\{"simpleText":"Likes"', w)
                meta["subs"] = subs.replace(" subscribers", " subs")
                meta["likes"] = likes.group(1) if likes else ""
            except Exception as e:
                log.debug(f"YouTube watch page failed: {e}")
            if not meta.get("channel"):
                return {}   # came with an ID but the page didn't load: try again later
            meta["age"] = short_age(meta.get("age", ""))
            if meta["avatar"].startswith("//"):
                meta["avatar"] = "https:" + meta["avatar"]
            if meta["avatar"]:   # small round channel picture, as a data URL (iCUE can't load remote images)
                try:
                    av = Image.open(io.BytesIO(get(meta["avatar"]))).convert("RGB").resize((88, 88), Image.LANCZOS)
                    b = io.BytesIO()
                    av.save(b, "JPEG", quality=85)
                    meta["avatar"] = "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
                except Exception:
                    meta["avatar"] = ""
            for name in ("maxresdefault", "sddefault", "hqdefault"):
                try:
                    img = Image.open(io.BytesIO(get(f"https://i.ytimg.com/vi/{vid}/{name}.jpg"))).convert("RGB")
                except Exception:
                    continue
                w, h = img.size
                if w < 300:
                    continue   # YouTube's 120x90 "no thumbnail" placeholder
                th = round(w * 9 / 16)
                if h > th:   # sd/hq thumbnails are 4:3 with black bars: keep the 16:9 middle
                    top = (h - th) // 2
                    img = img.crop((0, top, w, top + th))
                if img.width > 960:
                    img = img.resize((960, 540), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=88)
                meta["art"] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
                return meta
        except Exception as e:
            log.debug(f"YouTube HD thumbnail lookup failed: {type(e).__name__}: {e}")
        return {}

    @staticmethod
    def _search_youtube(title, channel, get, norm):
        """(video ID, card details) of the search result with exactly this title, or ("", {})."""
        import json as _json
        import re as _re
        import urllib.parse
        q = urllib.parse.quote_plus(f"{title} {channel}".strip())
        page = get(f"https://www.youtube.com/results?search_query={q}").decode("utf-8", "replace")
        m = _re.search(r"var ytInitialData = (\{.*?\});</script>", page)
        if not m:
            return "", {}
        data = _json.loads(m.group(1))
        found = []

        def walk(o):
            if isinstance(o, dict):
                v = o.get("videoRenderer")
                if isinstance(v, dict) and v.get("videoId"):
                    t = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
                    c = "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", []))
                    views = (v.get("shortViewCountText") or v.get("viewCountText") or {}).get("simpleText") \
                        or "".join(r.get("text", "") for r in (v.get("shortViewCountText") or {}).get("runs", []))
                    age = (v.get("publishedTimeText") or {}).get("simpleText", "")
                    thumbs = (v.get("channelThumbnailSupportedRenderers", {}).get("channelThumbnailWithLinkRenderer", {})
                              .get("thumbnail", {}).get("thumbnails", []))
                    owner = [(b.get("metadataBadgeRenderer") or {}).get("style", "") for b in v.get("ownerBadges", [])]
                    found.append((v["videoId"], t, c, {
                        "views": views or "", "age": age, "channel": c,
                        "avatar": thumbs[-1]["url"] if thumbs else "",
                        "verified": any("VERIFIED" in o or "ARTIST" in o for o in owner)}))
                for x in o.values():
                    walk(x)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(data)
        want = norm(title)
        matches = [f for f in found if norm(f[1]) == want]
        if channel:   # prefer the same channel when titles repeat
            matches.sort(key=lambda f: norm(f[2]) != norm(channel))
        if not matches:
            return "", {}
        return matches[0][0], dict(matches[0][3])

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
            return "youtube" if album else ("twitch" if live else "")   # a square picture alone isn't Twitch
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


# ── Self-install (packaged XE-Widgets-Server.exe only) ──────────────────────
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "XE Widgets Server"


def _installed_exe():
    import os
    from pathlib import Path
    return Path(os.environ["LOCALAPPDATA"]) / "XEWidgets" / "XE-Widgets-Server.exe"


def _autostart(enable=None):
    """Read (enable=None) or set whether the installed server starts with Windows."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if enable is None:
            try:
                winreg.QueryValueEx(k, RUN_NAME)
                return True
            except FileNotFoundError:
                return False
        if enable:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, '"%s"' % _installed_exe())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
        return enable


def _self_install():
    """Run from anywhere (e.g. Downloads): copy into %LOCALAPPDATA%/XEWidgets, start with Windows,
    replace any older running copy, launch the installed copy and return True so this one exits."""
    import ctypes
    import os
    import shutil
    import subprocess
    import sys
    if not getattr(sys, "frozen", False):
        return False
    me, target = os.path.abspath(sys.executable), str(_installed_exe())
    if os.path.normcase(me) == os.path.normcase(target):
        return False   # already the installed copy
    msg = ("XE Widgets server: the companion app for the XE Chat and XE Visualizer iCUE widgets.\n\n"
           "Installing will:\n"
           "  • copy this program to " + target + "\n"
           "  • start it with Windows (turn off any time from its tray icon)\n"
           "  • run it in the system tray\n\n"
           "While running it only listens on your own PC (localhost:16329). It connects to Twitch, 7TV, "
           "BetterTTV, FrankerFaceZ and ivr.fi for chat, emotes and stream info, reads what Windows shows as "
           "'now playing', and captures audio for the visualizer. No admin rights needed, nothing else is changed.\n\n"
           "Uninstall: right-click the tray icon → Uninstall.\n\nInstall now?")
    if ctypes.windll.user32.MessageBoxW(None, msg, "Install XE Widgets server?", 0x04 | 0x20) != 6:   # Yes/No; 6 = Yes
        return True
    home = os.path.dirname(target)
    os.makedirs(home, exist_ok=True)
    subprocess.run(["taskkill", "/F", "/IM", os.path.basename(target), "/FI", f"PID ne {os.getpid()}"],
                   capture_output=True, creationflags=0x08000000)   # updating: stop the old copy first
    for _ in range(20):
        try:
            shutil.copy2(me, target)
            break
        except PermissionError:
            time.sleep(0.25)
    _autostart(True)
    subprocess.Popen([target], cwd=home, creationflags=0x00000008)   # detached
    ctypes.windll.user32.MessageBoxW(
        None, "XE Widgets server is installed and running (look for the icon in your system tray).\n\n"
              "It starts with Windows automatically. Now add XE Chat and XE Visualizer in iCUE.",
        "XE Widgets", 0x40)
    return True


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

    items = [pystray.MenuItem(f"XE Widgets server - port {port}", None, enabled=False), pystray.Menu.SEPARATOR]
    if getattr(sys, "frozen", False):
        import ctypes
        import os
        import subprocess

        def on_uninstall(icon, item):
            if ctypes.windll.user32.MessageBoxW(None, "Remove the XE Widgets server from this PC?\n\n"
                                                "The widgets stay in iCUE; remove them there if you like.",
                                                "Uninstall XE Widgets server", 0x04 | 0x30) != 6:
                return
            _autostart(False)
            home = os.path.dirname(str(_installed_exe()))
            subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 3 >nul & rmdir /s /q "{home}"',   # after we exit
                             creationflags=0x08000000)
            on_quit(icon, item)

        items.append(pystray.MenuItem("Start with Windows", lambda icon, item: _autostart(not _autostart()),
                                      checked=lambda item: _autostart()))
        items.append(pystray.MenuItem("Uninstall…", on_uninstall))
    items.append(pystray.MenuItem("Quit", on_quit))
    menu = pystray.Menu(*items)
    threading.Thread(target=server_thread, daemon=True).start()
    pystray.Icon("XEWidgetsServer", img, "XE Widgets server", menu).run()


def main():
    p = argparse.ArgumentParser(description="XE Visualizer companion server")
    p.add_argument("--port", type=int, default=16329)
    p.add_argument("--fps", type=int, default=60, help="max audio frames pushed per second")
    p.add_argument("--no-tray", action="store_true", help="console mode, no tray icon")
    p.add_argument("--no-install", action="store_true", help="packaged exe: run in place without installing")
    args = p.parse_args()
    if not args.no_install and _self_install():
        return

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
