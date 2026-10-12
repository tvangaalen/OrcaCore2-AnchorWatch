"""Anchor watch for the Orca Core 2.

Reads the Core's live sensor and AIS streams (read-only, same streams as orca_reader.py), keeps an anchor
position and swing radius, and raises alarms when the boat drags, GPS is lost, the Core stops talking, or
optional depth/wind limits are crossed. The watch runs here, not in the browser: alarms are evaluated and
(on Windows) sounded on this PC even when no page is open. The page at http://<this PC>:8766/ shows the
swing circle, track and nearby AIS boats and works from a phone on the boat Wi-Fi.

Position source: the Core's own fused position (source 254) is NOT used for the watch. While the Core flags
the boat as stationary (navigation.motion.254.stationary = 1) it freezes that position (verified 2026-10-12:
254 stayed at one value for minutes while the raw receivers moved), which would hide a slow drag. The raw
GNSS receivers are used instead, in the priority order of the `gps_sources` setting, with failover.

State (anchor, settings, track, events) is kept in anchor_state.json so a restart does not lose the watch.

Map tiles (OpenStreetMap, OpenSeaMap seamarks, Esri satellite) are fetched by this PC and kept in tile_cache/, so
areas viewed while online still show offline. Only tiles a page asks for are fetched; nothing is bulk-downloaded.
"""

import argparse, asyncio, ipaddress, json, logging, math, os, re, ssl, sys, threading, time, urllib.error, urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from orca_reader import AIS_PATH, CORE_SOURCE, KN, RAD, SENSORS_PATH, Store, stream_loop

try: import winsound
except ImportError: winsound = None

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
log = logging.getLogger("anchor")
EARTH = 6371008.8
FRESH_S = 10            # a position older than this is not used
TRACK_EVERY_S = 5
AIS_RANGE_M = 600

DEFAULTS = {
    "default_radius": 40.0,   # m, used when dropping without a radius
    "confirm_s": 10,          # boat must stay outside the circle this long before the drag alarm
    "gps_timeout_s": 30,      # no usable position for this long -> GPS alarm
    "core_timeout_s": 30,     # sensor stream down for this long -> Core alarm
    "min_depth": 0.0,         # m below transducer, 0 = off
    "max_wind_kn": 0.0,       # true wind speed, 0 = off
    "bow_offset": 0.0,        # m from GPS antenna forward to the bow roller
    "gps_sources": "4,127,3", # raw GNSS sources in order of preference (254 is a last resort)
    "own_mmsi": "",           # "My MMSI": this boat's AIS, never shown as a target; empty = hide any target within 10 m of our GPS
    "pc_sound": True,         # beep on this PC while an alarm sounds
    "track_hours": 24,
}
SETTING_LIMITS = {"default_radius": (5, 2000), "confirm_s": (0, 600), "gps_timeout_s": (5, 600), "core_timeout_s": (5, 600),
                  "min_depth": (0, 500), "max_wind_kn": (0, 150), "bow_offset": (-50, 50), "track_hours": (1, 72)}

# ---------------------------------------------------------------- geometry (flat earth is fine for < a few km)


def dist_bearing_m(lat1, lon1, lat2, lon2):
    """Metres and true bearing (deg) from point 1 to point 2."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * EARTH * math.asin(min(1, math.sqrt(a)))
    b = math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360
    return d, b


def offset(lat, lon, bearing_deg, metres):
    b = math.radians(bearing_deg)
    return (lat + math.degrees(metres * math.cos(b) / EARTH),
            lon + math.degrees(metres * math.sin(b) / (EARTH * math.cos(math.radians(lat)))))


def num(x): return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)

# ---------------------------------------------------------------- reading the Core values


def read_boat(sensors, now, sources_pref):
    """Pick the position and the other values the watch needs from the Store's sensor map (key -> (value, t))."""
    def val(k):
        v = sensors.get(k); return (v[0], now - v[1]) if v and num(v[0]) else (None, None)

    cands = {}
    for k in sensors:
        if k.startswith("navigation.position.") and k.endswith(".latitude"):
            src = k.split(".")[2]
            lat, a1 = val(k); lon, a2 = val(f"navigation.position.{src}.longitude")
            fix, _ = val(f"navigation.gnss.{src}.method")
            if lat is None or lon is None or fix == 0 or (lat == 0 and lon == 0): continue
            if src == CORE_SOURCE and val("navigation.motion.254.stationary")[0] == 1: continue   # frozen, see module doc
            cands[src] = {"lat": lat, "lon": lon, "age": max(a1, a2), "source": src}
    order = [s for s in sources_pref if s != CORE_SOURCE]
    order += sorted((s for s in cands if s not in order and s != CORE_SOURCE), key=lambda s: cands[s]["age"]) + [CORE_SOURCE]
    pos = next((dict(cands[s]) for s in order if s in cands and cands[s]["age"] <= FRESH_S), None)
    if pos:
        src = pos["source"]
        pos["hdop"], _ = val(f"navigation.gnss.{src}.HDOP"); pos["sats"], _ = val(f"navigation.gnss.{src}.satellites")
        pos["frozen_core"] = src == CORE_SOURCE

    def first(keys):
        for k in keys:
            v, a = val(k)
            if v is not None and a <= FRESH_S: return v, k
        return None, None

    hdg, hk = first(["navigation.heading.254.heading", "navigation.heading.128.heading", "navigation.heading.9.heading"])
    if hdg is not None:
        src = hk.split(".")[2]; ref, _ = val(f"navigation.heading.{src}.reference")
        if ref == 1:                                   # magnetic -> true
            var, _ = first([f"navigation.heading.{src}.variation", "navigation.heading.254.variation", "navigation.magvar.254.variation"])
            hdg += var or 0
        hdg = hdg * RAD % 360
    depth, _ = first([k for k in sensors if k.startswith("environment.depth.") and k.endswith(".belowTransducer")])
    tws, _ = first(["environment.wind.254.0.speed", "environment.wind.254.3.speed", "environment.wind.2.0.speed", "environment.wind.2.3.speed"])
    twd, _ = first(["environment.wind.254.0.angle", "environment.wind.2.0.angle"])
    sog, _ = first(["navigation.cogsog.254.speed"] + ([f"navigation.cogsog.{pos['source']}.speed"] if pos else []))
    return {"pos": pos, "heading": hdg, "depth": depth, "tws": tws * KN if tws is not None else None,
            "twd": twd * RAD % 360 if twd is not None else None, "sog": sog * KN if sog is not None else None}


def read_ais(ais, now, lat, lon, own_mmsi="", near_us=None):
    """AIS targets within AIS_RANGE_M. My MMSI is dropped before anything else, so it can never be shown.
    With no MMSI set, targets within 10 m of our GPS are taken to be ourselves; they are added to near_us (if given)
    so the page can offer to save one as My MMSI."""
    targets = {}
    for k, (v, t) in ais.items():
        parts = k.split(".")                           # ais.x.<mmsi>.position.<field>
        if len(parts) < 5 or (own_mmsi and parts[2] == own_mmsi): continue
        tg = targets.setdefault(parts[2], {"mmsi": parts[2], "age": 1e9}); tg[parts[-1]] = v; tg["age"] = min(tg["age"], now - t)
    out = []
    for tg in targets.values():
        if not (num(tg.get("latitude")) and num(tg.get("longitude"))) or lat is None: continue
        d, b = dist_bearing_m(lat, lon, tg["latitude"], tg["longitude"])
        if d > AIS_RANGE_M or tg["age"] > 900: continue
        if not own_mmsi and d < 10:
            if near_us is not None: near_us.append({"mmsi": tg["mmsi"], "name": str(tg.get("name") or "").strip(), "dist": round(d, 1)})
            continue
        out.append({"mmsi": tg["mmsi"], "name": str(tg.get("name") or "").strip(), "lat": tg["latitude"], "lon": tg["longitude"],
                    "dist": round(d, 1), "bearing": round(b), "length": tg.get("length") if num(tg.get("length")) else None,
                    "heading": round(tg["headingTrue"] * RAD % 360) if num(tg.get("headingTrue")) and tg["headingTrue"] < 6.3 else None,
                    "cog": round(tg["COG"] * RAD % 360) if num(tg.get("COG")) else None,
                    "sog": round(tg["SOG"] * KN, 1) if num(tg.get("SOG")) else None, "age": round(tg["age"])})
    return sorted(out, key=lambda x: x["dist"])

# ---------------------------------------------------------------- the watch


class AnchorWatch:
    ALARM_TEXT = {"drag": "Anchor drag", "gps": "GPS position lost", "core": "No data from Orca Core",
                  "depth": "Shallow water", "wind": "Wind limit"}

    def __init__(self, store, state_file):
        self.store, self.state_file = store, Path(state_file)
        self.lock = threading.RLock()
        self.settings = dict(DEFAULTS)
        self.anchor = None                 # {"lat", "lon", "radius", "set_at"}
        self.track = deque()               # [t, lat, lon]
        self.track_gen = 0                 # bumped when the track is cleared, so pages refetch it
        self.events = deque(maxlen=300)
        self.alarms = {}                   # id -> {"since", "text"}
        self.pending = {}                  # id -> time the condition started
        self.silenced_until = 0
        self.max_dist = 0.0
        self.boat = {"pos": None, "heading": None, "depth": None, "tws": None, "twd": None, "sog": None}
        self.last_pos_t = None
        self.last_source = None
        self.core_down_since = None
        self.started = time.time()
        self.dirty = False
        self.load()

    # ---- persistence
    def load(self):
        try: d = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError: return
        except Exception as e: log.warning("could not read %s: %s", self.state_file, e); return
        self.settings.update({k: v for k, v in d.get("settings", {}).items() if k in DEFAULTS})
        self.anchor = d.get("anchor"); self.max_dist = d.get("max_dist", 0.0)
        self.track = deque(d.get("track", [])); self.events.extend(d.get("events", []))
        if self.anchor: log.info("anchor watch restored: %.6f, %.6f radius %.0f m", self.anchor["lat"], self.anchor["lon"], self.anchor["radius"])

    def save(self):
        with self.lock:
            d = {"settings": self.settings, "anchor": self.anchor, "max_dist": self.max_dist,
                 "track": list(self.track), "events": list(self.events)}
            self.dirty = False
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8"); os.replace(tmp, self.state_file)

    def event(self, text, now=None, kind="info"):
        self.events.append({"t": now or time.time(), "text": text, "kind": kind}); log.info(text); self.dirty = True

    # ---- evaluation, once per second
    def tick(self, now=None):
        now = now or time.time()
        with self.store.lock:
            sensors = dict(self.store.values["sensors"]); connected = self.store.status["sensors"]["connected"]
        with self.lock:
            s = self.settings
            prefs = [x.strip() for x in str(s["gps_sources"]).split(",") if x.strip()]
            boat = read_boat(sensors, now, prefs); self.boat = boat; pos = boat["pos"]
            self.core_down_since = None if connected else (self.core_down_since or now)
            if pos:
                self.last_pos_t = now
                if pos["source"] != self.last_source:
                    if self.last_source is not None or pos["frozen_core"]:
                        self.event(f"Position source now {pos['source']}" + (" (Core fused position, frozen while stationary)" if pos["frozen_core"] else ""), now, "warn")
                    self.last_source = pos["source"]
                if not self.track or now - self.track[-1][0] >= TRACK_EVERY_S:
                    self.track.append([round(now, 1), round(pos["lat"], 7), round(pos["lon"], 7)]); self.dirty = True
                while self.track and self.track[0][0] < now - s["track_hours"] * 3600: self.track.popleft()

            cond = {}
            if self.anchor:
                a = self.anchor
                core_down = self.core_down_since is not None
                cond["core"] = (core_down, s["core_timeout_s"], "")
                no_pos = pos is None
                cond["gps"] = (no_pos and not core_down, s["gps_timeout_s"], "")
                if pos:
                    d, b = dist_bearing_m(a["lat"], a["lon"], pos["lat"], pos["lon"])
                    self.max_dist = max(self.max_dist, d)
                    cond["drag"] = (d > a["radius"], s["confirm_s"], f"{d:.0f} m from anchor (limit {a['radius']:.0f} m)")
                if s["min_depth"] > 0 and boat["depth"] is not None:
                    cond["depth"] = (boat["depth"] < s["min_depth"], 10, f"{boat['depth']:.1f} m (limit {s['min_depth']:.1f} m)")
                if s["max_wind_kn"] > 0 and boat["tws"] is not None:
                    cond["wind"] = (boat["tws"] > s["max_wind_kn"], 10, f"{boat['tws']:.0f} kn (limit {s['max_wind_kn']:.0f} kn)")
            for aid, (on, hold, detail) in cond.items():
                if on:
                    self.pending.setdefault(aid, now)
                    if aid not in self.alarms and now - self.pending[aid] >= hold:
                        self.alarms[aid] = {"since": now}; self.event(f"ALARM: {self.ALARM_TEXT[aid]}" + (f" – {detail}" if detail else ""), now, "alarm")
                    if aid in self.alarms: self.alarms[aid]["detail"] = detail
                else:
                    self.pending.pop(aid, None)
                    if aid in self.alarms:
                        del self.alarms[aid]; self.event(f"Cleared: {self.ALARM_TEXT[aid]}", now, "ok")
            for aid in [x for x in self.alarms if x not in cond]: del self.alarms[aid]      # condition switched off
            for aid in [x for x in self.pending if x not in cond]: del self.pending[aid]
            if not self.alarms: self.silenced_until = 0

    def sounding(self, now=None):
        return bool(self.alarms) and (now or time.time()) >= self.silenced_until

    # ---- commands from the page
    def command(self, c):
        now = time.time(); act = c.get("action")
        with self.lock:
            pos, hdg, s = self.boat["pos"], self.boat["heading"], self.settings
            if act == "drop":
                if not pos: raise ValueError("No GPS position – cannot drop the anchor here")
                ahead = s["bow_offset"] + (_num(c, "distance", 0, 2000) if c.get("mode") == "ahead" else 0)
                if ahead and hdg is None: raise ValueError("No heading – cannot place the anchor ahead of the boat")
                lat, lon = offset(pos["lat"], pos["lon"], hdg or 0, ahead) if ahead else (pos["lat"], pos["lon"])
                radius = _num(c, "radius", 5, 2000) if c.get("radius") is not None else s["default_radius"]
                self.anchor = {"lat": lat, "lon": lon, "radius": radius, "set_at": now, "depth": self.boat["depth"]}
                self.max_dist = dist_bearing_m(lat, lon, pos["lat"], pos["lon"])[0]
                self.alarms.clear(); self.pending.clear()
                self.event(f"Anchor down at {lat:.6f}, {lon:.6f}" + (f", {ahead:.0f} m ahead of the GPS" if ahead else "") + f", radius {radius:.0f} m", now)
            elif act == "move":
                if not self.anchor: raise ValueError("Anchor is not set")
                self.anchor["lat"], self.anchor["lon"] = _num(c, "lat", -90, 90), _num(c, "lon", -180, 180)
                self.max_dist = 0.0; self.event(f"Anchor position moved to {self.anchor['lat']:.6f}, {self.anchor['lon']:.6f}", now)
            elif act == "radius":
                if not self.anchor: raise ValueError("Anchor is not set")
                self.anchor["radius"] = _num(c, "radius", 5, 2000); self.event(f"Radius set to {self.anchor['radius']:.0f} m", now)
            elif act == "raise":
                if self.anchor: self.event("Anchor up – watch stopped", now)
                self.anchor = None; self.alarms.clear(); self.pending.clear(); self.silenced_until = 0
            elif act == "silence":
                mins = _num(c, "minutes", 0.5, 60)
                if self.alarms: self.silenced_until = now + mins * 60; self.event(f"Alarm silenced for {mins:g} min", now)
            elif act == "settings":
                for k, v in (c.get("settings") or {}).items():
                    if k not in DEFAULTS: continue
                    if isinstance(DEFAULTS[k], bool): v = bool(v)
                    elif k == "own_mmsi":
                        v = str(v).strip()
                        if v and not (v.isdigit() and len(v) == 9): raise ValueError("My MMSI must be 9 digits (or empty)")
                    elif isinstance(DEFAULTS[k], str): v = ",".join(x.strip() for x in str(v).split(",") if x.strip().isdigit())
                    else:
                        lo, hi = SETTING_LIMITS[k]; v = float(v)
                        if not (math.isfinite(v) and lo <= v <= hi): raise ValueError(f"{k} must be between {lo} and {hi}")
                    self.settings[k] = v
                self.event("Settings changed", now)
            elif act == "clear_track":
                self.track.clear(); self.track_gen += 1; self.max_dist = 0.0; self.event("New track started (old track and max swing deleted)", now)
            else: raise ValueError(f"Unknown action {act!r}")
            self.dirty = True
        self.save()

    # ---- what the page shows
    def state(self):
        now = time.time()
        with self.store.lock:
            ais = dict(self.store.values["ais"]); st = self.store.status["sensors"]
            core = {"connected": st["connected"], "last_error": st["last_error"], "last_message_age": now - st["last_message"] if st["last_message"] else None}
            sa = self.store.status["ais"]
            ais_stream = {"connected": sa["connected"], "last_error": sa["last_error"], "reconnects": sa["reconnects"],
                          "last_message_age": now - sa["last_message"] if sa["last_message"] else None}
        with self.lock:
            b = self.boat; pos = b["pos"]; a = self.anchor
            out = {"time": now, "core": core, "ais_stream": ais_stream, "boat": b, "anchor": a, "settings": self.settings,
                   "alarms": [{"id": k, "text": self.ALARM_TEXT[k], "detail": v.get("detail", ""), "since": v["since"]} for k, v in self.alarms.items()],
                   "pending": {k: now - t for k, t in self.pending.items() if k not in self.alarms},
                   "sounding": self.sounding(now), "silenced_until": self.silenced_until if self.alarms else 0,
                   "max_dist": self.max_dist, "events": list(self.events)[-40:], "track_gen": self.track_gen,
                   "track_points": len(self.track), "track_start": self.track[0][0] if self.track else None, "version": VERSION,
                   "gps_age": now - self.last_pos_t if self.last_pos_t else None}
            if a and pos:
                out["distance"], out["bearing"] = dist_bearing_m(a["lat"], a["lon"], pos["lat"], pos["lon"])
            ref = (pos or a or {})
        near_us = []
        out["ais"] = read_ais(ais, now, ref.get("lat"), ref.get("lon"), str(self.settings["own_mmsi"]), near_us)
        out["own_candidate"] = min(near_us, key=lambda t: t["dist"]) if near_us else None
        return out

    def track_since(self, t, gen):
        with self.lock:
            if gen != self.track_gen: t = 0
            return {"gen": self.track_gen, "points": [p for p in self.track if p[0] > t]}


def _bad(key): raise ValueError(f"{key} must be digits")


def _num(c, key, lo, hi):
    v = c.get(key)
    try: v = float(v)
    except (TypeError, ValueError): raise ValueError(f"{key} must be a number")
    if not (math.isfinite(v) and lo <= v <= hi): raise ValueError(f"{key} must be between {lo} and {hi}")
    return v

# ---------------------------------------------------------------- loops and web server


async def watch_loop(watch):
    last_save = time.time()
    while True:
        try:
            watch.tick()
            if watch.dirty and time.time() - last_save > 30: await asyncio.to_thread(watch.save); last_save = time.time()
        except Exception: log.exception("watch tick failed")
        await asyncio.sleep(1)


def sound_loop(watch):
    """Beeps on this PC while an alarm sounds (Windows only)."""
    while True:
        if winsound and watch.sounding() and watch.settings["pc_sound"]:
            try: winsound.Beep(1800, 350); winsound.Beep(1200, 350)
            except RuntimeError: time.sleep(1)
            time.sleep(0.3)
        else: time.sleep(0.5)


def tile_bbox_3857(z, x, y):
    """Web Mercator bounding box 'minx,miny,maxx,maxy' (metres) of slippy-map tile z/x/y."""
    half = math.pi * 6378137; size = 2 * half / 2 ** z
    return f"{x * size - half:.2f},{half - (y + 1) * size:.2f},{(x + 1) * size - half:.2f},{half - y * size:.2f}"


class TileCache:
    """Fetches map tiles on demand from fixed sources and keeps them on disk.

    A cached tile is served as is for MAX_AGE; after that it is refetched, and if that fails (no internet) the old
    copy is served anyway. Tiles the source does not have (404, common for the seamark overlay) are remembered for
    MISSING_AGE so they are not asked for again on every pan.
    """
    SOURCES = {   # layer -> (URL template, file extension, max native zoom)
        "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", "png", 19),
        "seamark": ("https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png", "png", 18),
        "satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "jpg", 19),
        # OpenSeaMap crowd-sourced depth: sounding track points (red 0 m -> green >= 10 m) and contours. This is a WMS,
        # so each tile is asked for by its EPSG:3857 bounding box (as map.openseamap.org does).
        "depth": ("https://depth.openseamap.org/geoserver/openseamap/wms?service=WMS&version=1.1.0&request=GetMap&styles="
                  "&layers=openseamap:tracks_10m,openseamap:contour2,openseamap:contour&srs=EPSG:3857&bbox={bbox}"
                  "&width=256&height=256&format=image/png&transparent=true", "png", 18),
    }
    MAX_AGE, MISSING_AGE = 30 * 86400, 7 * 86400
    USER_AGENT = "OrcaCore2-AnchorWatch/1.0 (personal anchor alarm, tiles cached locally)"

    def __init__(self, folder):
        self.folder = Path(folder); self.upstream = threading.Semaphore(2)   # OSM policy: at most 2 connections
        self.offline_until = 0                                                  # skip fetching for a while after a network failure

    def get(self, layer, z, x, y):
        """Return (bytes, content type), or None when the tile is not available."""
        url, ext, maxz = self.SOURCES[layer]
        if not (0 <= z <= maxz and 0 <= x < 2 ** z and 0 <= y < 2 ** z): return None
        f = self.folder / layer / str(z) / str(x) / f"{y}.{ext}"; missing = f.with_suffix(".missing")
        ctype = "image/png" if ext == "png" else "image/jpeg"
        now = time.time()
        if f.exists() and now - f.stat().st_mtime < self.MAX_AGE: return f.read_bytes(), ctype
        if missing.exists() and now - missing.stat().st_mtime < self.MISSING_AGE: return None
        if now >= self.offline_until:
            req = urllib.request.Request(url.format(z=z, x=x, y=y, bbox=tile_bbox_3857(z, x, y)), headers={"User-Agent": self.USER_AGENT})
            try:
                with self.upstream, urllib.request.urlopen(req, timeout=8) as r: data = r.read()
                if data:
                    f.parent.mkdir(parents=True, exist_ok=True); tmp = f.with_suffix(".tmp"); tmp.write_bytes(data); os.replace(tmp, f)
                    missing.unlink(missing_ok=True); return data, ctype
            except urllib.error.HTTPError as e:
                if e.code == 404: missing.parent.mkdir(parents=True, exist_ok=True); missing.touch()
                else: log.warning("tile %s/%d/%d/%d: HTTP %s", layer, z, x, y, e.code)
            except Exception as e:
                self.offline_until = now + 30; log.info("map tiles offline (%s); using cached tiles only for 30 s", type(e).__name__)
        return (f.read_bytes(), ctype) if f.exists() else None


TILE_RE = re.compile(r"^/tiles/([a-z]+)/(\d{1,2})/(\d{1,7})/(\d{1,7})\.(?:png|jpg)$")
STATIC = {"/vendor/leaflet/leaflet.js": "application/javascript", "/vendor/leaflet/leaflet.css": "text/css",
          "/vendor/leaflet/images/layers.png": "image/png", "/vendor/leaflet/images/layers-2x.png": "image/png",
          "/icons/logo.svg": "image/svg+xml", "/icons/apple-touch-icon.png": "image/png",
          "/icons/icon-192.png": "image/png", "/icons/icon-512.png": "image/png",
          "/manifest.webmanifest": "application/manifest+json"}


def make_handler(watch, tiles):
    page_file = HERE / "static" / "anchor.html"

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype="application/json", code=200, cache="no-store"):
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            try: self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError): pass   # browser cancelled (tiles while zooming)

        def _json(self, obj, code=200): self._send(json.dumps(obj, default=lambda o: None).encode(), code=code)

        def do_GET(self):
            path, _, q = self.path.partition("?")
            args = dict(p.split("=", 1) for p in q.split("&") if "=" in p)
            if path == "/": self._send(page_file.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state": self._json(watch.state())
            elif path == "/api/track":
                try: self._json(watch.track_since(float(args.get("since", 0)), int(args.get("gen", -1))))
                except ValueError: self._json({"error": "bad query"}, 400)
            elif path in STATIC: self._send((HERE / "static" / path.lstrip("/")).read_bytes(), STATIC[path], cache="max-age=86400")
            elif path == "/apple-touch-icon.png": self._send((HERE / "static/icons/apple-touch-icon.png").read_bytes(), "image/png", cache="max-age=86400")
            elif (m := TILE_RE.match(path)) and m[1] in TileCache.SOURCES:
                t = tiles.get(m[1], int(m[2]), int(m[3]), int(m[4]))
                if t: self._send(t[0], t[1], cache="max-age=86400")
                else: self._send(b"", "text/plain", 404, cache="max-age=300")
            else: self._send(b"not found", "text/plain", 404)

        def do_POST(self):
            if self.path != "/api/command": return self._send(b"not found", "text/plain", 404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 10000: raise ValueError("request too large")
                watch.command(json.loads(self.rfile.read(n) or b"{}")); self._json({"ok": True, "state": watch.state()})
            except ValueError as e: self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e: log.exception("command failed"); self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

        timeout = 30                                   # a client that stalls (e.g. mid TLS handshake) only ties up its own thread

        def log_message(self, *a): pass
    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Phones dropping connections and clients that do not trust the certificate are normal, not crashes
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)): return
        super().handle_error(request, client_address)


def check_bind(addr):
    """Only loopback or a private (RFC 1918) LAN address: the watch must never listen on a public interface."""
    ip = ipaddress.ip_address(addr)
    if not (ip.is_loopback or (ip.is_private and not ip.is_unspecified and not ip.is_link_local)):
        raise SystemExit(f"--bind {addr}: only localhost or a private LAN address (10.x, 172.16-31.x, 192.168.x) is allowed")


def main():
    ap = argparse.ArgumentParser(description="Anchor watch for the Orca Core 2 (read-only on the Core)")
    ap.add_argument("--host", default="192.168.1.124", help="Orca Core address (default 192.168.1.124)")
    ap.add_argument("--bind", default="127.0.0.1", help="address for the web page: localhost or this PC's private LAN address "
                                                         "(start_anchor_watch.cmd passes the LAN address)")
    ap.add_argument("--port", type=int, default=8766, help="port for the web page (default 8766)")
    ap.add_argument("--state", default=str(HERE / "anchor_state.json"), help="file that keeps the anchor, settings and track")
    ap.add_argument("--no-ais", action="store_true", help="do not open the AIS stream")
    ap.add_argument("--tile-cache", default=str(HERE / "tile_cache"), help="folder for cached map tiles")
    ap.add_argument("--certfile", help="HTTPS certificate (certs/server-cert.pem from setup_local_https.ps1)")
    ap.add_argument("--keyfile", help="HTTPS private key (certs/server-key.pem)")
    a = ap.parse_args()
    check_bind(a.bind)
    if bool(a.certfile) != bool(a.keyfile): ap.error("--certfile and --keyfile go together")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    store = Store(); watch = AnchorWatch(store, a.state)
    server = Server((a.bind, a.port), make_handler(watch, TileCache(a.tile_cache)))
    scheme = "http"
    if a.certfile:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(a.certfile, a.keyfile)
        # handshake in the request thread (first read), not in accept(), so one slow client cannot block the others
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False); scheme = "https"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=sound_loop, args=(watch,), daemon=True).start()
    log.info("Anchor Watch %s: %s://%s:%d/  (Core %s)", VERSION, scheme, a.bind, a.port, a.host)

    async def run():
        tasks = [stream_loop(store, "sensors", f"ws://{a.host}:8089" + SENSORS_PATH.format(ms=1000), None), watch_loop(watch)]
        if not a.no_ais: tasks.append(stream_loop(store, "ais", f"ws://{a.host}:8089" + AIS_PATH, None))
        await asyncio.gather(*tasks)
    try: asyncio.run(run())
    except KeyboardInterrupt: pass
    finally:
        server.shutdown(); watch.save()


if __name__ == "__main__":
    main()
