#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hotdobb - m3u2satip, see LICENSE
"""
satipbridge.py
==============
A SAT>IP server that turns an IPTV provider's multicast channels (Init7)
and, optionally, HLS channels from an M3U playlist into a fake DVB-C cable
network for TVs with a SAT>IP client ("DVB via IP", e.g. Panasonic).

History
-------
It started as a proxy in front of Tvheadend: Tvheadend packs seven TS
packets into every RTP packet and only sends once it is full. When a TV
asks for nothing but the table PIDs during a channel scan, they trickle in
so slowly that the first packet leaves after more than two seconds - the
TV waits only one second and gives up. The bridge fetched the full mux,
filtered it itself and sent every RTP packet at once, padded with null
packets like a real cable multiplex. Today it reads Init7's multicast
directly and Tvheadend is no longer needed (--source tvheadend still
exists).

Layout
------
    TV  --RTSP:554-->  satipbridge  <--multicast--  Init7
        <--RTP-------              <--HLS/ffmpeg--  M3U playlist (optional)

Running
-------
    sudo python3 satipbridge.py --source init7 --bundle-size 8

sudo (or CAP_NET_BIND_SERVICE, see satipbridge.service) is needed because
port 554 is below 1024. Stop with Ctrl+C.

Testing without a TV:
    python3 satip_probe.py --host <IP> --freq 306.00 --pids 0,1,2,16,17,18,20

Several channels per frequency
------------------------------
With --bundle-size 8 up to 8 channels share one virtual frequency, just
like a real cable operator does. That makes ALL channels reachable (not
only those that fit below 858 MHz) and the channel scan is faster, because
the TV has far fewer frequencies to visit.

On the first start the bridge briefly samples every channel once to learn
its video/audio PIDs (several minutes, depending on the number of
channels). The result is cached in pmt_cache.json, so later restarts are
quick. --rescan-pmt discards the cache and samples again (e.g. when the
provider's channel list changed).

To try things without disturbing the real TV, start with --no-ssdp (the
bridge then does not announce itself) and query a bundle frequency with
satip_probe.py instead.
"""

import argparse
import base64
import html
import json
import os
import random
import re
import select
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

# Shown on every start, so you always know which version is running -
# increase it whenever a new version goes out.
BRIDGE_VERSION = "17 (only RTSP registers devices)"

TS = 188
NULL_TS = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xFF" * 184
TS_PER_RTP = 7
DEVICE_TYPE = "urn:ses-com:device:SatIPServer:1"
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

opts = None
BOOTID = int(time.time())

# ----------------------------------------------------------------------
# Programme guide (EPG)
# ----------------------------------------------------------------------
# It is an extra, not part of watching TV: if satipepg.py is missing or the
# source cannot be reached, the bridge keeps running unchanged. Hence the
# soft import and the EPG_FLAGS list - it stays at 0 while nothing is
# loaded, and the SDT then honestly reports "no programme guide".
try:
    import satipepg
except Exception as _e:                       # pragma: no cover
    satipepg = None
    _EPG_IMPORT_ERROR = _e
else:
    _EPG_IMPORT_ERROR = None

EPG = None
EPG_FLAGS = [0]

# CONFIGID tells a UPnP client whether the device description changed. If
# the value changes, the TV throws away everything it knows about this
# server and needs a new channel scan.
#
# It used to be derived from the VERSION STRING. That was a mistake: the
# number then changes with every code change, even when not a single letter
# of the description differs. On one afternoon with six versions that meant
# "forget everything" six times - and the running TV was left without
# channels twice. The fault was in the design, not in the device.
#
# Now it is a fixed value - exactly the one the TVs currently know, so
# nobody has to scan again because of it. If the description really
# changes, increase it by hand - or pass another one with --config-id.
CONFIGID = 1803910
sessions = {}
sessions_lock = threading.Lock()


# The last log lines are also kept, so the control page in the browser can
# show the same as the terminal.
LOG_RING = []
LOG_SEQ = 0
LOG_LOCK = threading.Lock()
LOG_MAX = 500


def log(*a):
    global LOG_SEQ
    line = time.strftime("[%H:%M:%S] ") + " ".join(str(x) for x in a)
    print(line, file=sys.stderr, flush=True)
    with LOG_LOCK:
        LOG_SEQ += 1
        LOG_RING.append((LOG_SEQ, line))
        if len(LOG_RING) > LOG_MAX:
            del LOG_RING[:len(LOG_RING) - LOG_MAX]


def log_since(seq):
    """Returns (new_lines, last_number) - for the live log on the control
    page, without sending everything again each time."""
    with LOG_LOCK:
        return [t for n, t in LOG_RING if n > seq], LOG_SEQ


# ----------------------------------------------------------------------
# RTSP client towards Tvheadend
# ----------------------------------------------------------------------

class Upstream:
    """An RTSP session to Tvheadend that always fetches the full mux."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.cseq = 0
        self.session = None
        self.stream_id = None
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(10)
        self.lock = threading.Lock()

    def request(self, method, url, extra=None):
        with self.lock:
            self.cseq += 1
            lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}"]
            if self.session:
                lines.append(f"Session: {self.session}")
            if extra:
                lines.extend(extra)
            self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        text = data.decode(errors="replace")
        m = re.search(r"^Session:\s*([^;\r\n]+)", text, re.I | re.M)
        if m and not self.session:
            self.session = m.group(1).strip()
        return text

    def setup(self, tune, rtp_port):
        url = f"rtsp://{self.host}:{self.port}/?{tune}&pids=all"
        r = self.request("SETUP", url,
                         [f"Transport: RTP/AVP;unicast;"
                          f"client_port={rtp_port}-{rtp_port + 1}"])
        if not r.startswith("RTSP/1.0 200"):
            raise RuntimeError(r.splitlines()[0] if r else "no answer")
        m = re.search(r"com\.ses\.streamID\s*:\s*(\d+)", r, re.I)
        self.stream_id = m.group(1) if m else "1"
        return self.stream_id

    def play(self):
        return self.request(
            "PLAY", f"rtsp://{self.host}:{self.port}/stream={self.stream_id}")

    def retune(self, tune):
        r = self.request(
            "PLAY",
            f"rtsp://{self.host}:{self.port}/stream={self.stream_id}"
            f"?{tune}&pids=all")
        ok = r.startswith("RTSP/1.0 200")
        return ok, (r.splitlines()[0] if r else "no answer")

    def keepalive(self):
        return self.request(
            "OPTIONS",
            f"rtsp://{self.host}:{self.port}/stream={self.stream_id}")

    def teardown(self):
        try:
            self.request(
                "TEARDOWN",
                f"rtsp://{self.host}:{self.port}/stream={self.stream_id}")
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass



def patch_ids(pkt, tsid, onid):
    """Rewrites transport_stream_id and original_network_id in PAT and SDT
    to the values we announced in the NIT.

    Without this the TV drops the stream: the NIT promises it transport
    stream X from network Y, but the stream itself carries the provider's
    original identifiers."""
    pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
    if pid not in (0, 17) or not (pkt[1] & 0x40):
        return pkt
    afc = (pkt[3] >> 4) & 0x3
    idx = 4
    if afc in (2, 3):
        idx += 1 + pkt[4]
    if idx >= 187:
        return pkt
    s = idx + 1 + pkt[idx]            # past the pointer_field
    if s + 12 > 188:
        return pkt
    tid = pkt[s]
    if (pid == 0 and tid != 0x00) or (pid == 17 and tid != 0x42):
        return pkt
    seclen = ((pkt[s + 1] & 0x0F) << 8) | pkt[s + 2]
    end = s + 3 + seclen
    if end > 188 or seclen < 9:
        return pkt                    # section runs past the end of the packet
    b = bytearray(pkt)
    b[s + 3] = (tsid >> 8) & 0xFF
    b[s + 4] = tsid & 0xFF
    if pid == 17:
        b[s + 8] = (onid >> 8) & 0xFF
        b[s + 9] = onid & 0xFF
    b[end - 4:end] = struct.pack("!I", crc32_mpeg(bytes(b[s:end - 4])))
    return bytes(b)


# ----------------------------------------------------------------------
# Session: receives from Tvheadend, filters, sends to the TV
# ----------------------------------------------------------------------

class Session:
    counter = 0
    counter_lock = threading.Lock()

    def __init__(self, client_ip, client_rtp_port, tune, pids):
        with Session.counter_lock:
            Session.counter += 1
            self.stream_id = Session.counter
        self.sid = f"{random.randint(0x10000000, 0x7FFFFFFF):08X}"
        self.client_ip = client_ip
        self.client_rtp_port = client_rtp_port
        self.tune = tune
        self.pids = pids                 # set() or "all"
        self.running = False
        self.thread = None
        self.up = None
        self.up_sock = None
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = random.randint(0, 0xFFFF)
        self.ssrc = random.randint(0, 0xFFFFFFFF)
        self.sent_rtp = 0
        self.got_ts = 0
        self.kept_ts = 0
        self.started = time.time()
        self.last_touch = time.time()
        self.locked = False          # reception on this frequency?
        self.last_data = 0.0
        self.upstream_ok = False
        self.last_nit = 0.0
        self.nit_sent = 0
        self.cur_tsid = None
        self.stopped = False
        self.update_ids()

    def update_ids(self):
        """Which identifier did we announce in the NIT for the frequency that
        is currently tuned?"""
        self.cur_tsid = None
        for kv in self.tune.split("&"):
            if kv.startswith("freq="):
                try:
                    hz = int(round(float(kv[5:]) * 1_000_000))
                except ValueError:
                    return
                self.cur_tsid = TSID_BY_FREQ.get(hz)
                return

    # -- Connection to Tvheadend -------------------------------------

    def reopen_upstream(self, tune):
        """Rebuild the connection to Tvheadend from scratch (after a refused
        frequency change)."""
        try:
            if self.up:
                self.up.teardown()
        except Exception:
            pass
        for s in (getattr(self, "up_sock", None),
                  getattr(self, "up_rtcp", None)):
            try:
                s and s.close()
            except OSError:
                pass
        self.up_sock = None
        self.tune = tune
        self.update_ids()
        self.open_upstream()

    def open_upstream(self):
        # look for a free local port pair
        base = 41000
        while base < 42000:
            try:
                s1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s1.bind(("", base))
                s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s2.bind(("", base + 1))
                self.up_sock = s1
                self.up_rtcp = s2
                break
            except OSError:
                try:
                    s1.close()
                except Exception:
                    pass
                base += 2
        else:
            raise RuntimeError("no free port pair")

        self.up_sock.settimeout(0.02)
        try:
            self.up = Upstream(opts.tvh_host, opts.tvh_rtsp_port)
            self.up.setup(self.tune, base)
            self.up.play()
            self.upstream_ok = True
            log(f"  Session {self.stream_id}: Tvheadend delivers "
                f"({self.tune}) on local port {base}")
        except Exception as e:
            # Tvheadend knows nothing on this frequency. A real tuner would
            # not refuse the connection here but simply report "no
            # reception". That is what we do - otherwise the TV aborts its
            # channel scan.
            self.upstream_ok = False
            self.locked = False
            self.up = None
            log(f"  Session {self.stream_id}: no reception on "
                f"{self.tune}  ({e})")

    # -- RTCP: report tuner status ------------------------------------
    #
    # A SAT>IP client uses these reports to decide whether a frequency has
    # reception. Without them a channel scan gets stuck.
    # Layout per SAT>IP specification: SR + SDES + APP with name SES1.

    def status_string(self):
        p = {}
        for kv in self.tune.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                p[k] = v
        pids = ("all" if self.pids == "all"
                else ",".join(str(x) for x in sorted(self.pids or [])) or "none")
        lvl = opts.level if self.locked else 0
        qual = opts.quality if self.locked else 0
        # DVB-C format per SAT>IP spec, appendix D (see BundleSession):
        # ver=1.2;tuner=<feID>,<level>,<lock>,<quality>,<freq>,<bw>,<msys>,
        #         <mtype>,<sr>,<c2tft>,<ds>,<plp>,<specinv>;pids=...
        tuner = (f"{self.stream_id},{lvl},{1 if self.locked else 0},"
                 f"{qual},{p.get('freq','')},"
                 f"{p.get('bw','8')},"
                 f"{p.get('msys','dvbc')},{p.get('mtype','256qam')},"
                 f"{p.get('sr','6900')},,,,"
                 f"{p.get('specinv','0')}")
        return f"ver=1.2;tuner={tuner};pids={pids}"

    @staticmethod
    def _pad4(b):
        r = len(b) % 4
        return b + b"\x00" * (4 - r) if r else b

    def rtcp_packet(self):
        ssrc = self.ssrc
        now = time.time()
        ntp = int(now) + 2208988800
        frac = int((now % 1) * (1 << 32))
        sr = struct.pack("!BBHIIIII", 0x80, 200, 6, ssrc, ntp, frac,
                         int((now - self.started) * 90000) & 0xFFFFFFFF,
                         self.sent_rtp & 0xFFFFFFFF) + struct.pack(
            "!I", (self.sent_rtp * 1316) & 0xFFFFFFFF)

        cname = b"satipbridge"
        item = self._pad4(bytes([1, len(cname)]) + cname + b"\x00")
        sdes = struct.pack("!BBH", 0x81, 202, (4 + len(item)) // 4) + \
            struct.pack("!I", ssrc) + item

        app = rtcp_app(ssrc, self.status_string(),
                       norm=strict_cached(self))

        return sr + sdes + app

    def rtcp_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = self.client_rtp_port + 1
        while self.running:
            try:
                sock.sendto(self.rtcp_packet(), (self.client_ip, port))
            except OSError:
                pass
            # The SAT>IP spec asks for five status reports per second.
            # We used to send two - the TV then learns more slowly that
            # the tuner has locked.
            time.sleep(0.2)
        try:
            sock.close()
        except OSError:
            pass

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()
        threading.Thread(target=self.rtcp_loop, daemon=True).start()

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.running = False
        if self.up:
            self.up.teardown()
        for s in (getattr(self, "up_sock", None), getattr(self, "up_rtcp", None)):
            try:
                s and s.close()
            except OSError:
                pass
        try:
            self.out.close()
        except OSError:
            pass
        dur = time.time() - self.started
        log(f"  Session {self.stream_id} ended after {dur:.0f}s: "
            f"{self.got_ts} TS in, {self.kept_ts} kept, "
            f"{self.sent_rtp} RTP out, NIT sent {self.nit_sent}x")

    # -- Core: filter and forward at once ----------------------------

    def send_rtp(self, packets):
        """packets: list of exactly TS_PER_RTP TS packets."""
        ts90 = int((time.time() - self.started) * 90000) & 0xFFFFFFFF
        hdr = struct.pack("!BBHII", 0x80, 33, self.seq & 0xFFFF,
                          ts90, self.ssrc)
        self.seq = (self.seq + 1) & 0xFFFF
        if strict_cached(self):
            if not hasattr(self, "_cc"):
                self._cc = {}
            packets = restamp_cc(packets, self._cc)
        try:
            self.out.sendto(hdr + b"".join(packets),
                            (self.client_ip, self.client_rtp_port))
            self.sent_rtp += 1
        except OSError as e:
            log(f"  ! sending to {self.client_ip}:{self.client_rtp_port}: {e}")

    def wanted(self, pkt):
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        # Init7's NIT describes their network, not ours. We replace it
        # with our own.
        if pid == 16 and NIT_PACKETS:
            return False
        if self.pids == "all":
            return True
        return pid in self.pids

    def wants_nit(self):
        return bool(NIT_PACKETS) and (
            self.pids == "all" or 16 in (self.pids or set()))

    def pump(self):
        buf = []
        last_flush = time.time()
        last_keepalive = time.time()

        while self.running:
            now = time.time()

            sock = self.up_sock
            if sock is None:          # currently being rebuilt
                time.sleep(0.02)
                continue
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                data = None
            except OSError:
                if self.up_sock is not sock:
                    continue          # was replaced, carry on
                break

            # counts as locked while data is flowing
            if data:
                self.last_data = now
                if not self.locked:
                    self.locked = True
            elif self.locked and now - self.last_data > 1.5:
                self.locked = False

            if data and len(data) >= 12 and (data[0] >> 6) == 2:
                hlen = 12 + 4 * (data[0] & 0x0F)
                if data[0] & 0x10 and len(data) >= hlen + 4:
                    hlen += 4 + 4 * ((data[hlen + 2] << 8) | data[hlen + 3])
                payload = data[hlen:]
                for i in range(0, len(payload) - (TS - 1), TS):
                    pkt = payload[i:i + TS]
                    if pkt[0] != 0x47:
                        continue
                    self.got_ts += 1
                    if self.wanted(pkt):
                        self.kept_ts += 1
                        if self.cur_tsid is not None:
                            pkt = patch_ids(pkt, self.cur_tsid, opts.onid)
                        buf.append(pkt)

            # Inject our own NIT while there is reception. That is how the
            # TV learns all the other frequencies of the network.
            if (self.locked and self.wants_nit()
                    and (now - self.last_nit) * 1000 >= opts.nit_interval):
                self.last_nit = now
                buf.extend(NIT_PACKETS)
                # Send the time along - without it the TV refuses every
                # recording for lack of a system clock.
                buf.extend(time_packets())
                self.nit_sent += 1

            # full packets out at once
            while len(buf) >= TS_PER_RTP:
                self.send_rtp(buf[:TS_PER_RTP])
                buf = buf[TS_PER_RTP:]
                last_flush = time.time()

            # After a short time pad the rest with null packets and send.
            # This is the actual trick: a real cable multiplex is padded
            # with null packets as well, and the TV discards them.
            if buf and (now - last_flush) * 1000 >= opts.flush_ms:
                pad = [NULL_TS] * (TS_PER_RTP - len(buf))
                self.send_rtp(buf + pad)
                buf = []
                last_flush = now

            # keep the session at Tvheadend alive
            if self.up and now - last_keepalive > 10:
                last_keepalive = now
                try:
                    self.up.keepalive()
                except Exception as e:
                    log(f"  ! Connection to Tvheadend lost: {e}")
                    self.upstream_ok = False
                    self.up = None

        try:
            self.stop()
        except Exception:
            pass


# ----------------------------------------------------------------------
# Bundle session: several channels on one frequency
# ----------------------------------------------------------------------
#
# Just like Session, except that several sources are fetched at once (one
# per bundled channel), their video/audio PIDs are renumbered without
# collisions, and a home-made PAT/PMT/SDT announces all channels of the
# bundle together - exactly like a real cable multiplex carries several
# programmes on one frequency.

class BundleSession:
    counter = 0
    counter_lock = threading.Lock()

    def __init__(self, client_ip, client_rtp_port, tune, pids, bundle):
        with BundleSession.counter_lock:
            BundleSession.counter += 1
            self.stream_id = BundleSession.counter
        self.sid = f"{random.randint(0x10000000, 0x7FFFFFFF):08X}"
        self.client_ip = client_ip
        self.client_rtp_port = client_rtp_port
        self.tune = tune
        self.pids = pids                 # set() or "all"
        self.bundle = bundle             # entry from BUNDLES
        self.running = False
        self.thread = None
        self.upstreams = []              # [{sock, rtcp, up, member}, ...]
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = random.randint(0, 0xFFFF)
        self.ssrc = random.randint(0, 0xFFFFFFFF)
        self.sent_rtp = 0
        self.got_ts = 0
        self.kept_ts = 0
        self.started = time.time()
        self.last_touch = time.time()
        self.locked = False
        self.last_data = 0.0
        self.upstream_ok = False
        self.last_tables = 0.0
        self.tables_sent = 0
        self.stopped = False
        # Programme guide: own continuity counter for PID 18 and the
        # position the schedule has reached (it cycles round).
        self._eit_cc = [0]
        self._epg_band = None
        self._epg_band_nr = -1
        self._epg_pos = 0
        # Zapping: a frequency change is held back briefly, see retune()
        self.switching = False
        self._pending = None
        self._retune_lock = threading.Lock()
        # RLock, not Lock: _apply_retune already holds this lock and
        # then calls sync_upstreams, which wants to take it again. With
        # an ordinary lock the thread waits for itself, never returns -
        # and from then on blocks every further channel change. An
        # RLock may be taken several times by the same thread.
        self._apply_lock = threading.RLock()
        self._retune_timer = None
        self._grace_timer = None
        self._tuned_at = time.time()
        self._last_rebuild = 0.0

    # -- Connections to the sources, one per bundled channel --------

    def _free_port_pair(self, start, end):
        base = start
        while base < end:
            try:
                s1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s1.bind(("", base))
                s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s2.bind(("", base + 1))
                return s1, s2
            except OSError:
                try:
                    s1.close()
                except Exception:
                    pass
                base += 2
        raise RuntimeError("no free port pair")

    def wanted_slots(self):
        """Which bundle slots does the TV really want to see?

        During a scan it only asks for the tables (PID 0, 1, 16, 17, 18,
        20). We build those ourselves - that takes not a single multicast
        join. Only when it orders video and audio, i.e. PIDs from 0x100
        up, does it actually want to watch, and only then do we fetch the
        stream - and only the one it asked for.

        That is the difference between 243 joins for one scan and none
        at all.
        """
        if self.pids == "all":
            return set(range(len(self.bundle["members"])))
        desired = set()
        # Find the channel by its OWN PMT PID, not by position: with fixed
        # HLS slots a bundle can have a gap (a channel that is missing), and
        # then "slot 5" is not the fifth entry of the member list.
        by_pmt = {m["pmt_pid"]: i
                  for i, m in enumerate(self.bundle["members"])}
        for pid in (self.pids or set()):
            if pid >= 0x0100:
                pmt = 0x0100 + ((pid - 0x0100) // 0x10) * 0x10
                if pmt in by_pmt:
                    desired.add(by_pmt[pmt])
        if (not desired and self.wants_tables()
                and time.time() - self._tuned_at >= opts.scan_grace / 1000.0):
            # The TV only asks for the tables - so it is scanning.
            # It does not work entirely without a stream, though: it wants
            # to see a live multiplex before it moves on to the next
            # frequency. Pure null packets give 0.3 Mbit/s without any
            # time reference, and then it gets stuck.
            # So we fetch ONE stream instead of all of them - a fraction
            # of the joins and of the load.
            #
            # But only after a short pause: when zapping, the TV says
            # within a second which of the channels it wants to see. If we
            # joined the first one at once we would have to leave it again
            # straight away and join the right group - two joins in a row,
            # and exactly that cost three to four seconds for channels in
            # slots two and three.
            return set(range(min(opts.scan_streams,
                                 len(self.bundle["members"]))))
        return desired

    def _arm_grace(self):
        """Look again after the pause - in case the TV orders nothing at
        all, i.e. is scanning."""
        if self._grace_timer is not None:
            self._grace_timer.cancel()
        t = threading.Timer(opts.scan_grace / 1000.0 + 0.05,
                            self._grace_check)
        t.daemon = True
        self._grace_timer = t
        t.start()

    def _grace_check(self):
        if not self.running:
            return
        try:
            self.sync_upstreams()
        except Exception as e:
            log(f"  ! Join after pause: {e}")

    def sync_upstreams(self):
        """Align the joins with what the TV wants: open missing ones,
        leave superfluous ones, leave the rest untouched."""
        with self._apply_lock:
            target_slots = self.wanted_slots()
            present = {u["slot"] for u in self.upstreams if "slot" in u}
            if target_slots == present:
                return
            too_many = [u for u in self.upstreams
                       if u.get("slot") not in target_slots]
            if too_many:
                kept = [u for u in self.upstreams
                            if u.get("slot") in target_slots]
                self._close_some(too_many)
                self.upstreams = kept
            absent = sorted(target_slots - {u["slot"] for u in self.upstreams
                                     if "slot" in u})
            if absent:
                self.open_upstreams(only=absent, supplement=True)

    def _close_some(self, items):
        for u in items:
            mc = u.get("mc")
            if mc is not None:
                mc.close()
                continue
            for sk in (u.get("sock"), u.get("rtcp")):
                try:
                    sk and sk.close()
                except OSError:
                    pass
            up = u.get("up")
            if up:
                threading.Thread(target=up.teardown, daemon=True).start()

    def open_upstreams(self, only=None, supplement=False):
        # One after the other, 8 RTSP connections to Tvheadend would be
        # too slow (the TV waits only about a second per frequency) - so
        # open them all at once in their own threads.
        results = [None] * len(self.bundle["members"])
        # While open, finished connections are taken over. After that a
        # latecomer unsubscribes itself instead of running on without an
        # owner - see below.
        pending = [True]
        pending_lock = threading.Lock()

        def connect_one(i, m):
            if opts.source == "init7":
                # Directly at the multicast group: no ports, no RTSP, no
                # middleman. One join, done. HLS channels come through ffmpeg
                # but look exactly the same afterwards.
                try:
                    if m.get("hls"):
                        src = HlsSource(m["url"], m["name"], m.get("headers"))
                    else:
                        src = McSource(m["group"], m["port"], opts.ip)
                except Exception as e:
                    log(f"  ! {m['name']}: connection failed ({e})")
                    return
                with pending_lock:
                    if pending[0]:
                        results[i] = {"sock": src.sock, "rtcp": None,
                                      "up": None, "mc": src, "member": m,
                                      "slot": i}
                        return
                log(f"  ! {m['name']}: connected too late, leaving again")
                src.close()
                return
            try:
                s1, s2 = self._free_port_pair(44000, 45000)
            except RuntimeError as e:
                log(f"  ! {m['name']}: {e}")
                return
            s1.settimeout(0.02)
            up = None
            try:
                up = Upstream(opts.tvh_host, opts.tvh_rtsp_port)
                up.setup(m["tune_real"], s1.getsockname()[1])
                up.play()
            except Exception as e:
                log(f"  ! {m['name']}: no reception ({e})")
                # Important: a SETUP that went through has already created a
                # session at Tvheadend. If only the PLAY fails, it must go
                # again - otherwise it stays there forever.
                if up is not None:
                    threading.Thread(target=up.teardown, daemon=True).start()
                for sk in (s1, s2):
                    try:
                        sk.close()
                    except OSError:
                        pass
                return

            with pending_lock:
                if pending[0]:
                    results[i] = {"sock": s1, "rtcp": s2, "up": up,
                                  "member": m}
                    return
            # Arrived too late: nobody knows this connection any more, so
            # it cleans itself up.
            log(f"  ! {m['name']}: connected too late, unsubscribing")
            threading.Thread(target=up.teardown, daemon=True).start()
            for sk in (s1, s2):
                try:
                    sk.close()
                except OSError:
                    pass

        slot_grid = (list(range(len(self.bundle["members"])))
                   if only is None else list(only))
        threads = []
        for i in slot_grid:
            m = self.bundle["members"][i]
            t = threading.Thread(target=connect_one, args=(i, m), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=2.0)
        with pending_lock:
            pending[0] = False

        new_items = [r for r in results if r is not None]
        self.upstreams = (self.upstreams + new_items) if supplement else new_items
        self.last_data = time.time()     # give the watchdog time to start
        names = [u["member"]["name"] for u in self.upstreams]
        self.upstream_ok = True
        with MC_LOCK:
            open_items = len(MC_OPEN)
        addition = f", {open_items} groups open" if opts.source == "init7" else ""
        if not slot_grid:
            log(f"  Bundle session {self.stream_id}: tables only, "
                f"no stream needed (0 joins){addition}")
        else:
            log(f"  Bundle session {self.stream_id}: "
                f"{len(self.upstreams)}/{len(slot_grid)} channels "
                f"connected ({', '.join(names)}){addition}")

    def close_upstreams(self):
        old = self.upstreams
        self.upstreams = []
        for u in old:
            mc = u.get("mc")
            if mc is not None:
                # Multicast: leave the group properly, so the router stops
                # forwarding at once.
                mc.close()
                continue
            # Close our own receive sockets AT ONCE (purely local, no
            # network access needed) - that stops reception immediately
            # and frees the port.
            for s in (u.get("sock"), u.get("rtcp")):
                try:
                    s and s.close()
                except OSError:
                    pass
            # Politely unsubscribe from Tvheadend (TEARDOWN) only in the
            # background, WITHOUT waiting for it. If Tvheadend does not answer
            # at once for some channel, this would otherwise block for up to
            # 10 seconds per channel - one after the other for all bundle
            # members, which quickly adds up to several minutes.
            up = u.get("up")
            if up:
                threading.Thread(target=up.teardown, daemon=True).start()

    def retune(self, new_tune, new_bundle):
        """Frequency change - deliberately with a short delay.

        When zapping quickly the TV sends several changes per second. If
        we carried out each one at once, the bridge would tear down three
        multicast groups and join three new ones every time - nine
        changes in two seconds are fifty-four joins and leaves. That
        brings line and router to their knees, and afterwards even the
        last chosen frequency gets no more data.

        Therefore: remember the target, wait briefly, and only carry out
        the last wish. Whoever zaps through creates one single change
        instead of nine.
        """
        with self._retune_lock:
            self._pending = (new_tune, new_bundle)
            self.tune = new_tune          # confirm to the TV at once
            self.locked = False
            self.switching = True         # no tables during the transition
            if self._retune_timer is not None:
                self._retune_timer.cancel()
            self._retune_timer = threading.Timer(
                max(0, opts.zap_delay) / 1000.0, self._apply_retune)
            self._retune_timer.daemon = True
            self._retune_timer.start()

    def _apply_retune(self):
        # Only one at a time. The timer cannot be cancelled once it has
        # started - so when zapping quickly, two channel changes can
        # overtake each other. Without this lock the second would simply
        # overwrite the connections of the first; those of the first would
        # stay open, never be left and keep pulling data until the end of
        # the programme.
        with self._apply_lock:
            self._apply_retune_inner()

    def _apply_retune_inner(self):
        with self._retune_lock:
            target_slots = self._pending
            self._pending = None
        if target_slots is None or not self.running:
            return
        tune, bundle = target_slots
        if bundle is self.bundle and self.upstreams:
            # The TV has landed where it already was - then touch
            # nothing at all.
            self.switching = False
            self.last_tables = 0.0
            return
        self.close_upstreams()
        self.tune = tune
        self.bundle = bundle
        self.last_tables = 0.0
        self._last_rebuild = time.time()
        self._tuned_at = time.time()
        self.switching = False
        self._arm_grace()
        # Only join if the TV has already asked for video and audio on
        # this frequency. It does not do that during a scan.
        try:
            self.sync_upstreams()
        except Exception as e:
            log(f"  ! Join after retune: {e}")

    # -- RTCP: report tuner status (as in Session) ---------------------

    def status_string(self):
        p = {}
        for kv in self.tune.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                p[k] = v
        pids = ("all" if self.pids == "all"
                else ",".join(str(x) for x in sorted(self.pids or [])) or "none")
        lvl = opts.level if self.locked else 0
        qual = opts.quality if self.locked else 0
        # For DVB-C the SAT>IP spec (appendix D, version 1.2.2) prescribes
        # its OWN format - not the DVB-S one:
        #
        #   ver=1.2;tuner=<feID>,<level>,<lock>,<quality>,<freq>,<bw>,
        #           <msys>,<mtype>,<sr>,<c2tft>,<ds>,<plp>,<specinv>;pids=...
        #   Example: ver=1.2;tuner=1,240,1,7,444.00,8,dvbc,256qam,6900,,,,0
        #
        # We used to send the DVB-S format with ver=1.0. In it, the place
        # where DVB-C expects the symbol rate holds something entirely
        # different - so the TV read a nonsensical symbol rate and an
        # empty field as modulation.
        tuner = (f"{self.stream_id},{lvl},{1 if self.locked else 0},"
                 f"{qual},{p.get('freq','')},"
                 f"{p.get('bw','8')},"
                 f"{p.get('msys','dvbc')},{p.get('mtype','256qam')},"
                 f"{p.get('sr','6900')},,,,"
                 f"{p.get('specinv','0')}")
        return f"ver=1.2;tuner={tuner};pids={pids}"

    @staticmethod
    def _pad4(b):
        r = len(b) % 4
        return b + b"\x00" * (4 - r) if r else b

    def rtcp_packet(self):
        ssrc = self.ssrc
        now = time.time()
        ntp = int(now) + 2208988800
        frac = int((now % 1) * (1 << 32))
        sr = struct.pack("!BBHIIIII", 0x80, 200, 6, ssrc, ntp, frac,
                         int((now - self.started) * 90000) & 0xFFFFFFFF,
                         self.sent_rtp & 0xFFFFFFFF) + struct.pack(
            "!I", (self.sent_rtp * 1316) & 0xFFFFFFFF)
        cname = b"satipbridge"
        item = self._pad4(bytes([1, len(cname)]) + cname + b"\x00")
        sdes = struct.pack("!BBH", 0x81, 202, (4 + len(item)) // 4) + \
            struct.pack("!I", ssrc) + item
        app = rtcp_app(ssrc, self.status_string(),
                       norm=strict_cached(self))
        return sr + sdes + app

    def rtcp_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        port = self.client_rtp_port + 1
        while self.running:
            try:
                sock.sendto(self.rtcp_packet(), (self.client_ip, port))
            except OSError:
                pass
            # The SAT>IP spec asks for five status reports per second.
            # We used to send two - the TV then learns more slowly that
            # the tuner has locked.
            time.sleep(0.2)
        try:
            sock.close()
        except OSError:
            pass

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()
        threading.Thread(target=self.rtcp_loop, daemon=True).start()

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.running = False
        self.close_upstreams()
        try:
            self.out.close()
        except OSError:
            pass
        dur = time.time() - self.started
        log(f"  Bundle session {self.stream_id} ended after {dur:.0f}s: "
            f"{self.got_ts} TS in, {self.kept_ts} kept, "
            f"{self.sent_rtp} RTP out, tables sent {self.tables_sent}x")

    def send_rtp(self, packets):
        ts90 = int((time.time() - self.started) * 90000) & 0xFFFFFFFF
        hdr = struct.pack("!BBHII", 0x80, 33, self.seq & 0xFFFF,
                          ts90, self.ssrc)
        self.seq = (self.seq + 1) & 0xFFFF
        if strict_cached(self):
            if not hasattr(self, "_cc"):
                self._cc = {}
            packets = restamp_cc(packets, self._cc)
        try:
            self.out.sendto(hdr + b"".join(packets),
                            (self.client_ip, self.client_rtp_port))
            self.sent_rtp += 1
        except OSError as e:
            log(f"  ! sending to {self.client_ip}:{self.client_rtp_port}: {e}")

    def epg_packets(self):
        """Programme guide packets for this round.

        Two kinds, deliberately treated differently:

        'now and next' is tiny - two sections per channel, together not
        even 20 packets - and is sent completely every round. That way
        the TV shows at once what is on when switching, without waiting
        for anything.

        The schedule, on the other hand, is big. Sending it completely
        every round would mean about 1.5 Mbit/s for eight channels - for
        a page you open once every few days. Instead a few packets travel
        along each round, in turn. A full cycle takes a few seconds; a
        real cable network does exactly the same, repeating its EIT in a
        loop.

        Nothing is sent unless the TV asked for PID 18 at all. During a
        scan it only asks for the tables it needs - then the guide costs
        nothing.
        """
        if EPG is None:
            return []
        if self.pids != "all" and satipepg.EIT_PID not in (self.pids or ()):
            return []
        outgoing = []
        member_list = self.bundle["members"]
        for m in member_list:
            p = EPG.pf(m["out_sid"])
            if p:
                outgoing.extend(p)

        # The schedule in turn: we remember where we are and take the
        # next packets from there - across all channels of the bundle,
        # as if it were one long ring.
        budget = max(0, int(opts.epg_packets))
        if budget:
            ring = self._epg_band
            if ring is None or self._epg_band_nr != EPG.build_no:
                ring = []
                for m in member_list:
                    ring.extend(EPG.plan(m["out_sid"]))
                self._epg_band = ring
                self._epg_band_nr = EPG.build_no
                self._epg_pos = 0
            if ring:
                for _ in range(min(budget, len(ring))):
                    outgoing.append(ring[self._epg_pos % len(ring)])
                    self._epg_pos += 1
        return satipepg.with_cc(outgoing, self._eit_cc) if outgoing else []

    def wants_tables(self):
        if self.pids == "all":
            return True
        return bool((self.pids or set()) & {0, 17, 18, 20})

    def pump(self):
        buf = []
        last_flush = time.time()
        last_keepalive = time.time()

        while self.running:
            now = time.time()
            # Always take a snapshot: during a retune self.upstreams can
            # change under us and old sockets get closed. select() on an
            # already closed socket raises an error - that is not a real
            # problem, just a timing accident, so simply skip this round
            # and read the then-current list next time.
            ups = self.upstreams
            rlist = [u["sock"] for u in ups]
            ready = []
            if rlist:
                try:
                    ready, _, _ = select.select(rlist, [], [], 0.02)
                except (OSError, ValueError):
                    ready = []
                    time.sleep(0.02)
            else:
                time.sleep(0.05)

            any_data = False
            for u in ups:
                if u["sock"] not in ready:
                    continue
                try:
                    data, _ = u["sock"].recvfrom(65535)
                except OSError:
                    continue
                if not data:
                    continue
                if u.get("mc") is not None:
                    # Init7 mostly delivers raw TS, some channels in RTP -
                    # ts_payload() recognises both.
                    payload = ts_payload(data)
                    if not payload:
                        continue
                    any_data = True
                else:
                    if len(data) < 12 or (data[0] >> 6) != 2:
                        continue
                    any_data = True
                    hlen = 12 + 4 * (data[0] & 0x0F)
                    if data[0] & 0x10 and len(data) >= hlen + 4:
                        hlen += 4 + 4 * ((data[hlen + 2] << 8)
                                         | data[hlen + 3])
                    payload = data[hlen:]
                remap = u["member"]["pid_map"]
                for i in range(0, len(payload) - (TS - 1), TS):
                    pkt = payload[i:i + TS]
                    if pkt[0] != 0x47:
                        continue
                    self.got_ts += 1
                    opid = ((pkt[1] & 0x1F) << 8) | pkt[2]
                    npid = remap.get(opid)
                    if npid is None:
                        continue        # not part of this channel's video/audio
                    if self.pids != "all" and npid not in self.pids:
                        continue
                    self.kept_ts += 1
                    b = bytearray(pkt)
                    b[1] = (b[1] & 0xE0) | (npid >> 8)
                    b[2] = npid & 0xFF
                    buf.append(bytes(b))

            if any_data:
                self.last_data = now
            # The bridge counts as locked as soon as it knows the frequency -
            # the TV needs this report before it asks for the tables at
            # all. Whether a stream is flowing right now does not matter;
            # it only decides from the tables which channel it wants.
            self.locked = not self.switching

            # Tables need no reception: we build PAT, PMT, SDT and NIT
            # ourselves from the cache. We used to wait until data came
            # in - and for that joined three multicast groups on every
            # frequency, just to be able to report "locked". A scan over 81
            # frequencies cost 243 joins that way. Now it costs none.
            if (not self.switching and self.wants_tables()
                    and (now - self.last_tables) * 1000 >= opts.nit_interval):
                self.last_tables = now
                buf.extend(self.bundle["pat_packets"])
                for pkts in self.bundle["pmt_packets"].values():
                    buf.extend(pkts)
                buf.extend(self.bundle["sdt_packets"])
                if NIT_PACKETS:
                    buf.extend(NIT_PACKETS)
                if strict_cached(self) and (
                        self.pids == "all" or CAT_PID in (self.pids or ())):
                    pkts, self._cat_cc = sections_to_ts(
                        [build_cat_section()], pid=CAT_PID,
                        cc=getattr(self, "_cat_cc", 0))
                    buf.extend(pkts)
                # Send the time along - without it the TV refuses every
                # recording for lack of a system clock.
                buf.extend(time_packets())
                buf.extend(self.epg_packets())
                self.tables_sent += 1

            while len(buf) >= TS_PER_RTP:
                self.send_rtp(buf[:TS_PER_RTP])
                buf = buf[TS_PER_RTP:]
                last_flush = time.time()

            if buf and (now - last_flush) * 1000 >= opts.flush_ms:
                pad = [NULL_TS] * (TS_PER_RTP - len(buf))
                self.send_rtp(buf + pad)
                buf = []
                last_flush = now
            elif (not buf and self.locked
                  and (now - last_flush) * 1000 >= opts.flush_ms):
                # A real cable multiplex transmits without pause, even when
                # there is nothing worth watching - the gaps are filled with
                # null packets. During a scan we deliberately fetch no stream
                # at all, so without this only a table packet would come every
                # 300 ms. To the TV that looks like a channel at the edge of
                # reception: it locks, becomes unsure, waits ten seconds.
                # With null packets the stream has no gaps and it is sure at
                # once.
                self.send_rtp([NULL_TS] * TS_PER_RTP)
                last_flush = now

            if now - last_keepalive > 10:
                last_keepalive = now
                for u in list(self.upstreams):
                    if u.get("up") is None:
                        continue          # multicast needs no keep-alive
                    try:
                        u["up"].keepalive()
                    except Exception as e:
                        log(f"  ! Connection to {u['member']['name']} "
                            f"lost: {e}")

            # Watchdog: connections exist but deliver nothing. That happens
            # when multicast delivery stalls briefly - Tvheadend then answers
            # the request with 200 OK and still sends no data. No data, no
            # tables; no tables, and the TV never asks for video and audio,
            # and everything stays silent. So rebuild by ourselves instead of
            # waiting. ffmpeg needs a few seconds for an HLS channel before
            # the first data arrives - the watchdog waits longer there.
            has_hls = any(getattr(u.get("mc"), "hls", False)
                            for u in self.upstreams)
            patience = 20 if has_hls else 6
            if (self.upstreams and not self.switching
                    and now - self.last_data > patience
                    and now - self._last_rebuild > 2 * patience):
                self._last_rebuild = now
                log(f"  Bundle session {self.stream_id}: no data for {patience} s "
                    f"- rebuilding connections")
                try:
                    with self._apply_lock:
                        bundle = self.bundle
                        self.close_upstreams()
                        self.bundle = bundle
                    self.sync_upstreams()
                except Exception as e:
                    log(f"  ! Rebuild failed: {e}")

        try:
            self.stop()
        except Exception:
            pass


# ----------------------------------------------------------------------
# RTSP server towards the TV
# ----------------------------------------------------------------------

def parse_pids(value):
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("all", "8192"):
        return "all"
    if v in ("none", ""):
        return set()
    out = set()
    for part in v.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def split_url(url):
    """Returns (path, parameter dict)."""
    m = re.match(r"rtsp://[^/?]+(/[^?]*)?(?:\?(.*))?$", url, re.I)
    if not m:
        return "/", {}
    path = m.group(1) or "/"
    params = {}
    if m.group(2):
        for kv in m.group(2).split("&"):
            if not kv:
                continue
            if "=" in kv:
                k, v = kv.split("=", 1)
            else:
                k, v = kv, ""
            params[k.lower()] = v
    return path, params


TUNE_KEYS = ("src", "freq", "pol", "msys", "mtype", "sr", "fec", "ro",
             "plts", "bw", "tmode", "gi", "fft", "plp", "t2id", "sm", "c2tft",
             "ds", "specinv")


def tune_string(params):
    parts = []
    for k in TUNE_KEYS:
        if k in params and params[k] != "":
            parts.append(f"{k}={params[k]}")
    return "&".join(parts)


def tune_freq_hz(tune):
    """Extracts the freq= value of a tune string in Hz (int), or None if
    there is none."""
    for kv in tune.split("&"):
        if kv.startswith("freq="):
            try:
                return int(round(float(kv[5:]) * 1_000_000))
            except ValueError:
                return None
    return None


def stop_fast(s):
    """Shut a session down at once without waiting for Tvheadend.

    Session.stop() unsubscribes from Tvheadend synchronously. If Tvheadend
    does not answer at once, that blocks for up to ten seconds - far too
    long in the middle of a channel scan. So here: close our own sockets
    locally at once (that stops reception immediately), and the
    unsubscribing continues in the background."""
    s.running = False
    if isinstance(s, BundleSession):
        s.close_upstreams()
    else:
        for sk in (getattr(s, "up_sock", None), getattr(s, "up_rtcp", None)):
            try:
                sk and sk.close()
            except OSError:
                pass
        up = getattr(s, "up", None)
        if up:
            threading.Thread(target=up.teardown, daemon=True).start()
        s.up = None
        s.up_sock = None
    try:
        s.out.close()
    except OSError:
        pass


def morph_session(old, new_tune, new_bundle):
    """Converts a running session into the other kind.

    During a scan the TV logs on with some old frequency (for example
    474 MHz from an earlier real cable scan) and then walks through the band
    with PLAY. So the session kind has to follow: if the target frequency
    is a bundle, a BundleSession is needed, otherwise an ordinary Session.

    Session number, stream ID, RTP sequence number and SSRC are carried
    over - for the TV it stays the same stream, it notices nothing.

    Returns the new session, or None if the conversion failed."""
    log("    Session kind changes to: "
        + ("bundle" if new_bundle else "Einzelsender"))

    # Shut the old one down first, so two sessions do not send to the
    # same client port at once and the TV gets packets of two streams
    # mixed up.
    try:
        stop_fast(old)
    except Exception as e:
        log(f"    (shutting down the old session: {e})")

    try:
        if new_bundle:
            new = BundleSession(old.client_ip, old.client_rtp_port,
                                new_tune, old.pids, new_bundle)
        else:
            new = Session(old.client_ip, old.client_rtp_port,
                          new_tune, old.pids)
        new.sid = old.sid
        new.stream_id = old.stream_id
        new.seq = old.seq
        new.ssrc = old.ssrc
        new.started = old.started
        new.last_touch = time.time()
        if new_bundle:
            new.open_upstreams()
        else:
            new.open_upstream()
    except Exception as e:
        log(f"  ! Session conversion failed: {e}")
        return None

    new.start()
    return new


class RtspHandler(socketserver.StreamRequestHandler):
    timeout = 120

    def reply(self, code, text, extra=None, cseq=0):
        lines = [f"RTSP/1.0 {code} {text}", f"CSeq: {cseq}"]
        if extra:
            lines.extend(extra)
        lines.append("")
        lines.append("")
        self.wfile.write("\r\n".join(lines).encode())
        self.wfile.flush()

    def handle(self):
        peer = self.client_address[0]
        # Log the knock as well, not only the finished command. A TV
        # that opens the connection and leaves again at once used to be
        # invisible - and "no line in the log" wrongly meant "it never
        # tried".
        log(f"{peer}  RTSP connection opened")
        try:
            self._loop(peer)
        finally:
            log(f"{peer}  RTSP connection closed")

    def _loop(self, peer):
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.decode(errors="replace").strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 3:
                log(f"{peer}  ! unintelligible line: {line[:120]!r}")
                return
            method, url = parts[0].upper(), parts[1]

            headers = {}
            while True:
                h = self.rfile.readline()
                if not h or h in (b"\r\n", b"\n"):
                    break
                h = h.decode(errors="replace").strip()
                if ":" in h:
                    k, v = h.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            cseq = headers.get("cseq", "0")
            sid = (headers.get("session", "") or "").split(";")[0].strip()
            log(f"{peer}  {method} {url}"
                + (f"  [session {sid}]" if sid else ""))

            try:
                self.dispatch(method, url, headers, cseq, sid, peer)
            except Exception as e:
                log(f"  ! Error: {e}")
                self.reply(500, "Internal Server Error", cseq=cseq)

    # -- the individual methods --------------------------------------

    def dispatch(self, method, url, headers, cseq, sid, peer):
        path, params = split_url(url)

        if method == "OPTIONS":
            if sid:
                with sessions_lock:
                    s = sessions.get(sid)
                if not s:
                    # We do not know this session (any more) - e.g. because the
                    # bridge was restarted in between and the TV still uses an
                    # old session number. Better to say honestly "unknown" than
                    # to fake OK - otherwise the TV waits needlessly long before
                    # it starts over by itself.
                    self.reply(454, "Session Not Found", cseq=cseq)
                    return
                s.last_touch = time.time()
                extra = ["Public: OPTIONS,DESCRIBE,SETUP,PLAY,TEARDOWN",
                        f"Session: {sid}"]
            else:
                extra = ["Public: OPTIONS,DESCRIBE,SETUP,PLAY,TEARDOWN"]
            self.reply(200, "OK", extra, cseq)
            return

        if method == "DESCRIBE":
            # The spec wants to see the actual tuner state here, in the
            # same format as in the RTCP reports:
            #   a=fmtp:33 ver=1.2;tuner=...;pids=...
            # We used to leave this line empty - but the TV asks exactly
            # for it when it wants to know whether waiting on this
            # frequency is worthwhile.
            with sessions_lock:
                all_sessions = list(sessions.values())
            match = None
            m_str = re.search(r"stream=(\d+)", url)
            if m_str:
                num_no = int(m_str.group(1))
                for st in all_sessions:
                    if st.stream_id == num_no:
                        match = st
                        break
            to_show = [match] if match else all_sessions
            media = ""
            for st in to_show:
                try:
                    tuner_state = st.status_string()
                except Exception:
                    tuner_state = ""
                media += (
                    "m=video 0 RTP/AVP 33\r\n"
                    "c=IN IP4 0.0.0.0\r\n"
                    f"a=control:stream={st.stream_id}\r\n"
                    f"a=fmtp:33 {tuner_state}\r\n"
                    "a=sendonly\r\n")
            if not media:
                media = ("m=video 0 RTP/AVP 33\r\n"
                          "c=IN IP4 0.0.0.0\r\n"
                          "a=control:stream=1\r\n"
                          "a=fmtp:33 \r\n"
                          "a=sendonly\r\n")
            # Spec, DVB-C appendix: "<sat>,<terr>,<cable>" with commas. We
            # had spaces - a strict reader then sees only the first 0, i.e.
            # no tuner at all. Only for the "strict" profile.
            if strict(self.client_address[0]):
                frontends = f"0,0,{opts.tuners}"
            else:
                frontends = f"0 0 {opts.tuners}"
            sdp = (
                "v=0\r\n"
                f"o=- {int(time.time())} 1 IN IP4 {opts.ip}\r\n"
                f"s=SatIPServer:1 {frontends}\r\n"
                "t=0 0\r\n"
                + media
            ).encode()
            self.wfile.write(
                ("RTSP/1.0 200 OK\r\n"
                 f"CSeq: {cseq}\r\n"
                 "Content-Type: application/sdp\r\n"
                 f"Content-Base: rtsp://{opts.ip}/\r\n"
                 f"Content-Length: {len(sdp)}\r\n\r\n").encode() + sdp)
            self.wfile.flush()
            return

        if method == "SETUP":
            # A device that wants a stream is a TV: register it now (with
            # the model name it may have sent with the description).
            device(peer)
            transport = headers.get("transport", "")
            m = re.search(r"client_port=(\d+)", transport)
            if not m:
                self.reply(461, "Unsupported Transport", cseq=cseq)
                return
            cport = int(m.group(1))

            tune = tune_string(params)
            if not tune:
                self.reply(400, "Bad Request", cseq=cseq)
                return
            pids = parse_pids(params.get("pids")) or set()

            bundle = BUNDLES.get(tune_freq_hz(tune))
            if bundle:
                s = BundleSession(peer, cport, tune, pids, bundle)
                try:
                    # Only join once video and audio have been asked for.
                    s.sync_upstreams()
                except Exception as e:
                    log(f"  ! Bundle session could not be created: "
                        f"{e}")
                    self.reply(500, "Internal Server Error", cseq=cseq)
                    return
            else:
                s = Session(peer, cport, tune, pids)
                try:
                    s.open_upstream()
                except Exception as e:
                    log(f"  ! Session could not be created: {e}")
                    self.reply(500, "Internal Server Error", cseq=cseq)
                    return
            s.start()
            with sessions_lock:
                sessions[s.sid] = s

            self.reply(200, "OK", [
                f"Session: {s.sid};timeout=30",
                f"Transport: RTP/AVP;unicast;client_port={cport}-{cport + 1}",
                f"com.ses.streamID: {s.stream_id}",
            ], cseq)
            sessions_save()
            return

        if method == "PLAY":
            with sessions_lock:
                s = sessions.get(sid)
            if not s:
                self.reply(454, "Session Not Found", cseq=cseq)
                return
            s.last_touch = time.time()

            new_tune = tune_string(params)
            if new_tune and new_tune != s.tune:
                log(f"  Retune: {s.tune}  ->  {new_tune}")
                new_bundle = BUNDLES.get(tune_freq_hz(new_tune))
                is_bundle = isinstance(s, BundleSession)

                if bool(new_bundle) != is_bundle:
                    # The kind of session no longer matches the target frequency.
                    # That is normal during a scan: the TV starts with an old
                    # single frequency and then walks into the bundle band.
                    s2 = morph_session(s, new_tune, new_bundle)
                    if s2 is not None:
                        with sessions_lock:
                            sessions[s2.sid] = s2
                        s = s2
                    else:
                        s.upstream_ok = False
                elif is_bundle:
                    s.retune(new_tune, new_bundle)
                else:
                    s.tune = new_tune
                    s.locked = False
                    s.update_ids()
                    try:
                        if s.up:
                            ok, status = s.up.retune(new_tune)
                            log(f"    Tvheadend: {status}")
                        else:
                            ok = False
                        if not ok:
                            s.reopen_upstream(new_tune)
                    except Exception as e:
                        log(f"    Change failed ({e}), rebuilding")
                        try:
                            s.reopen_upstream(new_tune)
                        except Exception as e2:
                            log(f"  ! Rebuild failed: {e2}")
                            s.upstream_ok = False

            if "pids" in params:
                s.pids = parse_pids(params["pids"])
            if "addpids" in params and s.pids != "all":
                s.pids = (s.pids or set()) | (parse_pids(params["addpids"]) or set())
            if "delpids" in params and s.pids != "all":
                s.pids = (s.pids or set()) - (parse_pids(params["delpids"]) or set())

            n = "all" if s.pids == "all" else len(s.pids or [])
            log(f"  PIDs now: {n}")

            # Only here is it decided whether a stream has to be fetched
            # at all - and if so, which single one.
            if isinstance(s, BundleSession):
                try:
                    s.sync_upstreams()
                except Exception as e:
                    log(f"  ! Aligning joins: {e}")

            self.reply(200, "OK", [
                f"Session: {s.sid}",
                f"RTP-Info: url=rtsp://{opts.ip}/stream={s.stream_id}",
            ], cseq)
            sessions_save()
            return

        if method == "TEARDOWN":
            with sessions_lock:
                s = sessions.pop(sid, None)
            if s:
                s.running = False
            self.reply(200, "OK", [f"Session: {sid}"] if sid else None, cseq)
            sessions_save()
            return

        self.reply(405, "Method Not Allowed", cseq=cseq)


class RtspServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class DescServer(socketserver.ThreadingTCPServer):
    """Web server for the device description and the channel list.

    allow_reuse_address MUST be a class attribute here: creating a server
    binds immediately, an assignment afterwards comes too late and never
    takes effect. Exactly that made every quick restart fail - port 55000
    stayed taken for up to a minute after stopping, and the new bridge quit
    with "Address already in use".
    """
    allow_reuse_address = True
    daemon_threads = True


# ----------------------------------------------------------------------
# Announcement on the network
# ----------------------------------------------------------------------

DDD = """<?xml version="1.0"?>
  <root xmlns="urn:schemas-upnp-org:device-1-0" configId="{configid}">
  <specVersion>
    <major>1</major>
    <minor>1</minor>
  </specVersion>
  <device>
    <deviceType>urn:ses-com:device:SatIPServer:1</deviceType>
    <friendlyName>{name}</friendlyName>
    <manufacturer>Tvheadend Bridge</manufacturer>
    <modelName>SAT&gt;IP Bridge</modelName>
    <modelNumber>1.0</modelNumber>
    <modelDescription></modelDescription>
    <serialNumber></serialNumber>
    <modelURL></modelURL>
    <manufacturerURL></manufacturerURL>
    <UDN>uuid:{uuid}</UDN>
    <iconList>
      <icon>
        <mimetype>image/png</mimetype>
        <width>48</width><height>48</height><depth>24</depth>
        <url>dlna_icon_48.png</url>
      </icon>
    </iconList>
    <satip:X_SATIPCAP xmlns:satip="urn-ses-com:satip">DVBC-{tuners}</satip:X_SATIPCAP>
    <satip:X_SATIPM3U xmlns:satip="urn-ses-com:satip">/satipsrv/satip.m3u</satip:X_SATIPM3U>
    <satip:X_SATIPCAP xmlns:satip="urn:ses-com:satip">DVBC-{tuners}</satip:X_SATIPCAP>
    <satip:X_SATIPM3U xmlns:satip="urn:ses-com:satip">/satipsrv/satip.m3u</satip:X_SATIPM3U>
  </device>
</root>
"""

# Second version of the description, rebuilt line by line after the real
# Panasonic (read from the TX-58DX800W): the namespace exactly once, four
# icons, otherwise identical layout.
#
# Why two versions and not one: when the only description was switched to
# this pattern, the other TV (TX-40EX730) stopped working. What is right for
# one is wrong for the other - so each gets what it understands, chosen per
# device. A running TV must not pay for connecting a second one.
DDD_PANA = """<?xml version="1.0"?>
  <root xmlns="urn:schemas-upnp-org:device-1-0" configId="{configid}">
  <specVersion>
    <major>1</major>
    <minor>1</minor>
  </specVersion>
  <device>
    <deviceType>urn:ses-com:device:SatIPServer:1</deviceType>
    <friendlyName>{name}</friendlyName>
    <manufacturer>Panasonic</manufacturer>
    <modelName>Panasonic VIErA</modelName>
    <modelNumber>TX-58DX800W</modelNumber>
    <modelDescription></modelDescription>
    <serialNumber></serialNumber>
    <modelURL></modelURL>
    <manufacturerURL></manufacturerURL>
    <UDN>uuid:{uuid}</UDN>
    <iconList>
      <icon>
        <mimetype>image/png</mimetype>
        <width>48</width>
        <height>48</height>
        <depth>24</depth>
        <url>dlna_icon_48.png</url>
      </icon>
      <icon>
        <mimetype>image/png</mimetype>
        <width>120</width>
        <height>120</height>
        <depth>24</depth>
        <url>dlna_icon_120.png</url>
      </icon>
      <icon>
        <mimetype>image/jpeg</mimetype>
        <width>48</width>
        <height>48</height>
        <depth>24</depth>
        <url>dlna_icon_48.jpg</url>
      </icon>
      <icon>
        <mimetype>image/jpeg</mimetype>
        <width>120</width>
        <height>120</height>
        <depth>24</depth>
        <url>dlna_icon_120.jpg</url>
      </icon>
    </iconList>
    <satip:X_SATIPCAP xmlns:satip="urn-ses-com:satip">DVBC-{tuners}</satip:X_SATIPCAP>
    <satip:X_SATIPM3U xmlns:satip="urn-ses-com:satip">/satipsrv/satip.m3u</satip:X_SATIPM3U>
  </device>
</root>
"""


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class DescHandler(socketserver.StreamRequestHandler):
    def handle(self):
        req = self.rfile.readline().decode(errors="replace")
        self.headers = {}
        while True:
            h = self.rfile.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
            k, _, v = h.decode(errors="replace").partition(":")
            self.headers[k.strip()] = v.strip()
        pieces = req.split()
        verb = pieces[0] if pieces else "?"
        path = pieces[1] if len(pieces) > 1 else "/"
        # Log every request. Previously only the channel list download was
        # logged - which made it impossible to tell whether a TV never fetches
        # the device description or fetches it and then gives up. Those are
        # two entirely different faults, and a log that does not separate
        # them is no help when searching.
        bare = path.split("?")[0]
        # The control panel polls every two seconds - keep that out of the
        # log, otherwise the TVs' requests drown in it.
        if not (bare.startswith("/api/") or bare in ("/", "/panel")):
            log(f"  HTTP from {self.client_address[0]}: {verb} {path}")
        status = "200 OK"
        extra = ""
        if bare.startswith("/api/") or bare in ("/", "/panel"):
            blob = b""
            if verb == "POST":
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = 0
                blob = self.rfile.read(min(n, 65536)) if n > 0 else b""
            status, ctype, body = panel_request(
                verb, path, blob, self.headers, self.client_address[0])
            extra = "Cache-Control: no-store\r\n"
        elif bare.rstrip("/") in ("/devices", "/geraete"):
            # Control page for the device registry - for people in a browser,
            # not for TVs. That is why it comes BEFORE the description,
            # otherwise every browser would register itself as a device.
            blob = b""
            if verb == "POST":
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = 0
                blob = self.rfile.read(min(n, 4096)) if n > 0 else b""
                devices_form(blob, self.client_address[0])
                status, extra = "303 See Other", "Location: /devices\r\n"
                body, ctype = b"", "text/plain"
            else:
                body = devices_page().encode("utf-8")
                ctype = "text/html; charset=utf-8"
        elif path.endswith(".png") or path.endswith(".jpg"):
            body, ctype = TINY_PNG, "image/png"
        elif "satip.m3u" in path.lower():
            # The real Panasonic server offers a ready-made channel list here.
            # A client that reads it needs no scan at all - which makes this
            # download the most important hint in the whole log.
            body = M3U_TEXT.encode("latin-1", "replace")
            ctype = "audio/x-mpegurl"
            log(f"  >>> {self.client_address[0]} fetches the CHANNEL LIST "
                f"({M3U_COUNT} channels, {len(body)} bytes) <<<")
        else:
            device_report(self.client_address[0], self.headers)
            mirror = is_mirror(self.client_address[0], create=False)
            template = DDD_PANA if mirror else DDD
            if mirror:
                log("        (Panasonic-style description)")
            body = template.format(
                name=(opts.mirror_name or opts.name) if mirror
                     else opts.name,
                uuid=mirror_uuid() if mirror else opts.uuid,
                tuners=opts.mirror_tuners if mirror else opts.tuners,
                configid=1 if mirror else CONFIGID).encode()
            ctype = "text/xml"
        self.wfile.write(
            (f"HTTP/1.1 {status}\r\n"
             f"{extra}"
             f"Content-Type: {ctype}\r\n"
             f"Content-Length: {len(body)}\r\n"
             "Connection: close\r\n\r\n").encode() + body)
        self.wfile.flush()


# In a network every SAT>IP device ID may occur only once. We had a fixed
# 1 - and the Panasonic, itself a SAT>IP server, has 1 as well. From its
# point of view a second device claimed to be itself. The spec says the
# newcomer must notice and move aside; we simply never checked.
DEVICE_ID = [1]
MIRROR_IPS = set()


# ======================================================================
# Device registry: recognise TVs by MAC, not by IP
# ======================================================================
#
# The TX-58DX800W got .64 via DHCP first, then .65 - and so dropped out of
# --mirror-ip. The MAC stays. Every device that talks to us ends up with
# MAC, last IP and (for Panasonic) model name in devices.json. Per device:
#
#   profile  "strict"  spec-compliant stream: continuous TS counters,
#                      empty CAT, RTCP-APP and DESCRIBE per spec
#            "legacy"  exactly the behaviour of the first versions
#   mirror   true      gets the Panasonic-style device description
#                      (own UUID - NEVER change it afterwards, or the TV
#                      takes us for a new server and needs a new scan)
#
# New devices get --new-profile (default: strict). The file may be edited
# by hand; the bridge re-reads it when it changes.
# Entries with the key "ip:<address>" are placeholders for devices whose
# MAC is not known yet - they are rewritten to the MAC on first contact.

DEVICES = {}
_devices_lock = threading.Lock()
_devices_stamp = [0.0, 0.0]      # [file mtime, last check]
_profile_cache = {}               # ip -> (time, entry)


def _devices_path():
    return getattr(opts, "devices", None) or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "devices.json")


def mac_of(ip):
    """MAC for an IP from the kernel's ARP table, or None."""
    try:
        with open("/proc/net/arp") as f:
            next(f)
            for text_line in f:
                t = text_line.split()
                if len(t) >= 4 and t[0] == ip and t[2] != "0x0":
                    mac = t[3].lower()
                    if mac != "00:00:00:00:00:00":
                        return mac
    except OSError:
        pass
    return None


def _devices_load(force=False):
    fpath = _devices_path()
    try:
        mt = os.path.getmtime(fpath)
    except OSError:
        return
    if not force and mt == _devices_stamp[0]:
        return
    try:
        with open(fpath, encoding="utf-8") as f:
            blob = json.load(f)
        DEVICES.clear()
        DEVICES.update(blob)
        _devices_stamp[0] = mt
        _profile_cache.clear()
        log(f"Device registry loaded: {len(DEVICES)} entries")
    except (OSError, ValueError) as e:
        log(f"  ! {fpath} not readable ({e}) - keeping the previous state")


def _devices_save():
    fpath = _devices_path()
    tmp = fpath + ".new"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DEVICES, f, indent=2, ensure_ascii=False,
                      sort_keys=True)
            f.write("\n")
        os.replace(tmp, fpath)
        _devices_stamp[0] = os.path.getmtime(fpath)
    except OSError as e:
        log(f"  ! {fpath} not writable: {e}")


_PENDING_NAMES = {}      # ip -> model name seen before the device registered


def device(ip, name=None, create=True):
    """Entry for an IP; creates new devices (unless create=False).
    Never None."""
    now_ts = time.time()
    if name is None:
        c = _profile_cache.get(ip)
        if c and now_ts - c[0] < 30:
            return c[1]
    with _devices_lock:
        if now_ts - _devices_stamp[1] > 10:
            _devices_stamp[1] = now_ts
            _devices_load()
        mac = mac_of(ip)
        entry = None
        changed = False
        if mac and mac in DEVICES:
            entry = DEVICES[mac]
            if entry.get("last_ip") != ip:
                log(f"Device {entry.get('name') or mac}: new IP "
                    f"{entry.get('last_ip')} -> {ip}")
                entry["last_ip"] = ip
                changed = True
        elif mac and ("ip:" + ip) in DEVICES:
            entry = DEVICES.pop("ip:" + ip)
            entry["last_ip"] = ip
            DEVICES[mac] = entry
            log(f"Device {entry.get('name') or ip}: learned MAC {mac}")
            changed = True
        elif mac and not create:
            # A mere SSDP search or a look at the device description: do not
            # register. Windows' network view, phone apps and media players
            # fetch the description of every UPnP device on the LAN and
            # would all end up as "TVs". Only RTSP - wanting to watch -
            # registers a device. A model name seen here is kept for then.
            if name:
                _PENDING_NAMES[ip] = name
            return {"profile": getattr(opts, "new_profile", "strict"),
                    "mirror": False}
        elif mac:
            entry = {"profile": getattr(opts, "new_profile", "strict"),
                       "mirror": False, "last_ip": ip,
                       "first_seen": time.strftime("%Y-%m-%d %H:%M")}
            name = name or _PENDING_NAMES.pop(ip, None)
            DEVICES[mac] = entry
            log(f"NEW device {mac} at {ip} - profile "
                f"'{entry['profile']}' (change in {_devices_path()})")
            changed = True
        else:
            # MAC still unknown (happens with the very first SSDP packet):
            # go by the last known IP, otherwise do not create anything.
            for e in DEVICES.values():
                if e.get("last_ip") == ip:
                    entry = e
                    break
            if entry is None:
                return {"profile": getattr(opts, "new_profile", "strict"),
                        "mirror": False}
        if name and entry.get("name") != name:
            entry["name"] = name
            changed = True
        if mac:
            now_txt = time.strftime("%Y-%m-%d %H:%M")
            if entry.get("last_seen", "")[:13] != now_txt[:13]:
                entry["last_seen"] = now_txt      # accurate to the hour
                changed = True
        if changed:
            _devices_save()
        _profile_cache[ip] = (now_ts, entry)
        return entry


def strict(ip):
    return device(ip).get("profile") == "strict"


def strict_cached(st):
    """strict() for a session, looked up at most every 5 seconds.

    send_rtp() asks for every single RTP packet - about 600 times a second
    per watching TV. Looking up the device every time (and for some
    addresses reading the kernel's ARP table) cost more CPU than the video
    itself: three TVs used 93 % of a core. The profile changes rarely, and
    a switch in the control panel taking up to 5 s to arrive is fine."""
    now = time.time()
    until = getattr(st, "_strict_until", 0)
    if now >= until:
        st._strict = strict(st.client_ip)
        st._strict_until = now + 5
    return st._strict


def is_mirror(ip, create=True):
    return bool(device(ip, create=create).get("mirror"))


def device_forget(mac, who):
    with _devices_lock:
        entry = DEVICES.pop(mac, None)
        if entry is None:
            return
        _profile_cache.clear()
        _devices_save()
    log(f"Device FORGOTTEN: {entry.get('name') or mac} ({mac}, "
        f"last at {entry.get('last_ip')}) - via control page from {who}")


def devices_form(blob, who):
    fields = urllib.parse.parse_qs(blob.decode("utf-8", "replace"))
    if fields.get("action") == ["forget"]:
        for mac in fields.get("mac", []):
            device_forget(mac, who)
    elif fields.get("action") == ["forget_all"]:
        # Harmless: every TV registers itself again on next contact.
        # Only devices that had "mirror" get a different server identity
        # afterwards and need a new scan.
        for mac in list(DEVICES):
            device_forget(mac, who)


def devices_page():
    with _devices_lock:
        _devices_load()
        items = sorted(DEVICES.items(),
                       key=lambda kv: kv[1].get("last_seen", ""), reverse=True)
    e = html.escape
    html_rows = []
    for mac, g in items:
        name = g.get("name") or "unnamed"
        warn = ("This TV will need a NEW channel scan afterwards - "
                "it gets its own server identity. ") if g.get("mirror") else ""
        question = (f"Forget {name} ({mac})? {warn}If it shows up again, "
                 f"it is registered as a new device with profile "
                 f"'{getattr(opts, 'new_profile', 'strict')}'.")
        html_rows.append(f"""
<tr>
  <td><strong>{e(name)}</strong><div class="small">{e(g.get("note", ""))}</div></td>
  <td class="mono">{e(mac)}</td>
  <td class="mono">{e(g.get("last_ip", ""))}</td>
  <td>{e(g.get("profile", ""))}{" · Panasonic identity" if g.get("mirror") else ""}</td>
  <td>{e(g.get("last_seen", "–"))}</td>
  <td><form method="post" action="/devices" onsubmit='return confirm({json.dumps(question)})'>
    <input type="hidden" name="action" value="forget">
    <input type="hidden" name="mac" value="{e(mac)}">
    <button type="submit">Forget</button></form></td>
</tr>""")
    if not html_rows:
        html_rows.append('<tr><td colspan="6">No TVs registered yet.</td></tr>')
    with_identity = [g.get("name") or m for m, g in items if g.get("mirror")]
    confirm_all = ("Forget all TVs? Each one registers itself again "
                  "the next time it is switched on.")
    if with_identity:
        confirm_all += (" CAUTION: " + ", ".join(with_identity) +
                       " will get a different server identity and "
                       "need a NEW channel scan.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bridge · Devices</title>
<style>
:root{{--bg:#f6f6f4;--fl:#fff;--tx:#1d1d1b;--mt:#6b6b66;--ln:#e2e2dd;--ak:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fl:#20201e;--tx:#ececE8;--mt:#9a9a94;--ln:#33332f;--ak:#f2b8b5}}}}
body{{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:24px 16px}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:var(--mt);margin:0 0 18px}}
.panel{{background:var(--fl);border:1px solid var(--ln);border-radius:10px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--ln);vertical-align:top}}
th{{font-size:12px;color:var(--mt);font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
tr:last-child td{{border-bottom:0}}
.mono{{font-family:ui-monospace,monospace;font-size:13px}}
.small{{font-size:12px;color:var(--mt);margin-top:2px}}
button{{background:transparent;color:var(--ak);border:1px solid var(--ak);border-radius:6px;padding:5px 10px;font:inherit;cursor:pointer}}
button:hover{{background:var(--ak);color:var(--fl)}}
</style></head><body><main>
<h1>TVs on the bridge</h1>
<p>Recognised by MAC address. New devices register themselves, IP changes are tracked automatically.
Version {e(BRIDGE_VERSION)}.</p>
<div class="panel"><table>
<tr><th>Device</th><th>MAC</th><th>Last IP</th><th>Profile</th><th>Last seen</th><th></th></tr>
{"".join(html_rows)}
</table></div>
<form method="post" action="/devices" style="margin-top:16px"
  onsubmit='return confirm({json.dumps(confirm_all)})'>
  <input type="hidden" name="action" value="forget_all">
  <button type="submit">Forget all (reset)</button>
  <span class="small" style="margin-left:8px">Each TV registers itself again the next time it is switched on.</span>
</form>
</main></body></html>"""


# ======================================================================
# Control panel: panel.html plus a small JSON API
# ======================================================================
#
# GET  /                 panel.html (next to this script)
# GET  /api/state        status, health lights, TVs and what they watch
# GET  /api/log?since=N  log lines after sequence number N
# GET  /api/channels     the channel list as the TVs see it
# POST /api/restart      exit; systemd (Restart=always) starts us again
# POST /api/device       {"mac", "action": "forget" | "profile", "profile"}
# POST /api/forget_all
#
# Everything that changes something must carry the header "X-Bridge: 1".
# A browser only sends custom headers to another site after a CORS
# preflight, which this server never answers - so a foreign web page open
# in some browser on the LAN cannot press these buttons behind your back.
# Reading the panel never registers the browser as a device.

BRIDGE_STARTED = [time.time()]
_RATE_PREV = {}          # session id -> (time, sent_rtp)


def _session_info(st, now):
    """What a session is doing right now, for the panel."""
    members = (st.bundle["members"] if isinstance(st, BundleSession)
               else [])
    if st.pids == "all":
        watched = list(members)
    else:
        pmts = {0x0100 + ((p - 0x0100) // 0x10) * 0x10
                for p in (st.pids or ()) if p >= 0x0100}
        watched = [m for m in members if m["pmt_pid"] in pmts]
    prev = _RATE_PREV.get(st.sid)
    _RATE_PREV[st.sid] = (now, st.sent_rtp)
    kbps = None
    if prev and now > prev[0]:
        kbps = round((st.sent_rtp - prev[1]) * TS_PER_RTP * 188 * 8 /
                     (now - prev[0]) / 1000)
    freq = tune_freq_hz(st.tune)
    hls = any(getattr(u.get("mc"), "hls", False)
              for u in getattr(st, "upstreams", []) or [])
    return {
        "session": st.sid, "stream": st.stream_id, "ip": st.client_ip,
        "freq_mhz": round(freq / 1e6, 2) if freq else None,
        "channels": [{"lcn": m.get("lcn"), "name": m.get("name"),
                      "source": m.get("source", "init7")} for m in watched],
        "scanning": not watched,
        "since": st.started, "kbps": kbps,
        "data_age": (round(now - st.last_data, 1)
                     if getattr(st, "last_data", 0) else None),
        "has_upstream": bool(getattr(st, "upstreams", None)),
        "hls": hls,
    }


def _health(now, sess):
    items = []
    watching = [x for x in sess if x["has_upstream"]]
    init7 = [x for x in watching if not x["hls"]]
    if init7 and all((x["data_age"] or 0) > 10 for x in init7):
        items.append({"level": "bad", "key": "init7_silent",
                      "args": {"s": int(min(x["data_age"] for x in init7))}})
    for x in watching:
        if x["hls"] and (x["data_age"] or 0) > 20 and x["channels"]:
            items.append({"level": "warn", "key": "channel_silent",
                          "args": {"name": x["channels"][0]["name"],
                                   "s": int(x["data_age"])}})
    if not opts.no_epg and opts.epg_url:
        if EPG is None or not getattr(EPG, "loaded_at", 0):
            items.append({"level": "warn", "key": "epg_missing", "args": {}})
        elif now - EPG.loaded_at > 24 * 3600:
            items.append({"level": "warn", "key": "epg_old",
                          "args": {"h": int((now - EPG.loaded_at) / 3600)}})
    if opts.source == "init7" and not opts.no_hls:
        with _hls_lock:
            admitted = list(HLS_STATE["admitted"])
            infos = set(HLS_STATE["info"])
        listed = {m.get("url") for b in BUNDLES.values()
                  for m in b["members"] if m.get("hls")}
        waiting = [u for u in admitted if u in infos and u not in listed]
        if waiting:
            items.append({"level": "warn", "key": "hls_new",
                          "args": {"n": len(waiting)}})
    if not items:
        items.append({"level": "ok", "key": "all_ok", "args": {}})
    return items


def api_state():
    now = time.time()
    with sessions_lock:
        all_sessions = list(sessions.values())
    sess = [_session_info(st, now) for st in all_sessions]
    live = {st.sid for st in all_sessions}
    for sid in list(_RATE_PREV):
        if sid not in live:
            _RATE_PREV.pop(sid, None)
    tvs, seen_ips = [], set()
    with _devices_lock:
        devs = {k: dict(v) for k, v in DEVICES.items()}
    for mac, d in sorted(devs.items(),
                         key=lambda kv: kv[1].get("last_seen", ""),
                         reverse=True):
        ip = d.get("last_ip")
        seen_ips.add(ip)
        tvs.append({"mac": mac, "name": d.get("name") or "",
                    "ip": ip, "profile": d.get("profile", ""),
                    "mirror": bool(d.get("mirror")),
                    "first_seen": d.get("first_seen", ""),
                    "last_seen": d.get("last_seen", ""),
                    "watching": [x for x in sess if x["ip"] == ip]})
    for x in sess:
        if x["ip"] not in seen_ips:
            tvs.append({"mac": None, "name": "", "ip": x["ip"],
                        "profile": "", "mirror": False, "first_seen": "",
                        "last_seen": "", "watching": [x]})
            seen_ips.add(x["ip"])
    freqs = sorted(BUNDLES) if BUNDLES else []
    members = [m for b in BUNDLES.values() for m in b["members"]]
    with _hls_lock:
        hls = {"enabled": opts.source == "init7" and not opts.no_hls,
               "admitted": len(HLS_STATE["admitted"]),
               "delivering": sum(1 for c in HLS_STATE["checks"].values()
                                 if c.get("ok")),
               "checked": len(HLS_STATE["checks"]),
               "finished": HLS_STATE.get("finished"),
               "url": opts.hls_url}
    epg = None
    if EPG is not None:
        epg = {"state": getattr(EPG, "state", ""),
               "loaded_at": getattr(EPG, "loaded_at", 0) or None,
               "hits": getattr(EPG, "hits", 0),
               "total": len(getattr(EPG, "services_info", []) or [])}
    return {
        "version": BRIDGE_VERSION, "now": now,
        "started": BRIDGE_STARTED[0], "ip": opts.ip,
        "rtsp_port": opts.rtsp_port, "http_port": opts.http_port,
        "device_id": DEVICE_ID[0],
        "channels": len(members),
        "init7_channels": sum(1 for m in members if not m.get("hls")),
        "hls_channels": sum(1 for m in members if m.get("hls")),
        "frequencies": len(freqs),
        "freq_min_mhz": round(freqs[0] / 1e6, 1) if freqs else None,
        "freq_max_mhz": round(freqs[-1] / 1e6, 1) if freqs else None,
        "bundle_size": opts.bundle_size, "country": opts.country,
        "groups_open": len(MC_OPEN), "epg": epg, "hls": hls,
        "health": _health(now, sess), "tvs": tvs, "sessions": sess,
        "systemd": bool(os.environ.get("INVOCATION_ID")),
        "init7_url": getattr(opts, "init7_url", ""),
        "epg_url": opts.epg_url,
    }


def api_channels():
    out = []
    for freq in sorted(BUNDLES):
        for m in BUNDLES[freq]["members"]:
            out.append({"lcn": m.get("lcn"), "name": m.get("name"),
                        "freq_mhz": round(freq / 1e6, 2),
                        "slot": (m["pmt_pid"] - 0x0100) // 0x10,
                        "sid": m.get("out_sid"),
                        "source": "hls" if m.get("hls") else "init7",
                        "language": m.get("language", ""),
                        "group": m.get("group", "")})
    out.sort(key=lambda c: (c["lcn"] or 0))
    return out


def _json(code, obj):
    return code, "application/json; charset=utf-8", \
        json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ======================================================================
# Settings edited in the control panel (settings.json)
# ======================================================================
#
# The sources can be changed in the control panel. They are saved in
# settings.json next to this script and take effect at the next start.
# Options given explicitly on the command line (e.g. in the systemd unit)
# still win: a setting is only applied where the option kept its default.

SETTINGS = {}
_SETTING_OPTS = {"source1_url": "init7_url", "source2_url": "hls_url",
                 "epg_url": "epg_url"}


def _settings_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "settings.json")


def settings_load():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def settings_save(d):
    with open(_settings_path() + ".new", "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(_settings_path() + ".new", _settings_path())


def settings_apply(parser):
    SETTINGS.clear()
    SETTINGS.update(settings_load())
    SETTINGS["_cli"] = []
    for key, dest in _SETTING_OPTS.items():
        if key not in SETTINGS:
            continue
        if getattr(opts, dest) == parser.get_default(dest):
            setattr(opts, dest, SETTINGS[key])
        else:
            SETTINGS["_cli"].append(key)
    if "source2_enabled" in SETTINGS:
        if not opts.no_hls:
            opts.no_hls = not SETTINGS["source2_enabled"]
        else:
            SETTINGS["_cli"].append("source2_enabled")
    if opts.epg_url == "":
        opts.no_epg = True
    if any(k in SETTINGS for k in _SETTING_OPTS) or \
            "source2_enabled" in SETTINGS:
        log("Settings from the control panel applied (settings.json)")


def _check_url(url):
    u = (url or "").strip()
    if not u:
        return None
    if not u.lower().startswith(("http://", "https://", "file://")):
        raise ValueError("address must start with http://, https:// or "
                         "file://")
    return u


def check_source(kind, url):
    """Downloads a list as a trial and says what is in it - before it is
    saved, so a wrong address is noticed before it does any harm."""
    import urllib.request as _u
    url = _check_url(url)
    if not url:
        return {"ok": False, "error": "empty"}
    req = _u.Request(url, headers={"User-Agent": HLS_UA})
    with _u.build_opener(_u.ProxyHandler({})).open(req, timeout=20) as r:
        head = r.read(64 * 1024 if kind == "epg" else 30 * 1024 * 1024)
    if kind == "epg":
        ok = head[:2] == b"\x1f\x8b" or b"<tv" in head[:4096]
        return {"ok": ok, "format": "gzip" if head[:2] == b"\x1f\x8b"
                else "xml" if ok else "unknown"}
    text = head.decode("utf-8", "replace")
    if kind == "source1":
        rows = parse_init7_m3u(text)
        hls = parse_hls_m3u(text)
        return {"ok": bool(rows), "multicast": len(rows),
                "other": len(hls)}
    rows = parse_hls_m3u(text)
    return {"ok": bool(rows), "channels": len(rows),
            "drm": sum(1 for x in rows if x.get("drm"))}


def settings_update(req, who):
    """Validates and saves new source settings. Returns what changed."""
    current = {"source1_url": opts.init7_url,
               "source2_url": opts.hls_url,
               "source2_enabled": not opts.no_hls,
               "epg_url": "" if opts.no_epg else opts.epg_url}
    new = dict(settings_load())
    changed = []
    for key in ("source1_url", "source2_url", "epg_url"):
        if key in req:
            v = (req[key] or "").strip()
            if v:
                _check_url(v)
            elif key != "epg_url":
                raise ValueError(f"{key} must not be empty")
            new[key] = v
            if v != current[key]:
                changed.append(key)
    if "source2_enabled" in req:
        new["source2_enabled"] = bool(req["source2_enabled"])
        if new["source2_enabled"] != current["source2_enabled"]:
            changed.append("source2_enabled")
    if req.get("source2_reset"):
        new["source2_reset"] = True
        changed.append("source2_reset")
    settings_save(new)
    if changed:
        log(f"Settings changed in the control panel ({who}): "
            f"{', '.join(changed)} - effective after a restart")
    return changed


def panel_request(verb, path, blob, headers, who):
    bare, _, query = path.partition("?")
    if bare in ("/", "/panel"):
        fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "panel.html")
        try:
            with open(fpath, "rb") as f:
                return "200 OK", "text/html; charset=utf-8", f.read()
        except OSError:
            return ("404 Not Found", "text/plain; charset=utf-8",
                    b"panel.html is missing next to satipbridge.py")
    if verb == "GET":
        if bare == "/api/state":
            return _json("200 OK", api_state())
        if bare == "/api/channels":
            return _json("200 OK", api_channels())
        if bare == "/api/settings":
            saved = settings_load()
            return _json("200 OK", {
                "running": {"source1_url": opts.init7_url,
                            "source2_url": opts.hls_url,
                            "source2_enabled": not opts.no_hls,
                            "epg_url": "" if opts.no_epg else opts.epg_url},
                "saved": {k: v for k, v in saved.items()
                          if not k.startswith("_")},
                "cli": SETTINGS.get("_cli", []),
                "source": opts.source})
        if bare == "/api/log":
            q = urllib.parse.parse_qs(query)
            try:
                since = int(q.get("since", ["0"])[0])
            except ValueError:
                since = 0
            lines, seq = log_since(since)
            return _json("200 OK", {"lines": lines[-500:], "seq": seq})
        return _json("404 Not Found", {"error": "unknown"})
    if verb != "POST":
        return _json("405 Method Not Allowed", {"error": "method"})
    if headers.get("X-Bridge") != "1":
        return _json("403 Forbidden", {"error": "missing X-Bridge header"})
    try:
        req = json.loads(blob.decode("utf-8") or "{}")
    except ValueError:
        return _json("400 Bad Request", {"error": "json"})
    if bare == "/api/restart":
        log(f"Restart requested from the control panel ({who})")
        try:
            sessions_save()
        except Exception:
            pass
        # Exit shortly after answering; systemd starts us again in 5 s
        # (Restart=always), and the TVs' sessions are resumed.
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return _json("200 OK", {"ok": True,
                                "systemd": bool(os.environ.get(
                                    "INVOCATION_ID"))})
    if bare == "/api/settings":
        try:
            changed = settings_update(req, who)
        except ValueError as e:
            return _json("400 Bad Request", {"error": str(e)})
        return _json("200 OK", {"ok": True, "changed": changed,
                                "restart_needed": bool(changed)})
    if bare == "/api/check_source":
        try:
            res = check_source(req.get("kind"), req.get("url"))
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        return _json("200 OK", res)
    if bare == "/api/forget_all":
        for mac in list(DEVICES):
            device_forget(mac, who)
        return _json("200 OK", {"ok": True})
    if bare == "/api/device":
        mac = str(req.get("mac", ""))
        if mac not in DEVICES:
            return _json("404 Not Found", {"error": "unknown device"})
        if req.get("action") == "forget":
            device_forget(mac, who)
            return _json("200 OK", {"ok": True})
        if req.get("action") == "profile":
            prof = req.get("profile")
            if prof not in ("strict", "legacy"):
                return _json("400 Bad Request", {"error": "profile"})
            with _devices_lock:
                DEVICES[mac]["profile"] = prof
                _profile_cache.clear()
                _devices_save()
            log(f"Device {DEVICES[mac].get('name') or mac}: profile set to "
                f"'{prof}' from the control panel ({who})")
            return _json("200 OK", {"ok": True})
    return _json("404 Not Found", {"error": "unknown"})


def device_report(ip, headers):
    """For HTTP requests: pick up the model name if there is one.
    Panasonic sends it Base64-encoded in X-PANASONIC-Registration
    (TX-58DX800W: "58DX804")."""
    name = None
    try:
        reg = headers.get("X-PANASONIC-Registration")
        if reg:
            name = base64.b64decode(reg).split(b"\x00")[0].decode(
                "ascii", "replace").strip() or None
    except Exception:
        name = None
    device(ip, name=name, create=False)


def restamp_cc(packets, ccmap):
    """Rewrites the continuity_counter per PID so it runs continuously.

    Tables (PAT, PMT, SDT, CAT) are kept as ready-made packets in memory
    and went out every round with THE SAME counter. A single-packet table
    like the PAT therefore looked like a duplicate every time, and the
    two-packet SDT jumped back from 1 to 0 - a packet loss. The TX-40EX730
    does not care, the TX-58DX800W drops them: it found channels only at a
    snail's pace and never asked for a PMT.

    ccmap: {pid: last counter}, owned by the session.
    """
    out = []
    for pkt in packets:
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid == 0x1FFF:
            out.append(pkt)
            continue
        afc = (pkt[3] >> 4) & 0x03
        last = ccmap.get(pid)
        if afc & 0x01:                      # carries payload: count on
            cc = 0 if last is None else (last + 1) & 0x0F
        else:                               # adaptation field only: counter stays
            cc = 0 if last is None else last
        ccmap[pid] = cc
        if (pkt[3] & 0x0F) != cc:
            b = bytearray(pkt)
            b[3] = (b[3] & 0xF0) | cc
            pkt = bytes(b)
        out.append(pkt)
    return out


def rtcp_app(ssrc, text, norm=False):
    """RTCP APP packet with the tuner status (name SES1).

    The SAT>IP spec (3.5.15.2) requires two 16-bit fields after "SES1":
    identifier (0) and string_length - only then the text. Both used to
    be missing. The TX-40EX730 reads it correctly anyway, the TX-58DX800W
    apparently does not: during a scan it waited exactly 240 s on every
    frequency and then moved on, as if "tuner locked" never came.

    Only for the "strict" profile (norm=True), so a running TV notices
    nothing.
    """
    raw = text.encode()
    r = len(raw) % 4
    data = raw + b"\x00" * (4 - r) if r else raw
    if norm:
        data = struct.pack("!HH", 0, len(raw)) + data
    return struct.pack("!BBH", 0x80, 204, (8 + len(data)) // 4) + \
        struct.pack("!I", ssrc) + b"SES1" + data


def find_free_device_id(preset=0, wait_s=3.0):
    """Asks the network for other SAT>IP servers and picks a device ID
    none of them uses.

    preset > 0 means the user decided; then do not ask. If anything goes
    wrong, it stays at 1 - as before. A failed search must not hold up
    the start.
    """
    if preset:
        DEVICE_ID[0] = preset
        log(f"SAT>IP device ID: {preset} (set by option)")
        return
    taken = set()
    try:
        query = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            f"ST: {DEVICE_TYPE}\r\n\r\n"
        ).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                         socket.inet_aton(opts.ip))
        except OSError:
            pass
        s.settimeout(0.5)
        s.sendto(query, (SSDP_ADDR, SSDP_PORT))
        end_time = time.time() + wait_s
        while time.time() < end_time:
            try:
                blob, sender_addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            if sender_addr[0] == opts.ip:
                continue                      # ourselves
            m = re.search(r"(?im)^DEVICEID\.SES\.COM:\s*(\d+)",
                          blob.decode("utf-8", "replace"))
            if m:
                taken.add(int(m.group(1)))
                log(f"  other SAT>IP server: {sender_addr[0]} "
                    f"with device ID {m.group(1)}")
        s.close()
    except Exception as e:
        log(f"  ! Device ID search failed: {e}")

    for k in range(1, 255):
        if k not in taken:
            DEVICE_ID[0] = k
            break
    if taken:
        log(f"SAT>IP device ID: {DEVICE_ID[0]} "
            f"(taken: {sorted(taken)})")
    else:
        log(f"SAT>IP device ID: {DEVICE_ID[0]} (no other server on the network)")


# The Panasonic server's identification, copied character by character.
# Why this could matter at all: the two were sold as a pair, and a
# manufacturer that promises a pair sometimes checks whether the other side
# comes from its own house.
SERVER_PANA = "FreeBSD/8.0 UPnP/1.0 Panasonic-MIL-DLNA-SV/1.0"
SERVER_OURS = "Linux/1.0 UPnP/1.1 SatipBridge/1.0"

# Own UUID for the mirror version. The Panasonic derives its UUID from its
# own MAC address: 4D454930-0400-1000-8001-<MAC>. Ours used to end
# in 0000000000B1 - which looks like nothing. Whether it checks that is
# unknown; it was the last difference not yet tested.
#
# Only one thing matters: what is in the SSDP answer (USN) must match what
# is in the description (UDN). Two different IDs for the same device would
# be a real error - so the mirror TV gets both from the same source.
MIRROR_UUID_CACHE = [None]


def own_mac_uuid():
    """4D454930-0400-1000-8001-<MAC of this computer>, Panasonic style."""
    try:
        import uuid as _u
        mac = f"{_u.getnode():012X}"
    except Exception:
        mac = "0000000000B1"
    return f"4D454930-0400-1000-8001-{mac}"


def mirror_uuid():
    if MIRROR_UUID_CACHE[0] is None:
        MIRROR_UUID_CACHE[0] = (opts.mirror_uuid if opts.mirror_uuid
                           else own_mac_uuid())
    return MIRROR_UUID_CACHE[0]


def ssdp_response(st, peer=None):
    return (
        "HTTP/1.1 200 OK\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        "EXT: \r\n"
        f"LOCATION: http://{opts.ip}:{opts.http_port}/satipsrv/ddd.xml\r\n"
        f"SERVER: {SERVER_PANA if is_mirror(peer, create=False) else SERVER_OURS}\r\n"
        f"ST: {st}\r\n"
        f"USN: uuid:{mirror_uuid() if is_mirror(peer, create=False) else opts.uuid}"
        f"::{DEVICE_TYPE}\r\n"
        f"BOOTID.UPNP.ORG:{BOOTID}\r\n"
        f"CONFIGID.UPNP.ORG:{1 if is_mirror(peer, create=False) else CONFIGID}\r\n"
        f"DEVICEID.SES.COM:{DEVICE_ID[0]}\r\n\r\n"
    ).encode()


def ssdp_notify(nt, usn):
    return (
        "NOTIFY * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        f"LOCATION: http://{opts.ip}:{opts.http_port}/satipsrv/ddd.xml\r\n"
        f"NT: {nt}\r\n"
        "NTS: ssdp:alive\r\n"
        "SERVER: Linux/1.0 UPnP/1.1 SatipBridge/1.0\r\n"
        f"USN: {usn}\r\n"
        f"BOOTID.UPNP.ORG:{BOOTID}\r\n"
        f"CONFIGID.UPNP.ORG:{CONFIGID}\r\n"
        f"DEVICEID.SES.COM:{DEVICE_ID[0]}\r\n\r\n"
    ).encode()


def ssdp_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", SSDP_PORT))
    mreq = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR),
                       socket.inet_aton(opts.ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(opts.ip))

    def announce():
        while True:
            for nt, usn in (
                ("upnp:rootdevice", f"uuid:{opts.uuid}::upnp:rootdevice"),
                (f"uuid:{opts.uuid}", f"uuid:{opts.uuid}"),
                (DEVICE_TYPE, f"uuid:{opts.uuid}::{DEVICE_TYPE}"),
            ):
                try:
                    sock.sendto(ssdp_notify(nt, usn), (SSDP_ADDR, SSDP_PORT))
                except OSError:
                    pass
                time.sleep(0.2)
            time.sleep(600)

    threading.Thread(target=announce, daemon=True).start()
    log(f"SSDP listening on {SSDP_ADDR}:{SSDP_PORT}")

    wanted = {"ssdp:all", "upnp:rootdevice", DEVICE_TYPE,
              f"uuid:{opts.uuid}"}
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            continue
        text = data.decode(errors="replace")
        if not text.startswith("M-SEARCH"):
            continue
        m = re.search(r"^ST\s*:\s*(\S+)", text, re.I | re.M)
        st = m.group(1).strip() if m else ""
        if st not in wanted:
            continue
        reply_st = DEVICE_TYPE if st in ("ssdp:all", "upnp:rootdevice") else st
        log(f"SSDP: search request from {addr[0]}")
        try:
            sock.sendto(ssdp_response(reply_st, addr[0]), addr)
        except OSError:
            pass


# ----------------------------------------------------------------------

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def mc_sweeper():
    """Collects multicast joins that no longer belong to any session.

    When zapping quickly two channel changes can overtake each other; then
    a set of connections is left behind without an owner - it belongs to
    no session any more, is therefore never left, and keeps pulling data.
    Three such corpses are already eighteen Mbit/s, and they add up with
    every zap. Exactly that once choked the line.

    This sweep is the last safety net: whatever belongs to no living
    session after thirty seconds is closed.
    """
    while True:
        time.sleep(10)
        try:
            alive = set()
            with sessions_lock:
                session_list = list(sessions.values())
            for s in session_list:
                for u in list(getattr(s, "upstreams", []) or []):
                    mc = u.get("mc")
                    if mc is not None:
                        alive.add(id(mc))
            now_ts = time.time()
            with MC_LOCK:
                orphans = [m for m in MC_OPEN
                            if id(m) not in alive
                            and now_ts - m.created > 30]
            for m in orphans:
                log(f"  Sweep: orphaned join to {m.group} "
                    f"is being left")
                try:
                    m.close()
                except Exception:
                    pass
        except Exception as e:
            log(f"  ! Kehraus: {e}")


# ======================================================================
# Keep sessions alive across a restart of the bridge
# ======================================================================
#
# After a restart the TX-58DX800W asks with its OLD session number
# (OPTIONS ... Session: 6C8D7631). The bridge no longer knew it and
# answered, as the spec says, "454 Session Not Found" - whereupon the TV
# wrote the bridge off: no picture, zapping did not help, only a scan.
#
# Therefore the state of every running bundle session is kept in
# sessions.json (every 5 s and on every change). On start, recent entries
# are resumed with THE SAME session number, stream ID and SSRC, and the
# bridge immediately sends on to the TV's port. For the TV it was a short
# hiccup. If a TV rescued this way does not show up (e.g. it was switched
# off meanwhile), the reaper cleans the session up after 60 s like any
# other.

SESSIONS_MAX_AGE = 300        # s; an older state counts as finished
_sessions_file_lock = threading.Lock()


def _sessions_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "sessions.json")


def sessions_save():
    with sessions_lock:
        all_sessions = list(sessions.values())
    state = []
    for st in all_sessions:
        if not isinstance(st, BundleSession) or not st.running:
            continue
        state.append({
            "sid": st.sid, "stream_id": st.stream_id,
            "client_ip": st.client_ip, "client_rtp_port": st.client_rtp_port,
            "tune": st.tune,
            "pids": "all" if st.pids == "all" else sorted(st.pids or []),
            "ssrc": st.ssrc, "seq": st.seq,
        })
    blob = {"time": time.time(), "version": BRIDGE_VERSION,
             "sessions": state}
    fpath = _sessions_path()
    with _sessions_file_lock:
        try:
            with open(fpath + ".new", "w", encoding="utf-8") as f:
                json.dump(blob, f)
            os.replace(fpath + ".new", fpath)
        except OSError as e:
            log(f"  ! {fpath} not writable: {e}")


def sessions_restore():
    try:
        with open(_sessions_path(), encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return
    age = time.time() - float(blob.get("time", 0))
    saved_entries = blob.get("sessions") or []
    if not saved_entries:
        return
    if age > SESSIONS_MAX_AGE:
        log(f"Saved sessions are {age:.0f} s old - not "
            f"resumed")
        return
    for e in saved_entries:
        try:
            bundle = BUNDLES.get(tune_freq_hz(e["tune"]))
            if not bundle:
                log(f"  Session {e['sid']}: frequency no longer exists "
                    f"({e['tune']}) - dropped")
                continue
            pids = e["pids"] if e["pids"] == "all" else set(e["pids"])
            st = BundleSession(e["client_ip"], int(e["client_rtp_port"]),
                               e["tune"], pids, bundle)
            st.sid = e["sid"]
            st.stream_id = int(e["stream_id"])
            st.ssrc = int(e["ssrc"])
            st.seq = (int(e["seq"]) + 100) & 0xFFFF
            with BundleSession.counter_lock:
                BundleSession.counter = max(BundleSession.counter,
                                            st.stream_id)
            st.sync_upstreams()
            st.start()
            with sessions_lock:
                sessions[st.sid] = st
            log(f"Session {st.sid} resumed: {st.client_ip} "
                f"stream={st.stream_id} {st.tune} "
                f"({'all' if pids == 'all' else len(pids)} PIDs, "
                f"state from {age:.0f} s ago)")
        except Exception as ex:
            log(f"  ! Session {e.get('sid')} not resumed: {ex}")


# ======================================================================
# Migration of the files written by the German versions (up to 12)
# ======================================================================
#
# Up to version 12 the bridge kept its state in German: geraete.json,
# hls_status.json and sitzungen.json, with German keys ("profil": "streng",
# "aufgenommen", ...). On the first start of an English version they are
# converted once into devices.json, hls_state.json and sessions.json. The
# old files are left in place untouched, so going back to an old version
# still works. Without this the TVs would be registered as new devices
# (losing the Panasonic identity) and every HLS channel would need a new
# check.

_DEVICE_KEYS = {"profil": "profile", "spiegel": "mirror", "zuletzt_ip": "last_ip",
                "zuletzt": "last_seen", "erstmals": "first_seen", "notiz": "note"}
_PROFILES = {"streng": "strict", "alt": "legacy"}


def _convert_devices(old):
    new = {}
    for key, entry in old.items():
        e = {_DEVICE_KEYS.get(k, k): v for k, v in entry.items()}
        if "profile" in e:
            e["profile"] = _PROFILES.get(e["profile"], e["profile"])
        new[key] = e
    return new


def _convert_hls(old):
    top = {"pruefung": "checks", "aufgenommen": "admitted",
           "fertig": "finished", "liste_anzahl": "list_count"}
    new = {top.get(k, k): v for k, v in old.items()}
    new["checks"] = {url: {("time" if k == "zeit" else k): v
                           for k, v in c.items()}
                     for url, c in new.get("checks", {}).items()}
    return new


def _convert_sessions(old):
    return {"time": old.get("zeit", old.get("time", 0)),
            "version": old.get("version", ""),
            "sessions": old.get("sitzungen", old.get("sessions", []))}


def migrate_old_files():
    here = os.path.dirname(os.path.abspath(__file__))
    plan = [("hls_status.json", "hls_state.json", _convert_hls),
            ("sitzungen.json", "sessions.json", _convert_sessions)]
    if not getattr(opts, "devices", None):
        plan.insert(0, ("geraete.json", "devices.json", _convert_devices))
    for old_name, new_name, convert in plan:
        old_path = os.path.join(here, old_name)
        new_path = os.path.join(here, new_name)
        if os.path.exists(new_path) or not os.path.exists(old_path):
            continue
        try:
            with open(old_path, encoding="utf-8") as f:
                blob = convert(json.load(f))
            with open(new_path + ".new", "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=False, indent=2)
            os.replace(new_path + ".new", new_path)
            log(f"Converted {old_name} -> {new_name} (old file kept)")
        except Exception as e:
            log(f"  ! Could not convert {old_name}: {e}")


def reaper():
    while True:
        time.sleep(5)
        try:
            sessions_save()
        except Exception as e:
            log(f"  ! Saving sessions: {e}")
        now = time.time()
        dead = []
        with sessions_lock:
            for sid, s in list(sessions.items()):
                if not s.running or now - s.last_touch > 60:
                    dead.append((sid, s))
                    sessions.pop(sid, None)
        for sid, s in dead:
            # Merely removing it from the list is not enough: the session
            # would keep running, its connections would stay open and keep
            # pulling data - impossible to find and therefore immortal.
            # Exactly that left three streams hanging per forgotten session,
            # until the line choked.
            log(f"Session {sid} cleaned up")
            s.running = False
            threading.Thread(target=s.stop, daemon=True).start()


def main():
    global opts
    p = argparse.ArgumentParser(
        description="SAT>IP bridge: IPTV (Init7 multicast, HLS) as a DVB-C "
                    "cable network for TVs with a SAT>IP client")
    p.add_argument("--ip", default=None, help="own LAN IP")
    p.add_argument("--rtsp-port", type=int, default=554,
                   help="RTSP port for the TV, default 554")
    p.add_argument("--http-port", type=int, default=55000,
                   help="HTTP port for the device description")
    p.add_argument("--tvh-host", default=None,
                   help="address of Tvheadend (--source tvheadend). Default: "
                        "our own LAN IP, because Tvheadend binds its RTSP "
                        "port to that one and not to 127.0.0.1")
    p.add_argument("--tvh-rtsp-port", type=int, default=9983,
                   help="RTSP port of Tvheadend, default 9983")
    p.add_argument("--tuners", type=int, default=4)
    p.add_argument("--flush-ms", type=float, default=20.0,
                   help="after this many milliseconds a started RTP packet "
                        "is padded with null packets and sent, default 20")
    p.add_argument("--tvh-http-port", type=int, default=9981,
                   help="web port of Tvheadend, default 9981")
    p.add_argument("--tvh-user", default="root")
    p.add_argument("--tvh-password", default="admin")
    p.add_argument("--no-nit", action="store_true",
                   help="do not inject our own NIT")
    p.add_argument("--network-id", type=int, default=7)
    p.add_argument("--network-name", default="Init7")
    p.add_argument("--onid", type=int, default=1,
                   help="original_network_id for the NIT")
    p.add_argument("--source", choices=("tvheadend", "init7"),
                   default="tvheadend",
                   help="where the stream comes from. 'init7' attaches the "
                        "bridge directly to the multicast, without Tvheadend.")
    p.add_argument("--init7-url",
                   default="https://api.init7.net/tvchannels.m3u",
                   help="Init7 channel list for --source init7")
    p.add_argument("--scan-grace", type=int, default=1200,
                   help="milliseconds the bridge waits after a frequency "
                        "change before it joins the first channel of the "
                        "bundle on the off chance. If the TV says what it "
                        "wants before that, a join and several seconds are "
                        "saved.")
    p.add_argument("--scan-streams", type=int, default=1,
                   help="how many streams of a frequency are fetched during "
                        "a channel scan. 1 is enough for the TV to see the "
                        "frequency as alive; 0 saves all joins, but is not "
                        "enough for it.")
    p.add_argument("--zap-delay", type=int, default=400,
                   help="milliseconds a frequency change is held back. When "
                        "zapping through, only the last wish is carried out "
                        "instead of every single one.")
    p.add_argument("--epg-url",
                   default="https://github.com/mathewmeconry/TV7_EPG_Data/"
                           "raw/master/tv7_epg.xml.gz",
                   help="XMLTV programme guide. Loaded in the background; if "
                        "it cannot be reached, everything else keeps running "
                        "unchanged.")
    p.add_argument("--hls-url",
                   default="https://iptv-org.github.io/iptv/languages/ara.m3u",
                   help="second source: M3U playlist with HLS channels. "
                        "Checked in the background; working channels appear "
                        "from --hls-lcn on after a restart of the bridge and "
                        "a channel scan.")
    p.add_argument("--no-hls", action="store_true",
                   help="no second source")
    p.add_argument("--hls-lcn", type=int, default=301,
                   help="first channel number of the HLS channels")
    p.add_argument("--hls-max", type=int, default=1000,
                   help="check at most this many HLS channels")
    p.add_argument("--hls-parallel", type=int, default=6,
                   help="how many HLS channels are checked at the same time")
    p.add_argument("--no-epg", action="store_true",
                   help="do not inject a programme guide")
    p.add_argument("--epg-refresh", type=float, default=6.0,
                   help="hours between two downloads of the programme guide, "
                        "default 6")
    p.add_argument("--epg-packets", "--epg-pakete", type=int, default=24,
                   help="how many packets of the schedule are sent along per "
                        "table round. Higher means the guide is complete "
                        "sooner but costs bandwidth; 24 is about 120 kbit/s.")
    p.add_argument("--no-time", action="store_true",
                   help="do not inject the time (TDT/TOT) - the TV then "
                        "cannot record")
    p.add_argument("--country", default="CHE",
                   help="country code for the time zone in the TOT, three "
                        "letters (CHE, DEU, AUT ...)")
    p.add_argument("--nit-mtype", default="256qam")
    p.add_argument("--nit-sr", type=int, default=6900)
    p.add_argument("--nit-max-hz", type=int, default=858_000_000,
                   help="channels above this frequency are left out of the "
                        "NIT, default 858 MHz (end of the cable band)")
    p.add_argument("--nit-interval", type=float, default=300.0,
                   help="milliseconds between two NIT injections, "
                        "default 300")
    p.add_argument("--level", type=int, default=220,
                   help="reported signal level 0-255, default 220")
    p.add_argument("--quality", type=int, default=15,
                   help="reported signal quality 0-15, default 15")
    p.add_argument("--name", default="Init7 Bridge")
    p.add_argument("--uuid", default="4D454930-0400-1000-8001-0000000000B1")
    p.add_argument("--mirror-ip", "--spiegel-ip", default="",
                   help="addresses (comma separated) that get the Panasonic-"
                        "style device description instead of the usual one. "
                        "For TVs that do not accept the usual one. Kept for "
                        "compatibility - devices.json is the better place.")
    p.add_argument("--devices", "--geraete", default=None,
                   help="device registry (default: devices.json next to "
                        "this script)")
    p.add_argument("--new-profile", "--profil-neu",
                   choices=("strict", "legacy", "streng", "alt"),
                   default="strict",
                   help="profile for TVs that show up for the first time")
    p.add_argument("--config-id", type=int, default=0,
                   help="UPnP CONFIGID. 0 means: use the built-in fixed value. "
                        "A change forces every TV into a new channel scan - so "
                        "only when the device description really changed.")
    p.add_argument("--mirror-name", "--spiegel-name", default="SATIP-Server",
                   help="device name for mirror devices. The Panasonic simply "
                        "names itself after its model, not after a service.")
    p.add_argument("--mirror-uuid", "--spiegel-uuid", default="",
                   help="UUID for mirror devices. Empty means: derive it from "
                        "this computer's MAC address, like the Panasonic "
                        "does.")
    p.add_argument("--mirror-tuners", "--spiegel-tuners", type=int, default=1,
                   help="number of tuners reported to mirror devices. The "
                        "Panasonic reports DVBC-1 for itself; its client side "
                        "may expect the same.")
    p.add_argument("--device-id", type=int, default=0,
                   help="SAT>IP device ID 1-254. 0 means: look on the network "
                        "at start and take a free one. Two servers with the "
                        "same ID do not get along.")
    p.add_argument("--no-ssdp", action="store_true",
                   help="do not announce (for testing)")
    p.add_argument("--bundle-size", type=int, default=1,
                   help="how many channels are bundled per frequency. "
                        "Default 1 = off (one channel per frequency). With "
                        "e.g. 8 all channels end up on a few frequencies, "
                        "like with a real cable operator.")
    p.add_argument("--bundle-base-hz", type=int, default=114_000_000,
                   help="first announced bundle frequency in Hz, "
                        "default 114 MHz")
    p.add_argument("--bundle-step-hz", type=int, default=8_000_000,
                   help="spacing between the bundle frequencies in Hz, "
                        "default 8 MHz")
    p.add_argument("--probe-seconds", type=float, default=2.5,
                   help="how long each channel is sampled to find its "
                        "video/audio PIDs, default 2.5")
    p.add_argument("--pmt-cache", default=None,
                   help="file caching the sampled PIDs, default "
                        "pmt_cache.json next to this script")
    p.add_argument("--rescan-pmt", action="store_true",
                   help="discard the cache and sample all channels again "
                        "(slower start)")
    opts = p.parse_args()
    BRIDGE_STARTED[0] = time.time()
    # Switch between threads every 0.5 ms instead of every 5 ms. The
    # threads forwarding video handle hundreds of packets per second; while
    # a CPU-bound thread (building the programme guide) runs, each of them
    # otherwise waits up to 5 ms per packet for its turn - measured: about
    # three seconds of stuttering picture while the guide is being built.
    sys.setswitchinterval(0.0005)
    # Old German values of --profil-neu are still accepted.
    opts.new_profile = {"streng": "strict", "alt": "legacy"}.get(
        opts.new_profile, opts.new_profile)
    migrate_old_files()
    settings_apply(p)

    if not opts.ip:
        opts.ip = local_ip()
    if not opts.tvh_host:
        opts.tvh_host = opts.ip
    if not opts.pmt_cache:
        opts.pmt_cache = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "pmt_cache.json")

    log("=" * 60)
    log(f"SAT>IP bridge   version: {BRIDGE_VERSION}")
    log(f"SAT>IP bridge   own IP {opts.ip}")
    log(f"  RTSP for the TV        : {opts.ip}:{opts.rtsp_port}")
    log(f"  Device description     : http://{opts.ip}:{opts.http_port}"
        f"/satipsrv/ddd.xml")
    log(f"  Tvheadend               : {opts.tvh_host}:{opts.tvh_rtsp_port}")
    log(f"  Pad after              : {opts.flush_ms:.0f} ms")
    if opts.bundle_size > 1:
        log(f"  Bundling               : {opts.bundle_size} channels/frequency"
            f" (from {opts.bundle_base_hz/1e6:.0f} MHz, "
            f"step {opts.bundle_step_hz/1e6:.0f} MHz)")
    else:
        log("  Bundling               : off (1 channel per frequency)")
    log("=" * 60)

    # Check that Tvheadend is reachable - not needed when the bridge reads
    # Init7's multicast directly.
    if opts.source == "init7":
        log("Source: Init7 directly (no Tvheadend).")
    else:
        try:
            t = socket.create_connection((opts.tvh_host, opts.tvh_rtsp_port),
                                         timeout=5)
            t.close()
            log("Tvheadend is reachable.")
        except OSError as e:
            log(f"ERROR: Tvheadend at "
                f"{opts.tvh_host}:{opts.tvh_rtsp_port} "
                f"not reachable ({e}).")
            log("Check with:  sudo ss -tlnp | grep -E '554|9983'")
            log("Is Tvheadend's RTSP port set to 9983? See the head of "
                "this file. If needed, give the right address with --tvh-host and "
                "--tvh-rtsp-port.")
            return 1

    if opts.no_nit:
        log("NIT injection switched off.")
    else:
        if not opts.no_hls:
            hls_load()
        prepare_nit()
        if opts.source == "init7" and not opts.no_hls:
            threading.Thread(target=hls_checker, daemon=True).start()

    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=mc_sweeper, daemon=True).start()

    try:
        http = DescServer(("0.0.0.0", opts.http_port), DescHandler)
    except OSError as e:
        log(f"ERROR: port {opts.http_port} cannot be bound ({e}).")
        log("Probably a bridge is already running. Check and stop it:")
        log("   systemctl status satipbridge")
        log("   sudo systemctl stop satipbridge")
        log("   pgrep -a -f satipbridge.py")
        return 1
    threading.Thread(target=http.serve_forever, daemon=True).start()
    log(f"Device description running on port {opts.http_port}")

    try:
        rtsp = RtspServer(("0.0.0.0", opts.rtsp_port), RtspHandler)
    except PermissionError:
        log(f"ERROR: no permission for port {opts.rtsp_port}. "
            f"Start with sudo or CAP_NET_BIND_SERVICE.")
        return 1
    except OSError as e:
        log(f"ERROR: port {opts.rtsp_port} not available ({e}). "
            f"Is another SAT>IP server (Tvheadend, minisatip) using it?")
        return 1
    # Before the TVs' first requests: otherwise their OPTIONS with the old
    # session number would come too early and get 454 again after all.
    sessions_restore()
    threading.Thread(target=rtsp.serve_forever, daemon=True).start()
    log(f"RTSP running on port {opts.rtsp_port}")

    if opts.no_ssdp:
        log("Announcement switched off.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        if opts.config_id:
            globals()["CONFIGID"] = opts.config_id
            log(f"CONFIGID set by hand: {opts.config_id} "
                f"- the TVs need a new channel scan")
        MIRROR_IPS.update(
            a.strip() for a in opts.mirror_ip.split(",") if a.strip())
        _devices_load(force=True)
        for ip in MIRROR_IPS:
            # Legacy --mirror-ip: take over as a device with the Panasonic-style
            # description, unless it is already in the registry.
            if not any(e.get("last_ip") == ip for e in DEVICES.values()):
                mac = mac_of(ip)
                DEVICES[mac or ("ip:" + ip)] = {
                    "profile": "strict", "mirror": True, "last_ip": ip,
                    "first_seen": time.strftime("%Y-%m-%d %H:%M")}
                _devices_save()
        if MIRROR_IPS:
            log(f"Panasonic-style description for: "
                f"{', '.join(sorted(MIRROR_IPS))}")
            log(f"  Name: {opts.mirror_name or opts.name}   "
                f"UUID: {mirror_uuid()}   DVBC-{opts.mirror_tuners}")
        try:
            find_free_device_id(opts.device_id)
        except Exception as e:
            log(f"  ! Device ID stays at {DEVICE_ID[0]}: {e}")
        try:
            ssdp_loop()
        except KeyboardInterrupt:
            pass

    log("Stopped.")
    return 0



# ======================================================================
# NIT generation
# ======================================================================
#
# The Network Information Table on PID 16 lists ALL frequencies of a cable
# network. A TV locks on any frequency, reads it once and then knows where
# everything else is - so it does not have to search the whole band.
#
# Modelled on a real cable NIT (captured from the Panasonic server): per
# transport stream one cable_delivery_system_descriptor, a service list,
# the EACEM marker and the channel numbers.

def _crc_table():
    t = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 \
                else (crc << 1) & 0xFFFFFFFF
        t.append(crc)
    return t


_CRC_TABLE = _crc_table()


def crc32_mpeg(data):
    # Table-driven: one step per byte instead of eight. Same result as the
    # bit-by-bit version; building the programme guide went from about
    # 3.5 s of pure CPU (which made the picture stutter) to a fraction.
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


def bcd(value, digits):
    s = str(int(value)).rjust(digits, "0")[-digits:]
    out = bytearray()
    for i in range(0, digits, 2):
        out.append((int(s[i]) << 4) | int(s[i + 1]))
    return bytes(out)


MOD_CODE = {"16qam": 1, "32qam": 2, "64qam": 3, "128qam": 4, "256qam": 5}


def cable_descriptor(freq_hz, mtype="256qam", sr_ksym=6900):
    body = bytearray()
    body += bcd(freq_hz // 100, 8)            # frequency in 100 Hz steps
    body += bytes([0xFF, 0xF0 | 2])           # reserved + FEC_outer = RS
    body += bytes([MOD_CODE.get(mtype, 5)])
    # symbol rate: 7 BCD digits in steps of 100 sym/s, then
    # 4 Bit FEC_inner. 6900 kSym/s -> 6 900 000 Sym/s -> 69000 -> "0069000"
    digits = str(int(sr_ksym) * 10).rjust(7, "0")[-7:] + "F"   # F = no FEC
    body += bytes(int(digits[i], 16) << 4 | int(digits[i + 1], 16)
                  for i in range(0, 8, 2))
    return bytes([0x44, len(body)]) + bytes(body)


def service_list_descriptor(services):
    body = bytearray()
    for sid, stype in services:
        body += struct.pack("!HB", sid & 0xFFFF, stype & 0xFF)
    return bytes([0x41, len(body)]) + bytes(body)


def lcn_descriptor(services_lcn):
    body = bytearray()
    for sid, lcn in services_lcn:
        body += struct.pack("!HH", sid & 0xFFFF, 0xFC00 | (lcn & 0x03FF))
    return bytes([0x83, len(body)]) + bytes(body)


PRIVATE_EACEM = bytes([0x5F, 0x04, 0x00, 0x00, 0x00, 0x28])


def ts_entry(ch):
    desc = cable_descriptor(ch["freq_hz"], ch["mtype"], ch["sr"])
    desc += service_list_descriptor([(ch["sid"], ch["stype"])])
    desc += PRIVATE_EACEM
    desc += lcn_descriptor([(ch["sid"], ch["lcn"])])
    return struct.pack("!HHH", ch["tsid"] & 0xFFFF, ch["onid"] & 0xFFFF,
                       0xF000 | len(desc)) + desc


def build_nit_sections(channels, network_id, network_name, version=1):
    """Returns a list of finished NIT sections (without pointer field)."""
    name = network_name.encode("latin-1", "replace")[:250]
    net_desc = bytes([0x40, len(name)]) + name

    entries = [ts_entry(c) for c in channels]

    # Split into sections: a section may carry at most 1021 bytes of
    # payload (section_length is 12 bits).
    groups, cur, cur_len = [], [], 0
    head = 5 + 2 + len(net_desc) + 2      # from network_id to TS loop length
    for e in entries:
        if cur and head + cur_len + len(e) + 4 > 1021:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(e)
        cur_len += len(e)
    if cur:
        groups.append(cur)

    last = len(groups) - 1
    out = []
    for n, grp in enumerate(groups):
        body = bytearray()
        body += struct.pack("!H", network_id & 0xFFFF)
        body += bytes([0xC1 | ((version & 0x1F) << 1)])   # reserved+ver+current
        body += bytes([n, last])
        body += struct.pack("!H", 0xF000 | len(net_desc)) + net_desc
        loop = b"".join(grp)
        body += struct.pack("!H", 0xF000 | len(loop)) + loop
        seclen = len(body) + 4                             # + CRC
        sec = bytes([0x40]) + struct.pack("!H", 0xB000 | seclen) + bytes(body)
        sec += struct.pack("!I", crc32_mpeg(sec))
        out.append(sec)
    return out


def build_pat_section(programs, tsid, version=1):
    """programs: list of (program_number, pmt_pid). Builds a PAT section,
    as Tvheadend would normally send it itself - but here for an
    artificial bundle of several channels on one frequency."""
    body = bytearray()
    body += struct.pack("!H", tsid & 0xFFFF)
    body += bytes([0xC1 | ((version & 0x1F) << 1)])
    body += bytes([0, 0])                       # section/last_section_number
    for prog, pmt_pid in programs:
        body += struct.pack("!H", prog & 0xFFFF)
        body += struct.pack("!H", 0xE000 | (pmt_pid & 0x1FFF))
    seclen = len(body) + 4
    sec = bytes([0x00]) + struct.pack("!H", 0xB000 | seclen) + bytes(body)
    sec += struct.pack("!I", crc32_mpeg(sec))
    return sec


def build_pmt_section(program_number, pcr_pid, streams, version=1):
    """streams: list of (stream_type, pid, es_info_bytes). One PMT per
    bundled channel, with the renumbered video/audio PIDs."""
    body = bytearray()
    body += struct.pack("!H", program_number & 0xFFFF)
    body += bytes([0xC1 | ((version & 0x1F) << 1)])
    body += bytes([0, 0])
    body += struct.pack("!H", 0xE000 | (pcr_pid & 0x1FFF))
    body += struct.pack("!H", 0xF000)            # program_info_length = 0
    for stype, pid, es_info in streams:
        body += bytes([stype & 0xFF])
        body += struct.pack("!H", 0xE000 | (pid & 0x1FFF))
        body += struct.pack("!H", 0xF000 | (len(es_info) & 0x0FFF)) + es_info
    seclen = len(body) + 4
    sec = bytes([0x02]) + struct.pack("!H", 0xB000 | seclen) + bytes(body)
    sec += struct.pack("!I", crc32_mpeg(sec))
    return sec


def sdt_service_descriptor(service_type, provider, name):
    prov_b = (provider or "").encode("latin-1", "replace")[:63]
    name_b = (name or "").encode("latin-1", "replace")[:63]
    body = (bytes([service_type & 0xFF, len(prov_b)]) + prov_b
            + bytes([len(name_b)]) + name_b)
    return bytes([0x48, len(body)]) + body


def build_sdt_section(services, tsid, onid, version=1):
    """services: list of dict(sid, stype, provider, name). Lists the
    channel names of all bundle members together on one frequency."""
    body = bytearray()
    body += struct.pack("!H", tsid & 0xFFFF)
    body += bytes([0xC1 | ((version & 0x1F) << 1)])
    body += bytes([0, 0])
    body += struct.pack("!H", onid & 0xFFFF)
    body += bytes([0xFF])                         # reserved_future_use
    for s in services:
        desc = sdt_service_descriptor(s["stype"], s.get("provider", ""),
                                      s["name"])
        body += struct.pack("!H", s["sid"] & 0xFFFF)
        # The last two bits of this byte say whether there is a programme
        # guide for this channel: EIT_schedule_flag and
        # EIT_present_following_flag. If they are 0, the TV does not even
        # look at PID 18 - you can inject whatever you like, the guide
        # stays empty.
        body += bytes([0xFF if EPG_FLAGS[0] else 0xFC])
        # running_status=4 (running), free_CA_mode=0, descriptors_loop_length
        body += struct.pack("!H", 0x8000 | (len(desc) & 0x0FFF))
        body += desc
    seclen = len(body) + 4
    sec = bytes([0x42]) + struct.pack("!H", 0xB000 | seclen) + bytes(body)
    sec += struct.pack("!I", crc32_mpeg(sec))
    return sec


def ts_entry_bundle(freq_hz, tsid, onid, mtype, sr, members):
    """Like ts_entry(), but for a whole bundle: one transport stream entry
    in the NIT that lists ALL channels of the bundle together."""
    desc = cable_descriptor(freq_hz, mtype, sr)
    desc += service_list_descriptor([(m["out_sid"], m["stype"])
                                     for m in members])
    desc += PRIVATE_EACEM
    desc += lcn_descriptor([(m["out_sid"], m["lcn"]) for m in members])
    return struct.pack("!HHH", tsid & 0xFFFF, onid & 0xFFFF,
                       0xF000 | len(desc)) + desc


def build_nit_sections_bundled(nit_rows, network_id, network_name, version=1):
    """Like build_nit_sections(), but one TS entry per bundle instead of per
    single channel - each entry lists all channels of that bundle."""
    name = network_name.encode("latin-1", "replace")[:250]
    net_desc = bytes([0x40, len(name)]) + name

    entries = [ts_entry_bundle(r["freq_hz"], r["tsid"], r["onid"],
                               r["mtype"], r["sr"], r["members"])
              for r in nit_rows]

    groups, cur, cur_len = [], [], 0
    head = 5 + 2 + len(net_desc) + 2
    for e in entries:
        if cur and head + cur_len + len(e) + 4 > 1021:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(e)
        cur_len += len(e)
    if cur:
        groups.append(cur)

    last = len(groups) - 1
    out = []
    for n, grp in enumerate(groups):
        body = bytearray()
        body += struct.pack("!H", network_id & 0xFFFF)
        body += bytes([0xC1 | ((version & 0x1F) << 1)])
        body += bytes([n, last])
        body += struct.pack("!H", 0xF000 | len(net_desc)) + net_desc
        loop = b"".join(grp)
        body += struct.pack("!H", 0xF000 | len(loop)) + loop
        seclen = len(body) + 4
        sec = bytes([0x40]) + struct.pack("!H", 0xB000 | seclen) + bytes(body)
        sec += struct.pack("!I", crc32_mpeg(sec))
        out.append(sec)
    return out


def sections_to_ts(sections, pid=16, cc=0):
    """Packs sections into TS packets."""
    packets = []
    for sec in sections:
        data = b"\x00" + sec                 # pointer_field
        first = True
        while data:
            chunk = data[:184]
            data = data[184:]
            hdr = bytes([0x47,
                         (0x40 if first else 0x00) | (pid >> 8),
                         pid & 0xFF,
                         0x10 | (cc & 0x0F)])
            cc = (cc + 1) & 0x0F
            packets.append(hdr + chunk + b"\xFF" * (184 - len(chunk)))
            first = False
    return packets, cc


# ======================================================================
# Time: TDT and TOT
# ======================================================================
#
# A DVB receiver does NOT get the time from the network but from the
# transport stream - from the Time and Date Table on PID 20. Without it the
# TV does not know what time it is and refuses every recording, saying
# there is no system time.
#
# The TV asks for PID 20 on every connection. Init7 delivers nothing there,
# Tvheadend passes nothing through - so we build it ourselves, just like
# the NIT.
#
# Plus the TOT, the Time Offset Table: it gives the offset of local time
# from UTC. Without it the TV would be two hours off in summer, and so
# would a programmed recording.

TDT_PID = 20


def bcd2(v):
    """A two-digit number as one BCD byte: 23 -> 0x23."""
    v = int(v) % 100
    return ((v // 10) << 4) | (v % 10)


def _mjd_bcd(t):
    """The 5-byte time of the DVB tables: 16-bit Modified Julian Date,
    then hour, minute, second, each as BCD. Always UTC."""
    tm = time.gmtime(t)
    mjd = int(t // 86400) + 40587
    return (struct.pack("!H", mjd & 0xFFFF)
            + bytes([bcd2(tm.tm_hour), bcd2(tm.tm_min), bcd2(tm.tm_sec)]))


def build_tdt_section(t=None):
    """TDT: UTC only, without CRC (that is what the spec says)."""
    t = time.time() if t is None else t
    body = _mjd_bcd(t)
    return bytes([0x70]) + struct.pack("!H", 0x7000 | len(body)) + body


def _local_offset(t):
    """(sign, hours, minutes) of local time relative to UTC, from the
    system's time zone data - so the computer's clock must be right and
    its time zone correct."""
    off = (time.altzone if time.localtime(t).tm_isdst > 0 else time.timezone)
    off = -off                      # time.timezone counts the other way round
    neg = off < 0
    off = abs(off)
    return neg, off // 3600, (off % 3600) // 60


def build_tot_section(t=None, region=None):
    """TOT: UTC plus the offset to local time. With CRC."""
    t = time.time() if t is None else t
    region = (region or opts.country or "CHE")[:3].upper().ljust(3)
    neg, std, minute = _local_offset(t)

    # local_time_offset_descriptor (0x58)
    d = region.encode("latin-1", "replace")
    d += bytes([(0 << 2) | 0x02 | (1 if neg else 0)])   # region 0, sign
    d += bytes([bcd2(std), bcd2(minute)])
    # We do not know the time of the next change - we enter the
    # current time and the same offset. The TV needs the field, but
    # follows the offset above for its clock.
    d += _mjd_bcd(t)
    d += bytes([bcd2(std), bcd2(minute)])
    desc = bytes([0x58, len(d)]) + d

    body = _mjd_bcd(t) + struct.pack("!H", 0xF000 | len(desc)) + desc
    sec = bytes([0x73]) + struct.pack("!H", 0x7000 | (len(body) + 4)) + body
    return sec + struct.pack("!I", crc32_mpeg(sec))


_tdt_cc = [0]


def time_packets():
    """Fresh TDT and TOT packets for PID 20. Rebuilt on every injection,
    so the time is right and does not stand still."""
    if opts is not None and getattr(opts, "no_time", False):
        return []
    try:
        pkts, cc = sections_to_ts(
            [build_tdt_section(), build_tot_section()],
            pid=TDT_PID, cc=_tdt_cc[0])
        _tdt_cc[0] = cc
        return pkts
    except Exception as e:
        log(f"  ! Time table could not be built: {e}")
        return []


CAT_PID = 1


def build_cat_section(version=1):
    """Empty Conditional Access Table: "nothing on this transport stream
    is scrambled". The TX-58DX800W's own Panasonic server sends it exactly
    like this (01 b0 09 ff ff c3 00 00 + CRC, without descriptors).

    That TV asks for PID 1 explicitly during a scan. Without a CAT it
    apparently waited about 30 s per channel to see whether one would come
    - eight channels per bundle, so 240 s per frequency.
    """
    body = bytes([0x01, 0xB0, 0x09, 0xFF, 0xFF,
                  0xC1 | ((version & 0x1F) << 1), 0x00, 0x00])
    return body + struct.pack("!I", crc32_mpeg(body))


# ======================================================================
# Fetch the channel list from Tvheadend and build the NIT from it
# ======================================================================

NIT_PACKETS = []
TSID_BY_FREQ = {}      # frequency in Hz -> tsid we announce in the NIT
BUNDLES = {}            # virtual frequency in Hz -> bundle entry
M3U_TEXT = "#EXTM3U\n"  # ready-made channel list, as the Panasonic offers it
M3U_COUNT = 0


def build_m3u(entries):
    """Builds the channel list in the format of the real Panasonic server.

    Each line gives number and name, followed by a complete RTSP address
    with all tuning values. A client that reads this list need not search:
    it knows every channel with its frequency and can request it directly.

    entries: list of dict(lcn, name, freq_hz, tsid, onid, svcid, stype)"""
    out = ["#EXTM3U"]
    for e in sorted(entries, key=lambda x: x["lcn"]):
        out.append(f"#EXTINF:0,{e['lcn']}. {e['name']}")
        # Format per SAT>IP spec, appendix A ("Extended M3U, Unicast"):
        #   rtsp://<server>/?freq=...&msys=...&mtype=...&sr=...&pids=...
        # The spec explicitly names the channel's PIDs. We used to copy svcid
        # and fca instead - neither appears in the spec, both are Panasonic
        # extras. With the PIDs the bridge also knows at once which channel
        # of a bundle the TV means and need not wait.
        pids = ",".join(str(x) for x in e["pids"])
        out.append(
            f"rtsp://{opts.ip}:{opts.rtsp_port}/?"
            f"freq={freq_mhz_str(e['freq_hz'])}"
            f"&msys=dvbc&mtype={opts.nit_mtype}&sr={opts.nit_sr}"
            f"&specinv=0&pids={pids}")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------
# Sample a channel: read PAT and PMT to find its video/audio PIDs. The
# same technique as in satip_probe.py, but automated for all channels when
# the bridge starts.
# ----------------------------------------------------------------------

def collect_sections(ts, pid):
    """Collects the payload bytes of one PID and cuts out sections."""
    buf = b""
    started = False
    for i in range(0, len(ts) - 187, 188):
        p = ts[i:i + 188]
        if p[0] != 0x47:
            continue
        if ((p[1] & 0x1F) << 8 | p[2]) != pid:
            continue
        pusi = bool(p[1] & 0x40)
        afc = (p[3] >> 4) & 0x3
        idx = 4
        if afc in (2, 3):
            idx += 1 + p[4]
        if idx >= 188:
            continue
        payload = p[idx:]
        if pusi:
            if not payload:
                continue
            ptr = payload[0]
            if started and ptr:
                buf += payload[1:1 + ptr]
                yield buf
            buf = payload[1 + ptr:]
            started = True
        elif started:
            buf += payload
    if started and buf:
        yield buf


def parse_pat_raw(ts):
    """Returns {program_number: pmt_pid}."""
    out = {}
    for sec in collect_sections(ts, 0):
        if len(sec) < 8 or sec[0] != 0x00:
            continue
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        end = 3 + length - 4
        i = 8
        while i + 4 <= end and i + 4 <= len(sec):
            prog = (sec[i] << 8) | sec[i + 1]
            pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
            if prog != 0:
                out[prog] = pid
            i += 4
        if out:
            break
    return out


def parse_pmt_raw(ts, pmt_pid):
    """Returns (pcr_pid, [(stream_type, pid, es_info_bytes), ...])."""
    for sec in collect_sections(ts, pmt_pid):
        if len(sec) < 12 or sec[0] != 0x02:
            continue
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        end = 3 + length - 4
        pcr_pid = ((sec[8] & 0x1F) << 8) | sec[9]
        pil = ((sec[10] & 0x0F) << 8) | sec[11]
        i = 12 + pil
        streams = []
        while i + 5 <= end and i + 5 <= len(sec):
            stype = sec[i]
            pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
            esil = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
            es_info = bytes(sec[i + 5:i + 5 + esil])
            streams.append((stype, pid, es_info))
            i += 5 + esil
        if streams:
            return pcr_pid, streams
    return None, []


MC_OPEN = set()          # all open multicast joins
MC_LOCK = threading.Lock()


class McSource:
    """Direct reception of an Init7 multicast group - without Tvheadend.

    Init7 delivers the finished transport stream as plain UDP packets to a
    multicast group. Joining and reading is all it takes; Tvheadend did
    nothing in between that we cannot do ourselves.

    The socket binds to the group address, not to 0.0.0.0 - otherwise
    every socket would get the packets of *all* groups on the same port,
    and with 243 channels on port 5000 that would be utter chaos.
    """

    def __init__(self, group, port, iface):
        self.group = group
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            self.sock.bind((group, port))
        except OSError:
            self.sock.bind(("", port))
        mreq = struct.pack("4s4s", socket.inet_aton(group),
                           socket.inet_aton(iface))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             mreq)
        self.sock.settimeout(0.02)
        self._mreq = mreq
        self.created = time.time()
        with MC_LOCK:
            MC_OPEN.add(self)

    def close(self):
        with MC_LOCK:
            MC_OPEN.discard(self)
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP,
                                 self._mreq)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ======================================================================
# Second source: HLS channels from an M3U playlist (e.g. iptv-org)
# ======================================================================
#
# Init7 delivers a finished transport stream via multicast; HLS channels
# sit as small chunks on web servers. ffmpeg fetches them and reassembles
# them WITHOUT transcoding (-c copy) into a transport stream, which it sends
# to the bridge via UDP. To the rest of the bridge that looks like a
# multicast group: the same interface as McSource (.sock, .close(),
# .created, .group), the same bundles, tables and sessions.

HLS_UA = "Mozilla/5.0 (X11; Linux x86_64) SatipBridge"


def _die_with_bridge():
    """In the child process before ffmpeg starts: if the bridge dies (even
    by a crash or kill -9), ffmpeg gets SIGTERM. Otherwise it would be
    left orphaned and keep pulling data from the network.

    CAUTION: Linux ties this to the THREAD that starts ffmpeg, not to the
    process. If a short-lived thread starts ffmpeg, ffmpeg dies as soon as
    that thread finishes - exactly why no HLS channel showed a picture at
    first. Therefore only ffmpeg_start() starts ffmpeg, from a single
    thread that lives as long as the bridge."""
    try:
        import ctypes, signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


_spawner_jobs = None


def _spawner():
    while True:
        cmd, done, result = _spawner_jobs.get()
        try:
            result.append(subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, preexec_fn=_die_with_bridge))
        except Exception as e:
            result.append(e)
        done.set()


def ffmpeg_starten(cmd):
    """Starts ffmpeg through the long-lived spawner thread (see above)."""
    global _spawner_jobs
    import queue
    with MC_LOCK:
        if _spawner_jobs is None:
            _spawner_jobs = queue.Queue()
            threading.Thread(target=_spawner, daemon=True,
                             name="ffmpeg-starter").start()
    done, result = threading.Event(), []
    _spawner_jobs.put((cmd, done, result))
    done.wait(10)
    if not result:
        raise RuntimeError("ffmpeg could not be started")
    if isinstance(result[0], Exception):
        raise result[0]
    return result[0]


class HlsSource:
    hls = True

    def __init__(self, url, name="", headers=None):
        self.url = url
        self.group = f"HLS {name or url}"       # for log and sweep
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.bind(("127.0.0.1", 0))
        port = self.sock.getsockname()[1]
        self.sock.settimeout(0.02)
        h = dict(headers or {})
        ua = h.pop("User-Agent", None) or h.pop("user-agent", None)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-user_agent", ua or HLS_UA]
        if h and url.lower().startswith(("http://", "https://")):
            cmd += ["-headers", "".join(f"{k}: {v}\r\n"
                                        for k, v in h.items())]
        # -re: pass on at the video's real pace. Without it ffmpeg
        # forwards every 6-second chunk at once in full (1 MB at once,
        # then nothing for 6 s). A cable receiver expects an even
        # stream; its buffer overflowed and the TV showed no picture
        # on any HLS channel.
        cmd += ["-re",
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5", "-rw_timeout", "15000000",
                "-i", url,
                "-c", "copy", "-f", "mpegts",
                "-mpegts_flags", "+resend_headers", "-flush_packets", "1",
                f"udp://127.0.0.1:{port}?pkt_size=1316"]
        try:
            self.proc = ffmpeg_starten(cmd)
        except Exception:
            self.sock.close()
            raise
        self.created = time.time()
        with MC_LOCK:
            MC_OPEN.add(self)

    def close(self):
        with MC_LOCK:
            MC_OPEN.discard(self)
        p = getattr(self, "proc", None)
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        try:
            self.sock.close()
        except OSError:
            pass


def _extinf_name_and_attributes(z):
    """'#EXTINF:-1 tvg-id="a" group-title="News, Info",Channel' -> name and
    attributes. The comma that separates the name is the first one OUTSIDE
    quotation marks - a comma in group-title must not count."""
    rest = z.split(":", 1)[1] if ":" in z else ""
    in_q = False
    for i, c in enumerate(rest):
        if c == '"':
            in_q = not in_q
        elif c == "," and not in_q:
            attrs, name = rest[:i], rest[i + 1:].strip()
            break
    else:
        attrs, name = rest, ""
    attr_map = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
    return name, attr_map


# What ffmpeg can read besides http(s). Multicast in a second playlist
# works too - ffmpeg then joins the group itself.
HLS_SCHEMES = ("http://", "https://", "rtmp://", "rtmps://", "rtsp://",
                "udp://", "rtp://", "srt://")


def parse_hls_m3u(text):
    """Reads common M3U formats (iptv-org, VLC, Kodi, TiviMate):

      #EXTINF with attributes; commas inside attributes too
      #EXTVLCOPT:http-user-agent= / http-referrer=     (VLC)
      address|User-Agent=...&Referer=...              (Kodi)
      #EXTHTTP:{"User-Agent": "...", ...}             (TiviMate)
      #KODIPROP with a licence (DRM)  -> marked as encrypted
      address without #EXTINF before it -> name from the address

    Result per channel: name, url, logo, language, headers {}, drm."""
    import urllib.parse as _up
    rows, cur = [], None
    text = text.lstrip("﻿")
    for text_line in text.splitlines():
        z = text_line.strip()
        if not z:
            continue
        if z.startswith("#EXTINF"):
            name, w = _extinf_name_and_attributes(z)
            cur = {"name": name or w.get("tvg-name") or "?",
                   "logo": w.get("tvg-logo", ""),
                   "language": w.get("group-title", ""),
                   "headers": {}, "drm": False}
            if w.get("http-user-agent"):
                cur["headers"]["User-Agent"] = w["http-user-agent"]
            if w.get("http-referrer"):
                cur["headers"]["Referer"] = w["http-referrer"]
            continue
        if z.startswith("#"):
            if cur is None:
                cur = {"name": "", "logo": "", "language": "",
                       "headers": {}, "drm": False}
            if z.startswith("#EXTVLCOPT:"):
                k, _, v = z[11:].partition("=")
                if k == "http-user-agent":
                    cur["headers"]["User-Agent"] = v
                elif k == "http-referrer":
                    cur["headers"]["Referer"] = v
                elif k == "http-origin":
                    cur["headers"]["Origin"] = v
            elif z.startswith("#EXTHTTP:"):
                try:
                    for k, v in json.loads(z[9:]).items():
                        cur["headers"][str(k)] = str(v)
                except ValueError:
                    pass
            elif z.startswith("#KODIPROP:") and "license" in z.lower():
                cur["drm"] = True
            continue
        # an address line
        url, _, kodi = z.partition("|")
        if not url.lower().startswith(HLS_SCHEMES):
            cur = None
            continue
        if cur is None:
            cur = {"name": "", "logo": "", "language": "",
                   "headers": {}, "drm": False}
        if kodi:
            for portion in kodi.split("&"):
                k, _, v = portion.partition("=")
                if k:
                    cur["headers"][k.strip()] = _up.unquote(v.strip())
        if not cur["name"]:
            fpath = _up.urlparse(url)
            cur["name"] = (fpath.hostname or "?") + fpath.path.rsplit("/", 1)[0][-20:]
        cur["url"] = url
        rows.append(cur)
        cur = None
    seen, unique = set(), []
    for r in rows:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)
    return unique


def probe_service_hls(row, seconds=25):
    """Starts ffmpeg for an HLS channel and reads PAT+PMT. None if nothing
    usable arrives in time (channel dead, blocked, ...)."""
    src = None
    try:
        src = HlsSource(row["url"], row.get("name", ""), row.get("headers"))
        buf = bytearray()
        deadline = time.time() + seconds
        last_check = 0
        while time.time() < deadline:
            if src.proc.poll() is not None and not buf:
                return None                     # ffmpeg gave up
            try:
                data, _ = src.sock.recvfrom(65535)
            except socket.timeout:
                continue
            buf += ts_payload(data)
            now = time.time()
            if now - last_check > 0.3 and len(buf) > 50_000:
                last_check = now
                pat = parse_pat_raw(bytes(buf))
                if pat:
                    pmt_pid = next(iter(pat.values()))
                    pcr_pid, streams = parse_pmt_raw(bytes(buf), pmt_pid)
                    if streams:
                        return {"pcr_pid": pcr_pid,
                                "streams": [(t, p, e.hex())
                                            for t, p, e in streams]}
        return None
    except Exception:
        return None
    finally:
        if src:
            src.close()


# State of the HLS check: which channels deliver, which are admitted
# permanently into the channel list (order = channel number from 301).
# Admitted stays admitted - otherwise every restart would move all HLS
# channels to other frequencies.
_hls_lock = threading.Lock()
# "info": video/audio tracks per channel. Deliberately kept HERE and not
# in pmt_cache.json: that file may still belong to root (from the time
# with sudo); then writing fails silently and the channels are missing.
HLS_STATE = {"checks": {}, "admitted": [], "info": {}, "finished": None}


def _hls_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "hls_state.json")


def hls_load():
    try:
        with open(_hls_path(), encoding="utf-8") as f:
            d = json.load(f)
        HLS_STATE.update(d)
        HLS_STATE.setdefault("info", {})
    except (OSError, ValueError):
        pass
    if SETTINGS.get("source2_reset"):
        # Chosen in the control panel when a new list was set for source 2:
        # forget the admitted channels and number them afresh. The TVs
        # need a channel scan afterwards - the panel said so.
        HLS_STATE.update({"checks": {}, "admitted": [], "info": {},
                          "finished": None})
        hls_save()
        SETTINGS.pop("source2_reset", None)
        settings_save({k: v for k, v in SETTINGS.items()
                       if not k.startswith("_")})
        log("Source 2: starting over with the new list (channels will be "
            "checked and numbered afresh)")


def hls_save():
    try:
        with open(_hls_path() + ".new", "w", encoding="utf-8") as f:
            json.dump(HLS_STATE, f, ensure_ascii=False)
        os.replace(_hls_path() + ".new", _hls_path())
    except OSError as e:
        log(f"  ! {_hls_path()} not writable: {e}")


def load_hls_playlist():
    import urllib.request as _u
    req = _u.Request(opts.hls_url, headers={"User-Agent": HLS_UA})
    with _u.build_opener(_u.ProxyHandler({})).open(req, timeout=30) as r:
        return parse_hls_m3u(r.read().decode("utf-8", "replace"))


def hls_checker():
    """Checks in the background all HLS channels that are not admitted yet
    (and dead ones again after a week). NEVER changes the running channel
    list - new channels appear after a restart of the bridge and a channel
    scan on the TV."""
    time.sleep(30)                      # serve the TVs first
    while True:
        try:
            items = load_hls_playlist()
        except Exception as e:
            log(f"HLS playlist not loadable ({opts.hls_url}): {e}")
            time.sleep(3600)
            continue
        drm = [r for r in items if r.get("drm")]
        items = [r for r in items if not r.get("drm")]
        if drm:
            log(f"HLS: {len(drm)} channels are encrypted (DRM) - "
                f"not possible, skipped")
        if len(items) > opts.hls_max:
            log(f"HLS: playlist has {len(items)} channels, checking the "
                f"first {opts.hls_max} (--hls-max)")
            items = items[:opts.hls_max]
        now_ts = time.time()
        with _hls_lock:
            for r in drm:
                HLS_STATE["checks"][r["url"]] = {
                    "ok": False, "drm": True, "time": time.time(),
                    "name": r["name"]}
            admitted = set(HLS_STATE["admitted"])
            pr = HLS_STATE["checks"]
            infos = HLS_STATE["info"]
            pending = [r for r in items
                     if (r["url"] in admitted and r["url"] not in infos)
                     or (r["url"] not in admitted and
                         now_ts - pr.get(r["url"], {}).get("time", 0)
                         > 7 * 86400)]
        if pending:
            log(f"HLS check: {len(pending)} of {len(items)} channels "
                f"being checked ({opts.hls_parallel} at a time) ...")
        fresh = []
        gate = threading.Semaphore(max(1, opts.hls_parallel))

        def check_one(r):
            with gate:
                info = probe_service_hls(r)
            with _hls_lock:
                HLS_STATE["checks"][r["url"]] = {
                    "ok": info is not None, "time": time.time(),
                    "name": r["name"], "logo": r.get("logo", ""),
                    "headers": r.get("headers") or {}}
                if info is not None:
                    HLS_STATE["info"][r["url"]] = info
                    if r["url"] not in HLS_STATE["admitted"]:
                        HLS_STATE["admitted"].append(r["url"])
                        fresh.append(r["name"])
                hls_save()

        workers = [threading.Thread(target=check_one, args=(r,), daemon=True)
                  for r in pending]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        with _hls_lock:
            HLS_STATE["finished"] = time.time()
            HLS_STATE["list_count"] = len(items)
            hls_save()
            ok = sum(1 for v in HLS_STATE["checks"].values() if v.get("ok"))
        if pending:
            log(f"HLS check finished: {ok} channels deliver, "
                f"{len(fresh)} newly admitted"
                + (" - visible after a restart of the bridge and a channel scan"
                   if fresh else ""))
        time.sleep(7 * 86400)


def ts_payload(data):
    """Extracts the pure transport stream from a UDP packet.

    Init7's playlist says udp:// for every channel, but not all deliver the
    same: most come as a raw transport stream, some wrapped in RTP - i.e.
    with a twelve-byte header in front. Reading blindly from byte zero
    never finds a PAT in those, and the channel is wrongly taken for dead.
    Exactly that made 27 channels fail, among them Bloomberg TV and BBC Two.

    Tvheadend handles this silently; here it is written out.
    """
    if not data:
        return b""
    # Raw transport stream: starts with 0x47, and 188 bytes later there
    # is another one. Two hits suffice, a single 0x47 could be chance.
    if data[0] == 0x47 and (len(data) < 189 or data[188] == 0x47):
        return data
    # RTP: version 2 in the top two bits, then the transport stream.
    if len(data) > 12 and (data[0] >> 6) == 2:
        hlen = 12 + 4 * (data[0] & 0x0F)
        if data[0] & 0x10 and len(data) >= hlen + 4:
            hlen += 4 + 4 * ((data[hlen + 2] << 8) | data[hlen + 3])
        if len(data) > hlen and data[hlen] == 0x47:
            return data[hlen:]
    # Last resort: find the first 0x47 that fits the 188-byte grid.
    for i in range(1, min(len(data) - 1, 200)):
        if data[i] == 0x47 and (i + 188 >= len(data)
                                or data[i + 188] == 0x47):
            return data[i:]
    return b""


def parse_init7_m3u(text):
    """Reads Init7's channel list. Order = channel number."""
    rows = []
    name = None
    language = ""
    for text_line in text.splitlines():
        z = text_line.strip()
        if z.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', z)
            language = m.group(1) if m else ""
            # The logo address later is the key to the programme guide:
            # Init7 and the TV7 guide both name the same one, and through it
            # every channel finds its programme exactly.
            m = re.search(r'tvg-logo="([^"]*)"', z)
            logo = m.group(1) if m else ""
            name = z.split(",", 1)[1].strip() if "," in z else "?"
        elif z.startswith("udp://") or z.startswith("rtp://"):
            m = re.search(r"@?([0-9.]+):(\d+)", z)
            if m and name:
                rows.append({"name": name, "group": m.group(1),
                             "port": int(m.group(2)), "language": language,
                             "logo": logo,
                             "number": len(rows) + 1})
            name = None
    return rows


def load_channels_init7():
    """Channel list directly from Init7 - without Tvheadend."""
    import urllib.request as _u
    log(f"Fetching channel list directly from Init7 ({opts.init7_url}) ...")
    op = _u.build_opener(_u.ProxyHandler({}))
    with op.open(opts.init7_url, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    rows = parse_init7_m3u(text)
    if not rows:
        raise RuntimeError("channel list empty or unreadable")
    log(f"  {len(rows)} channels in the list.")
    out = []
    for r in rows:
        out.append({
            "freq_hz": 0,                  # only comes from the bundling
            "sid": r["number"],
            "stype": 1,
            "name": r["name"],
            "number": r["number"],
            "provider": "Init7",
            "group": r["group"],
            "port": r["port"],
            "language": r.get("language", ""),
            "logo": r.get("logo", ""),
        })
    return out


def hls_rows():
    """The permanently admitted HLS channels as rows for the bundling,
    channel numbers from --hls-lcn in admission order."""
    out = []
    with _hls_lock:
        admitted = list(HLS_STATE["admitted"])
        pr = dict(HLS_STATE["checks"])
        infos = dict(HLS_STATE["info"])
    lacking = 0
    for i, url in enumerate(admitted):
        info = infos.get(url)
        if not info:
            lacking += 1
            continue
        meta = pr.get(url, {})
        video = any(t in (0x01, 0x02, 0x1B, 0x24, 0x10)
                    for t, _p, _e in info["streams"])
        out.append({
            "freq_hz": 0, "sid": opts.hls_lcn + i, "stype": 1 if video else 2,
            "name": meta.get("name") or url, "number": opts.hls_lcn + i,
            "provider": "HLS", "hls": True, "url": url,
            "headers": meta.get("headers") or {
                k: v for k, v in (("User-Agent", meta.get("ua")),
                                  ("Referer", meta.get("referer"))) if v},
            "logo": meta.get("logo", ""), "source": "hls",
        })
    if out:
        log(f"  {len(out)} HLS channels from the second source (from number "
            f"{opts.hls_lcn})")
    if lacking:
        log(f"  {lacking} admitted HLS channels without stored tracks - "
            f"being checked again in the background")
    return out


def probe_service_mc(group, port, seconds):
    """Like probe_service(), but directly at the multicast group."""
    src = None
    try:
        src = McSource(group, port, opts.ip)
        buf = bytearray()
        deadline = time.time() + seconds
        last_check = 0
        while time.time() < deadline:
            try:
                data, _ = src.sock.recvfrom(65535)
            except socket.timeout:
                continue
            buf += ts_payload(data)         # raw or RTP - recognised
            now = time.time()
            if now - last_check > 0.15:
                last_check = now
                pat = parse_pat_raw(bytes(buf))
                if pat:
                    pmt_pid = next(iter(pat.values()))
                    pcr_pid, streams = parse_pmt_raw(bytes(buf), pmt_pid)
                    if streams:
                        return {"pcr_pid": pcr_pid,
                                "streams": [(t, p, e.hex())
                                            for t, p, e in streams]}
        pat = parse_pat_raw(bytes(buf))
        if not pat:
            return None
        pmt_pid = next(iter(pat.values()))
        pcr_pid, streams = parse_pmt_raw(bytes(buf), pmt_pid)
        if not streams:
            return None
        return {"pcr_pid": pcr_pid,
                "streams": [(t, p, e.hex()) for t, p, e in streams]}
    except Exception:
        return None
    finally:
        if src:
            src.close()


def probe_service(tune_real, seconds):
    """Briefly connects to Tvheadend (like satip_probe.py) and reads the
    PAT+PMT of the channel on tune_real. Returns {"pcr_pid":.., "streams":
    [(stype,pid,es_info_hex), ...]}, or None."""
    try:
        s1, s2 = None, None
        base = 43000
        while base < 44000:
            try:
                s1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s1.bind(("", base))
                s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s2.bind(("", base + 1))
                break
            except OSError:
                try:
                    s1.close()
                except Exception:
                    pass
                base += 2
        else:
            return None
    except OSError:
        return None

    up = None
    try:
        s1.settimeout(1.0)
        up = Upstream(opts.tvh_host, opts.tvh_rtsp_port)
        up.setup(tune_real, base)
        up.play()
        buf = bytearray()
        deadline = time.time() + seconds
        last_check = 0
        while time.time() < deadline:
            try:
                data, _ = s1.recvfrom(65535)
            except socket.timeout:
                continue
            if len(data) < 12 or (data[0] >> 6) != 2:
                continue
            hlen = 12 + 4 * (data[0] & 0x0F)
            if data[0] & 0x10 and len(data) >= hlen + 4:
                hlen += 4 + 4 * ((data[hlen + 2] << 8) | data[hlen + 3])
            buf += data[hlen:]
            # Do not parse again on every packet (costs time), but once PAT+PMT
            # are found, do not wait longer than necessary.
            now = time.time()
            if now - last_check > 0.15:
                last_check = now
                pat = parse_pat_raw(bytes(buf))
                if pat:
                    pmt_pid = next(iter(pat.values()))
                    pcr_pid, streams = parse_pmt_raw(bytes(buf), pmt_pid)
                    if streams:
                        return {"pcr_pid": pcr_pid,
                                "streams": [(t, p, e.hex())
                                           for t, p, e in streams]}
        pat = parse_pat_raw(bytes(buf))
        if not pat:
            return None
        pmt_pid = next(iter(pat.values()))
        pcr_pid, streams = parse_pmt_raw(bytes(buf), pmt_pid)
        if not streams:
            return None
        return {"pcr_pid": pcr_pid,
                "streams": [(t, p, e.hex()) for t, p, e in streams]}
    except Exception:
        return None
    finally:
        try:
            if up:
                up.teardown()
        except Exception:
            pass
        for s in (s1, s2):
            try:
                s and s.close()
            except OSError:
                pass


def load_pmt_cache():
    if opts.rescan_pmt or not os.path.exists(opts.pmt_cache):
        return {}
    try:
        with open(opts.pmt_cache) as f:
            return json.load(f)
    except Exception as e:
        log(f"  ! PID cache could not be read: {e}")
        return {}


def load_pmt_cache_raw():
    try:
        with open(opts.pmt_cache) as f:
            return json.load(f)
    except Exception:
        return {}


def save_pmt_cache(cache):
    try:
        with open(opts.pmt_cache, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        log(f"  ! PID cache could not be written: {e}")


def freq_mhz_str(hz):
    return f"{hz / 1_000_000:.2f}"


def slot_base_pid(slot_index):
    """16 PIDs of room per bundle slot: PMT + up to 15 elementary streams.
    Starts at 0x0100, far away from the standard PSI PIDs 0-31."""
    return 0x0100 + slot_index * 0x10


def build_bundle_plan(rows):
    """Groups the channel list into bundles of opts.bundle_size channels,
    samples missing channels (cache), and builds the finished PAT/PMT/SDT
    packets and the NIT entry for every bundle."""
    cache = load_pmt_cache()
    cache_dirty = False

    order = sorted(rows, key=lambda x: (x["number"] == 0, x["number"],
                                        x["name"].lower()))
    for i, r in enumerate(order, 1):
        r["lcn"] = r["number"] or i

    # Every source starts on a frequency of its own. Otherwise new HLS
    # channels would fill up the last, half-full Init7 bundle - and the TV
    # would see different content there after every change.
    groups = []
    portion = [r for r in order if r.get("source", "init7") == "init7"]
    groups += [portion[i:i + opts.bundle_size]
               for i in range(0, len(portion), opts.bundle_size)]
    # HLS: FIXED slots. Channel number n always sits in bundle
    # (n - hls_lcn) // bundle size, in slot (n - hls_lcn) % ..., with
    # service ID n. If a channel is missing (tracks unknown, currently
    # dead), its slot stays empty - otherwise all following channels slip
    # to other frequencies and the TV needs a new scan.
    hls = [r for r in order if r.get("source") == "hls"]
    if hls:
        highest = max(r["number"] for r in hls) - opts.hls_lcn
        count = highest // opts.bundle_size + 1
        slot_grid = [[None] * opts.bundle_size for _ in range(count)]
        for r in hls:
            i = r["number"] - opts.hls_lcn
            slot_grid[i // opts.bundle_size][i % opts.bundle_size] = r
        groups += slot_grid

    log(f"Bundling {len(order)} channels into {len(groups)} frequencies "
        f"(up to {opts.bundle_size} each) ...")

    bundles = {}
    nit_rows = []
    ok_channels = 0
    # Service IDs are assigned NETWORK-WIDE in sequence, not per bundle.
    # A real cable network does the same: onid stays the same, tsid changes
    # per frequency, and a service ID occurs only once in the whole network.
    # If every bundle started again at 1, 2, 3, TVs that keep their channel
    # list by network and service ID would merge all frequencies into the
    # same three entries - and you would end up with three channels instead
    # of 243.
    next_sid = 1
    for g, group in enumerate(groups):
        vfreq = opts.bundle_base_hz + g * opts.bundle_step_hz
        tsid = 10 + g
        members = []
        pmt_packets = {}
        for k, ch in enumerate(group):
            if ch is None:
                continue                # free HLS slot, see above
            if ch.get("hls"):
                tune_real = None
                key = "hls:" + ch["url"]
            elif opts.source == "init7":
                tune_real = None
                key = f"mc:{ch['group']}:{ch['port']}"
            else:
                tune_real = (f"freq={freq_mhz_str(ch['freq_hz'])}&msys=dvbc"
                            f"&mtype={opts.nit_mtype}&sr={opts.nit_sr}")
                key = f"{ch['sid']}@{ch['freq_hz']}"
            info = (HLS_STATE["info"].get(ch["url"]) if ch.get("hls")
                    else cache.get(key))
            if info is None and ch.get("hls"):
                pass        # HLS is only checked in the background
            elif info is None:
                if opts.source == "init7":
                    info = probe_service_mc(ch["group"], ch["port"],
                                            opts.probe_seconds)
                else:
                    info = probe_service(tune_real, opts.probe_seconds)
                cache_dirty = True
                if info is not None:
                    cache[key] = info

            if info is None:
                log(f"  ! {ch['name']}: no PAT/PMT found, "
                    f"channel left out")
                continue

            pmt_pid_new = slot_base_pid(k)
            remap = {}
            new_pid = pmt_pid_new + 1
            streams_new = []
            for stype, opid, es_hex in info["streams"]:
                if new_pid > pmt_pid_new + 15:
                    log(f"  ! {ch['name']}: more than 15 elementary streams, "
                        f"rest left out")
                    break
                remap[opid] = new_pid
                streams_new.append((stype, new_pid, bytes.fromhex(es_hex)))
                new_pid += 1
            pcr_pid_orig = info.get("pcr_pid")
            if pcr_pid_orig is not None and pcr_pid_orig not in remap:
                if new_pid <= pmt_pid_new + 15:
                    remap[pcr_pid_orig] = new_pid
                    new_pid += 1
            pcr_pid_new = remap.get(pcr_pid_orig, streams_new[0][1]
                                    if streams_new else pmt_pid_new + 1)

            # The original "sid" comes from the respective provider and is
            # often a small, frequently used number (e.g. 1) - between
            # providers that collides all the time. So every channel gets a
            # fresh ID here, sequential across the whole network (see note
            # above).
            if ch.get("hls"):
                ch["out_sid"] = ch["number"]    # fixed, see above
            else:
                ch["out_sid"] = next_sid
                next_sid += 1
            ch["pmt_pid"] = pmt_pid_new
            ch["pid_map"] = remap
            ch["tune_real"] = tune_real
            members.append(ch)
            pmt_packets[pmt_pid_new], _ = sections_to_ts(
                [build_pmt_section(ch["out_sid"], pcr_pid_new, streams_new)],
                pid=pmt_pid_new)

        if not members:
            continue

        pat_packets, _ = sections_to_ts(
            [build_pat_section([(m["out_sid"], m["pmt_pid"])
                                for m in members], tsid)], pid=0)
        sdt_packets, _ = sections_to_ts(
            [build_sdt_section(
                [{"sid": m["out_sid"], "stype": m["stype"],
                  "provider": m.get("provider", ""), "name": m["name"]}
                 for m in members], tsid, opts.onid)], pid=17)

        bundles[vfreq] = {
            "tsid": tsid,
            "members": members,
            "pat_packets": pat_packets,
            "pmt_packets": pmt_packets,
            "sdt_packets": sdt_packets,
        }
        nit_rows.append({"freq_hz": vfreq, "tsid": tsid, "onid": opts.onid,
                         "mtype": opts.nit_mtype, "sr": opts.nit_sr,
                         "members": members})
        ok_channels += len(members)
        if (g + 1) % 5 == 0 or g + 1 == len(groups):
            log(f"  Frequency {g + 1}/{len(groups)} done "
                f"({ok_channels} channels so far)")
            # Save the intermediate state at once. When it was only saved at the
            # end, an abort lost the whole sampling - and on the first run over
            # 242 channels that is a lot of lost time.
            if cache_dirty:
                try:
                    save_pmt_cache(cache)
                    cache_dirty = False
                except Exception as e:
                    log(f"  ! Cache not writable: {e}")

    if cache_dirty:
        save_pmt_cache(cache)

    return bundles, nit_rows


def tvh_api(path):
    import base64 as _b64
    import json as _json
    import urllib.request as _u
    req = _u.Request(f"http://{opts.tvh_host}:{opts.tvh_http_port}{path}")
    if opts.tvh_user:
        tok = _b64.b64encode(
            f"{opts.tvh_user}:{opts.tvh_password}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    op = _u.build_opener(_u.ProxyHandler({}))
    with op.open(req, timeout=20) as r:
        return _json.loads(r.read().decode())


def load_channels():
    """Returns the channel list for the NIT: frequency, IDs, number."""
    log("Fetching channel list from Tvheadend ...")
    muxes = {}
    missing = []
    for e in tvh_api("/api/mpegts/mux/grid?limit=100000").get("entries", []):
        u = e.get("uuid")
        if not u:
            continue
        muxes[u] = {"freq_hz": e.get("iptv_satip_dvbc_freq"),
                    "enabled": e.get("enabled", 1)}
        if muxes[u]["freq_hz"] is None:
            missing.append(u)
    if missing:
        log(f"  Frequency field missing in the grid, loading {len(missing)} muxes "
            f"one by one (takes a moment) ...")
        for n, u in enumerate(missing, 1):
            try:
                for p in tvh_api(f"/api/idnode/load?uuid={u}")["entries"][0]["params"]:
                    if p["id"] == "iptv_satip_dvbc_freq":
                        muxes[u]["freq_hz"] = p.get("value", 0)
            except Exception:
                pass
            if n % 50 == 0:
                log(f"    {n}/{len(missing)}")

    services = tvh_api("/api/mpegts/service/grid?limit=100000").get("entries", [])

    # channel names and numbers, if maintained in Tvheadend
    names = {}
    try:
        for e in tvh_api("/api/channel/grid?limit=100000").get("entries", []):
            for su in e.get("services", []) or []:
                names[su] = (e.get("number", 0), e.get("name", ""))
    except Exception:
        pass

    rows = []
    for s in services:
        if not s.get("enabled", True):
            continue
        m = muxes.get(s.get("multiplex_uuid"))
        if not m or not m.get("freq_hz"):
            continue
        num, nm = names.get(s["uuid"], (0, ""))
        rows.append({"freq_hz": int(m["freq_hz"]),
                     "sid": int(s.get("sid", 0)),
                     "stype": int(s.get("dvb_servicetype", 1)) or 1,
                     "name": nm or s.get("svcname", ""),
                     "number": num,
                     "provider": s.get("provider", ""),
                     "svc_uuid": s.get("uuid")})

    # Sort by frequency, assign a tsid per frequency, number the
    # channels without gaps
    rows.sort(key=lambda r: (r["freq_hz"], r["sid"]))
    tsid_of = {}
    for r in rows:
        f = r["freq_hz"]
        if f not in tsid_of:
            tsid_of[f] = 10 + len(tsid_of)
        r["tsid"] = tsid_of[f]
        r["onid"] = opts.onid
        r["mtype"] = opts.nit_mtype
        r["sr"] = opts.nit_sr
    for i, r in enumerate(sorted(rows, key=lambda x: (x["number"] == 0,
                                                      x["number"],
                                                      x["name"].lower())), 1):
        r["lcn"] = r["number"] or i

    return rows


def auto_bundle_size(count):
    """Smallest bundle size at which all channels fit into the cable band."""
    slot_grid = max(1, (opts.nit_max_hz - opts.bundle_base_hz)
                  // opts.bundle_step_hz + 1)
    if count <= 0:
        return 1
    return max(1, (count + slot_grid - 1) // slot_grid)


def prepare_nit():
    global NIT_PACKETS, BUNDLES, M3U_TEXT, M3U_COUNT
    # The SDT tells the TV whether to look for a programme guide on PID 18.
    # It is built further down during bundling, so the answer must be
    # settled here already - not only once the guide is loaded. That is no
    # white lie: we inject it shortly, it just takes a few seconds longer.
    EPG_FLAGS[0] = 0 if (opts.no_epg or not opts.epg_url
                         or satipepg is None) else 1
    try:
        rows = (load_channels_init7() if opts.source == "init7"
                else load_channels())
    except Exception as e:
        log(f"  ! Channel list could not be loaded: {e}")
        log("  ! The bridge keeps running, but without NIT - the TV "
            "then only finds the frequency it is currently on.")
        return

    if not rows:
        log("  ! No channels with a SAT>IP frequency found, no NIT.")
        return

    if opts.source == "init7" and not opts.no_hls:
        rows += hls_rows()

    if opts.source == "init7" and opts.bundle_size <= 1:
        # Without Tvheadend there are no given frequencies - the bridge assigns
        # them itself, and the bundling part further down does that.
        opts.bundle_size = auto_bundle_size(len(rows))
        log(f"  Bundle size chosen automatically: {opts.bundle_size} "
            f"channels per frequency")

    if opts.bundle_size <= 1:
        inband = [r for r in rows if r["freq_hz"] <= opts.nit_max_hz]
        skipped = len(rows) - len(inband)

        global TSID_BY_FREQ
        TSID_BY_FREQ = {r["freq_hz"]: r["tsid"] for r in inband}

        secs = build_nit_sections(inband, opts.network_id, opts.network_name)
        NIT_PACKETS, _ = sections_to_ts(secs, pid=16)

        M3U_TEXT = build_m3u([
            {"lcn": r["lcn"], "name": r["name"], "freq_hz": r["freq_hz"],
             "tsid": r["tsid"], "onid": r["onid"], "svcid": r["sid"],
             "stype": r["stype"], "pids": ["all"]} for r in inband])
        M3U_COUNT = len(inband)

        freqs = sorted({r["freq_hz"] for r in inband})
        log(f"NIT built: {len(inband)} channels on {len(freqs)} frequencies "
            f"({freqs[0]/1e6:.0f}-{freqs[-1]/1e6:.0f} MHz), "
            f"{len(secs)} sections, {len(NIT_PACKETS)} TS packets")
        if skipped:
            log(f"  {skipped} channels lie above "
                f"{opts.nit_max_hz/1e6:.0f} MHz and are left out - "
                f"outside the cable band.")
        return

    # -- Bundle mode: several channels per frequency -------------------
    bundles, nit_rows = build_bundle_plan(rows)
    if not nit_rows:
        log("  ! Not a single bundle could be built (see "
            "errors above) - no NIT.")
        return

    BUNDLES = bundles
    secs = build_nit_sections_bundled(nit_rows, opts.network_id,
                                      opts.network_name)
    NIT_PACKETS, _ = sections_to_ts(secs, pid=16)

    m3u_entries = []
    for r in nit_rows:
        for m in r["members"]:
            own_pids = sorted(set(m.get("pid_map", {}).values()))
            m3u_entries.append({
                "lcn": m["lcn"], "name": m["name"], "freq_hz": r["freq_hz"],
                "tsid": r["tsid"], "onid": r["onid"],
                "svcid": m["out_sid"], "stype": m["stype"],
                # PAT, NIT, SDT, then this channel's PMT and its video
                # and audio streams. 18 is the programme guide, 20 the
                # time - without both the guide stays empty and the
                # record button silent.
                "pids": [0, 16, 17, 18, 20, m["pmt_pid"]] + own_pids})
    M3U_TEXT = build_m3u(m3u_entries)
    M3U_COUNT = len(m3u_entries)

    total_ch = sum(len(r["members"]) for r in nit_rows)
    freqs = sorted(BUNDLES.keys())
    log(f"NIT built: {total_ch} channels bundled on {len(freqs)} "
        f"frequencies ({freqs[0]/1e6:.1f}-{freqs[-1]/1e6:.1f} MHz), "
        f"{len(secs)} sections, {len(NIT_PACKETS)} TS packets")
    log(f"Channel list ready: {M3U_COUNT} entries at "
        f"http://{opts.ip}:{opts.http_port}/satipsrv/satip.m3u")

    start_epg(nit_rows)


def start_epg(nit_rows):
    """Starts the programme guide - in the background, without obligation.

    Everything in here is wrapped in protection: a missing programme guide
    must never cost the TV picture. If something fails, it is in the log
    and the bridge keeps running as before.
    """
    global EPG
    if opts.no_epg or not opts.epg_url:
        log("Programme guide: switched off.")
        return
    if satipepg is None:
        log(f"  ! Programme guide not possible: satipepg.py missing "
            f"or broken ({_EPG_IMPORT_ERROR})")
        return
    try:
        services_info = []
        for r in nit_rows:
            for m in r["members"]:
                services_info.append({"sid": m["out_sid"], "name": m["name"],
                                "logo": m.get("logo", ""),
                                "tsid": r["tsid"], "onid": r["onid"]})
        EPG = satipepg.Guide(opts.epg_url, crc32_mpeg, log=log,
                               refresh=opts.epg_refresh * 3600)
        EPG.learn(services_info)
        EPG.start_guide()
        log(f"Programme guide loading in the background "
            f"({len(services_info)} channels).")
    except Exception as e:
        EPG = None
        EPG_FLAGS[0] = 0
        log(f"  ! Programme guide could not be started: {e}")


if __name__ == "__main__":
    sys.exit(main())
