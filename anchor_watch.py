"""Anchor watch for the Orca Core 2.

Reads the Core's live sensor and AIS streams (read-only, same streams as orca_reader.py), keeps an anchor
position and swing radius, and raises alarms when the boat drags, GPS is lost, the Core stops talking, or
optional depth/wind limits are crossed. The watch runs here, not in the browser: alarms are evaluated and
(on Windows) sounded on this PC even when no page is open. The page at http://<this PC>:8766/ shows the
swing circle, track and nearby AIS boats and works from a phone on the boat Wi-Fi.

Position source: the GNSS sources are used in the order of the `gps_sources` setting, with failover. The Core's
own GPS (source 254) can be chosen too: with the rest of the NMEA 2000 network switched off it is the only one
left. It is not always trustworthy while the boat lies still: on 2026-10-12, with the other receivers on, 254 stayed
at one exact value for minutes, while on 2026-10-16 (Core alone) it kept moving although the Core flagged the boat
as stationary both times. A frozen position would hide a slow drag, so every source is checked directly: a position
that has not changed at all for FROZEN_S is treated as frozen and skipped (real receivers always jitter in the last
digit), and with no usable source left the GPS alarm goes off.

State (anchor, settings, track, events) is kept in anchor_state.json so a restart does not lose the watch.

Map tiles (OpenStreetMap, OpenSeaMap seamarks, Esri satellite) are fetched by this PC and kept in tile_cache/, so
areas viewed while online still show offline. Only tiles a page asks for are fetched; nothing is bulk-downloaded.
"""

import argparse, asyncio, ipaddress, json, logging, math, os, queue, re, ssl, sys, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

from orca_reader import AIS_PATH, CORE_SOURCE, KN, RAD, SENSORS_PATH, Store, devices_loop, stream_loop

try: import winsound
except ImportError: winsound = None

VERSION = "1.3.0"
HERE = Path(__file__).resolve().parent
log = logging.getLogger("anchor")
EARTH = 6371008.8
FRESH_S = 10            # a position older than this is not used
FROZEN_S = 60           # a position that has not changed at all for this long is frozen (see module doc)
DEFAULT_GPS = "4,127,3,254"
TRACK_EVERY_S = 5
AIS_RANGE_M = 600
AIS_MAX_AGE_S = 900     # an AIS position older than this is not shown (local or internet)

DEFAULTS = {
    "default_radius": 40.0,   # m, used when dropping without a radius
    "confirm_s": 10,          # boat must stay outside the circle this long before the drag alarm
    "gps_timeout_s": 30,      # no usable position for this long -> GPS alarm
    "core_timeout_s": 30,     # sensor stream down for this long -> Core alarm
    "min_depth": 0.0,         # m below transducer, 0 = off
    "max_wind_kn": 0.0,       # true wind speed, 0 = off
    "bow_offset": 0.0,        # m from GPS antenna forward to the bow roller
    "gps_sources": DEFAULT_GPS,  # GNSS sources in order of preference; 254 = the Orca Core's own GPS
    "own_mmsi": "",           # "My MMSI": this boat's AIS, never shown as a target; empty = hide any target within 10 m of our GPS
    "pc_sound": True,         # beep on this PC while an alarm sounds
    "push_alerts": True,      # send alarms to the phone through Pushover (needs pushover.json)
    "alive_minutes": 60.0,    # still-alive check: alert from outside after this long without a sign of life, 0 = off
    "internet_ais": True,     # add AIS targets from aisstream.io (needs aisstream.json); the local receiver always wins
    "track_hours": 24,
}
SETTING_LIMITS = {"default_radius": (5, 2000), "confirm_s": (0, 600), "gps_timeout_s": (5, 600), "core_timeout_s": (5, 600),
                  "min_depth": (0, 500), "max_wind_kn": (0, 150), "bow_offset": (-50, 50), "track_hours": (1, 72),
                  "alive_minutes": (0, 1440)}

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


def position_sources(sensors, now):
    """Every source with a fixed, fresh-or-not position: src -> {lat, lon, age, sats, hdop}."""
    def val(k):
        v = sensors.get(k); return (v[0], now - v[1]) if v and num(v[0]) else (None, None)
    out = {}
    for k in sensors:
        if k.startswith("navigation.position.") and k.endswith(".latitude"):
            src = k.split(".")[2]
            lat, a1 = val(k); lon, a2 = val(f"navigation.position.{src}.longitude")
            fix, _ = val(f"navigation.gnss.{src}.method")
            if lat is None or lon is None or fix == 0 or (lat == 0 and lon == 0): continue
            out[src] = {"lat": lat, "lon": lon, "age": max(a1, a2), "source": src,
                        "sats": val(f"navigation.gnss.{src}.satellites")[0], "hdop": val(f"navigation.gnss.{src}.HDOP")[0]}
    return out


def read_boat(sensors, now, sources_pref, frozen=frozenset()):
    """Pick the position and the other values the watch needs from the Store's sensor map (key -> (value, t)).
    Sources in `frozen` (position unchanged for FROZEN_S) are skipped."""
    def val(k):
        v = sensors.get(k); return (v[0], now - v[1]) if v and num(v[0]) else (None, None)

    cands = {s: c for s, c in position_sources(sensors, now).items() if s not in frozen}
    order = list(sources_pref) + sorted((s for s in cands if s not in sources_pref), key=lambda s: cands[s]["age"])
    pos = next((dict(cands[s]) for s in order if s in cands and cands[s]["age"] <= FRESH_S), None)

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


def read_ais(ais, now, lat, lon, own_mmsi="", near_us=None, internet=None):
    """AIS targets within AIS_RANGE_M, from the Core's own (local) AIS and, optionally, internet AIS.

    My MMSI is dropped before anything else, so it can never be shown. The local receiver always has priority: an
    internet report is only used for a ship the local receiver has no current position for (none, or older than
    AIS_MAX_AGE_S). `internet` is a list of InternetAIS records (degrees, knots, metres, unix time "t").
    With no MMSI set, targets within 10 m of our GPS are taken to be ourselves; they are added to near_us (if given)
    so the page can offer to save one as My MMSI."""
    targets = {}
    for k, (v, t) in ais.items():
        parts = k.split(".")                           # ais.x.<mmsi>.position.<field>
        if len(parts) < 5 or (own_mmsi and parts[2] == own_mmsi): continue
        tg = targets.setdefault(parts[2], {"mmsi": parts[2], "age": 1e9}); tg[parts[-1]] = v; tg["age"] = min(tg["age"], now - t)
    recs = {}
    for tg in targets.values():
        if not (num(tg.get("latitude")) and num(tg.get("longitude"))): continue
        recs[tg["mmsi"]] = {"mmsi": tg["mmsi"], "name": str(tg.get("name") or "").strip(), "lat": tg["latitude"], "lon": tg["longitude"],
                            "length": tg.get("length") if num(tg.get("length")) else None,
                            "heading": round(tg["headingTrue"] * RAD % 360) if num(tg.get("headingTrue")) and tg["headingTrue"] < 6.3 else None,
                            "cog": round(tg["COG"] * RAD % 360) if num(tg.get("COG")) else None,
                            "sog": round(tg["SOG"] * KN, 1) if num(tg.get("SOG")) else None, "age": tg["age"], "src": "local"}
    for tg in internet or ():
        m = tg["mmsi"]
        if (own_mmsi and m == own_mmsi) or not (num(tg.get("lat")) and num(tg.get("lon"))): continue
        loc = recs.get(m)
        if loc and loc["age"] <= AIS_MAX_AGE_S: continue          # the local receiver always has priority
        recs[m] = {"mmsi": m, "name": tg.get("name") or (loc or {}).get("name", ""), "lat": tg["lat"], "lon": tg["lon"],
                   "length": tg.get("length") or (loc or {}).get("length"), "heading": tg.get("heading"), "cog": tg.get("cog"),
                   "sog": tg.get("sog"), "age": max(0.0, now - tg["t"]), "src": "internet"}
    out = []
    for r in recs.values():
        if lat is None: continue
        d, b = dist_bearing_m(lat, lon, r["lat"], r["lon"])
        if d > AIS_RANGE_M or r["age"] > AIS_MAX_AGE_S: continue
        if not own_mmsi and d < 10:
            if near_us is not None: near_us.append({"mmsi": r["mmsi"], "name": r["name"], "dist": round(d, 1)})
            continue
        out.append({**r, "dist": round(d, 1), "bearing": round(b), "age": round(r["age"])})
    return sorted(out, key=lambda x: x["dist"])


class InternetAIS:
    """AIS targets around the boat from aisstream.io (https://aisstream.io/documentation), as an addition to the
    Core's own AIS receiver, which always has priority (see read_ais).

    One WebSocket, subscribed to a box of BOX_DEG around the boat; it is re-opened with a new box when the boat has
    moved more than MOVE_M from the box centre. The API key comes from a local file that is never committed:
    {"api_key": "..."}. Only the key and the box are sent; nothing about the boat itself.
    """
    URL = "wss://stream.aisstream.io/v0/stream"
    BOX_DEG = 0.03                   # half size of the box (about 3 km north-south)
    MOVE_M = 1500
    TYPES = ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport", "ShipStaticData", "StaticDataReport"]
    POSITION = {"PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"}

    def __init__(self, cfg_file, center, enabled, on_event=None):
        self.cfg_file, self.center, self.enabled = Path(cfg_file), center, enabled      # center() -> (lat, lon) or None
        self.on_event = on_event or (lambda text, kind: None)
        self.lock = threading.Lock(); self.targets = {}                                  # mmsi -> record
        self.cfg = None; self.connected = False; self.last_message = None; self.last_error = None
        self.box_center = None; self.kick = False; self.logged_error = None
        self.reload()

    def reload(self):
        try: d = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        except FileNotFoundError: self.cfg = None; return f"{self.cfg_file.name} not found"
        except Exception as e: self.cfg = None; return f"{self.cfg_file.name}: {type(e).__name__}: {e}"
        key = str(d.get("api_key", "")).strip()
        if any(re.fullmatch(r"[A-Za-z0-9_-]{20,100}", str(k)) for k in d):
            self.cfg = None; return f"{self.cfg_file.name}: the key was pasted in place of the field name; keep \"api_key\" and put the key after the colon"
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", key):
            self.cfg = None; return f"{self.cfg_file.name}: api_key must be the API key from aisstream.io (letters and digits)"
        self.cfg = {"api_key": key}; self.kick = True; return None

    @property
    def configured(self): return self.cfg is not None

    def snapshot(self, now=None):
        now = now or time.time()
        with self.lock:
            for m in [m for m, t in self.targets.items() if now - t.get("t", 0) > AIS_MAX_AGE_S]: del self.targets[m]
            return [dict(t) for t in self.targets.values() if "lat" in t]

    @staticmethod
    def parse_time(text, now):
        try: t = datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError: return now
        return min(t, now)

    def handle(self, m, now=None):
        """Folds one aisstream.io message into the targets."""
        now = now or time.time()
        if m.get("error"): self.last_error = f"aisstream.io: {m['error']}"; return
        md = m.get("MetaData") or {}; mmsi = str(md.get("MMSI") or "")
        typ = m.get("MessageType"); body = (m.get("Message") or {}).get(typ) or {}
        if not (mmsi.isdigit() and len(mmsi) == 9): return
        with self.lock:
            tg = self.targets.setdefault(mmsi, {"mmsi": mmsi})
            name = str(md.get("ShipName") or "").strip()
            if name: tg["name"] = name
            if typ in self.POSITION:
                la, lo = body.get("Latitude", md.get("latitude")), body.get("Longitude", md.get("longitude"))
                if not (num(la) and num(lo) and -90 <= la <= 90 and -180 <= lo <= 180): return
                tg.update(lat=la, lon=lo, t=self.parse_time(md.get("time_utc"), now),
                          sog=round(body["Sog"], 1) if num(body.get("Sog")) and body["Sog"] < 102.3 else None,
                          cog=round(body["Cog"]) % 360 if num(body.get("Cog")) and body["Cog"] < 360 else None,
                          heading=round(body["TrueHeading"]) if num(body.get("TrueHeading")) and body["TrueHeading"] < 360 else None)
            else:
                static = body.get("ReportB") or body if typ == "StaticDataReport" else body
                if typ == "StaticDataReport" and (body.get("ReportA") or {}).get("Name"): tg["name"] = str(body["ReportA"]["Name"]).strip()
                elif str(static.get("Name") or "").strip(): tg["name"] = str(static["Name"]).strip()
                dim = static.get("Dimension") or {}
                length = (dim.get("A") or 0) + (dim.get("B") or 0)
                if num(length) and 0 < length < 500: tg["length"] = length
        self.last_message = now

    def box(self, lat, lon):
        return [[round(lat - self.BOX_DEG, 4), round(lon - self.BOX_DEG * 1.5, 4)], [round(lat + self.BOX_DEG, 4), round(lon + self.BOX_DEG * 1.5, 4)]]

    async def run(self):
        delay = 5
        while True:
            c = self.center()
            if not (self.configured and self.enabled() and c):
                self.connected = False; await asyncio.sleep(5)
                if not self.configured: self.reload()                  # pick up a key file added later
                continue
            self.kick = False; got = False
            try:
                async with websockets.connect(self.URL, open_timeout=15, ping_interval=30, ping_timeout=30, max_size=2**20) as ws:
                    await ws.send(json.dumps({"APIKey": self.cfg["api_key"], "BoundingBoxes": [self.box(*c)], "FilterMessageTypes": self.TYPES}))
                    self.box_center = c; self.connected = True
                    while not self.kick and self.enabled():
                        try: raw = await asyncio.wait_for(ws.recv(), 30)
                        except asyncio.TimeoutError: raw = None
                        if raw is not None:
                            self.handle(json.loads(raw))
                            if self.last_error and not got: break      # refused (bad key): do not hammer the service
                            got = True; self.last_error = None; delay = 5
                        now_c = self.center()
                        if now_c and dist_bearing_m(*self.box_center, *now_c)[0] > self.MOVE_M: break   # new box around the boat
            except asyncio.CancelledError: raise
            except websockets.ConnectionClosed as e:
                # aisstream.io drops the connection straight after a subscription it does not accept (bad API key)
                self.last_error = ("aisstream.io closed the connection right after subscribing: API key not accepted?"
                                   if self.connected and not got else f"connection closed: {e}")
            except Exception as e: self.last_error = f"{type(e).__name__}: {e}"
            refused = bool(self.last_error) and self.last_error.startswith("aisstream.io")
            if self.last_error and self.last_error != self.logged_error:
                self.on_event(f"Internet AIS: {self.last_error}", "warn"); self.logged_error = self.last_error
            elif not self.last_error and self.logged_error:
                self.on_event("Internet AIS: receiving again", "ok"); self.logged_error = None
            self.connected = False
            await asyncio.sleep(300 if refused else delay)
            delay = 5 if got else min(delay * 2, 120)


# ---------------------------------------------------------------- push alerts (Pushover)


class Pushover:
    """Sends alarms to the phone through Pushover (https://pushover.net/api).

    Keys come from a local JSON file that is never committed: {"app_token": "...", "user_key": "..."}.
    Messages go out from one worker thread, in order, with retries, so a slow or absent internet link never delays the
    alarm checks. An emergency message (priority 2) repeats on the phone until it is acknowledged; its receipt is kept
    so it can be cancelled when the alarm clears, is silenced in the app, or the anchor is raised.
    """
    API = "https://api.pushover.net/1"
    PRIORITY = {"drag": 2, "gps": 1, "core": 1, "depth": 1, "wind": 1}   # 2 = emergency, 1 = high
    RETRY_S, EXPIRE_S = 60, 3600                                        # emergency: repeat every minute for an hour
    KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")
    BACKOFF_S = (5, 15, 45)

    def __init__(self, cfg_file, page_url=None, on_event=None, start=True):
        self.cfg_file, self.page_url = Path(cfg_file), page_url
        self.on_event = on_event or (lambda text, kind: None)
        self.cfg, self.receipts, self.q = None, {}, queue.Queue()
        self.last_error = None; self.last_sent = None
        self.reload()
        if start: threading.Thread(target=self._worker, daemon=True).start()

    def reload(self):
        """(Re)reads the key file; returns an error text, or None when it is usable."""
        try: d = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        except FileNotFoundError: self.cfg = None; return f"{self.cfg_file.name} not found"
        except Exception as e: self.cfg = None; return f"{self.cfg_file.name}: {type(e).__name__}: {e}"
        tok, usr = str(d.get("app_token", "")).strip(), str(d.get("user_key", "")).strip()
        if any(self.KEY_RE.match(str(k)) for k in d):          # a key pasted over a field name instead of after it
            self.cfg = None
            return (f"{self.cfg_file.name}: a key was pasted in place of a field name; keep the names \"app_token\" and "
                    f"\"user_key\" and put each key after the colon, between quotes")
        if not (self.KEY_RE.match(tok) and self.KEY_RE.match(usr)):
            self.cfg = None; return f"{self.cfg_file.name}: app_token and user_key must each be the 30-character Pushover keys"
        self.cfg = {**d, "app_token": tok, "user_key": usr}; return None

    @property
    def configured(self): return self.cfg is not None

    def alarm(self, key, title, message, when):
        self.q.put(("send", key, {"title": title, "message": message, "priority": self.PRIORITY.get(key, 1), "timestamp": int(when)}))

    def cleared(self, key, title, message, when):
        self.q.put(("cancel", key, None))
        self.q.put(("send", None, {"title": title, "message": message, "priority": 0, "timestamp": int(when)}))

    def cancel(self, key=None):
        """Stops the repeating emergency for one alarm, or (no key) for all of them."""
        self.q.put(("cancel", key or "*", None))

    def test(self):
        self.q.put(("send", None, {"title": "Anchor Watch test", "priority": 0, "timestamp": int(time.time()),
                                   "message": "Push alerts work. Anchor drag alarms arrive as emergency alerts that repeat until acknowledged."}))

    def _post(self, path, fields):
        req = urllib.request.Request(f"{self.API}/{path}", data=urllib.parse.urlencode(fields).encode(), method="POST",
                                     headers={"User-Agent": f"OrcaCore2-AnchorWatch/{VERSION}"})
        with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read() or b"{}")

    def _do(self, op, key, msg):
        if op == "cancel":
            for k in (list(self.receipts) if key == "*" else [key]):
                receipt = self.receipts.get(k)
                if receipt: self._post(f"receipts/{receipt}/cancel.json", {"token": self.cfg["app_token"]})
                self.receipts.pop(k, None)
            return
        fields = {"token": self.cfg["app_token"], "user": self.cfg["user_key"], **msg}
        if self.cfg.get("device"): fields["device"] = self.cfg["device"]
        if self.page_url: fields.update(url=self.page_url, url_title="Open Anchor Watch")
        if msg["priority"] == 2:
            fields.update(retry=self.RETRY_S, expire=self.EXPIRE_S, sound=self.cfg.get("emergency_sound", "persistent"))
        elif self.cfg.get("sound"): fields["sound"] = self.cfg["sound"]
        r = self._post("messages.json", fields)
        if key and r.get("receipt"): self.receipts[key] = r["receipt"]
        self.last_sent = time.time()

    def process(self, item):
        """Runs one queued item with retries (called by the worker thread; tests call it directly)."""
        op, key, msg = item
        if not self.configured: return
        for attempt in range(len(self.BACKOFF_S) + 1):
            try: self._do(op, key, msg); self.last_error = None; return
            except urllib.error.HTTPError as e:
                try: detail = "; ".join(json.loads(e.read()).get("errors", [])) or e.reason
                except Exception: detail = e.reason
                self.last_error = f"Pushover refused: HTTP {e.code} {detail}"
                if e.code < 500: break                         # bad key or request: retrying will not help
            except Exception as e: self.last_error = f"{type(e).__name__}: {e}"
            if attempt < len(self.BACKOFF_S): time.sleep(self.BACKOFF_S[attempt])
        if op == "send": self.on_event(f"Push alert not delivered: {self.last_error}", "warn")

    def _worker(self):
        while True: self.process(self.q.get())


# ---------------------------------------------------------------- still-alive check (dead man's switch)


class StillAlive:
    """Pings an outside monitor (healthchecks.io, or any ping URL) so silence can be noticed from outside the boat.

    If the PC, this program or the boat's internet stops, the pings stop; after the still-alive interval the monitor
    alerts the phone (healthchecks.io's own Pushover integration). The PC pings every PING_EVERY_S (more often for
    short intervals), so one lost ping over a flaky link does not raise a false alarm.

    Config, a local file that is never committed: {"ping_url": "https://hc-ping.com/<uuid>", "api_key": "<optional>"}.
    With a read-write API key for a healthchecks.io ping URL, the check's period follows the setting (period =
    interval, grace = 1 min) and is paused when the interval is 0; without one, set the period on the website.
    """
    PING_EVERY_S = 300
    GRACE_S = 60
    HC_PING = re.compile(r"^https://hc-ping\.com/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$")
    HC_API = "https://healthchecks.io/api/v3/checks"

    def __init__(self, cfg_file, interval_minutes, on_event=None, start=True):
        self.cfg_file, self.interval = Path(cfg_file), interval_minutes        # interval: callable -> minutes (0 = off)
        self.on_event = on_event or (lambda text, kind: None)
        self.cfg = None; self.last_ok = self.last_error = None; self.synced = None; self.sync_note = None
        self.next_ping = 0.0; self.next_sync_try = 0.0; self.failing = False
        self.wake = threading.Event()
        self.reload()
        if start: threading.Thread(target=self._worker, daemon=True).start()

    def reload(self):
        """(Re)reads the config file; returns an error text, or None when it is usable."""
        try: d = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        except FileNotFoundError: self.cfg = None; return f"{self.cfg_file.name} not found"
        except Exception as e: self.cfg = None; return f"{self.cfg_file.name}: {type(e).__name__}: {e}"
        url = str(d.get("ping_url", "")).strip(); u = urllib.parse.urlparse(url)
        if u.scheme != "https" or not u.netloc: self.cfg = None; return f"{self.cfg_file.name}: ping_url must be an https:// URL"
        self.cfg = {"ping_url": url, "api_key": str(d.get("api_key") or "").strip()}
        self.synced = None; self.next_sync_try = 0.0
        return None

    @property
    def configured(self): return self.cfg is not None

    def poke(self, ping_now=False):
        """Setting or config changed: re-sync the period (and optionally ping straight away)."""
        self.synced = None; self.next_sync_try = 0.0
        if ping_now: self.next_ping = 0.0
        self.wake.set()

    def ping_every(self, minutes): return max(30.0, min(self.PING_EVERY_S, minutes * 60 / 3))

    def _request(self, url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"User-Agent": f"OrcaCore2-AnchorWatch/{VERSION}", **(headers or {})})
        with urllib.request.urlopen(req, timeout=15) as r: return r.status, r.read()

    def _sync(self, minutes):
        m = self.HC_PING.match(self.cfg["ping_url"])
        if not (self.cfg["api_key"] and m):
            self.synced = minutes
            self.sync_note = (f"set the check's period to {minutes:g} min and grace to 1 min on the monitor's website"
                              if minutes else "pause the check on the monitor's website")
            return
        hdr = {"X-Api-Key": self.cfg["api_key"], "Content-Type": "application/json"}
        if minutes: self._request(f"{self.HC_API}/{m[1]}", json.dumps({"timeout": int(minutes * 60), "grace": self.GRACE_S}).encode(), hdr)
        else: self._request(f"{self.HC_API}/{m[1]}/pause", b"", hdr)
        self.synced = minutes; self.sync_note = None
        self.on_event(f"Still-alive check on healthchecks.io " + (f"set to {minutes:g} min" if minutes else "paused"), "info")

    def step(self, now=None):
        """One round of the worker: sync the period if needed, ping when due. Tests call it directly."""
        now = now or time.time()
        if not self.configured: return
        minutes = float(self.interval() or 0)
        if self.synced != minutes and now >= self.next_sync_try:
            try: self._sync(minutes)
            except Exception as e:
                self.sync_note = f"could not update the check: {type(e).__name__}: {e}"; self.next_sync_try = now + 300
        if minutes <= 0 or now < self.next_ping: return
        self.next_ping = now + self.ping_every(minutes)
        try:
            self._request(self.cfg["ping_url"])
            self.last_ok = now; self.last_error = None
            if self.failing: self.failing = False; self.on_event("Still-alive pings get through again", "ok")
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            if not self.failing: self.failing = True; self.on_event(f"Still-alive ping failed (will retry): {self.last_error}", "warn")

    def _worker(self):
        while True:
            try: self.step()
            except Exception: log.exception("still-alive step failed")
            self.wake.wait(timeout=5); self.wake.clear()


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
        self.pos_seen = {}                 # source -> (lat, lon, time the value last changed)
        self.frozen = set()                # sources whose position stopped changing (FROZEN_S)
        self.gps_list = []                 # every position source, for the page
        self.core_down_since = None
        self.started = time.time()
        self.dirty = False
        self.notifier = None               # Pushover, set by main() (tests use a fake)
        self.alive = None                  # StillAlive, set by main()
        self.netais = None                 # InternetAIS, set by main()
        self.load()

    # ---- persistence
    def load(self):
        try: d = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError: return
        except Exception as e: log.warning("could not read %s: %s", self.state_file, e); return
        self.settings.update({k: v for k, v in d.get("settings", {}).items() if k in DEFAULTS})
        if self.settings["gps_sources"] == "4,127,3": self.settings["gps_sources"] = DEFAULT_GPS   # old default (< 1.2.0)
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
            srcs = position_sources(sensors, now)
            for src, c in srcs.items():
                seen = self.pos_seen.get(src)
                if not seen or (seen[0], seen[1]) != (c["lat"], c["lon"]): self.pos_seen[src] = (c["lat"], c["lon"], now)
            frozen = {src for src, (_, _, t) in self.pos_seen.items()
                      if src in srcs and srcs[src]["age"] <= FRESH_S and now - t >= FROZEN_S}
            for src in frozen - self.frozen:
                self.event(f"GPS {self.source_name(src)} has not moved at all for {FROZEN_S} s: treated as frozen, not used", now, "warn")
            for src in self.frozen - frozen:
                if src in srcs: self.event(f"GPS {self.source_name(src)} is updating again", now, "ok")
            self.frozen = frozen
            boat = read_boat(sensors, now, prefs, frozen); self.boat = boat; pos = boat["pos"]
            self.gps_list = [{"source": src, "name": self.source_name(src), "sats": c["sats"], "hdop": c["hdop"],
                              "age": round(c["age"], 1), "frozen": src in frozen, "in_use": bool(pos and pos["source"] == src)}
                             for src, c in sorted(srcs.items(), key=lambda kv: (kv[0] != CORE_SOURCE, int(kv[0])))]
            self.core_down_since = None if connected else (self.core_down_since or now)
            if pos:
                self.last_pos_t = now
                if pos["source"] != self.last_source:
                    if self.last_source is not None: self.event(f"Position source now {self.source_name(pos['source'])}", now, "warn")
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
                        self._push_alarm(aid, detail, now)
                    if aid in self.alarms: self.alarms[aid]["detail"] = detail
                else:
                    self.pending.pop(aid, None)
                    if aid in self.alarms:
                        del self.alarms[aid]; self.event(f"Cleared: {self.ALARM_TEXT[aid]}", now, "ok")
                        self._push_cleared(aid, now)
            for aid in [x for x in self.alarms if x not in cond]:                          # condition switched off
                del self.alarms[aid]; self._push_cancel(aid)
            for aid in [x for x in self.pending if x not in cond]: del self.pending[aid]
            if not self.alarms: self.silenced_until = 0

    def _push_ok(self): return self.notifier is not None and self.notifier.configured and self.settings["push_alerts"]

    def _push_alarm(self, aid, detail, now):
        if not self._push_ok(): return
        b = self.boat
        extra = [x for x in (f"Depth {b['depth']:.1f} m" if b.get("depth") is not None else "",
                             f"wind {b['tws']:.0f} kn" if b.get("tws") is not None else "") if x]
        msg = (detail or self.ALARM_TEXT[aid]) + "." + (" " + ", ".join(extra) + "." if extra else "")
        self.notifier.alarm(aid, f"\u2693 {self.ALARM_TEXT[aid]}", msg, now)

    def _push_cleared(self, aid, now):
        if self._push_ok(): self.notifier.cleared(aid, f"Cleared: {self.ALARM_TEXT[aid]}", "The alarm condition has ended.", now)

    def _push_cancel(self, aid=None):
        if self.notifier is not None: self.notifier.cancel(aid)

    def source_name(self, src):
        if src == CORE_SOURCE: return "254 (Orca Core 2)"
        label = self.store.source_label(src)
        return f"{src} ({label})" if label and not label.startswith("Source ") else str(src)

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
                self._push_cancel(); self.alarms.clear(); self.pending.clear()
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
                self._push_cancel(); self.anchor = None; self.alarms.clear(); self.pending.clear(); self.silenced_until = 0
            elif act == "silence":
                mins = _num(c, "minutes", 0.5, 60)
                if self.alarms:
                    self.silenced_until = now + mins * 60; self.event(f"Alarm silenced for {mins:g} min", now)
                    self._push_cancel()                      # someone is on it: stop the repeating phone alert
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
                if self.alive is not None and "alive_minutes" in (c.get("settings") or {}): self.alive.poke()
            elif act == "push_test":
                if self.notifier is None: raise ValueError("Push alerts are not available")
                err = self.notifier.reload()
                if err: raise ValueError(f"Pushover is not set up: {err}")
                self.notifier.test(); self.event("Test push sent to Pushover", now)
            elif act == "alive_check":
                if self.alive is None: raise ValueError("The still-alive check is not available")
                err = self.alive.reload()
                if err: raise ValueError(f"Still-alive check is not set up: {err}")
                self.alive.poke(ping_now=True); self.event("Still-alive ping requested", now)
            elif act == "net_ais_check":
                if self.netais is None: raise ValueError("Internet AIS is not available")
                err = self.netais.reload()
                if err: raise ValueError(f"Internet AIS is not set up: {err}")
                self.event("Internet AIS: key re-read, reconnecting", now)
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
            out = {"time": now, "core": core, "ais_stream": ais_stream, "boat": b, "gps_list": self.gps_list, "gps_default": DEFAULT_GPS, "anchor": a, "settings": self.settings,
                   "alarms": [{"id": k, "text": self.ALARM_TEXT[k], "detail": v.get("detail", ""), "since": v["since"]} for k, v in self.alarms.items()],
                   "pending": {k: now - t for k, t in self.pending.items() if k not in self.alarms},
                   "sounding": self.sounding(now), "silenced_until": self.silenced_until if self.alarms else 0,
                   "max_dist": self.max_dist, "events": list(self.events)[-40:], "track_gen": self.track_gen,
                   "push": {"configured": bool(self.notifier and self.notifier.configured),
                            "last_error": self.notifier.last_error if self.notifier else None,
                            "last_sent": self.notifier.last_sent if self.notifier else None},
                   "alive": {"configured": bool(self.alive and self.alive.configured),
                             "last_ok": self.alive.last_ok if self.alive else None,
                             "last_error": self.alive.last_error if self.alive else None,
                             "note": self.alive.sync_note if self.alive else None,
                             "every_s": self.alive.ping_every(self.settings["alive_minutes"]) if self.alive else None},
                   "track_points": len(self.track), "track_start": self.track[0][0] if self.track else None, "version": VERSION,
                   "gps_age": now - self.last_pos_t if self.last_pos_t else None}
            if a and pos:
                out["distance"], out["bearing"] = dist_bearing_m(a["lat"], a["lon"], pos["lat"], pos["lon"])
            ref = (pos or a or {})
        near_us = []
        net = self.netais.snapshot(now) if self.netais is not None and self.settings["internet_ais"] else None
        out["ais"] = read_ais(ais, now, ref.get("lat"), ref.get("lon"), str(self.settings["own_mmsi"]), near_us, net)
        n = self.netais
        out["net_ais"] = {"configured": bool(n and n.configured), "connected": bool(n and n.connected),
                          "last_message": n.last_message if n else None, "last_error": n.last_error if n else None,
                          "ships": len(net) if net is not None else 0}
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
    ap.add_argument("--pushover", default=str(HERE / "pushover.json"), help="Pushover keys file (see pushover.example.json)")
    ap.add_argument("--still-alive", default=str(HERE / "still_alive.json"), help="still-alive ping config (see still_alive.example.json)")
    ap.add_argument("--aisstream", default=str(HERE / "aisstream.json"), help="aisstream.io key for internet AIS (see aisstream.example.json)")
    a = ap.parse_args()
    check_bind(a.bind)
    if bool(a.certfile) != bool(a.keyfile): ap.error("--certfile and --keyfile go together")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    store = Store(); watch = AnchorWatch(store, a.state)
    page_url = f"{'https' if a.certfile else 'http'}://{a.bind}:{a.port}/"
    watch.notifier = Pushover(a.pushover, page_url, on_event=lambda text, kind: watch.event(text, kind=kind))
    log.info("Push alerts: %s", "Pushover ready" if watch.notifier.configured else f"off ({Path(a.pushover).name} missing or invalid)")
    watch.alive = StillAlive(a.still_alive, lambda: watch.settings["alive_minutes"], on_event=lambda text, kind: watch.event(text, kind=kind))
    log.info("Still-alive check: %s", "ready" if watch.alive.configured else f"off ({Path(a.still_alive).name} missing or invalid)")
    def boat_center():
        pos = watch.boat.get("pos") or watch.anchor
        return (pos["lat"], pos["lon"]) if pos else None
    watch.netais = InternetAIS(a.aisstream, boat_center, lambda: watch.settings["internet_ais"], on_event=lambda text, kind: watch.event(text, kind=kind))
    log.info("Internet AIS: %s", "aisstream.io ready" if watch.netais.configured else f"off ({Path(a.aisstream).name} missing or invalid)")
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
        tasks = [stream_loop(store, "sensors", f"ws://{a.host}:8089" + SENSORS_PATH.format(ms=1000), None), watch_loop(watch),
                 devices_loop(store, a.host), watch.netais.run()]
        if not a.no_ais: tasks.append(stream_loop(store, "ais", f"ws://{a.host}:8089" + AIS_PATH, None))
        await asyncio.gather(*tasks)
    try: asyncio.run(run())
    except KeyboardInterrupt: pass
    finally:
        server.shutdown(); watch.save()


if __name__ == "__main__":
    main()
