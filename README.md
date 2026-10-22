# XE Widgets

Touch-friendly Twitch chat and a now-playing audio visualizer for the **Corsair Xeneon Edge** (iCUE 5 HTML widgets).

A vibe-coded fork by **Tkalva**: XE Chat started as a rework of the Lumo chat widget, and XE Visualizer and its companion server grew out of QuadraKev's QK XE Visualizer. Both were rebuilt from the ground up for speed, touch and a cleaner look on the Edge.

<p align="center">
  <a href="https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Widgets-Server.exe"><img src="https://img.shields.io/badge/%E2%AC%87%20Server-XE--Widgets--Server.exe-9146FF?style=for-the-badge" alt="Download the server"></a>
  <a href="https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Chat.icuewidget"><img src="https://img.shields.io/badge/%E2%AC%87%20Widget-XE%20Chat-2b2b33?style=for-the-badge" alt="Download XE Chat"></a>
  <a href="https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Visualizer.icuewidget"><img src="https://img.shields.io/badge/%E2%AC%87%20Widget-XE%20Visualizer-2b2b33?style=for-the-badge" alt="Download XE Visualizer"></a>
</p>

| Widget | What it does |
|---|---|
| **XE Chat** | Twitch chat with 7TV / BTTV / FFZ emotes, 7TV name paints and badges, stream info, chatting and moderation once you sign in, your followed channels (live first), and a live stream preview with sound and a theater view. |
| **XE Visualizer** | Audio visualizer with the current song or video, album art, playback controls and themes. Works with Spotify, YouTube, Twitch and anything else Windows shows as "now playing". |

Both widgets talk to a small companion server that runs in your system tray. It relays Twitch chat (iCUE's browser can't reach Twitch directly), reads what's playing, captures audio for the visualizer and decodes the stream preview.

## Install

1. **Server** — download **[XE-Widgets-Server.exe](https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Widgets-Server.exe)** and run it. It shows exactly what it will do and asks before installing. No Python or admin rights needed.
2. **Widgets** — download **[XE-Chat.icuewidget](https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Chat.icuewidget)** and/or **[XE-Visualizer.icuewidget](https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Visualizer.icuewidget)** and double-click them to add them to iCUE.
3. Add the widgets to your Xeneon Edge in iCUE.

> **"Windows protected your PC"?** The server isn't code-signed (that costs money), so SmartScreen may warn on first run. Click *More info → Run anyway*. You can check the download against the SHA256 hash listed on the release, or build it yourself from this repo (see below).

To update, run a newer `XE-Widgets-Server.exe`; it replaces the old one.

> iCUE's own settings page shows a small preview of each widget that doesn't match the real layout. Adjust XE Chat with the settings button on the widget itself.

## What the server installs

Running `XE-Widgets-Server.exe` asks first, then:

- copies itself to `%LOCALAPPDATA%\XEWidgets\XE-Widgets-Server.exe` (your user folder only, no admin rights),
- adds one startup entry (`HKCU\...\Run\XE Widgets Server`) so it starts with Windows — toggle it from the tray icon,
- runs in the system tray.

While running it:

- **only listens on your own PC** (`localhost:16329`), nothing is reachable from the network,
- connects to Twitch, 7TV, BetterTTV, FrankerFaceZ and ivr.fi for chat, emotes, badges and stream info,
- reads the "now playing" info Windows already shows (song, video, thumbnail) and captures audio for the visualizer,
- decodes the stream preview when you turn it on.

It collects nothing and sends nothing anywhere else. **Uninstall:** right-click the tray icon → *Uninstall…* (removes the program and its startup entry), then remove the widgets in iCUE.

## Signing in to Twitch (optional)

Reading chat works without an account. To **send messages, moderate, or see your follows**, XE Chat signs in with your own free Twitch app (so nobody else's app or keys are involved):

1. Go to [dev.twitch.tv/console](https://dev.twitch.tv/console) → **Register Your Application**.
2. Name: anything. OAuth Redirect URL: `http://localhost`. Category: *Chat Bot* (or any). **Client Type: Public**.
3. Copy the **Client ID** and paste it into XE Chat's *Twitch Client ID* setting in iCUE (or tap **Sign in** on the widget and paste it there).
4. Tap **Sign in** on the widget and enter the code it shows at twitch.tv/activate.

Your sign-in is stored only on your PC.

## Audio source (visualizer)

By default the visualizer follows SteelSeries Sonar's *Media* and *Aux* channels if you use Sonar, and otherwise your default Windows output. Change it with the visualizer's **Audio Source** setting in iCUE.

## Troubleshooting

- **Chat says it's connecting / nothing loads** — make sure the server is running (tray icon). If not, run `XE-Widgets-Server.exe` again.
- **Widgets don't show up in iCUE** — restart iCUE after adding them.
- **Server port** — both widgets use port `16329`. If you change it, change it in both widgets' settings.

## Build it yourself

Everything in the release is built from this repo. With Python 3.10+ installed, run `build-release.ps1`; it produces the server exe (via PyInstaller) and both `.icuewidget` files in `dist\`, and prints their SHA256 hashes. To run the server from source instead: `pip install -r server/requirements.txt`, then `pythonw server/NowPlayingServer.pyw`.

## Notes

- Not affiliated with Corsair, Twitch, 7TV, BetterTTV or FrankerFaceZ.
- The stream preview uses the same public player access Twitch's own web player uses. Twitch may change it at any time.
- Emote, badge and profile data come from the public 7TV, BetterTTV, FrankerFaceZ and ivr.fi APIs.

## Credits

- Made by **Tkalva**, vibe coded with Claude.
- XE Chat is a fork of the **Lumo** Twitch chat widget for iCUE.
- XE Visualizer and the companion server build on **QuadraKev**'s original QK XE Visualizer and Now Playing server.
- Inter typeface by Rasmus Andersson, SIL Open Font License (`widgets/xe-chat/fonts/LICENSE.txt`).

## License

MIT, see [LICENSE](LICENSE).
