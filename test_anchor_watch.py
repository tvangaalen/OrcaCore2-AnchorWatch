"""Tests for anchor_watch.py with simulated Core data:  .venv\\Scripts\\python.exe -m unittest test_anchor_watch"""

import tempfile, unittest
from pathlib import Path

import asyncio, io, json, math, os, time, urllib.error
from datetime import datetime, timezone
from unittest import mock

from anchor_watch import AnchorWatch, InternetAIS, Pushover, StillAlive, TileCache, read_ais, dist_bearing_m, offset
from orca_reader import Store

LAT, LON = 39.0, 1.5        # an arbitrary point at sea; the tests only use positions relative to it


class Sim:
    def __init__(self):
        self.store = Store(); self.store.status["sensors"]["connected"] = True
        self.dir = tempfile.TemporaryDirectory(); self.file = Path(self.dir.name) / "state.json"
        self.watch = AnchorWatch(self.store, self.file)
        self.t = 1_000_000.0

    def put(self, **kv):
        for k, v in kv.items(): self.store.values["sensors"][k.replace("__", ".")] = (v, self.t)

    def boat(self, lat, lon, src="4", heading_deg=0.0, frozen=True):
        lat += (int(self.t) % 3) * 1e-7                     # a real receiver jitters in the last digit (about 1 cm here)
        self.put(**{f"navigation__position__{src}__latitude": lat, f"navigation__position__{src}__longitude": lon,
                    f"navigation__gnss__{src}__method": 1,
                    "navigation__heading__254__heading": heading_deg / 57.29577951308232, "navigation__heading__254__reference": 0})
        if frozen: self.put(navigation__position__254__latitude=LAT, navigation__position__254__longitude=LON,   # Core keeps its frozen fix
                            navigation__motion__254__stationary=1)

    def run(self, seconds, **boat):
        for _ in range(int(seconds)):
            if boat: self.boat(**boat)
            else: self.put(navigation__position__254__latitude=LAT, navigation__position__254__longitude=LON, navigation__motion__254__stationary=1)
            self.watch.tick(self.t); self.t += 1


class AnchorWatchTest(unittest.TestCase):
    def setUp(self):
        self.s = Sim(); self.w = self.s.watch
        self.s.run(2, lat=LAT, lon=LON)
        self.w.boat["pos"]  # position known
        self.w.command({"action": "drop", "mode": "here", "radius": 30})

    def tearDown(self): self.s.dir.cleanup()

    def test_geometry(self):
        lat, lon = offset(LAT, LON, 90, 100)
        d, b = dist_bearing_m(LAT, LON, lat, lon)
        self.assertAlmostEqual(d, 100, delta=0.05); self.assertAlmostEqual(b, 90, delta=0.1)

    def test_swinging_inside_circle_no_alarm(self):
        for brg in range(0, 360, 30):
            lat, lon = offset(LAT, LON, brg, 25); self.s.run(5, lat=lat, lon=lon)
        self.assertEqual(self.w.alarms, {})
        self.assertAlmostEqual(self.w.max_dist, 25, delta=0.1)

    def test_drag_needs_confirm_time(self):
        lat, lon = offset(LAT, LON, 200, 35)
        self.s.run(5, lat=lat, lon=lon)                     # GPS jump outside for 5 s: below 10 s confirm
        self.assertNotIn("drag", self.w.alarms)
        self.s.run(3, lat=LAT, lon=LON); self.s.run(11, lat=lat, lon=lon)
        self.assertIn("drag", self.w.alarms); self.assertTrue(self.w.sounding(self.s.t))
        self.s.run(1, lat=LAT, lon=LON)
        self.assertNotIn("drag", self.w.alarms)

    def test_uses_raw_gps_not_frozen_core_position(self):
        lat, lon = offset(LAT, LON, 0, 60)                  # boat dragged 60 m, Core 254 still says LAT/LON
        self.s.run(12, lat=lat, lon=lon)
        self.assertEqual(self.w.boat["pos"]["source"], "4")
        self.assertIn("drag", self.w.alarms)

    def test_source_failover(self):
        self.s.t += 20                                      # source 4 goes stale
        lat, lon = offset(LAT, LON, 90, 10)
        self.s.run(2, lat=lat, lon=lon, src="127")
        self.assertEqual(self.w.boat["pos"]["source"], "127")

    def test_gps_lost(self):
        self.s.t += 11
        self.s.run(61)                                      # raw receivers silent, only the frozen 254 keeps updating
        self.assertIsNone(self.w.boat["pos"]); self.assertIn("254", self.w.frozen)
        self.assertTrue(any("treated as frozen" in e["text"] for e in self.w.events))
        self.s.run(31)
        self.assertIn("gps", self.w.alarms)

    def test_frozen_core_is_used_only_until_it_is_seen_to_be_frozen(self):
        self.s.t += 11
        self.s.run(20)                                      # unchanged for 33 s since first seen: not yet judged frozen
        self.assertEqual(self.w.boat["pos"]["source"], "254")
        self.s.run(30)
        self.assertIsNone(self.w.boat["pos"])

    def core_gps(self, lat, lon):
        lat += (int(self.s.t) % 3) * 1e-7
        self.s.put(navigation__position__254__latitude=lat, navigation__position__254__longitude=lon,
                   navigation__gnss__254__method=2, navigation__gnss__254__satellites=22, navigation__motion__254__stationary=1)

    def test_core_gps_alone_works_while_stationary(self):
        # 2026-10-16: rest of the network off, the Core's own GPS keeps moving although it flags the boat as stationary
        self.s.t += 11
        for _ in range(120): self.core_gps(LAT, LON); self.w.tick(self.s.t); self.s.t += 1
        self.assertEqual(self.w.boat["pos"]["source"], "254"); self.assertEqual(self.w.frozen, set())
        lat, lon = offset(LAT, LON, 0, 50)
        for _ in range(11): self.core_gps(lat, lon); self.w.tick(self.s.t); self.s.t += 1
        self.assertIn("drag", self.w.alarms)

    def test_core_gps_can_be_chosen_first(self):
        self.w.command({"action": "settings", "settings": {"gps_sources": "254,4,127,3"}})
        for _ in range(5):
            self.core_gps(*offset(LAT, LON, 90, 3)); self.s.boat(LAT, LON, frozen=False); self.w.tick(self.s.t); self.s.t += 1
        self.assertEqual(self.w.boat["pos"]["source"], "254")
        names = {g["source"]: g for g in self.w.state()["gps_list"]}
        self.assertTrue(names["254"]["in_use"]); self.assertEqual(names["254"]["name"], "254 (Orca Core 2)")
        self.assertEqual(self.w.state()["gps_default"], "4,127,3,254")

    def test_frozen_raw_receiver_fails_over_and_recovers(self):
        for _ in range(62):                                 # source 4 stuck on one value, 127 alive
            self.s.put(navigation__position__4__latitude=LAT, navigation__position__4__longitude=LON, navigation__gnss__4__method=1)
            self.s.boat(*offset(LAT, LON, 90, 2), src="127", frozen=False); self.w.tick(self.s.t); self.s.t += 1
        self.assertEqual(self.w.boat["pos"]["source"], "127"); self.assertIn("4", self.w.frozen)
        self.s.run(2, lat=LAT, lon=LON)                     # 4 moves again
        self.assertEqual(self.w.boat["pos"]["source"], "4"); self.assertNotIn("4", self.w.frozen)
        self.assertTrue(any("is updating again" in e["text"] for e in self.w.events))

    def test_core_lost(self):
        self.s.store.status["sensors"]["connected"] = False
        for _ in range(31): self.w.tick(self.s.t); self.s.t += 1
        self.assertIn("core", self.w.alarms); self.assertNotIn("gps", self.w.alarms)

    def test_silence_then_retrigger(self):
        lat, lon = offset(LAT, LON, 0, 50)
        self.s.run(11, lat=lat, lon=lon)
        import anchor_watch; real = anchor_watch.time.time; anchor_watch.time.time = lambda: self.s.t
        try: self.w.command({"action": "silence", "minutes": 5})
        finally: anchor_watch.time.time = real
        self.assertFalse(self.w.sounding(self.s.t))
        self.s.run(1, lat=LAT, lon=LON)                     # back inside: alarm clears, silence resets
        self.s.run(11, lat=lat, lon=lon)
        self.assertTrue(self.w.sounding(self.s.t))

    def test_drop_ahead_uses_heading_and_bow_offset(self):
        self.w.command({"action": "settings", "settings": {"bow_offset": 5}})
        self.s.run(1, lat=LAT, lon=LON, heading_deg=270)
        self.w.command({"action": "drop", "mode": "ahead", "distance": 25, "radius": 40})
        d, b = dist_bearing_m(LAT, LON, self.w.anchor["lat"], self.w.anchor["lon"])
        self.assertAlmostEqual(d, 30, delta=0.1); self.assertAlmostEqual(b, 270, delta=0.5)

    def test_depth_and_wind_alarms(self):
        self.w.command({"action": "settings", "settings": {"min_depth": 3, "max_wind_kn": 25}})
        self.s.put(environment__depth__35__belowTransducer=2.5, **{"environment__wind__254__0__speed": 15.0})  # 15 m/s = 29 kn
        self.s.run(11, lat=LAT, lon=LON)
        self.assertIn("depth", self.w.alarms); self.assertIn("wind", self.w.alarms)

    def test_state_survives_restart(self):
        self.s.run(6, lat=LAT, lon=LON); self.w.save()
        w2 = AnchorWatch(self.s.store, self.s.file)
        self.assertEqual(w2.anchor["radius"], 30); self.assertGreater(len(w2.track), 0)

    def test_bad_input_rejected(self):
        for c in ({"action": "radius", "radius": 1}, {"action": "radius", "radius": "x"}, {"action": "nope"},
                  {"action": "settings", "settings": {"confirm_s": -1}}):
            with self.assertRaises(ValueError): self.w.command(c)


class TileCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.cache = TileCache(self.dir.name); self.calls = []

    def tearDown(self): self.dir.cleanup()

    def fake(self, result):
        def urlopen(req, timeout):
            self.calls.append(req.full_url)
            if isinstance(result, Exception): raise result
            return io.BytesIO(result)
        return mock.patch("anchor_watch.urllib.request.urlopen", urlopen)

    def test_fetch_then_cache(self):
        with self.fake(b"PNG1"): self.assertEqual(self.cache.get("osm", 16, 33034, 25049), (b"PNG1", "image/png"))
        with self.fake(OSError("offline")): self.assertEqual(self.cache.get("osm", 16, 33034, 25049)[0], b"PNG1")
        self.assertEqual(self.calls, ["https://tile.openstreetmap.org/16/33034/25049.png"])

    def test_satellite_url_is_z_y_x(self):
        with self.fake(b"JPG"): self.assertEqual(self.cache.get("satellite", 3, 4, 2)[1], "image/jpeg")
        self.assertTrue(self.calls[0].endswith("/tile/3/2/4"))

    def test_stale_tile_served_when_offline(self):
        with self.fake(b"OLD"): self.cache.get("osm", 10, 1, 1)
        f = Path(self.dir.name, "osm", "10", "1", "1.png"); old = time.time() - 40 * 86400; os.utime(f, (old, old))
        with self.fake(OSError("offline")): self.assertEqual(self.cache.get("osm", 10, 1, 1)[0], b"OLD")
        with self.fake(b"NEW"): self.assertEqual(self.cache.get("osm", 10, 1, 1)[0], b"OLD")   # backs off 30 s after a failure

    def test_missing_tile_remembered(self):
        with self.fake(urllib.error.HTTPError("u", 404, "nf", {}, None)): self.assertIsNone(self.cache.get("seamark", 12, 5, 5))
        with self.fake(b"X"): self.assertIsNone(self.cache.get("seamark", 12, 5, 5))
        self.assertEqual(len(self.calls), 1)

    def test_tile_bbox_3857(self):
        from anchor_watch import tile_bbox_3857
        self.assertEqual(tile_bbox_3857(0, 0, 0), "-20037508.34,-20037508.34,20037508.34,20037508.34")
        minx, miny, maxx, maxy = map(float, tile_bbox_3857(16, 33034, 25110).split(","))
        self.assertAlmostEqual(maxx - minx, 611.5, delta=0.1); self.assertLess(minx, 163006); self.assertGreater(maxx, 163006)

    def test_depth_source_removed(self):
        self.assertNotIn("depth", TileCache.SOURCES)

    def test_official_spanish_chart_is_wms_by_tile_bbox(self):
        from anchor_watch import tile_bbox_3857
        with self.fake(b"PNG"): self.assertEqual(self.cache.get("ihm", 15, 8584, 6201), (b"PNG", "image/png"))
        u = self.calls[0]
        self.assertTrue(u.startswith("https://ideihm.covam.es/wms/enc?")); self.assertIn("layers=RasterENC", u)
        self.assertIn("crs=EPSG:3857&bbox=" + tile_bbox_3857(15, 8584, 6201), u); self.assertIn("transparent=true", u)

    def test_out_of_range_rejected(self):
        with self.fake(b"X"):
            self.assertIsNone(self.cache.get("osm", 20, 0, 0)); self.assertIsNone(self.cache.get("osm", 2, 4, 0))
        self.assertEqual(self.calls, [])


class ParseMessageTest(unittest.TestCase):
    def test_raw_backslash_from_core(self):
        from orca_reader import parse_message
        bs = chr(92)   # one backslash, spelled out so no tool or editor can collapse it
        # as the Core sent it on 2026-10-12: a lone backslash in an AIS destination, next to valid escapes
        raw = ('{"values": {"ais.x.224672000.position.destination": "IBIZA' + bs + 'FORMENTERA    ", '
               '"a": "x' + bs + bs + 'y", "b": "' + bs + 'u00e9' + bs + 'n"}}')
        v = parse_message(raw)["values"]
        self.assertEqual(v["ais.x.224672000.position.destination"], "IBIZA" + bs + "FORMENTERA    ")
        self.assertEqual(v["a"], "x" + bs + "y"); self.assertEqual(v["b"], chr(0xE9) + chr(10))   # valid escapes untouched

    def test_other_errors_still_raise(self):
        from orca_reader import parse_message
        with self.assertRaises(json.JSONDecodeError): parse_message('{"a": ')


class MyMmsiTest(unittest.TestCase):
    def ais(self, mmsi, lat, lon, t=1000.0):
        return {f"ais.x.{mmsi}.position.latitude": (lat, t), f"ais.x.{mmsi}.position.longitude": (lon, t),
                f"ais.x.{mmsi}.position.name": ("BOAT " + mmsi, t)}

    def test_my_mmsi_never_shown_even_far_away(self):
        from anchor_watch import read_ais
        far = offset(LAT, LON, 90, 300)
        ais = {**self.ais("123456789", *far), **self.ais("987654321", *offset(LAT, LON, 0, 75))}
        self.assertEqual([t["mmsi"] for t in read_ais(ais, 1001, LAT, LON, "123456789")], ["987654321"])

    def test_without_my_mmsi_close_target_is_suggested(self):
        from anchor_watch import read_ais
        near = []
        ais = {**self.ais("123456789", *offset(LAT, LON, 40, 2.5)), **self.ais("987654321", *offset(LAT, LON, 0, 75))}
        shown = read_ais(ais, 1001, LAT, LON, "", near)
        self.assertEqual([t["mmsi"] for t in shown], ["987654321"]); self.assertEqual(near[0]["mmsi"], "123456789")

    def test_setting_validation_and_state(self):
        s = Sim(); w = s.watch
        try:
            with self.assertRaises(ValueError): w.command({"action": "settings", "settings": {"own_mmsi": "12345"}})
            with self.assertRaises(ValueError): w.command({"action": "settings", "settings": {"own_mmsi": "24400147x"}})
            w.command({"action": "settings", "settings": {"own_mmsi": " 123456789 "}})
            self.assertEqual(w.settings["own_mmsi"], "123456789")
            s.store.values["ais"].update(self.ais("123456789", *offset(LAT, LON, 40, 2.5), t=time.time()))   # state() ages AIS on the real clock
            s.run(1, lat=LAT, lon=LON)
            st = w.state(); self.assertEqual(st["ais"], []); self.assertIsNone(st["own_candidate"])
            w.command({"action": "settings", "settings": {"own_mmsi": "111111111"}})   # another MMSI: ours shows again
            self.assertEqual([t["mmsi"] for t in w.state()["ais"]], ["123456789"])
            w.command({"action": "settings", "settings": {"own_mmsi": ""}})
            self.assertEqual(w.state()["own_candidate"]["mmsi"], "123456789")
        finally: s.dir.cleanup()

    def test_new_track_resets_track_and_max_swing(self):
        s = Sim(); w = s.watch
        try:
            s.run(12, lat=LAT, lon=LON); w.command({"action": "drop", "mode": "here", "radius": 30})
            s.run(12, lat=offset(LAT, LON, 0, 20)[0], lon=LON)
            self.assertGreater(w.state()["track_points"], 0); self.assertGreater(w.max_dist, 19)
            gen = w.track_gen; w.command({"action": "clear_track"})
            st = w.state(); self.assertEqual(st["track_points"], 0); self.assertEqual(st["max_dist"], 0); self.assertEqual(st["track_gen"], gen + 1)
            self.assertIsNotNone(w.anchor)                   # the watch itself carries on
        finally: s.dir.cleanup()


class FakeNotifier:
    configured = True
    last_error = last_sent = None

    def __init__(self): self.calls = []
    def alarm(self, key, title, message, when): self.calls.append(("alarm", key, title, message))
    def cleared(self, key, title, message, when): self.calls.append(("cleared", key))
    def cancel(self, key=None): self.calls.append(("cancel", key))
    def reload(self): return None
    def test(self): self.calls.append(("test",))


class PushHooksTest(unittest.TestCase):
    def setUp(self):
        self.s = Sim(); self.w = self.s.watch; self.n = self.w.notifier = FakeNotifier()
        self.s.run(2, lat=LAT, lon=LON); self.w.command({"action": "drop", "mode": "here", "radius": 30})
        self.n.calls.clear()

    def tearDown(self): self.s.dir.cleanup()

    def drag(self, seconds=11): self.s.run(seconds, lat=offset(LAT, LON, 0, 50)[0], lon=LON)

    def test_drag_pushes_once_then_clears(self):
        self.drag(30)
        alarms = [c for c in self.n.calls if c[0] == "alarm"]
        self.assertEqual(len(alarms), 1); self.assertEqual(alarms[0][1], "drag")
        self.assertIn("50 m from anchor (limit 30 m)", alarms[0][3])
        self.s.run(1, lat=LAT, lon=LON)
        self.assertEqual(self.n.calls[-1], ("cleared", "drag"))

    def test_no_push_before_confirm_time(self):
        self.drag(5); self.assertEqual(self.n.calls, [])

    def test_silence_and_raise_cancel_the_emergency(self):
        self.drag()
        with mock.patch("anchor_watch.time.time", lambda: self.s.t): self.w.command({"action": "silence", "minutes": 5})
        self.assertEqual(self.n.calls[-1], ("cancel", None))
        self.w.command({"action": "raise"}); self.assertEqual(self.n.calls[-1], ("cancel", None))

    def test_switched_off(self):
        self.w.command({"action": "settings", "settings": {"push_alerts": False}})
        self.drag(); self.assertEqual([c for c in self.n.calls if c[0] == "alarm"], [])

    def test_push_test_command(self):
        self.w.command({"action": "push_test"}); self.assertEqual(self.n.calls[-1], ("test",))


class PushoverTest(unittest.TestCase):
    TOK, USR = "a" * 30, "u" * 30

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.f = Path(self.dir.name, "pushover.json")
        self.f.write_text(json.dumps({"app_token": self.TOK, "user_key": self.USR}), encoding="utf-8")
        self.events = []
        self.p = Pushover(self.f, "https://192.168.1.10:8766/", on_event=lambda t, k: self.events.append(t), start=False)
        self.p.BACKOFF_S = (0, 0, 0); self.posts = []

    def tearDown(self): self.dir.cleanup()

    def run_queue(self, responses):
        def post(path, fields):
            self.posts.append((path, fields)); r = responses.pop(0)
            if isinstance(r, Exception): raise r
            return r
        with mock.patch.object(self.p, "_post", post):
            while not self.p.q.empty(): self.p.process(self.p.q.get())

    def test_emergency_drag_then_cancel_by_receipt(self):
        self.p.alarm("drag", "Anchor drag", "50 m", 1000); self.p.cancel()
        self.run_queue([{"status": 1, "receipt": "R123"}, {"status": 1}])
        path, f = self.posts[0]
        self.assertEqual(path, "messages.json")
        self.assertEqual((f["priority"], f["retry"], f["expire"], f["token"], f["user"]), (2, 30, 10800, self.TOK, self.USR))
        self.assertEqual(f["sound"], "persistent")
        self.assertEqual(f["url"], "https://192.168.1.10:8766/")
        self.assertEqual(self.posts[1], ("receipts/R123/cancel.json", {"token": self.TOK}))
        self.assertEqual(self.p.receipts, {})

    def test_cleared_cancels_and_sends_normal_message(self):
        self.p.receipts["drag"] = "R9"; self.p.cleared("drag", "Cleared", "ok", 1000)
        self.run_queue([{"status": 1}, {"status": 1}])
        self.assertEqual(self.posts[0][0], "receipts/R9/cancel.json")
        self.assertEqual(self.posts[1][1]["priority"], 0); self.assertNotIn("retry", self.posts[1][1])

    def test_test_alarm_is_a_short_emergency_that_can_be_stopped(self):
        self.p.test_alarm(); self.p.cancel("test")
        self.run_queue([{"status": 1, "receipt": "RT"}, {"status": 1}])
        f = self.posts[0][1]
        self.assertEqual((f["priority"], f["retry"], f["expire"]), (2, 30, 600)); self.assertIn("TEST", f["title"])
        self.assertEqual(self.posts[1][0], "receipts/RT/cancel.json")

    def test_gps_lost_is_high_priority_not_emergency(self):
        self.p.alarm("gps", "GPS", "lost", 1000); self.run_queue([{"status": 1}])
        self.assertEqual(self.posts[0][1]["priority"], 1); self.assertNotIn("retry", self.posts[0][1])

    def test_network_errors_are_retried(self):
        self.p.alarm("drag", "Anchor drag", "x", 1000)
        self.run_queue([OSError("no route"), OSError("no route"), {"status": 1, "receipt": "R1"}])
        self.assertEqual(len(self.posts), 3); self.assertIsNone(self.p.last_error); self.assertEqual(self.events, [])

    def test_bad_key_is_not_retried_and_logged(self):
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"errors":["application token is invalid"]}'))
        self.p.alarm("drag", "Anchor drag", "x", 1000); self.run_queue([err])
        self.assertEqual(len(self.posts), 1); self.assertIn("application token is invalid", self.p.last_error)
        self.assertTrue(self.events and "not delivered" in self.events[0])

    def test_key_file_validation(self):
        self.f.write_text(json.dumps({"app_token": "short", "user_key": self.USR}), encoding="utf-8")
        self.assertIn("30-character", self.p.reload()); self.assertFalse(self.p.configured)
        # keys pasted over the field names, placeholders left as values
        self.f.write_text(json.dumps({self.TOK: "replace with the API token", self.USR: "replace with your user key"}), encoding="utf-8")
        self.assertIn("in place of a field name", self.p.reload()); self.assertFalse(self.p.configured)
        self.f.unlink(); self.assertIn("not found", self.p.reload())


class StillAliveTest(unittest.TestCase):
    UUID = "0f9a3c2e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.f = Path(self.dir.name, "still_alive.json")
        self.minutes = 60.0; self.events = []; self.reqs = []; self.fail = False

    def tearDown(self): self.dir.cleanup()

    def make(self, api_key=""):
        self.f.write_text(json.dumps({"ping_url": f"https://hc-ping.com/{self.UUID}", "api_key": api_key}), encoding="utf-8")
        a = StillAlive(self.f, lambda: self.minutes, on_event=lambda t, k: self.events.append((k, t)), start=False)
        def request(url, data=None, headers=None):
            self.reqs.append((url, data, headers))
            if self.fail: raise OSError("network down")
            return 200, b"OK"
        a._request = request
        return a

    def pings(self): return [r for r in self.reqs if r[0].startswith("https://hc-ping.com/")]

    def test_pings_every_5_min_for_a_60_min_interval(self):
        a = self.make()
        for t in range(0, 3601, 30): a.step(1000 + t)
        self.assertEqual(len(self.pings()), 13)                     # t = 0, 300, ..., 3600
        self.assertEqual(a.ping_every(60), 300); self.assertEqual(a.ping_every(6), 120); self.assertEqual(a.ping_every(1), 30)

    def test_without_api_key_tells_what_to_set(self):
        a = self.make(); a.step(1000)
        self.assertIn("60 min", a.sync_note); self.assertEqual(len(self.reqs), 1)    # only the ping, no API call

    def test_api_key_syncs_period_and_pauses_at_zero(self):
        a = self.make(api_key="k" * 32); a.step(1000)
        url, data, hdr = self.reqs[0]
        self.assertEqual(url, f"https://healthchecks.io/api/v3/checks/{self.UUID}")
        self.assertEqual(json.loads(data), {"timeout": 3600, "grace": 60}); self.assertEqual(hdr["X-Api-Key"], "k" * 32)
        self.assertIsNone(a.sync_note)
        self.minutes = 0; a.poke(); a.step(2000)
        self.assertEqual(self.reqs[-1][0], f"https://healthchecks.io/api/v3/checks/{self.UUID}/pause")
        n = len(self.reqs); a.step(5000); self.assertEqual(len(self.reqs), n)       # off: no more pings
        self.minutes = 30; a.poke(); a.step(6000)
        self.assertEqual(json.loads(self.reqs[-2][1])["timeout"], 1800); self.assertTrue(self.reqs[-1][0].startswith("https://hc-ping.com/"))

    def test_failed_pings_are_logged_once_and_recovery_too(self):
        a = self.make(); self.fail = True
        for t in (0, 300, 600): a.step(1000 + t)
        self.assertEqual([k for k, _ in self.events], ["warn"]); self.assertIn("network down", a.last_error)
        self.fail = False; a.step(1900)
        self.assertEqual([k for k, _ in self.events], ["warn", "ok"]); self.assertIsNone(a.last_error)

    def test_config_validation(self):
        self.f.write_text(json.dumps({"ping_url": "http://hc-ping.com/x"}), encoding="utf-8")
        a = StillAlive(self.f, lambda: 60, start=False)
        self.assertFalse(a.configured); self.assertIn("https://", a.reload())
        self.f.unlink(); self.assertIn("not found", a.reload())

    def test_setting_change_pokes_the_check(self):
        s = Sim(); w = s.watch
        try:
            w.alive = a = self.make(); a.step(1000); a.synced = 60.0
            with self.assertRaises(ValueError): w.command({"action": "settings", "settings": {"alive_minutes": 5000}})
            w.command({"action": "settings", "settings": {"alive_minutes": 15}})
            self.assertIsNone(a.synced); self.assertEqual(w.settings["alive_minutes"], 15)
            self.assertEqual(w.state()["alive"]["every_s"], 300)
        finally: s.dir.cleanup()


class InternetAISTest(unittest.TestCase):
    KEY = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.f = Path(self.dir.name, "aisstream.json")
        self.f.write_text(json.dumps({"api_key": self.KEY}), encoding="utf-8")
        self.n = InternetAIS(self.f, lambda: (LAT, LON), lambda: True)

    def tearDown(self): self.dir.cleanup()

    def pos(self, mmsi, lat, lon, typ="PositionReport", when="2026-10-16 15:45:45.948838287 +0000 UTC", **body):
        return {"MessageType": typ, "MetaData": {"MMSI": int(mmsi), "ShipName": "SEA LADY   ", "latitude": lat, "longitude": lon, "time_utc": when},
                "Message": {typ: {"Latitude": lat, "Longitude": lon, "Cog": 123.4, "Sog": 4.2, "TrueHeading": 511, **body}}}

    def test_position_and_static_reports(self):
        now = datetime(2026, 10, 16, 15, 46, 0, tzinfo=timezone.utc).timestamp()
        self.n.handle(self.pos("987654321", *offset(LAT, LON, 0, 200)), now)
        self.n.handle({"MessageType": "ShipStaticData", "MetaData": {"MMSI": 987654321},
                       "Message": {"ShipStaticData": {"Name": "SEA LADY", "Dimension": {"A": 10, "B": 4, "C": 2, "D": 2}}}}, now)
        t = self.n.snapshot(now)[0]
        self.assertEqual((t["name"], t["sog"], t["cog"], t["heading"], t["length"]), ("SEA LADY", 4.2, 123, None, 14))
        self.assertEqual(now - t["t"], 15)                                                # age from the report's own time (whole seconds)
        self.n.handle(self.pos("111222333", 91, 181), now); self.assertEqual(len(self.n.snapshot(now)), 1)   # "not available"
        self.n.handle(self.pos("123", 39.0, 1.5), now); self.assertEqual(len(self.n.snapshot(now)), 1)        # no valid MMSI

    def test_class_b_report_and_expiry(self):
        self.n.handle(self.pos("222333444", LAT, LON, typ="StandardClassBPositionReport", when="garbage", TrueHeading=90), 1000)
        t = self.n.snapshot(1000)[0]; self.assertEqual((t["heading"], t["t"]), (90, 1000))
        self.assertEqual(self.n.snapshot(1000 + 901), [])

    def test_local_receiver_has_priority(self):
        now = 10_000.0
        local = {f"ais.x.987654321.position.{k}": (v, now - 20) for k, v in
                 (("latitude", offset(LAT, LON, 0, 100)[0]), ("longitude", LON), ("name", "LOCAL NAME"))}
        net = [{"mmsi": "987654321", "lat": offset(LAT, LON, 0, 300)[0], "lon": LON, "t": now - 5, "name": "NET NAME"},
               {"mmsi": "555666777", "lat": offset(LAT, LON, 90, 150)[0], "lon": offset(LAT, LON, 90, 150)[1], "t": now - 40},
               {"mmsi": "123456789", "lat": LAT, "lon": LON, "t": now}]                  # My MMSI
        out = {t["mmsi"]: t for t in read_ais(local, now, LAT, LON, "123456789", None, net)}
        self.assertEqual(set(out), {"987654321", "555666777"})
        self.assertEqual((out["987654321"]["src"], out["987654321"]["name"], round(out["987654321"]["dist"])), ("local", "LOCAL NAME", 100))
        self.assertEqual((out["555666777"]["src"], out["555666777"]["age"]), ("internet", 40))
        # local position gone stale (older than 15 min): the internet report takes over, keeping the local name if it has none
        stale = {k: (v, now - 1000) for k, (v, _) in local.items()}
        net[0].pop("name")
        r = {t["mmsi"]: t for t in read_ais(stale, now, LAT, LON, "123456789", None, net)}["987654321"]
        self.assertEqual((r["src"], r["name"], round(r["dist"])), ("internet", "LOCAL NAME", 300))

    def test_key_file(self):
        self.assertTrue(self.n.configured)
        self.f.write_text(json.dumps({self.KEY: "replace with your key"}), encoding="utf-8")
        self.assertIn("in place of the field name", self.n.reload())
        self.f.write_text(json.dumps({"api_key": "short"}), encoding="utf-8"); self.assertIn("API key", self.n.reload())
        self.f.unlink(); self.assertIn("not found", self.n.reload())

    def test_box_is_around_the_boat(self):
        (la1, lo1), (la2, lo2) = self.n.box(LAT, LON)
        self.assertTrue(la1 < LAT < la2 and lo1 < LON < lo2); self.assertAlmostEqual(la2 - la1, 0.06, places=4)

    def run_loop(self, frames):
        sent = []; n = self.n

        class FakeWS:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def send(self, text): sent.append(json.loads(text))
            async def recv(self):
                if frames:
                    f = frames.pop(0)
                    if isinstance(f, Exception): raise f
                    return json.dumps(f)
                n.kick = True; return json.dumps({})

        sleeps = []
        async def stop(seconds): sleeps.append(seconds); raise asyncio.CancelledError
        with mock.patch("anchor_watch.websockets.connect", lambda *a, **k: FakeWS()), mock.patch("anchor_watch.asyncio.sleep", stop):
            with self.assertRaises(asyncio.CancelledError): asyncio.run(n.run())
        return sent, sleeps

    def test_loop_subscribes_and_collects(self):
        sent, sleeps = self.run_loop([self.pos("987654321", *offset(LAT, LON, 0, 200))])
        self.assertEqual(sent[0]["APIKey"], self.KEY); self.assertEqual(sent[0]["BoundingBoxes"], [self.n.box(LAT, LON)])
        self.assertIn("ShipStaticData", sent[0]["FilterMessageTypes"])
        self.assertEqual(len(self.n.targets), 1); self.assertIsNone(self.n.last_error); self.assertEqual(sleeps, [5])

    def test_reconnects_after_a_drop_and_keeps_the_new_connection(self):
        # 2026-10-18: after one dropped connection the next one was broken off again right after aisstream's
        # SubscriptionConfirmation, because the old error was still set; internet AIS never came back.
        import websockets
        self.n.last_error = "connection closed: no close frame received or sent"     # left over from the drop
        events = []; self.n.on_event = lambda t, k: events.append(t)
        sent, sleeps = self.run_loop([{"MessageType": "SubscriptionConfirmation"},
                                      self.pos("987654321", *offset(LAT, LON, 0, 200))])
        self.assertEqual(len(self.n.targets), 1)          # the position after the confirmation was still read
        self.assertIsNone(self.n.last_error); self.assertEqual(sleeps, [5])

    def test_abrupt_close_after_subscribing_counts_as_refused_and_logs_once(self):
        import websockets
        events = []; self.n.on_event = lambda t, k: events.append(t)
        for _ in range(2):
            self.run_loop([websockets.ConnectionClosedError(None, None)])
        self.assertIn("API key not accepted", self.n.last_error); self.assertEqual(len(events), 1)

    def test_loop_backs_off_on_refused_key(self):
        sent, sleeps = self.run_loop([{"error": "Api Key Is Not Valid"}])
        self.assertIn("Api Key Is Not Valid", self.n.last_error); self.assertEqual(sleeps, [300])


class TideTest(unittest.TestCase):
    P = 12.42 * 3600                                            # semidiurnal (M2) period

    def series(self, start=1_000_000, hours=72, amp=0.08, offset=-0.4):
        # hourly, rounded to cm like the Open-Meteo model; high water at start + k*P
        return [[start + i * 3600, round(offset + amp * math.cos(2 * math.pi * i * 3600 / self.P), 2)] for i in range(hours)]

    def test_highs_lows_trend_and_range(self):
        from anchor_watch import tide_summary
        s = self.series(); start = s[0][0]
        now = start + 30 * 3600                                 # between high (2P = 24.84 h) and low (2.5P = 31.05 h)
        r = tide_summary(s, now)
        self.assertEqual(r["trend"], "falling"); self.assertEqual([e["type"] for e in r["next"]], ["low", "high"])
        self.assertLess(abs(r["next"][0]["t"] - (start + 2.5 * self.P)), 20 * 60)     # within 20 min of the truth
        self.assertLess(abs(r["next"][1]["t"] - (start + 3 * self.P)), 20 * 60)
        self.assertAlmostEqual(r["range"], 0.16, delta=0.02)
        self.assertAlmostEqual(r["level"], -0.4 + 0.08 * math.cos(2 * math.pi * 30 * 3600 / self.P), delta=0.012)
        self.assertTrue(all(now - 6 * 3600 <= t <= now + 18 * 3600 for t, _ in r["curve"]))
        self.assertEqual(tide_summary(s, start + 8 * 3600)["trend"], "rising")         # after the low at 0.5P = 6.2 h

    def test_outside_the_series(self):
        from anchor_watch import tide_summary
        s = self.series(); self.assertIsNone(tide_summary(s, s[-1][0] + 1)); self.assertIsNone(tide_summary(s[:3], s[0][0]))

    def test_feed_fetch_cache_and_refresh(self):
        from anchor_watch import TideFeed
        d = tempfile.TemporaryDirectory(); f = Path(d.name, "tide.json")
        try:
            hours = [datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp() + i * 3600 for i in range(48)]
            body = {"latitude": 39.0, "longitude": 1.55, "hourly": {
                "time": [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M") for t in hours],
                "sea_level_height_msl": [round(-0.4 + 0.05 * math.sin(i / 2), 2) if i != 7 else None for i in range(48)]}}
            urls = []
            def urlopen(req, timeout):
                urls.append(req.full_url); return io.BytesIO(json.dumps(body).encode())
            feed = TideFeed(f, lambda: (39.00321, 1.50487))
            with mock.patch("anchor_watch.urllib.request.urlopen", urlopen): feed.fetch((39.00321, 1.50487), now=hours[10])
            self.assertIn("latitude=39.00&longitude=1.50", urls[0])                # only a rounded position is sent
            self.assertEqual(len(feed.data["series"]), 47)                         # the missing hour is skipped
            again = TideFeed(f, lambda: None)                                      # offline: the cached copy is used
            self.assertTrue(again.summary(hours[12])["available"]); self.assertEqual(again.data["grid"], [39.0, 1.55])
            self.assertFalse(feed.due(hours[10] + 600, (39.0032, 1.5049)))
            self.assertTrue(feed.due(hours[10] + 4 * 3600 + 1, (39.0032, 1.5049)))   # older than 3 h
            self.assertTrue(feed.due(hours[10] + 600, (39.075, 1.5049)))            # boat moved 8 km
        finally: d.cleanup()


class DepthPlanTest(unittest.TestCase):
    P = 12.42 * 3600

    def tide(self, start, hours=72, amp=0.10):
        return [[start + i * 3600, round(-0.4 + amp * math.cos(2 * math.pi * i * 3600 / self.P), 3)] for i in range(hours)]

    def test_window_min_max_and_chain(self):
        from anchor_watch import chain_advice, depth_window
        s = self.tide(1_000_000); now = s[0][0] + 6 * 3600
        w = depth_window(s, now, 8.0, now)                       # live reading of 8.0 m now
        self.assertEqual(w["now"], 8.0)
        self.assertAlmostEqual(w["max"] - w["min"], 0.20, delta=0.01)          # the full tidal range fits in 24 h
        self.assertTrue(now <= w["min_t"] <= now + 86400 and now <= w["max_t"] <= now + 86400)
        self.assertEqual(chain_advice(w["max"], 1.2, 5), math.ceil((w["max"] + 1.2) * 5))
        self.assertEqual(chain_advice(8.0, 1.2, 5), 46); self.assertEqual(chain_advice(8.01, 1.2, 5), 47)   # whole metres, up
        self.assertIsNone(depth_window(s, s[-1][0] - 3600, 8.0, s[-1][0] - 3600))   # 24 h not covered
        c = w["curve"]                                           # for the chart: 6 h back to 24 h ahead, in depth
        self.assertEqual((c[0][0], c[-1][0]), (now - 6 * 3600, now + 24 * 3600))
        self.assertIn([now, 8.0], c); self.assertEqual(min(v for t, v in c if t >= now), w["min"])

    def test_last_reading_is_corrected_for_the_tide(self):
        from anchor_watch import depth_window, tide_at
        s = self.tide(1_000_000); then = s[0][0] + 2 * 3600; now = s[0][0] + 8 * 3600
        w = depth_window(s, now, 8.0, then)
        self.assertAlmostEqual(w["now"], 8.0 + tide_at(s, now) - tide_at(s, then), delta=0.01)

    def watch_with_tide(self):
        sim = Sim(); w = sim.watch
        class Feed: pass
        w.tide = Feed(); w.tide.data = {"series": self.tide(sim.t - 3600, hours=60)}
        w.tide.summary = lambda now=None: {"available": False}
        return sim, w

    def test_plan_uses_sounder_with_waterline_offset(self):
        sim, w = self.watch_with_tide()
        try:
            sim.put(environment__depth__35__belowTransducer=7.6, environment__depth__35__offset=0.4)
            sim.run(2, lat=LAT, lon=LON)
            self.assertEqual((w.boat["depth"], w.boat["depth_transducer"], w.boat["depth_offset_src"]), (8.0, 7.6, "sounder"))
            p = w.depth_plan(sim.t)
            self.assertTrue(p["available"]); self.assertEqual(p["source"], "sounder")
            self.assertAlmostEqual(p["now"], 8.0, delta=0.01)                   # 7.6 below the transducer + 0.4 to the waterline
            self.assertEqual(p["chain"], math.ceil((p["max"] + 1.2) * 5)); self.assertIsNone(p["clearance_min"])   # no draught set
            w.command({"action": "settings", "settings": {"scope_ratio": 3, "bow_height": 2, "draught": 2.3}})
            p2 = w.depth_plan(sim.t)
            self.assertEqual(p2["chain"], math.ceil((p["max"] + 2) * 3))
            self.assertAlmostEqual(p2["clearance_min"], p["min"] - 2.3, delta=0.001)   # shallowest water in 24 h minus the keel
        finally: sim.dir.cleanup()

    def test_plan_falls_back_to_last_reading_here_only(self):
        sim, w = self.watch_with_tide()
        try:
            sim.put(environment__depth__35__belowTransducer=7.6, environment__depth__35__offset=0.4)
            sim.run(2, lat=LAT, lon=LON)
            del sim.store.values["sensors"]["environment.depth.35.belowTransducer"]   # sounder switched off
            sim.run(3, lat=LAT, lon=LON)
            p = w.depth_plan(sim.t); self.assertEqual(p["source"], "last reading"); self.assertTrue(p["available"])
            w.save(); self.assertIsNotNone(AnchorWatch(sim.store, sim.file).last_depth)      # kept over a restart
            far = offset(LAT, LON, 90, 400); sim.t += 11; sim.run(2, lat=far[0], lon=far[1])  # 400 m away
            p = w.depth_plan(sim.t); self.assertFalse(p["available"]); self.assertIn("elsewhere", p["reason"])
        finally: sim.dir.cleanup()

    def test_transducer_setting_when_the_sounder_does_not_report_it(self):
        sim, w = self.watch_with_tide()
        try:
            sim.put(environment__depth__35__belowTransducer=7.6, environment__depth__35__offset=-1.5)   # offset to the keel
            sim.run(2, lat=LAT, lon=LON)
            self.assertEqual((w.boat["depth"], w.boat["depth_offset_src"]), (7.6, "setting"))      # setting 0 by default
            w.command({"action": "settings", "settings": {"transducer_depth": 0.5}}); sim.run(1, lat=LAT, lon=LON)
            self.assertEqual(w.boat["depth"], 8.1); self.assertAlmostEqual(w.depth_plan(sim.t)["now"], 8.1, delta=0.01)
        finally: sim.dir.cleanup()

    def test_min_depth_alarm_uses_water_depth(self):
        sim = Sim(); w = sim.watch
        try:
            sim.run(2, lat=LAT, lon=LON); w.command({"action": "drop", "mode": "here", "radius": 30})
            w.command({"action": "settings", "settings": {"min_depth": 3}})
            def sounder(bt, seconds):                                              # the Core refreshes the reading
                for _ in range(seconds):
                    sim.put(environment__depth__35__belowTransducer=bt, environment__depth__35__offset=0.4); sim.run(1, lat=LAT, lon=LON)
            sounder(2.7, 12); self.assertNotIn("depth", w.alarms)                # 3.1 m of water: above 3 m
            sounder(2.5, 12); self.assertIn("depth", w.alarms)                   # 2.9 m of water (2.5 below the transducer)
        finally: sim.dir.cleanup()


if __name__ == "__main__":
    unittest.main()
