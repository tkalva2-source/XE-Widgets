# XE Widgets

Touch-friendly Twitch chat and a now-playing audio visualizer for the **Corsair Xeneon Edge** (iCUE 5 HTML widgets).

A vibe-coded fork by **Tkalva**: XE Chat started as a rework of the Lumo chat widget, and XE Visualizer and its companion server grew out of QuadraKev's QK XE Visualizer. Both were rebuilt from the ground up for speed, touch and a cleaner look on the Edge.

<p align="center">
  <a href="https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Widgets-Setup.bat"><img src="https://img.shields.io/badge/%E2%AC%87%20Install%20XE%20Widgets-one%20click-9146FF?style=for-the-badge" alt="Install XE Widgets"></a>
</p>

| Widget | What it does |
|---|---|
| **XE Chat** | Twitch chat with 7TV / BTTV / FFZ emotes, 7TV name paints and badges, stream info, chatting and moderation once you sign in, your followed channels (live first), and a live stream preview with sound and a theater view. |
| **XE Visualizer** | Audio visualizer with the current song or video, album art, playback controls and themes. Works with Spotify, YouTube, Twitch and anything else Windows shows as "now playing". |

Both widgets talk to a small companion server that runs in your system tray. It relays Twitch chat (iCUE's browser can't reach Twitch directly), reads what's playing, captures audio for the visualizer and decodes the stream preview.

## Install

**One click:** download **[XE-Widgets-Setup.bat](https://github.com/tkalva2-source/XE-Widgets/releases/latest/download/XE-Widgets-Setup.bat)** and double-click it. (Windows may say it protected your PC: click *More info → Run anyway*.)

**Or paste this into PowerShell:**

```powershell
irm https://raw.githubusercontent.com/tkalva2-source/XE-Widgets/main/setup.ps1 | iex
```

**Or manually:** download `XE-Widgets.zip` from [Releases](../../releases), unzip it and double-click `install.bat`.

Any of these will:

- check for Python 3.10+ (and offer to install it with winget if missing),
- install the server's Python packages,
- install the server to `%LOCALAPPDATA%\XEWidgets` and start it with Windows,
- copy both widgets into iCUE.

Then restart iCUE and add **XE Chat** / **XE Visualizer** to your Xeneon Edge.

Run the installer again any time to update. `uninstall.bat` removes everything it installed.

> iCUE's own settings page shows a small preview of each widget that doesn't match the real layout. Adjust XE Chat with the settings button on the widget itself.

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

- **Chat says it's connecting / nothing loads** — make sure the server is running (tray icon). Start it again by running `install.bat`.
- **Widgets don't show up in iCUE** — restart iCUE after installing.
- **Server port** — both widgets use port `16329`. If you change it, change it in both widgets' settings.

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
