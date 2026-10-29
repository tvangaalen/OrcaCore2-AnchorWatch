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
  power-saving setup). A held `navigation.position` may fall back to the same source's own `navigation.gnss` fix only
  while that fix itself keeps changing; never use a position whose values are unchanged for FROZEN_S. Only exception:
  source 254's measured static hold (releases after HOLD_M = 10 m), used only while its fix time keeps advancing;
  never extend that exception to other receivers or drop the fix-time check. The hold is detected on 254's own fix
  (`navigation.gnss`) with the position within `HOLD_NEAR_M` of it (it flickers after a release); never widen that
  to positions that really move. `HOLD_SOON_S` (10 s unchanged own fix) only puts 254 behind live receivers; it must
  never keep 254 out of the frozen check or log a hold before it is confirmed. Slow-by-design receivers may get a longer limit in `SLOW_GPS` (B&G ZG100: 100 s,
  approved by the user 2026-10-28); keep it per model and only as long as the recordings show. Tests must give
  simulated receivers jitter, or they count as frozen.
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
- Test servers (`.claude/launch.json`) point `--pushover`, `--still-alive`, `--aisstream` and `--nfl` at non-existent
  files so a test can never send a real alert, ping, subscription or position, and `--recordings` at a scratch folder
  so tests never write into the real GPS diagnostics.
- noforeignland.com uploads go out only from the `NoForeignLand` worker thread and only take points from the watch's
  track (never a frozen position). NFL issues an integration key per app (requested 2026-10-28). Until it arrives the
  user chose to use the Signal K plugin's shared key, in the local `nfl.json` only: never put that key in code, tests
  or docs, and replace it in `nfl.json` as soon as NFL's own key arrives.
- Times are always shown 24-hour (`clock()` / `dayTime()` in the page); never use locale time defaults (AM/PM).
- Map tiles are fetched on demand only (OpenStreetMap tile policy): no bulk or prefetch downloads.
- The app has no sea charts (the user had them removed in 1.12.0; Seamarks stay). Only add a map source whose
  terms allow this use. Do not add HNHS (Greece) tiles without their written agreement, and never decode, convert
  or commit CM93 / C-MAP or other proprietary chart data.
- Never colour-invert charts in dark mode (depth-area and light colours carry meaning); dim them instead. Night
  mode may invert, because everything is tinted red there anyway.
- Never commit `pushover.json` (Pushover keys), `still_alive.json` (ping URL, API key), `aisstream.json` (aisstream.io key), `nfl.json` (noforeignland.com keys), `tide_cache.json` (boat area), `certs/`, `anchor_state.json`, `tile_cache/`, `captures/` or `recordings/` (all in `.gitignore`).
  They hold keys, the boat's positions and network captures. Keep real MMSIs and anchorage positions out of tests.

## Verifying changes

```powershell
.venv\Scripts\python.exe -m unittest test_anchor_watch   # simulated Core data, no network
```

For frontend edits, syntax-check the JavaScript in `static/anchor.html` and check the page at phone width (375 px)
in light, dark and night mode. A test server can run beside the live one with `--port 8767 --state <scratch file>`.
Bump `VERSION` in `anchor_watch.py` and add a `CHANGELOG.md` entry for each release.
