# Changelog

All notable changes to the Orca Core 2 Anchor Watch. Newest first.

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
