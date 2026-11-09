# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hotdobb - m3u2satip, see LICENSE
"""Offline tests for satipbridge.py - no TV, no network needed.

    python3 -m unittest discover -s tests -v

Each test guards a fault that really happened while building the bridge.
"""
import importlib.util
import json
import os
import struct
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_bridge():
    spec = importlib.util.spec_from_file_location(
        "satipbridge", os.path.join(ROOT, "satipbridge.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["satipbridge"] = mod
    spec.loader.exec_module(mod)
    mod.log = lambda *a: None          # keep test output quiet
    return mod


B = load_bridge()


def crc_bitwise(data):
    """Reference: the original bit-by-bit MPEG-2 CRC32."""
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF \
                if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


class TestTables(unittest.TestCase):

    def test_crc_matches_bitwise_reference(self):
        for data in (b"", b"\x00", bytes(range(256)), os.urandom(4093)):
            self.assertEqual(B.crc32_mpeg(data), crc_bitwise(data))

    def test_empty_cat_is_byte_identical_to_panasonic(self):
        # Captured from the Panasonic TX-58DX800W's own SAT>IP server.
        self.assertEqual(B.build_cat_section().hex(),
                         "01b009ffffc30000d5dcfb4c")

    def test_sections_round_trip(self):
        pat = B.build_pat_section([(1, 0x100), (2, 0x110)], tsid=10)
        pkts, _ = B.sections_to_ts([pat], pid=0)
        self.assertEqual(B.parse_pat_raw(b"".join(pkts)),
                         {1: 0x100, 2: 0x110})


class TestContinuityCounter(unittest.TestCase):
    """The TX-58DX800W dropped tables sent with the same counter every
    round; channel scans took 4 minutes per frequency."""

    def test_counters_run_on_across_repeated_tables(self):
        pat = B.build_pat_section([(1, 0x100)], tsid=10)
        pkts, _ = B.sections_to_ts([pat], pid=0)
        ccmap, seen = {}, []
        for _ in range(20):                      # the same packet, 20 rounds
            out = B.restamp_cc(pkts, ccmap)
            seen.append(out[0][3] & 0x0F)
        self.assertEqual(seen, [i % 16 for i in range(20)])

    def test_payload_untouched_and_null_packets_ignored(self):
        pkt = bytes([0x47, 0x00, 0x00, 0x10]) + bytes(range(184))
        out = B.restamp_cc([pkt, B.NULL_TS], {0: 4})
        self.assertEqual(out[0][4:], pkt[4:])
        self.assertEqual(out[0][3] & 0x0F, 5)
        self.assertEqual(out[1], B.NULL_TS)


class TestRtcp(unittest.TestCase):
    """SAT>IP spec 3.5.15.2: identifier and string_length after SES1."""

    def test_spec_layout(self):
        text = "ver=1.2;tuner=1,240,1,7,444.00,8,dvbc,256qam,6900,,,,0;pids=0"
        app = B.rtcp_app(7, text, norm=True)
        _v, pt, length = struct.unpack("!BBH", app[:4])
        self.assertEqual(pt, 204)
        self.assertEqual((length + 1) * 4, len(app))
        self.assertEqual(app[8:12], b"SES1")
        ident, slen = struct.unpack("!HH", app[12:16])
        self.assertEqual((ident, slen), (0, len(text)))
        self.assertEqual(app[16:16 + slen].decode(), text)

    def test_legacy_layout_unchanged(self):
        app = B.rtcp_app(7, "ver=1.2", norm=False)
        self.assertEqual(app[12:19], b"ver=1.2")


class TestPlaylists(unittest.TestCase):

    def test_multicast_list(self):
        rows = B.parse_init7_m3u(
            '#EXTM3U\n#EXTINF:0 tvg-logo="x.png" group-title="de",SRF 1\n'
            'udp://@233.50.230.80:5000\n'
            '#EXTINF:0 group-title="en",BBC Two\nrtp://@233.50.230.156:5000\n')
        self.assertEqual([(r["name"], r["group"], r["port"], r["language"])
                          for r in rows],
                         [("SRF 1", "233.50.230.80", 5000, "de"),
                          ("BBC Two", "233.50.230.156", 5000, "en")])

    def test_hls_list_formats(self):
        text = "﻿#EXTM3U\r\n" + "\r\n".join([
            '#EXTINF:-1 group-title="News, Info",Comma Channel',
            'https://a.example/a.m3u8',
            '#EXTINF:-1,Kodi',
            'https://b.example/b.m3u8|User-Agent=Mozilla%2F5.0&Referer=https://b/',
            '#EXTINF:-1,TiviMate',
            '#EXTHTTP:{"User-Agent":"TiviMate/4"}',
            'https://c.example/c.m3u8',
            '#EXTINF:-1,VLC',
            '#EXTVLCOPT:http-referrer=https://d/',
            'https://d.example/d.m3u8',
            '#EXTINF:-1,Encrypted',
            '#KODIPROP:inputstream.adaptive.license_type=com.widevine.alpha',
            'https://e.example/e.mpd',
            'https://f.example/live/bare.m3u8',
            '#EXTINF:-1,Not supported',
            'ftp://g.example/x',
            '#EXTINF:-1,Duplicate',
            'https://a.example/a.m3u8',
        ])
        rows = {r["url"]: r for r in B.parse_hls_m3u(text)}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows["https://a.example/a.m3u8"]["name"],
                         "Comma Channel")
        self.assertEqual(rows["https://b.example/b.m3u8"]["headers"],
                         {"User-Agent": "Mozilla/5.0",
                          "Referer": "https://b/"})
        self.assertEqual(rows["https://c.example/c.m3u8"]["headers"],
                         {"User-Agent": "TiviMate/4"})
        self.assertEqual(rows["https://d.example/d.m3u8"]["headers"],
                         {"Referer": "https://d/"})
        self.assertTrue(rows["https://e.example/e.mpd"]["drm"])
        self.assertIn("https://f.example/live/bare.m3u8", rows)

    def test_ts_payload_raw_and_rtp(self):
        ts = B.NULL_TS * 2
        self.assertEqual(B.ts_payload(ts), ts)
        rtp = struct.pack("!BBHII", 0x80, 33, 1, 0, 0) + ts
        self.assertEqual(B.ts_payload(rtp), ts)


class TestMigration(unittest.TestCase):
    """Files of the German versions (up to 12) must keep TVs and HLS slots."""

    def test_devices(self):
        new = B._convert_devices({"02:00:00:00:00:01": {
            "profil": "streng", "spiegel": True, "zuletzt_ip": "192.168.1.50",
            "zuletzt": "2026-11-08 20:00", "erstmals": "2026-09-25 17:00",
            "name": "58DX800_Series"}})
        self.assertEqual(new["02:00:00:00:00:01"], {
            "profile": "strict", "mirror": True, "last_ip": "192.168.1.50",
            "last_seen": "2026-11-08 20:00", "first_seen": "2026-09-25 17:00",
            "name": "58DX800_Series"})
        self.assertEqual(
            B._convert_devices({"x": {"profil": "alt"}})["x"]["profile"],
            "legacy")

    def test_hls_order_is_kept(self):
        new = B._convert_hls({"aufgenommen": ["u2", "u1"], "info": {"u1": 1},
                              "pruefung": {"u1": {"ok": True, "zeit": 5}},
                              "fertig": 9})
        self.assertEqual(new["admitted"], ["u2", "u1"])
        self.assertEqual(new["checks"]["u1"], {"ok": True, "time": 5})
        self.assertEqual(new["finished"], 9)

    def test_sessions(self):
        self.assertEqual(B._convert_sessions({"zeit": 3, "sitzungen": [1]}),
                         {"time": 3, "version": "", "sessions": [1]})


class TestFixedHlsSlots(unittest.TestCase):
    """A missing HLS channel must not move the others, and the TV must get
    the channel whose PIDs it asked for - also behind a gap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        B.opts = types.SimpleNamespace(
            bundle_size=8, bundle_base_hz=114_000_000,
            bundle_step_hz=8_000_000, onid=1, source="init7", hls_lcn=301,
            rescan_pmt=False, probe_seconds=0.1, scan_grace=1200,
            nit_mtype="256qam", nit_sr=6900,
            pmt_cache=os.path.join(self.tmp.name, "pmt.json"))
        info = {"pcr_pid": 0x100,
                "streams": [(0x1B, 0x100, ""), (0x0F, 0x101, "")]}
        urls = [f"https://h{i}.example/x.m3u8" for i in range(10)]
        B.HLS_STATE.clear()
        B.HLS_STATE.update({"admitted": urls, "checks": {},
                            "info": {u: info for u in urls}})
        self.urls = urls

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        bundles, _ = B.build_bundle_plan(B.hls_rows())
        return {m["name"]: (f, m["pmt_pid"], m["out_sid"])
                for f, b in bundles.items() for m in b["members"]}, bundles

    def test_gap_keeps_positions(self):
        full, _ = self.plan()
        del B.HLS_STATE["info"][self.urls[3]]          # channel 304 missing
        gap, _ = self.plan()
        self.assertNotIn(self.urls[3], gap)
        for name, where in gap.items():
            self.assertEqual(where, full[name], name)

    def test_pids_behind_gap_select_the_right_channel(self):
        del B.HLS_STATE["info"][self.urls[3]]
        _, bundles = self.plan()
        freq = min(bundles)
        st = B.BundleSession("127.0.0.1", 40000, "freq=114.00", set(),
                             bundles[freq])
        st.pids = {0, 16, 17, 0x150, 0x151}           # slot 6 -> number 306
        idx = st.wanted_slots()
        st.out.close()
        names = [bundles[freq]["members"][i]["name"] for i in idx]
        self.assertEqual(names, [self.urls[5]])


class TestDeviceRegistry(unittest.TestCase):
    """Windows, phones and media players fetch the description of every
    UPnP device; they must not end up as TVs. Only RTSP registers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        B.opts = types.SimpleNamespace(
            devices=os.path.join(self.tmp.name, "devices.json"),
            new_profile="strict")
        B.DEVICES.clear()
        B._profile_cache.clear()
        B._PENDING_NAMES.clear()
        B._devices_stamp[:] = [0.0, 0.0]
        self._mac_of = B.mac_of
        B.mac_of = lambda ip: "02:00:00:00:00:0" + ip[-1]

    def tearDown(self):
        B.mac_of = self._mac_of
        self.tmp.cleanup()

    def test_description_only_does_not_register(self):
        B.device_report("192.168.1.7", {})
        self.assertFalse(B.is_mirror("192.168.1.7", create=False))
        self.assertEqual(B.DEVICES, {})

    def test_rtsp_registers_with_name_from_description(self):
        reg = __import__("base64").b64encode(b"58DX804\x00\x00").decode()
        B.device_report("192.168.1.8", {"X-PANASONIC-Registration": reg})
        self.assertEqual(B.DEVICES, {})
        B.device("192.168.1.8")                       # as on RTSP SETUP
        entry = B.DEVICES["02:00:00:00:00:08"]
        self.assertEqual((entry["name"], entry["profile"], entry["last_ip"]),
                         ("58DX804", "strict", "192.168.1.8"))


class TestSettings(unittest.TestCase):

    def test_url_check(self):
        self.assertEqual(B._check_url(" https://x/y.m3u "), "https://x/y.m3u")
        self.assertIsNone(B._check_url(""))
        with self.assertRaises(ValueError):
            B._check_url("ftp://x")


if __name__ == "__main__":
    unittest.main()
