# Orca Core 2 Anchor Watch

Windows-hosted anchor alarm (`anchor_watch.py` + `static/anchor.html`) and live reader (`orca_reader.py`) for a
boat's Orca Core 2. Separate from MasterVolt YMS; it only borrows that project's local HTTPS CA.

Overview: `README.md`. Release history: `CHANGELOG.md`.

## Hard rules

- **Read-only on the Core.** Never call `PUT /v1/sources`, `POST :9001/imu/calibration`, the `/v1/sync` WebSocket
  or the firmware page on port 8090, and never send anything on the sensor WebSockets. Only GETs and listening.
- **Never trust a GPS position just because it is fresh**: the Core's position (source 254) can freeze while it flags
  the boat as stationary, and a frozen position hides a drag. Keep the frozen-position check in `AnchorWatch.tick`
  (`FROZEN_S`: unchanged for 60 s = not used) for every source, including 254, which the user can choose (Core-only
  power-saving setup). Tests must give simulated receivers jitter, or they count as frozen.
- **Parse Core messages with `orca_reader.parse_message`**, never plain `json.loads`: the Core sends unescaped text.
- The alarm logic runs on the server (`AnchorWatch.tick`); the page only displays and sends commands. Do not move
  alarm decisions into the browser.
- Keep everything on a private LAN: the server binds only to localhost or an RFC 1918 address (`check_bind`).
  Never bind a public interface, port-forward it or add a public tunnel (Cloudflare, ngrok): the app has no login.
  Remote access is through UniFi Teleport (VPN into the boat LAN; clients come from 192.168.2.x), so do not
  restrict requests to the 192.168.1.x subnet.
- HTTPS certificates come from the existing *Mastervolt Local CA* (`setup_local_https.ps1`). Never create a new CA
  (the iPhone would need it installed again), never write into the CA folder, never change the Windows trust store.
- The chain formula is user input: only ever evaluate it through `ChainFormula` (parsed, no `eval`).
- Push alerts go out only through the `Pushover` worker thread; never send from `tick()` or a request thread, and keep
  anchor drag as the only emergency (priority 2) alert so the phone does not learn to ignore it.
- The still-alive ping must keep running independently of the alarm logic (its own thread in `StillAlive`) and must
  ping more often than the interval; never make it depend on the page being open.
- AIS from the Core's own receiver always has priority over internet AIS (`read_ais`): never let an internet report
  replace a current local one, and keep My MMSI filtered in both. The Orca app's internet AIS is Orca-cloud only
  (`europe-west1-the-orca.cloudfunctions.net`); do not try to reproduce that private API.
- Test servers (`.claude/launch.json`) point `--pushover`, `--still-alive` and `--aisstream` at non-existent files so a
  test can never send a real alert, ping or subscription.
- Times are always shown 24-hour (`clock()` / `dayTime()` in the page); never use locale time defaults (AM/PM).
- Map tiles are fetched on demand only (OpenStreetMap tile policy): no bulk or prefetch downloads.
- Only add chart sources whose terms allow this use. The IHM (Spain) licence allows non-commercial use and requires
  the attribution "© Instituto Hidrográfico de la Marina", so keep it. Do not add HNHS (Greece) tiles without their
  written agreement, and never decode, convert or commit CM93 / C-MAP or other proprietary chart data.
- Never colour-invert charts in dark mode (depth-area and light colours carry meaning); dim them instead. Night
  mode may invert, because everything is tinted red there anyway.
- Never commit `pushover.json` (Pushover keys), `still_alive.json` (ping URL, API key), `aisstream.json` (aisstream.io key), `tide_cache.json` (boat area), `certs/`, `anchor_state.json`, `tile_cache/`, `captures/` or `recordings/` (all in `.gitignore`).
  They hold keys, the boat's positions and network captures. Keep real MMSIs and anchorage positions out of tests.

## Verifying changes

```powershell
.venv\Scripts\python.exe -m unittest test_anchor_watch   # simulated Core data, no network
```

For frontend edits, syntax-check the JavaScript in `static/anchor.html` and check the page at phone width (375 px)
in light, dark and night mode. A test server can run beside the live one with `--port 8767 --state <scratch file>`.
Bump `VERSION` in `anchor_watch.py` and add a `CHANGELOG.md` entry for each release.
