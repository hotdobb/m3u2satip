#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hotdobb - m3u2satip, see LICENSE
"""
satipepg.py - programme guide for satipbridge.py
================================================

Fetches an XMLTV programme guide and builds EIT sections from it, as a DVB
receiver expects them.

Why at all: the TV does NOT read the programme guide from the network but
from the transport stream - from the Event Information Table on PID 18.
Just as it reads the time from the TDT and the frequency list from the NIT.
An XMLTV file alone is of no use to it; someone has to translate it into
DVB tables. That is what this file does.

Two kinds of tables, both needed:

  EIT present/following (0x4E)  "now" and "next" for the channel currently
                                tuned. That is what is shown briefly when
                                switching channels.
  EIT schedule (0x50)           The schedule over days - the actual
                                programme guide. Split into three-hour
                                segments, as the DVB standard prescribes.

Matching XMLTV channels <-> Init7 channels goes by the logo address: both
sources take their logos from api.tv.init7.net, so the address matches
character by character. That is more reliable than comparing names.

This file also runs on its own:

    python3 satipepg.py <xmltv-url> [init7.m3u]

It then checks the source, shows the matching and builds sections as a
trial, without the bridge running.

No third-party libraries.
"""

import gzip
import io
import re
import struct
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

EIT_PID = 18
TS = 188

# How far ahead programmes are kept. More costs memory and brings little -
# hardly anyone browses further than two days.
LOOKAHEAD_HOURS = 50

# A schedule segment covers three hours, as DVB prescribes, and at most
# eight sections are allowed per segment.
SEGMENT = 3 * 3600
SECTIONS_PER_SEGMENT = 8

# A section may be up to 4093 bytes long. We stay well below: smaller
# sections cross the line faster and a lost packet costs less. 2000 bytes
# hold about twenty programmes, so eight sections per segment are plenty.
SECTION_MAX = 2000


# ----------------------------------------------------------------------
# Fetch and parse XMLTV
# ----------------------------------------------------------------------

def _time(s):
    """XMLTV time in seconds since 1970.

    Format: '20260925143000 +0200' - the time zone is included, and it is
    not always the same. Overlooking it puts the whole programme guide
    hours off.
    """
    s = (s or "").strip()
    m = re.match(r"^(\d{14})(?:\s*([+-])(\d{2})(\d{2}))?$", s)
    if not m:
        return None
    t = m.group(1)
    tm = (int(t[0:4]), int(t[4:6]), int(t[6:8]),
          int(t[8:10]), int(t[10:12]), int(t[12:14]), 0, 0, 0)
    try:
        import calendar
        sec = calendar.timegm(tm)
    except Exception:
        return None
    if m.group(2):
        offset = int(m.group(3)) * 3600 + int(m.group(4)) * 60
        sec -= offset if m.group(2) == "+" else -offset
    return sec


def load_xmltv(url, log=print, timeout=120, lookahead=None):
    """Loads the programme guide and returns

        ({channel_id: {"logo":.., "name":..}},
         {channel_id: [(start, end, title, text), ...]})

    It is read as a stream: the unpacked file is over 50 MB, and keeping it
    completely in memory and then again as a tree would be needlessly
    wasteful.
    """
    lookahead = LOOKAHEAD_HOURS if lookahead is None else lookahead
    log(f"Programme guide loading: {url}")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(url, timeout=timeout) as r:
        raw = r.read()
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    now_ts = time.time()
    until = now_ts + lookahead * 3600
    since = now_ts - 3600          # do not throw away the programme currently running

    channels = {}
    programmes = {}
    total = 0

    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag == "channel":
            chan_id = el.get("id") or ""
            icon = el.find("icon")
            name = el.find("display-name")
            channels[chan_id] = {
                "logo": (icon.get("src") if icon is not None else "") or "",
                "name": (name.text if name is not None else "") or "",
            }
            el.clear()
        elif el.tag == "programme":
            chan_id = el.get("channel") or ""
            a = _time(el.get("start"))
            e = _time(el.get("stop"))
            if a is None or e is None or e <= a or e < since or a > until:
                el.clear()
                continue
            title = el.findtext("title") or ""
            text = (el.findtext("sub-title") or el.findtext("desc") or "")
            programmes.setdefault(chan_id, []).append(
                (a, e, title.strip(), text.strip()))
            total += 1
            el.clear()

    for v in programmes.values():
        v.sort()
    log(f"  {len(channels)} channels, {total} programmes in the next "
        f"{lookahead} hours")
    return channels, programmes


def _die_with_parent():
    """Runs in the helper process: if the bridge dies (even by kill -9),
    the helper gets SIGTERM instead of parsing on as an orphan. The guide
    thread that starts the helper lives as long as the bridge, so Linux's
    per-thread rule for PR_SET_PDEATHSIG does no harm here."""
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
    except Exception:
        pass


def _child_log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def load_xmltv_separately(url, log=print):
    """load_xmltv() in a process of its own.

    Parsing the 50 MB XML takes about ten seconds of pure CPU. In a thread of
    the bridge it holds Python's global interpreter lock all that time, and
    the threads forwarding video to the TVs only get a turn every few
    milliseconds - far too rarely for hundreds of packets per second. The
    result, measured: about 11 seconds of stuttering picture after every
    start of the bridge and on every refresh. In its own process the parsing
    competes for nothing. If that fails for any reason, it falls back to the
    old way, so a missing guide never costs the picture."""
    try:
        import multiprocessing
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(1, initializer=_die_with_parent) as pool:
            return pool.apply(load_xmltv, (url,), {"log": _child_log})
    except Exception as e:
        log(f"  ! Separate process for the guide not possible ({e}) - "
            f"loading in the bridge itself")
        return load_xmltv(url, log=log)


def map_channels(channels, member_list, log=print):
    """Assigns its XMLTV ID to every channel of the bridge.

    member_list: list of dicts with "name", "logo" and if possible "sid".
    Returns {sid_or_name: channel_id}

    First by logo address - for both sources it comes from Init7 itself and
    therefore matches exactly. Only if that fails, by the displayed name.

    The key is the service ID, not the name: Init7 carries channels with the
    same name (e.g. two regional windows of the same programme). A mapping
    by name would merge them, and one of them would lose its guide.
    """
    by_logo = {v["logo"]: k for k, v in channels.items() if v["logo"]}
    by_name = {_key(v["name"]): k
                 for k, v in channels.items() if v["name"]}
    mapping = {}
    per_logo = per_name = 0
    for m in member_list:
        chan_id = by_logo.get(m.get("logo") or "")
        if chan_id:
            per_logo += 1
        else:
            chan_id = by_name.get(_key(m.get("name") or ""))
            if chan_id:
                per_name += 1
        if chan_id:
            mapping[m.get("sid", m.get("name"))] = chan_id
    lacking = len(member_list) - len(mapping)
    log(f"  Matching: {per_logo} by logo, {per_name} by name, "
        f"{lacking} without programme data")
    return mapping


def _key(name):
    """Make names comparable: lower case, without HD/SD and without
    spaces and special characters. 'SRF 1 HD' and 'srf1' become equal."""
    s = (name or "").lower()
    s = re.sub(r"\b(hd|sd|uhd|4k)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


# ----------------------------------------------------------------------
# Build EIT sections
# ----------------------------------------------------------------------

def _bcd2(v):
    v = int(v) % 100
    return ((v // 10) << 4) | (v % 10)


def _mjd_time(t):
    """5 bytes: 16-bit Modified Julian Date, then hour/minute/second BCD,
    in UTC."""
    tm = time.gmtime(t)
    mjd = int(t // 86400) + 40587
    return (struct.pack("!H", mjd & 0xFFFF)
            + bytes([_bcd2(tm.tm_hour), _bcd2(tm.tm_min), _bcd2(tm.tm_sec)]))


def _duration(sec):
    """3 bytes BCD: hours, minutes, seconds."""
    sec = max(0, min(int(sec), 99 * 3600 + 59 * 60 + 59))
    return bytes([_bcd2(sec // 3600), _bcd2(sec % 3600 // 60),
                  _bcd2(sec % 60)])


def _text_dvb(s, room):
    """Text the way DVB expects it, and at most 'room' bytes long.

    The first byte names the character table: 0x15 means UTF-8. Without
    this byte ISO 6937 applies, an old table in which umlauts are encoded
    differently and a dash does not exist at all - 'Traumschiff - Curacao'
    would then be shown garbled.

    It is cut at a character boundary: cutting into the middle of a
    multi-byte character would produce gibberish.
    """
    b = (s or "").encode("utf-8", "replace")
    if len(b) > room - 1:
        b = b[:room - 1]
        while b and (b[-1] & 0xC0) == 0x80:      # drop the partial character
            b = b[:-1]
        if b and b[-1] >= 0xC0:
            b = b[:-1]
    return b"\x15" + b


def _short_text(title, text, language=b"deu"):
    """short_event_descriptor (0x4D).

    A descriptor holds at most 255 bytes of content; 3 bytes go to the
    language and two to length bytes. Title and text are shortened if
    necessary - better a cut text than a programme the TV drops entirely
    because of a line that is too long.
    """
    t = _text_dvb(title, 120)
    rest = max(2, 255 - 5 - len(t))
    b = _text_dvb(text, min(rest, 180)) if text else b""
    body = language + bytes([len(t)]) + t + bytes([len(b)]) + b
    return bytes([0x4D, len(body)]) + body


def event_no(start):
    """The ID of a programme.

    It must be unique within a channel and must NOT change when the guide is
    rebuilt - otherwise the TV takes the same programme for a new one and a
    programmed recording loses its reference. The start minute does both:
    it is unique (two programmes of a channel do not start in the same
    minute) and it only repeats after 45 days. Using the second as the ID
    would have been wrong - 16 bits then only reach 18 hours ahead, and the
    guide looks two days ahead.
    """
    return (int(start) // 60) & 0xFFFF


def _event(start, end_time, title, text, running):
    """One entry in the event loop of an EIT section."""
    d = _short_text(title, text)
    header = struct.pack("!H", event_no(start)) + _mjd_time(start) \
        + _duration(end_time - start)
    # running_status (3 Bit) + free_CA_mode (1) + descriptors_loop_length (12)
    header += struct.pack("!H", ((4 if running else 1) << 13) | (len(d) & 0x0FFF))
    return header + d


def _section(table, sid, tsid, onid, sec_no, last_sec, segment_letzte,
             last_table, event_list, version, crc32):
    body = bytearray()
    body += struct.pack("!H", sid & 0xFFFF)
    body += bytes([0xC1 | ((version & 0x1F) << 1)])
    body += bytes([sec_no & 0xFF, last_sec & 0xFF])
    body += struct.pack("!H", tsid & 0xFFFF)
    body += struct.pack("!H", onid & 0xFFFF)
    body += bytes([segment_letzte & 0xFF, last_table & 0xFF])
    for e in event_list:
        body += e
    length = len(body) + 4
    sec = bytes([table]) + struct.pack("!H", 0xF000 | length) + bytes(body)
    return sec + struct.pack("!I", crc32(sec))


def eit_now_next(sid, tsid, onid, programmes, crc32, version=0, now_ts=None):
    """EIT present/following: two sections, 'on now' and 'next'.

    That is the banner when switching channels. Without it the TV shows
    only the name and nothing else when the channel changes.
    """
    now_ts = time.time() if now_ts is None else now_ts
    running = following = None
    for a, e, ti, tx in programmes:
        if a <= now_ts < e and running is None:
            running = (a, e, ti, tx)
        elif a > now_ts and following is None:
            following = (a, e, ti, tx)
            break
    out_list = []
    for num_no, s, is_running in ((0, running, True), (1, following, False)):
        event_list = []
        if s:
            event_list.append(_event(s[0], s[1], s[2], s[3], is_running))
        out_list.append(_section(0x4E, sid, tsid, onid, num_no, 1, 1, 0x4E,
                            event_list, version, crc32))
    return out_list


def eit_schedule(sid, tsid, onid, programmes, crc32, version=0, now_ts=None):
    """EIT schedule (0x50): the actual programme guide.

    The DVB standard splits the schedule into three-hour segments. Every
    segment gets eight possible section numbers.

    Two subtleties a TV takes badly if you get them wrong:

      * last_section_number must be the number of the last section that
        really exists. If you put the highest conceivable number there, the
        TV waits for sections that never come, and the guide stays on
        'loading'.
      * Segments without programmes still need one - empty - section.
        Otherwise there is a gap in the numbering and it waits again. An
        empty section costs 18 bytes.
    """
    now_ts = time.time() if now_ts is None else now_ts
    # Segment 0 starts at the last hour divisible by 3 (UTC).
    base = int(now_ts // SEGMENT) * SEGMENT

    by_segment = {}
    for a, e, ti, tx in programmes:
        if e <= now_ts:
            continue
        seg = int((a - base) // SEGMENT)
        if 0 <= seg < 32:
            by_segment.setdefault(seg, []).append((a, e, ti, tx))

    if not by_segment:
        return []

    last_segment = max(by_segment)

    # First build all sections per segment - before that you do not know
    # how many there will be, and therefore not which one is the last.
    parts_per_segment = {}
    for seg in range(last_segment + 1):
        pieces, current, size = [], [], 14
        for a, e, ti, tx in by_segment.get(seg, []):
            ev = _event(a, e, ti, tx, a <= now_ts < e)
            if size + len(ev) > SECTION_MAX and current:
                pieces.append(current)
                current, size = [], 14
            current.append(ev)
            size += len(ev)
        if current:
            pieces.append(current)
        if not pieces:
            pieces = [[]]                 # empty segment, see above
        parts_per_segment[seg] = pieces[:SECTIONS_PER_SEGMENT]

    last_sec = (last_segment * SECTIONS_PER_SEGMENT
                  + len(parts_per_segment[last_segment]) - 1)

    out_list = []
    for seg in range(last_segment + 1):
        pieces = parts_per_segment[seg]
        segment_letzte = seg * SECTIONS_PER_SEGMENT + len(pieces) - 1
        for i, event_list in enumerate(pieces):
            out_list.append(_section(0x50, sid, tsid, onid,
                                seg * SECTIONS_PER_SEGMENT + i,
                                last_sec, segment_letzte, 0x50,
                                event_list, version, crc32))
    return out_list


# ----------------------------------------------------------------------
# Sections into transport packets
# ----------------------------------------------------------------------

def sections_to_ts(sections, pid=EIT_PID):
    """Packs sections into 188-byte packets. The continuity counter stays
    at 0 here - every session sets it itself when sending, see with_cc()."""
    packets = []
    for sec in sections:
        blob = b"\x00" + sec                 # pointer_field
        first_seg = True
        while blob:
            piece = blob[:184]
            blob = blob[184:]
            header = bytes([0x47,
                          (0x40 if first_seg else 0x00) | (pid >> 8),
                          pid & 0xFF,
                          0x10])
            packets.append(header + piece + b"\xFF" * (184 - len(piece)))
            first_seg = False
    return packets


def with_cc(packets, counter):
    """Sets the continuity counter afresh.

    Why this is needed: the counter counts up per PID and per stream. But
    we build the packets once for all sessions - two TVs getting the same
    packets would otherwise need the same counter value, and the second
    would report packet loss. 'counter' is a one-element list, so the value
    outlives the session.
    """
    out_list = []
    for p in packets:
        b = bytearray(p)
        b[3] = 0x10 | (counter[0] & 0x0F)
        counter[0] = (counter[0] + 1) & 0x0F
        out_list.append(bytes(b))
    return out_list


# ----------------------------------------------------------------------
# The guide as a whole
# ----------------------------------------------------------------------

class Guide:
    """Holds the programme guide and delivers finished packets per channel.

    Designed so the bridge only needs to know three things: learn(), pf()
    and plan(). Everything else - loading, matching, refreshing daily,
    keeping track of the current programme - happens in here in a thread of
    its own.

    If the source fails, the old state stays and the bridge keeps running.
    A programme guide is nice, but no reason to stop the TV.
    """

    def __init__(self, url, crc32, log=print, refresh=6 * 3600):
        self.url = url
        self.crc32 = crc32
        self.log = log
        self.refresh = refresh
        self.services_info = []          # [{"sid","name","logo","tsid","onid"}]
        self._pf = {}              # sid -> [TS packets]
        self._plan = {}            # sid -> [TS packets]
        self._programmes = {}       # sid -> [(start, end, title, text)]
        # Two separate version counters. "now/next" and the schedule are two
        # tables of their own for the TV, each with its own version. With one
        # shared counter the schedule would be back at its old number after
        # 32 refreshes of "now/next" - and the TV would take a real change for
        # one it has already read and drop it.
        self._version_pf = 0
        self._version_plan = 0
        self._pf_state = None
        # Counts every rebuild of the schedule. The bridge assembles its
        # send ring from it and notices by this number when it has to
        # reassemble it.
        self.build_no = 0
        self._lock = threading.Lock()
        self._run = False
        self.state = "not loaded yet"
        self.loaded_at = 0
        self.hits = 0

    # -- what the bridge calls ----------------------------------------

    def learn(self, services_info):
        """services_info: list of dicts with sid, name, logo, tsid, onid."""
        self.services_info = list(services_info)

    def pf(self, sid):
        """'now and next' for a channel - or an empty list."""
        return self._pf.get(sid, ())

    def plan(self, sid):
        """The schedule for a channel - or an empty list."""
        return self._plan.get(sid, ())

    def has_data(self, sid):
        return sid in self._pf

    def start_guide(self):
        if self._run:
            return
        self._run = True
        threading.Thread(target=self._thread, daemon=True,
                         name="epg").start()

    def stop_guide(self):
        self._run = False

    # -- internals -----------------------------------------------------

    def _thread(self):
        next_load = 0
        next_pf = 0
        while self._run:
            now_ts = time.time()
            if now_ts >= next_load:
                next_load = now_ts + self.refresh
                try:
                    self._load()
                    self._build(everything=True)
                    next_pf = time.time() + 30
                except Exception as e:
                    self.state = f"error: {e}"
                    self.log(f"  ! Programme guide: {e}")
                    # Not again right away - otherwise during a network outage you
                    # hammer the server every second.
                    next_load = time.time() + 600
            elif now_ts >= next_pf:
                next_pf = now_ts + 30
                try:
                    self._build(everything=False)
                except Exception as e:
                    self.log(f"  ! Programme guide (now/next): {e}")
            time.sleep(1.0)

    def _load(self):
        channels, programmes = load_xmltv_separately(self.url, log=self.log)
        mapping_ = map_channels(channels, self.services_info, log=self.log)
        fresh = {}
        for d in self.services_info:
            chan_id = mapping_.get(d["sid"])
            if chan_id and programmes.get(chan_id):
                fresh[d["sid"]] = programmes[chan_id]
        with self._lock:
            self._programmes = fresh
        self.hits = len(fresh)
        self.loaded_at = time.time()
        self.state = (f"{self.hits} of {len(self.services_info)} channels "
                      f"with programme")
        self.log(f"  Programme guide ready: {self.state}")

    def _build(self, everything):
        """everything=True rebuilds the schedule as well - that takes a few
        seconds and is only needed after a fresh load. Otherwise only 'now
        and next' is refreshed, so the current programme is right; that
        costs nothing."""
        with self._lock:
            programmes = dict(self._programmes)
        by_sid = {d["sid"]: d for d in self.services_info}
        now_ts = time.time()

        # If the same programme is still on everywhere as last time, there
        # is nothing to rebuild. Otherwise the version number would rise
        # every thirty seconds, and the TV would re-read the table each
        # time although nothing changed.
        state = tuple(sorted(
            (sid, next((a for a, e, _t, _x in items if a <= now_ts < e), 0))
            for sid, items in programmes.items()))
        if not everything and state == self._pf_state:
            return
        self._pf_state = state

        self._version_pf = (self._version_pf + 1) & 0x1F
        if everything:
            self._version_plan = (self._version_plan + 1) & 0x1F
        pf, plan = {}, {}
        t0 = time.time()
        for sid, items in programmes.items():
            d = by_sid.get(sid)
            if not d:
                continue
            pf[sid] = sections_to_ts(
                eit_now_next(sid, d["tsid"], d["onid"], items,
                                 self.crc32, self._version_pf, now_ts))
            if everything:
                plan[sid] = sections_to_ts(
                    eit_schedule(sid, d["tsid"], d["onid"], items,
                                 self.crc32, self._version_plan, now_ts))
        self._pf = pf
        if everything:
            self._plan = plan
            self.build_no += 1
            gr = sum(len(p) for p in plan.values()) * TS
            self.log(f"  Programme guide built: {gr/1024/1024:.1f} MB "
                     f"schedule in {time.time()-t0:.1f} s")


# ----------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------

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


def _crc32_mpeg(blob):
    # Table-driven: one step per byte instead of eight. Same result as the
    # bit-by-bit version; building the programme guide went from about
    # 3.5 s of pure CPU (which made the picture stutter) to a fraction.
    crc = 0xFFFFFFFF
    for b in blob:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


def _selftest(url, m3u=None):
    channels, programmes = load_xmltv(url)
    member_list = []
    if m3u:
        for text_line in open(m3u, encoding="utf-8", errors="replace"):
            if not text_line.startswith("#EXTINF"):
                continue
            logo = re.search(r'tvg-logo="([^"]*)"', text_line)
            member_list.append({"name": text_line.rsplit(",", 1)[-1].strip(),
                               "logo": logo.group(1) if logo else ""})
        print(f"  Channel list: {len(member_list)} channels")
        map_channels(channels, member_list)

    chan_id = max(programmes, key=lambda k: len(programmes[k]))
    prog = programmes[chan_id]
    print(f"\nSample for '{chan_id}' ({len(prog)} programmes)")
    pf = eit_now_next(1, 10, 1, prog, _crc32_mpeg)
    vs = eit_schedule(1, 10, 1, prog, _crc32_mpeg)
    print(f"  now/next: {len(pf)} sections")
    print(f"  schedule    : {len(vs)} sections, "
          f"{sum(len(s) for s in vs)} Byte")
    bad = 0
    for sec in pf + vs:
        length = struct.unpack("!H", sec[1:3])[0] & 0x0FFF
        if length + 3 != len(sec) or length > 4093:
            bad += 1
        if struct.unpack("!I", sec[-4:])[0] != _crc32_mpeg(sec[:-4]):
            bad += 1
    print(f"  faulty sections: {bad}")
    pkts = sections_to_ts(vs)
    z = [0]
    pkts = with_cc(pkts, z)
    print(f"  Packets: {len(pkts)}, all 188 bytes: "
          f"{all(len(p) == TS and p[0] == 0x47 for p in pkts)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(_selftest(sys.argv[1],
                         sys.argv[2] if len(sys.argv) > 2 else None))
