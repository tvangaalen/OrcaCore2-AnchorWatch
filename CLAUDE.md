# Orca Core 2 Anchor Watch

Windows-hosted anchor alarm (`anchor_watch.py` + `static/anchor.html`) and live reader (`orca_reader.py`) for a
boat's Orca Core 2. Separate from MasterVolt YMS; it only borrows that project's local HTTPS CA.

Overview: `README.md`. Release history: `CHANGELOG.md`.

## Hard rules

- **Read-only on the Core.** Never call `PUT /v1/sources`, `POST :9001/imu/calibration`, the `/v1/sync` WebSocket
  or the firmware page on port 8090, and never send anything on the sensor WebSockets. Only GETs and listening.
- **Never use the Core's fused position (source 254) to detect drift while it flags the boat as stationary**: it is
  frozen then. Keep the raw-receiver priority and the `navigation.motion.254.stationary` check in `read_boat`.
- **Parse Core messages with `orca_reader.parse_message`**, never plain `json.loads`: the Core sends unescaped text.
- The alarm logic runs on the server (`AnchorWatch.tick`); the page only displays and sends commands. Do not move
  alarm decisions into the browser.
- Keep everything on a private LAN: the server binds only to localhost or an RFC 1918 address (`check_bind`).
  Never bind a public interface, port-forward it or add a public tunnel (Cloudflare, ngrok): the app has no login.
  Remote access is through UniFi Teleport (VPN into the boat LAN; clients come from 192.168.2.x), so do not
  restrict requests to the 192.168.1.x subnet.
- HTTPS certificates come from the existing *Mastervolt Local CA* (`setup_local_https.ps1`). Never create a new CA
  (the iPhone would need it installed again), never write into the CA folder, never change the Windows trust store.
- Push alerts go out only through the `Pushover` worker thread; never send from `tick()` or a request thread, and keep
  anchor drag as the only emergency (priority 2) alert so the phone does not learn to ignore it.
- The still-alive ping must keep running independently of the alarm logic (its own thread in `StillAlive`) and must
  ping more often than the interval; never make it depend on the page being open.
- Map tiles are fetched on demand only (OpenStreetMap tile policy): no bulk or prefetch downloads.
- Never commit `pushover.json` (Pushover keys), `still_alive.json` (ping URL, API key), `certs/`, `anchor_state.json`, `tile_cache/`, `captures/` or `recordings/` (all in `.gitignore`).
  They hold keys, the boat's positions and network captures. Keep real MMSIs and anchorage positions out of tests.

## Verifying changes

```powershell
.venv\Scripts\python.exe -m unittest test_anchor_watch   # simulated Core data, no network
```

For frontend edits, syntax-check the JavaScript in `static/anchor.html` and check the page at phone width (375 px)
in light, dark and night mode. A test server can run beside the live one with `--port 8767 --state <scratch file>`.
Bump `VERSION` in `anchor_watch.py` and add a `CHANGELOG.md` entry for each release.
