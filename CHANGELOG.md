# Changelog

All notable changes to the Orca Core 2 Anchor Watch. Newest first.

---

## 1.9 — Chain formula, AIS proximity

### 1.9.0
- **Recommended chain** (renamed from Chain) now comes from a **formula setting** (`ChainFormula`): D = deepest water in
  the next 24 h, B = bow height, LOA = the new *LOA* setting; numbers, + - * / (or x, ×), brackets, comma or point
  decimals, e.g. `3*D + LOA`. Parsed into an expression tree and never executed; invalid formulas are refused with a
  message. The Tide tile shows the formula with its values under the label (readable on a phone). The old fixed
  *Scope for chain advice* is converted to `<scope>*(D+B)`.
- **AIS closest** tile: the distance to the nearest target in whole metres as the main number, with its name, the
  number within 600 m and the alarm limit.
- **AIS proximity alarm** (*Min AIS distance*, 0 = off): a target closer than the limit for 10 s while anchored raises
  "AIS target close" (high-priority push). My MMSI never counts, nor internet AIS older than 3 minutes.

---

## 1.8 — Water depth and under-keel clearance

### 1.8.1
- **Every depth with two decimals**: Depth tile, depth at drop, the Tide tile (now, lowest/highest, under keel, water
  at the shallowest moment), the chain-advice tooltip, the rode helper and the depth-alarm text.
- **Tide tile on a row of its own** at the end of the tiles, full width; the other tiles are packed densely, so Sun
  and Heading keep their normal height instead of stretching to the Tide tile's. The chart is drawn at the tile's
  real width (no scaling), so its labels keep their size.

### 1.8.0
- **All depths are water depth** (below the waterline): Depth tile, depth alarm (*Min water depth*), depth recorded at
  drop, the rode helper, chain advice and the Tide tile. Water depth = reading + transducer depth, from the sounder's
  own N2K offset when it reports a positive one, otherwise the new *Transducer below waterline* setting (the settings
  page says which is used). Note: a *Min depth* set before was compared below the transducer; it now means water depth.
- **Draught** setting (0 = off) and an **under-keel clearance** bar in the Tide tile: the shallowest water in the next
  24 h minus the draught, as draught + clearance in one bar, amber below 1 m, red below 0.5 m.
- **No second set of low/high times** under the Tide tile: the chart's times (the lowest and highest depth in the next
  24 h, from the hourly values) and the tide's smoothed turning points could be hours apart for a tide of a few cm,
  which read as a contradiction. The chart is now the only source.

---

## 1.7 — Depth and chain advice

### 1.7.1
- **New Tide tile layout**: a chart of the depth from 6 h back to 24 h ahead (sounder depth now plus the tide curve,
  `depth_window`'s new `curve`), with the lowest and highest depth of the next 24 h as dashed lines and values, their
  times as markers, and "now" as a dotted line with a dot. Below it the tide icon with Rising/Falling and the range,
  and the chain advice as a label (its formula in the tooltip). Without a depth reading the chart shows the sea level
  and says why there is no depth.

### 1.7.0
- **Depth over the next 24 h** in the Tide tile: lowest and highest depth with their times, from the depth now and the
  tide curve (`depth_window`). The depth is below the waterline when the sounder reports a positive transducer offset
  (added), otherwise below the transducer (marked).
- **Chain advice in whole metres**: scope x (highest depth in the next 24 h + bow height), rounded up
  (`chain_advice`); new settings *Bow height above water* (1.2 m) and *Scope for chain advice* (5:1).
- With the sounder off, the **last depth reading** (kept over restarts) is used, corrected for the tide since it was
  taken, and only within 150 m (or twice the radius) of where it was taken, so a reading from another anchorage is
  never used.

---

## 1.6 — Tide

### 1.6.0
- **Tide tile**: rising/falling with an icon, the next high and low water (24-hour local time), the range in cm and a
  curve from 6 h back to 18 h ahead with the present level marked. Data: the Open-Meteo marine API's hourly sea level
  (tide plus weather effects) for the boat's area (`TideFeed`), fetched every 3 h or after the boat moved 5 km with
  a position rounded to 0.01 degree, and cached for 7 days in `tide_cache.json` so it works offline. High and low
  water are found on the 3-hour-smoothed series and refined with a parabola (tested within 20 min on a cm-rounded
  M2 tide). Attribution "Open-Meteo" in the tile.
- **Sun tile** counts down to tomorrow's sunrise after sunset.
- **Night mode: the map's scale bar** no longer has Leaflet's white text shadow, so it is dark red like the rest.

---

## 1.5 — Sun times, precise anchor moves

### 1.5.1
- **Internet AIS now recovers after a dropped connection.** The old "connection closed" error was still set when the
  next connection came up, so the check for a refused key broke that connection off right after aisstream.io's
  subscription confirmation, again and again with growing waits: after one drop internet AIS stayed down until the
  program was restarted (seen from 07:43 to after 11:47 on 2026-10-18). Only an error in the current connection
  counts now; recovery is logged as "receiving again".
- **Anchor-drag alarm on the phone repeats every 30 s for up to 3 h** (was every 60 s for 1 h), the closest Pushover and
  iOS get to "ring until I stop it".
- **Test alarm** / **Stop test alarm** under Phone alerts: a real emergency message (repeating, Critical Alert), so the
  phone set-up can be checked without a drag; *Send test* only sent a normal message. A test alarm stops after
  10 minutes on its own.

### 1.5.0
- **Sunrise and sunset** tile with icons, for the boat's position, in 24-hour local time and with the time left to
  the next one; computed on the page with the usual solar formulas (checked against NOAA's: within 2 minutes), so
  it works offline. Polar day/night says so.
- **Move anchor on map** now keeps a draft position: click the map and/or nudge it with the arrow keys (1 m,
  Shift = 10 m) or the arrow buttons in the bar on a phone. The circle and anchor move with it, the bar shows the
  offset (e.g. "3 m N, 9 m E"), and nothing is sent until *Save* or Enter; Esc cancels. Clicking no longer asks for
  confirmation, *Save* does that job. The map's own arrow-key panning is off while moving.
- **Depth overlay removed**: OpenSeaMap's crowd-sourced soundings added nothing at the anchorage now that the official
  sea chart has real soundings.
- **Checkboxes** are drawn in the theme's colours, so they turn dark red in night mode instead of staying light.

---

## 1.4 — Official sea chart

### 1.4.1
- No more vertical stripes over the sea chart when zoomed in. From zoom 17 the IHM service draws the ENC "overscale"
  pattern wherever the display is larger than the chart's compilation scale, which at the anchorage covers
  everything. The sea chart is now fetched up to zoom 16 and enlarged beyond that: same chart content, no stripes.

### 1.4.0
- **Sea chart layer with the official Spanish electronic charts** of the Instituto Hidrográfico de la Marina (WMS
  `ideihm.covam.es/wms/enc`, layer `RasterENC`), fetched per tile through the PC's tile cache like the other layers.
  At the anchorage it shows soundings, the 5 m contour, rocks, buoys and lights, the anchorage symbol and restricted
  areas. Outside Spanish waters the service returns transparent tiles, so *Sea chart* shows the street map there.
  Attribution "© Instituto Hidrográfico de la Marina" as the IHM licence requires (non-commercial use).
- The base layers are now *Sea chart* (default), *Street map* (the former *Chart*, which was only OpenStreetMap's land
  map), *Satellite* and *Plain*; a saved *Chart* choice becomes *Sea chart*. Dark mode dims the sea chart instead of
  inverting it; night mode turns it dark red like the rest.
- Looked for official sources for Italy and Greece: none usable without an agreement (see README).

---

## 1.3 — Internet AIS

### 1.3.0
- **Internet AIS from aisstream.io** (`InternetAIS`): one WebSocket subscribed to a box around the boat (re-opened when
  the boat has moved 1.5 km), position and static reports folded into targets that expire after 15 min.
- **The own AIS receiver always has priority** (`read_ais`): an internet report is only used for a ship without a
  current local position; a ship that drops out locally is taken over from internet and keeps its local name. My
  MMSI is filtered from both. Internet ships are drawn hollow with an italic name and the report's age; the *AIS
  nearby* tile counts them, and no longer says "feed down" while internet AIS covers a dead local feed.
- *Alarm settings* has an *Internet AIS* switch, a status line and **Reconnect** (re-reads `aisstream.json`, which is
  git-ignored). A key aisstream.io does not accept (it drops the connection straight after the subscription) is
  reported once and retried every 5 minutes instead of hammering the service.
- Checked first: the Orca app's own internet AIS does not come from the Core. With the Core alone its AIS stream was
  empty, and a capture showed the app fetching it from Orca's cloud (`europe-west1-the-orca.cloudfunctions.net`).

---

## 1.2 — Orca Core GPS

### 1.2.0
- **The Orca Core 2's own GPS (source 254) can be chosen** under *Alarm settings → GPS source*, as first choice or
  through *Automatic* (`4 → 127 → 3 → 254`). With the rest of the NMEA 2000 network switched off to save the battery
  it is the only GPS left; measured on 2026-10-16 with the Core alone, it updated its position about every 1.7 s
  (21-32 satellites, HDOP ~0.5) although the Core flagged the boat as stationary, and the watch ran on it normally.
- **Frozen-position check replaces the old rule** that skipped 254 whenever the Core said "stationary": any GPS
  whose position has not changed at all for 60 s is treated as frozen, skipped (failover to the next source) and
  logged, and logged again when it recovers. With no usable GPS left the GPS alarm goes off. This still catches the
  frozen 254 position seen on 2026-10-12 and also a stuck external receiver.
- The page lists every GPS with its name (from the NMEA 2000 device list, read every minute again), satellites,
  HDOP, frozen state and which one is in use. The old default order `4,127,3` is upgraded to `4,127,3,254`.
- The *Depth* and *True wind* tiles say when their alarm is set but no data arrives (instruments switched off).

---

## 1.1 — Phone alerts

### 1.1.2
- All times on the page are 24-hour (`HH:MM:SS`, `Mon 20:47`) whatever the device language: the log, *Watching
  since* and *Track since* no longer follow the browser's 12-hour AM/PM default.

### 1.1.1
- `pushover.json` with a key pasted in place of a field name (instead of after `"app_token":` / `"user_key":`) now
  gets a message that says exactly that, instead of the general "30-character keys" one. Seen during setup.

### 1.1.0
- **Push alerts through Pushover** (`Pushover` in `anchor_watch.py`), so an alarm reaches the phone ashore with the
  page closed: anchor drag as an **emergency** message that repeats every minute until acknowledged (and can be a
  Critical Alert that sounds through silent mode), GPS lost / no Core data / depth / wind as high priority, and an
  all-clear when the condition ends. The emergency is cancelled through its receipt when the alarm clears, is
  silenced in the app or the anchor is raised.
- Sending runs on its own worker thread with retries (5, 15, 45 s), so a slow or absent internet link never delays
  the alarm checks; a key Pushover rejects is not retried and is shown on the page and in the log.
- Keys live in `pushover.json` (git-ignored; `pushover.example.json` shows the format). *Alarm settings* has a
  *Phone alerts* switch, a status line and **Send test**, which re-reads the key file without a restart.
- **Still-alive check** (`StillAlive`): the PC pings healthchecks.io (or any ping URL, `still_alive.json`) every
  5 minutes; if the pings stop for longer than the *Still-alive check* interval (default 60 min, settable, 0 = off),
  healthchecks.io alerts the phone through its Pushover integration. This covers what the PC cannot report itself:
  the PC, the program or the boat's internet going down. With a healthchecks.io API key the check's period follows
  the setting and the check is paused at 0. Failed pings are logged once, recovery too; **Ping now** tests it.

---

## 1.0.0 — first release

Everything below was built and checked against a live Orca Core 2 (firmware 1.21.0) on 2026-10-12.

### Anchor watch
- Anchor alarm served from the PC (`anchor_watch.py`, `static/anchor.html`): drag alarm with a confirm delay against
  GPS jumps, GPS lost, no data from the Core, optional minimum depth and maximum wind; *Silence 5 min*; PC beep;
  the page raises its own alarm when it cannot reach the PC for 15 s.
- Drop modes *Dropping now* (anchor at the bow, using *GPS → bow roller*) and *Already lying to it* (a given
  distance ahead along the heading); *Move anchor on map*; radius helper from rode length, depth and bow height.
- Position from the raw GNSS receivers (`4,127,3`, with failover), **not** from the Core's fused source 254: the
  Core freezes that position while it flags the boat as stationary, which would hide a slow drag. 254 is only
  used while the Core says the boat is moving.
- State (anchor, settings, track, log) survives a restart (`anchor_state.json`).
- **HTTPS, same setup as MasterVolt YMS:** `start_anchor_watch.cmd` + `setup_local_https.ps1` issue the server
  certificate from the existing *Mastervolt Local CA*, so an iPhone that trusts it needs no change. The server binds
  only to the PC's private LAN address (`certs/bind-address.txt`) and refuses any public address. HTTPS also makes
  Wake Lock available on the phone.
- **Start new track** button under the map: deletes the recorded track and the max swing; the watch carries on.
- **My MMSI** setting: that MMSI is dropped before anything else and is never shown as an AIS target. While it is
  empty, a target within 10 m of the GPS is hidden and offered as *Use as My MMSI*.
- App logo (anchor in a dashed swing circle with the boat and its track), used for the browser tab, the page header
  and the iPhone home screen (`static/icons/`, `manifest.webmanifest`).

### Map
- Leaflet map (bundled locally) with *Chart* (OpenStreetMap), *Satellite* (Esri World Imagery) and *Plain* base
  layers, OpenSeaMap *Seamarks* and *Depth* overlays, scale bar, auto-fit to the circle and nearby AIS boats.
- Tiles are fetched by the PC and cached in `tile_cache/` for offline use (30-day refresh, stale copy when offline,
  remembered 404s, at most 2 upstream connections).
- Theme-aware tiles: dark mode inverts the base chart; night mode tints everything red; seamarks are never inverted.

### Reader / protocol
- `orca_reader.parse_message`: the Core passes text through unescaped; one AIS destination containing a backslash
  made every AIS message invalid JSON and stopped the whole AIS feed. Lone backslashes are now repaired.
- The *AIS nearby* tile shows when the AIS feed is down instead of quietly showing 0.

Read-only on the Core throughout: no write endpoints are called.
