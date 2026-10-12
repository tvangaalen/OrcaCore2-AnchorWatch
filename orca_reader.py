"""Read-only live reader for an Orca Core 2 on the local network.

Connects to the same WebSocket streams the Orca iPhone app uses (found by capturing an app session,
firmware 1.21.0) and serves the live values on a local web page:

    ws://<core>:8089/v1/sensors/delta?interval=200&ns=^(?!.*(ais))&enableUnknownSources   all sensors except AIS
    ws://<core>:8089/v1/sensors/delta?interval=5000&ns=ais&enableUnknownSources           AIS targets
    http://<core>:8088/v1/devices                                                         NMEA 2000 device list

Each stream starts with one "full" message, then "delta" messages that carry only changed values.
Keys look like  <group>.<N2K source address>.<field>  and values are SI units (radians, m/s, metres).
Source 254 is the Core itself (its filtered/fused values).

This program only reads. It never sends a WebSocket message (the library only answers pings), and it
never calls the app's write endpoints (PUT /v1/sources, POST /imu/calibration, /v1/sync, port 8090).
"""

import argparse, asyncio, json, logging, math, re, threading, time, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
SENSORS_PATH = "/v1/sensors/delta?interval={ms}&ns=%5E(?!.*(ais))&enableUnknownSources"
AIS_PATH = "/v1/sensors/delta?interval=5000&ns=ais&enableUnknownSources"
CORE_SOURCE = "254"
log = logging.getLogger("orca")

# ---------------------------------------------------------------- units and labels

RAD = 180 / math.pi
KN = 3600 / 1852
ANGLE_FIELDS = {"course", "heading", "angle", "variation", "pitch", "roll", "yaw", "bearingFromOrigin",
                "bearingFromPosition", "bearingToNextWaypoint", "COG", "headingTrue"}
SPEED_FIELDS = {"speed", "SOG", "velocity"}
METRE_FIELDS = {"belowTransducer", "offset", "altitude", "xte", "length", "beam", "draft"}
ENUMS = {
    "wind.reference": {0: "True (ground, north)", 1: "Magnetic (ground)", 2: "Apparent", 3: "True (boat)", 4: "True (water)"},
    "heading.reference": {0: "True", 1: "Magnetic"},
    "cogsog.headingReference": {0: "True", 1: "Magnetic"},
    "gnss.method": {0: "No fix", 1: "GNSS fix", 2: "DGNSS fix", 3: "Precise GNSS", 4: "RTK fixed", 5: "RTK float",
                    6: "Estimated (DR)", 7: "Manual", 8: "Simulated"},
    "motion.stationary": {0: "No", 1: "Yes"},
    "historical.stationary": {0: "No", 1: "Yes"},
}
WIND_INSTANCE = {"0": "true N", "1": "magnetic", "2": "apparent", "3": "true boat", "4": "true water"}


def _date(days): return (datetime(1970, 1, 1) + timedelta(days=days)).strftime("%Y-%m-%d")
def _clock(sec): sec = int(sec) % 86400; return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def _latlon(v, pos, neg):
    h = pos if v >= 0 else neg; v = abs(v); d = int(v)
    return f"{d}° {(v - d) * 60:06.3f}' {h}"


def describe(group, field, v):
    """Return (display text, unit) for one raw value."""
    name = field.rsplit(".", 1)[-1]
    if not isinstance(v, (int, float)) or isinstance(v, bool): return str(v), ""
    enum = ENUMS.get(f"{group.rsplit('.', 1)[-1]}.{name}")
    if enum is not None: return enum.get(int(v), f"Unknown ({v})"), ""
    if name == "latitude": return _latlon(v, "N", "S"), ""
    if name == "longitude": return _latlon(v, "E", "W"), ""
    if name in ANGLE_FIELDS or (group == "steering.rudder" and name == "position"):
        d = v * RAD
        if name in ("heading", "course", "COG", "headingTrue") or name.startswith("bearing"): d %= 360
        return f"{d:.1f}", "°"
    if name == "rot": return f"{v * RAD * 60:.1f}", "°/min"
    if name in ("pitchRate", "rollRate"): return f"{v * RAD:.2f}", "°/s"
    if name in SPEED_FIELDS: return f"{v * KN:.2f}", "kn"
    if name == "distance": return f"{v / 1852:.3f}", "NM"
    if name in METRE_FIELDS: return f"{v:.2f}", "m"
    if name == "voltage": return f"{v:.2f}", "V"
    if name in ("daysSince1970", "etaDate", "ageOfService") and v >= 0: return _date(v), ""
    if name in ("secondsSinceMidnight", "etaTime") and v >= 0: return _clock(v), "UTC"
    return (f"{v:.6g}" if isinstance(v, float) else str(v)), ""


_ESCAPE = re.compile(r'\\(["\\/bfnrt]|u[0-9a-fA-F]{4})|\\')


def parse_message(raw):
    """json.loads, tolerating raw backslashes in text the Core passes through unescaped.

    Seen 2026-10-12: an AIS destination "IBIZA\\FORMENTERA" made every AIS message invalid JSON, so the whole AIS
    stream failed until that ship left. Valid escapes are kept; a lone backslash becomes a literal one.
    """
    try: return json.loads(raw)
    except json.JSONDecodeError as e:
        if "escape" not in e.msg: raise
        return json.loads(_ESCAPE.sub(lambda m: m[0] if m[1] else "\\\\", raw))


def split_key(key):
    """'environment.wind.254.2.angle' -> ('environment.wind', '254', '2.angle')."""
    parts = key.split(".")
    i = next((j for j, p in enumerate(parts) if p.isdigit()), None)
    if i is None: return key, "", ""
    return ".".join(parts[:i]), parts[i], ".".join(parts[i + 1:])


def distance_bearing(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    nm = 2 * 6371008.8 * math.asin(math.sqrt(a)) / 1852
    brg = math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360
    return nm, brg

# ---------------------------------------------------------------- live state


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {"sensors": {}, "ais": {}}          # stream -> key -> (value, unix time the Core last updated it)
        self.addr_to_name = {}                             # N2K source address -> 64-bit NAME (from the stream)
        self.name_info = {}                                # NAME -> device record (from /v1/devices)
        self.status = {s: {"connected": False, "messages": 0, "last_message": None, "reconnects": 0, "last_error": None}
                       for s in self.values}
        self.devices_error = None

    def apply(self, stream, msg):
        now = time.time(); vals = msg.get("values") or {}; ages = msg.get("values_age") or {}
        with self.lock:
            d = self.values[stream]
            if msg.get("event_type") == "full": d.clear()
            for k, v in vals.items(): d[k] = (v, now - ages.get(k, 0) / 1000)
            for k, a in ages.items():                      # age refresh for values that did not change
                if k not in vals and k in d: d[k] = (d[k][0], now - a / 1000)
            if msg.get("devices"): self.addr_to_name.update(msg["devices"])
            st = self.status[stream]; st["messages"] += 1; st["last_message"] = now

    def source_label(self, addr):
        if addr == CORE_SOURCE: return "Orca Core (computed)"
        info = self.name_info.get(self.addr_to_name.get(addr, ""))
        if not info: return f"Source {addr}" if addr else ""
        return " ".join(x for x in (str(info.get("manufacturer", "")).strip(), str(info.get("modelID", "")).strip()) if x)

    def flat(self):
        with self.lock: return {s: {k: v for k, (v, _) in d.items()} for s, d in self.values.items()}

    def snapshot(self):
        now = time.time()
        with self.lock:
            sensors = dict(self.values["sensors"]); ais = dict(self.values["ais"])
            status = json.loads(json.dumps(self.status)); devices_error = self.devices_error
            labels = {a: self.source_label(a) for a in {split_key(k)[1] for k in sensors}}
            devices = [{"source": i.get("source"), "manufacturer": i.get("manufacturer"), "model": str(i.get("modelID", "")).strip(),
                        "serial": str(i.get("modelSerialCode", "")).strip(), "description": i.get("deviceDescription", "")}
                       for i in self.name_info.values()]
        rows = []
        for k, (v, t) in sensors.items():
            g, src, field = split_key(k); text, unit = describe(g, field, v)
            if g == "environment.wind" and "." in field:
                inst = field.split(".")[0]; field = f"{field} ({WIND_INSTANCE.get(inst, inst)})"
            rows.append({"key": k, "group": g, "source": src, "source_label": labels.get(src, ""), "field": field,
                         "raw": v, "text": text, "unit": unit, "age_s": round(now - t, 1)})
        rows.sort(key=lambda r: (r["group"], r["source"] != CORE_SOURCE, int(r["source"] or 0), r["field"]))
        own = (sensors.get("navigation.position.254.latitude", (None,))[0], sensors.get("navigation.position.254.longitude", (None,))[0])
        targets = {}
        for k, (v, t) in ais.items():
            parts = k.split(".")                           # ais.x.<mmsi>.position.<field>
            if len(parts) < 5: continue
            tgt = targets.setdefault(parts[2], {"mmsi": parts[2], "age_s": 1e9}); tgt[parts[-1]] = v
            tgt["age_s"] = min(tgt["age_s"], round(now - t, 1))
        for tgt in targets.values():
            if None not in own and isinstance(tgt.get("latitude"), (int, float)) and isinstance(tgt.get("longitude"), (int, float)):
                tgt["range_nm"], tgt["bearing"] = (round(x, 2) for x in distance_bearing(*own, tgt["latitude"], tgt["longitude"]))
            for f in ("SOG",): tgt[f] = round(tgt[f] * KN, 1) if isinstance(tgt.get(f), (int, float)) else tgt.get(f)
            for f in ("COG", "headingTrue"): tgt[f] = round(tgt[f] * RAD % 360, 0) if isinstance(tgt.get(f), (int, float)) else tgt.get(f)
        return {"time": now, "status": status, "devices": sorted(devices, key=lambda d: d["source"] if isinstance(d["source"], int) else 999),
                "devices_error": devices_error, "rows": rows,
                "ais": sorted(targets.values(), key=lambda t: t.get("range_nm", 1e9))}

# ---------------------------------------------------------------- Core connections


class Recorder:
    """Appends every raw message to recordings/<date>_<stream>.jsonl."""
    def __init__(self, folder): self.folder = Path(folder); self.folder.mkdir(parents=True, exist_ok=True)
    def write(self, stream, msg):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with open(self.folder / f"{day}_{stream}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"received": datetime.now(timezone.utc).isoformat(), "msg": msg}, separators=(",", ":")) + "\n")


async def stream_loop(store, stream, uri, recorder):
    delay = 2
    while True:
        st = store.status[stream]
        try:
            async with websockets.connect(uri, open_timeout=10, ping_interval=20, ping_timeout=20,
                                          max_size=32 * 2**20, compression=None) as ws:
                log.info("%s connected: %s", stream, uri); st["connected"] = True; st["last_error"] = None; delay = 2
                async for raw in ws:                       # read only: nothing is ever sent
                    msg = parse_message(raw); store.apply(stream, msg)
                    if recorder: recorder.write(stream, msg)
        except asyncio.CancelledError: raise
        except Exception as e:
            st["last_error"] = f"{type(e).__name__}: {e}"; log.warning("%s: %s", stream, st["last_error"])
        st["connected"] = False; st["reconnects"] += 1
        await asyncio.sleep(delay); delay = min(delay * 2, 30)


async def devices_loop(store, host):
    while True:
        try:
            data = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(f"http://{host}:8088/v1/devices", timeout=10)))
            with store.lock: store.name_info = data.get("data", {}); store.devices_error = None
        except Exception as e:
            store.devices_error = f"{type(e).__name__}: {e}"; log.warning("devices: %s", store.devices_error)
        await asyncio.sleep(60)

# ---------------------------------------------------------------- local web page


def make_handler(store):
    page = (HERE / "static" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, ctype, code=200):
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/": self._send(page, "text/html; charset=utf-8")
            elif path == "/api/state": self._send(json.dumps(store.snapshot()).encode(), "application/json")
            elif path == "/api/raw": self._send(json.dumps(store.flat()).encode(), "application/json")
            else: self._send(b"not found", "text/plain", 404)

        def log_message(self, *a): pass
    return Handler


def main():
    ap = argparse.ArgumentParser(description="Read-only live reader for the Orca Core 2")
    ap.add_argument("--host", default="192.168.1.124", help="Orca Core address (default 192.168.1.124)")
    ap.add_argument("--interval", type=int, default=200, help="sensor update interval in ms (the app uses 200)")
    ap.add_argument("--bind", default="127.0.0.1", help="address for the local web page (default: this PC only)")
    ap.add_argument("--port", type=int, default=8765, help="port for the local web page (default 8765)")
    ap.add_argument("--record", nargs="?", const=str(HERE / "recordings"), help="append every raw message to JSONL files")
    ap.add_argument("--no-ais", action="store_true", help="do not open the AIS stream")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    store = Store(); recorder = Recorder(a.record) if a.record else None
    server = ThreadingHTTPServer((a.bind, a.port), make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Live page: http://%s:%d/  (Core %s)%s", "localhost" if a.bind == "127.0.0.1" else a.bind, a.port, a.host,
             f", recording to {recorder.folder}" if recorder else "")

    async def run():
        tasks = [stream_loop(store, "sensors", f"ws://{a.host}:8089" + SENSORS_PATH.format(ms=a.interval), recorder),
                 devices_loop(store, a.host)]
        if not a.no_ais: tasks.append(stream_loop(store, "ais", f"ws://{a.host}:8089" + AIS_PATH, recorder))
        await asyncio.gather(*tasks)
    try: asyncio.run(run())
    except KeyboardInterrupt: pass
    finally: server.shutdown()


if __name__ == "__main__":
    main()
