<p align="center"><img src="static/icons/logo.svg" width="96" alt="Anchor Watch logo"></p>

# Orca Core 2 Anchor Watch

An anchor alarm for a boat with an **Orca Core 2**, plus a read-only live reader for everything the Core publishes.
Runs on a Windows PC on the boat LAN; open it on an iPhone (or any browser) over HTTPS.
**Read-only on the Core**: it only listens to the streams the Orca app uses.

| Part | Start | Page |
|---|---|---|
| Anchor watch | `start_anchor_watch.cmd` | `https://<PC LAN address>:8766/` |
| Live reader (all Core values, AIS, NMEA 2000 devices) | `run.cmd` | `http://localhost:8765/` |

Not for navigation.

## Anchor watch

### Starting it (HTTPS, same setup as MasterVolt YMS)

Double-click `start_anchor_watch.cmd`. It:

1. runs `setup_local_https.ps1`, which issues this app's certificate from the existing **Mastervolt Local CA**
   (`C:\Temp\mastervoltproject\certs`, override with the `ANCHOR_CA_DIR` environment variable). An iPhone that
   already trusts that CA for MasterVolt YMS opens the anchor watch with no change. The script never creates a CA,
   never writes into the CA folder and never touches the Windows certificate store;
2. creates `.venv` on first run;
3. starts the server on the PC's private LAN address only (the same address MasterVolt YMS uses, written to
   `certs/bind-address.txt`), port 8766. It prints the address but does not open a browser tab.

`run_anchor.cmd` still works and calls `start_anchor_watch.cmd`. The server refuses any address that is not localhost
or a private LAN address (10.x, 172.16-31.x, 192.168.x). There is no login: anyone on the boat network can open
the page and change the watch.

On the iPhone, *Share → Add to Home Screen* gives the app its own icon and a full-screen window. HTTPS also lets the
page keep the screen awake (Wake Lock) while sound is on.

### Remote access (UniFi Teleport)

Away from the boat, connect the iPhone with **UniFi Teleport** (WiFiman app) and open the same address,
`https://<PC LAN address>:8766/`. Teleport is a WireGuard VPN run by the boat's UniFi router: it works although
both internet links (Starlink and 5G) sit behind carrier NAT, the page and its certificate stay exactly the same, and
the server is never reachable from the internet. Teleport clients arrive from their own subnet (192.168.2.x), so the
Windows Firewall rule for Python must allow remote addresses beyond the local subnet on the *Private* profile
(the default "allow" rule does). Note that Teleport gives the phone the whole boat LAN, not only this app.

Do not port-forward the server or put it behind a public tunnel: it has no login.

### Phone alerts ashore (Pushover)

The PC pushes alarms to the iPhone through [Pushover](https://pushover.net), so they arrive with the page closed,
the phone locked and without Teleport:

| Alarm | Pushover priority |
|---|---|
| Anchor drag | **Emergency**: repeats every 30 s (Pushover's minimum) for up to 3 h until you tap *Acknowledge* in Pushover |
| GPS lost, no data from the Core, depth, wind | High |
| Alarm cleared | Normal (and the emergency for it is cancelled) |

Silencing the alarm in the app or raising the anchor also cancels the repeating emergency. Each alert links back to
the app (open it over Teleport).

Setup, once:

1. Install Pushover on the iPhone (one-time purchase after the trial) and sign in.
2. On [pushover.net](https://pushover.net), copy your **user key**, and create an application named *Anchor Watch*
   ([apps/build](https://pushover.net/apps/build)) to get its **API token**.
3. Copy `pushover.example.json` to `pushover.json` next to `anchor_watch.py` and fill in both keys
   (`pushover.json` is in `.gitignore`; never commit it). Optional: `emergency_sound`, `device`.
4. In the app, under *Alarm settings*, press **Send test** (no restart needed).
5. On the iPhone: in the Pushover app, *Settings → Critical Alerts*, turn on critical alerts for emergency-priority
   messages and allow them when iOS asks; check *iPhone Settings → Notifications → Pushover* that *Critical Alerts*
   and *Allow Notifications* are on. Then an anchor drag sounds even in silent mode and Focus / Do Not Disturb.
6. Press **Test alarm**: a real emergency message that repeats every 30 s until you tap *Acknowledge* in Pushover or
   press **Stop test alarm** (it stops by itself after 10 minutes). Check it rings with the phone on silent.

iOS limits the sound of one notification to 30 seconds, so no push app can ring without a break; repeating every
30 s until acknowledged is the closest there is. With the page open on the phone (sound on), the page itself also
keeps beeping until the alarm is silenced.

*Phone alerts (Pushover)* in Alarm settings switches them off. Alerts need the PC and its internet link: if either is
down, nothing can be sent. That is what the still-alive check is for.

### Still-alive check (healthchecks.io)

The PC pings an outside monitor, [healthchecks.io](https://healthchecks.io) (free plan), every 5 minutes. If the pings
stop for longer than the **Still-alive check** interval (Alarm settings, default **60 min**, 0 = off), because the
PC, this program or the boat's internet went down, healthchecks.io alerts the phone through its own Pushover
integration. Pinging more often than the interval means one lost ping over a flaky link causes no false alarm.

Setup, once:

1. Create a free account on healthchecks.io and add a check named *Anchor Watch*. Set **Period** to the same value as
   the app's interval (60 min) and **Grace** to 1 min.
2. Under *Integrations*, add **Pushover** and choose the priority for "down" (Emergency makes it repeat until
   acknowledged) and for "up". Make sure the check uses that integration.
3. Copy `still_alive.example.json` to `still_alive.json` (git-ignored) and paste the check's **ping URL**.
   Optional: a **read-write API key** of the project (*Settings → API Access*); the app then sets the check's period
   itself whenever you change the interval, and pauses the check when you set 0.
4. Press **Ping now** under Alarm settings; the check turns green on the website.

Without an API key, change the period on the website whenever you change the interval in the app (the page reminds
you). If you shut the watch PC down on purpose, pause the check on healthchecks.io (or set the interval to 0 first
when the API key is in place), otherwise it alerts after the interval.

### What it does

- **Two screens:** the anchor watch (alarm banner, map, tiles, drop/watch controls) and **Settings** behind the gear
  at the top right, in the sections *Alarms*, *Boat details*, *Other settings* and *Credits* (all required
  attributions, and only there), with the log at the bottom. The (i) button in the map's corner opens the credits; the
  map itself carries no attribution line. The
  alarm banner stays visible on both. `#settings` in the address opens the settings directly, and the phone's back
  button returns to the watch.
- **Position** tile: the GPS position in the format chosen under *Other settings → Position format*: degrees and
  decimal minutes (`12° 34.567′ N`, default), degrees/minutes/seconds, or decimal degrees.
- The map always opens on **Street map**; another layer chosen while the page is open lasts until it is reloaded.

- **The watch runs on the PC, not in the browser.** Alarms are evaluated every second and beep through the PC
  speakers (setting *Beep on the watch PC*) even with no page open. Pages sound too after one tap on *Sound*, and a page
  raises its own alarm if it cannot reach the PC for 15 s (e.g. the phone dropped off Wi-Fi).
- **Alarms:** drag (outside the radius for *Drag confirm* seconds, default 10, which filters GPS jumps), GPS lost,
  no data from the Core, and optional minimum depth and maximum true wind. *Silence* mutes for 5 min; an alarm that
  clears and comes back sounds again.
- **Dropping:** *Dropping now* puts the anchor at the bow (GPS position + *GPS → bow roller* along the heading);
  *Already lying to it* puts it a given distance further ahead. *Move anchor on map* corrects it afterwards: click
  the map and/or nudge it with the arrow keys (1 m, Shift = 10 m; on the phone the arrow buttons in the bar); the
  circle follows, and nothing changes until *Save* (or Enter). Esc cancels.
- **Sun:** sunrise and sunset for the boat's position (computed on the page, no internet needed), in 24-hour time,
  with the time left to the next one (after sunset: to tomorrow's sunrise).
- **Tide:** rising or falling, the next high and low water, the range and a curve from 6 h back to 18 h ahead. From
  the [Open-Meteo](https://open-meteo.com/) marine model (sea level including tide and weather, hourly, a grid of
  several km; free for non-commercial use with attribution). The PC fetches it every 3 h or when the boat has moved
  5 km, sending only a position rounded to about 1 km, and keeps a 7-day copy in `tide_cache.json` (git-ignored) so it
  also shows offline. Mediterranean tides are small (often 10-20 cm); not for navigation.
- **Depth next 24 h and minimum chain** (in the Tide tile): the lowest and highest water depth over the next 24 h,
  from the sounder's depth now plus the tide curve, and the minimum chain in whole metres (rounded up) from the
  *Minimum chain formula* setting. Variables: **D** = deepest water in the next 24 h, **B** = *Bow height above
  water*, **LOA** = *LOA*; numbers, + - * / (or x) and brackets, e.g. `5*(D+B)` (the default) or `3*D + LOA`. The
  formula is parsed, never run as code; an invalid one is refused when saving. With the sounder switched off, the last
  reading is used, corrected for the tide since then, but only within 150 m (or twice the radius) of where it was taken.
- **Water depth everywhere:** every depth in the app (Depth tile, depth alarm, depth at drop, chain advice, Tide tile)
  is below the waterline: the sounder's reading plus the transducer depth, which the sounder reports itself when it
  can (N2K offset), otherwise the *Transducer below waterline* setting.
- **Under the keel:** with *Draught* set (0 = off), the Tide tile shows the least water under the keel in the next
  24 h as a bar (draught + clearance), amber below 1 m and red below 0.5 m.
  The rode helper suggests a radius from rode length, depth and bow height.
- **Track:** the last 24 h (setting *Track kept*) is drawn on the map, older parts fainter. *Start new track* under
  the map deletes the recorded track and the max swing and starts afresh; the anchor watch itself carries on.
- **My MMSI** (Alarm settings): this boat's own AIS is never shown as a target. While it is empty, any target within
  10 m of the GPS is hidden and the page offers it as *Use as My MMSI*.
- **AIS:** targets within 600 m are drawn on the map; boats within 120 m (or twice the radius) are kept in view. The
  *AIS closest* tile shows the distance to the nearest target in whole metres. With *Min AIS distance* set (0 = off),
  a target closer than that for 10 s while anchored raises the "AIS target close" alarm (high-priority push); internet
  AIS only counts when its report is less than 3 minutes old.
  The *AIS nearby* tile says so when the AIS feed is down.
- **Internet AIS** (optional, [aisstream.io](https://aisstream.io), free): adds ships the boat's own AIS receiver does
  not see, e.g. with the instruments switched off. **The own receiver always has priority**: an internet report is
  only used for a ship with no current local position (none, or older than 15 min). Internet ships are drawn hollow,
  with the age of the report; My MMSI is hidden here too. The Orca app's own internet AIS cannot be used: it comes
  from Orca's cloud straight to the app, not through the Core (checked with a capture, 2026-10-16).
  Setup: on aisstream.io *Sign in with GitHub*, create a key under *API Keys*, copy `aisstream.example.json` to
  `aisstream.json` (git-ignored) with the key, and press **Reconnect** under Alarm settings. Only the key and a box
  of about 6 x 8 km around the boat are sent; the box follows the boat.
- **GPS source** (Alarm settings): *Automatic* tries `4 → 127 → 3 → 254` (B&G iGPS, ZG100, Onwa AIS GPS, Orca Core),
  or pick one GPS to go first, e.g. **254, the Orca Core 2's own GPS**: with the rest of the NMEA 2000 network switched
  off to save the battery it is the only one left, and the watch keeps working on it. The next source in line takes
  over when one stops. The list shows which GPS is in use, its satellites and HDOP.
- **Frozen-position check:** the Core's position is not always live while it flags the boat as stationary (on
  2026-10-12, with the other receivers on, it stayed at one exact value for minutes; on 2026-10-16, Core alone, it
  kept moving). A frozen position would hide a slow drag, so any GPS whose position has not changed at all for 60 s
  is skipped and logged; real receivers always jitter in the last digit. Each source sends two positions: the normal
  one and the receiver's own GNSS fix (`navigation.gnss`). If the normal one is held but the own fix still changes,
  the own fix is used (shown as *own GNSS fix* in the GPS list); only when both are held is the source frozen.
  The Core's own GPS (254) is the exception: it has a **static hold**. Recordings of 2026-10-26 show it keeps one fix
  for up to 10 min while the boat lies still and moves it on as soon as the boat is about 10 m away (every release
  jumped 10.0-10.1 m). While its receiver keeps producing fixes, a held 254 position stays in use, shown as
  *GPS hold* in the Position tile: it is within about 10 m, so a drag shows at the latest about 10 m later.
  A live receiver is preferred over a held 254, so with the instruments on the track stays smooth. With no
  usable GPS left, the GPS alarm goes off. While a source is held, its GPS values are recorded for 10 min to
  `recordings/gps_held_*.jsonl` (local only, git-ignored), to find out why it held.
- With the instruments off there is no depth or wind; the tiles then say that the depth or wind alarm is inactive.
- Anchor, settings, track and log are kept in `anchor_state.json`, so a restart resumes the watch.

### Map

- **Layers:**
  - *Sea chart*: the **official Spanish electronic charts** of the Instituto Hidrográfico de la Marina (IHM) in
    Spanish waters (depth contours and soundings, rocks, buoys and lights, anchorages, restricted areas), over the
    street map elsewhere, where the chart service is transparent. Free for non-commercial use with the attribution
    "© Instituto Hidrográfico de la Marina" ([licence](https://ideihm.covam.es/portal/licencias/)); not for navigation.
  - *Street map* (OpenStreetMap): land map only, the sea is plain blue.
  - *Satellite* (Esri World Imagery) and *Plain*.
  - Overlay from OpenSeaMap: *Seamarks* (buoys, beacons, lights).
  - No official source could be used for Italy (no public chart service of the Istituto Idrografico della Marina) or
    Greece (the HNHS chart service needs an application and agreement), and CM93 (C-MAP) is proprietary data that is
    not built in. The Orca Core holds no chart data (the Orca app keeps its licensed charts on the phone).
- **Offline:** the PC fetches tiles on demand and keeps them in `tile_cache/` (refreshed after 30 days; the old copy
  is used when there is no internet). Any area viewed while online stays available offline. Nothing is
  bulk-downloaded, in line with the OpenStreetMap tile policy. With no internet and no cached tiles, the circle, track
  and boats still show on a plain background. `--tile-cache <folder>` moves the cache.
- Dark and night (red) display modes tint the map too; seamark colours are never inverted.
- Leaflet 1.9.4 is included in `static/vendor/leaflet/` (checked against the cdnjs SHA-512 hashes; BSD-2-Clause,
  © Vladimir Agafonkin), so the page needs no CDN.

### Command line

```
.venv\Scripts\python.exe anchor_watch.py [--host 192.168.1.124] [--bind 127.0.0.1] [--port 8766]
    [--certfile certs\server-cert.pem --keyfile certs\server-key.pem] [--state anchor_state.json]
    [--tile-cache tile_cache] [--no-ais]
```

Without `--certfile/--keyfile` it serves plain HTTP (development only; phones then get no wake lock).

## Live reader

Double-click `run.cmd` (creates `.venv` on first run), or:

```
.venv\Scripts\python.exe orca_reader.py [--host 192.168.1.124] [--record] [--interval 200] [--no-ais]
```

- `--record [folder]` appends every raw message to `recordings/<date>_sensors.jsonl` / `_ais.jsonl`
  (roughly 0.5 MB per minute with the default 200 ms interval).
- `--bind 0.0.0.0` makes the page reachable from other devices on the LAN (default: this PC only).
- `GET /api/state` returns the decoded values as JSON; `GET /api/raw` returns the raw key → value map.

## Orca Core 2 protocol (found by capturing the iPhone app, Core firmware 1.21.0)

Orca publishes no third-party API; this came from capturing the Orca app's own traffic.

| What | Where |
|---|---|
| Live sensors | `ws://<core>:8089/v1/sensors/delta?interval=200&ns=%5E(?!.*(ais))&enableUnknownSources` |
| AIS targets | `ws://<core>:8089/v1/sensors/delta?interval=5000&ns=ais&enableUnknownSources` |
| NMEA 2000 devices | `GET http://<core>:8088/v1/devices` |
| Other read-only GETs the app uses | `:8088/v1/sources?name`, `/v1/settings`, `/v1/nmea2000/status`, `/v1/logbook`, `:9081/v1/radars?active`, `:8080/info` |

A stream starts with one `"event_type":"full"` message, followed by `"delta"` messages holding only changed values
plus `values_age` (ms since the Core last updated each value). Keys are `<group>.<N2K source address>.<field>`,
e.g. `navigation.heading.254.heading`; wind adds an instance (`environment.wind.254.2.angle`, 2 = apparent).
Source 254 is the Core's own fused value. Units are SI: radians, rad/s, m/s, metres, volts.
Text fields are passed through unescaped: an AIS destination containing a backslash (seen: `IBIZA\FORMENTERA`)
makes the message invalid JSON, so `parse_message` repairs lone backslashes before giving up.

**Not used on purpose** (they change state on the Core): `PUT /v1/sources`, `POST :9001/imu/calibration`,
the `/v1/sync` WebSocket, and the firmware update page on port 8090.

The packet captures the protocol was worked out from stay in `captures/` locally and are not in the repository.

## Tests

```
.venv\Scripts\python.exe -m unittest test_anchor_watch
```

Simulated Core data, no network: drag, GPS jumps, the frozen Core position, receiver failover, GPS and Core loss,
silence, restart, depth/wind, tile cache, the JSON repair, My MMSI and *Start new track*.

Release history: [CHANGELOG.md](CHANGELOG.md).
