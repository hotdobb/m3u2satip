#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Hotdobb - m3u2satip, see LICENSE
"""
satip_probe.py
==============
Pretends to be a TV: opens an RTSP connection to a SAT>IP server, has it
send the stream of one frequency, catches the RTP packets and looks inside.

Useful to compare what a working server delivers (e.g. a Panasonic TV's
own SAT>IP server) with what the bridge or Tvheadend delivers.

Output:
  * the complete RTSP conversation
  * how many RTP packets arrived
  * which PIDs occur in the transport stream
  * the content of the PAT  (which programmes exist, where their PMT is)
  * the content of the SDT  (what the programmes are called)
  * the video and audio PIDs from the PMT

Examples
--------
    # A Panasonic TV's own server, a channel on 322 MHz:
    python3 satip_probe.py --host 192.168.1.50 --freq 322.00

    # The bridge, first frequency on 114 MHz:
    python3 satip_probe.py --host 192.168.1.10 --freq 114.00

The raw stream is saved as <host>_<freq>.ts and can be opened with VLC or
ffprobe for checking.

No sudo needed.
"""

import argparse
import os
import re
import socket
import sys
import time


# ----------------------------------------------------------------------
# RTSP
# ----------------------------------------------------------------------

class Rtsp:
    def __init__(self, host, port, verbose=True):
        self.host = host
        self.port = port
        self.verbose = verbose
        self.cseq = 0
        self.session = None
        self.sock = socket.create_connection((host, port), timeout=10)

    def request(self, method, url, extra=None):
        self.cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}"]
        if self.session:
            lines.append(f"Session: {self.session}")
        if extra:
            lines.extend(extra)
        msg = "\r\n".join(lines) + "\r\n\r\n"

        if self.verbose:
            print("--> " + msg.replace("\r\n", "\n--> ").rstrip())
        self.sock.sendall(msg.encode())

        self.sock.settimeout(10)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        text = data.decode(errors="replace")
        if self.verbose:
            print("<-- " + text.replace("\r\n", "\n<-- ").rstrip())
            print()

        m = re.search(r"^Session:\s*([^;\r\n]+)", text, re.I | re.M)
        if m and not self.session:
            self.session = m.group(1).strip()
        return text

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------
# Analyse the transport stream
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


def parse_pat(ts):
    """Returns {program_number: pmt_pid}."""
    out = {}
    for sec in collect_sections(ts, 0):
        if len(sec) < 8 or sec[0] != 0x00:
            continue
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        end = 3 + length - 4          # without CRC
        i = 8
        while i + 4 <= end and i + 4 <= len(sec):
            prog = (sec[i] << 8) | sec[i + 1]
            pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
            if prog != 0:             # 0 = NIT
                out[prog] = pid
            i += 4
        if out:
            break
    return out


STREAM_TYPES = {
    0x01: "MPEG-1 Video", 0x02: "MPEG-2 Video", 0x03: "MPEG-1 Audio",
    0x04: "MPEG-2 Audio", 0x06: "private (often AC-3/subtitles)",
    0x0F: "AAC", 0x11: "AAC LATM", 0x1B: "H.264 Video",
    0x24: "HEVC Video", 0x81: "AC-3", 0x87: "E-AC-3",
}


def parse_pmt(ts, pmt_pid):
    """Returns a list of (pid, stream_type_text)."""
    out = []
    for sec in collect_sections(ts, pmt_pid):
        if len(sec) < 12 or sec[0] != 0x02:
            continue
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        end = 3 + length - 4
        pil = ((sec[10] & 0x0F) << 8) | sec[11]
        i = 12 + pil
        while i + 5 <= end and i + 5 <= len(sec):
            stype = sec[i]
            pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
            esil = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
            out.append((pid, STREAM_TYPES.get(stype, f"type 0x{stype:02X}")))
            i += 5 + esil
        if out:
            break
    return out


def parse_sdt(ts):
    """Returns a list of (service_id, provider, name)."""
    out = []
    for sec in collect_sections(ts, 17):
        if len(sec) < 12 or sec[0] not in (0x42, 0x46):
            continue
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        end = 3 + length - 4
        i = 11
        while i + 5 <= end and i + 5 <= len(sec):
            sid = (sec[i] << 8) | sec[i + 1]
            dll = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
            j = i + 5
            stop = min(j + dll, len(sec))
            prov = name = ""
            while j + 2 <= stop:
                tag = sec[j]
                dlen = sec[j + 1]
                body = sec[j + 2:j + 2 + dlen]
                if tag == 0x48 and len(body) >= 3:   # service_descriptor
                    k = 1
                    plen = body[k]
                    prov = body[k + 1:k + 1 + plen].decode(
                        "latin-1", "replace").strip()
                    k += 1 + plen
                    if k < len(body):
                        nlen = body[k]
                        name = body[k + 1:k + 1 + nlen].decode(
                            "latin-1", "replace").strip()
                j += 2 + dlen
            out.append((sid, prov, name))
            i += 5 + dll
        if out:
            break
    return out


def pid_histogram(ts):
    hist = {}
    for i in range(0, len(ts) - 187, 188):
        if ts[i] != 0x47:
            continue
        pid = ((ts[i + 1] & 0x1F) << 8) | ts[i + 2]
        hist[pid] = hist.get(pid, 0) + 1
    return hist


PID_NAMES = {0: "PAT", 1: "CAT", 2: "TSDT", 16: "NIT", 17: "SDT/BAT",
             18: "EIT", 20: "TDT/TOT", 8191: "null"}


# ----------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Query a SAT>IP server like a TV "
                    "and examine the stream it delivers")
    p.add_argument("--host", required=True, help="IP of the SAT>IP server")
    p.add_argument("--port", type=int, default=554)
    p.add_argument("--freq", required=True, help="frequency in MHz, e.g. 322.00")
    p.add_argument("--msys", default="dvbc")
    p.add_argument("--mtype", default="256qam")
    p.add_argument("--sr", default="6900")
    p.add_argument("--pids", default="all",
                   help="requested PIDs, default 'all'. "
                        "like the TV during a scan: 0,1,2,16,17,18,20")
    p.add_argument("--seconds", type=float, default=8.0,
                   help="how long to receive, default 8")
    p.add_argument("--client-port", type=int, default=0,
                   help="local RTP port, 0 = choose automatically")
    p.add_argument("--out", default=None, help="file name for the raw stream")
    a = p.parse_args()

    out_name = a.out or f"{a.host}_{a.freq}.ts"

    # RTP port pair: even number for RTP, odd for RTCP
    base = a.client_port
    if not base:
        base = 40000
        while base < 41000:
            try:
                t1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                t1.bind(("", base))
                t2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                t2.bind(("", base + 1))
                rtp, rtcp = t1, t2
                break
            except OSError:
                try:
                    t1.close()
                except Exception:
                    pass
                base += 2
        else:
            print("No free port pair found.", file=sys.stderr)
            return 1
    else:
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp.bind(("", base))
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp.bind(("", base + 1))

    print("=" * 70)
    print(f"Server {a.host}:{a.port}   frequency {a.freq} MHz   "
          f"{a.msys}/{a.mtype}/{a.sr}")
    print(f"Receiving on local UDP port {base}")
    print("=" * 70)
    print()

    tune = (f"freq={a.freq}&msys={a.msys}&mtype={a.mtype}&sr={a.sr}"
            f"&pids={a.pids}")

    try:
        r = Rtsp(a.host, a.port)
    except OSError as e:
        print(f"Connection failed: {e}")
        return 1

    resp = r.request(
        "SETUP", f"rtsp://{a.host}:{a.port}/?{tune}",
        [f"Transport: RTP/AVP;unicast;client_port={base}-{base + 1}"])

    if not resp.startswith("RTSP/1.0 200"):
        print("SETUP was refused - stopping here.")
        r.close()
        return 1

    m = re.search(r"com\.ses\.streamID\s*:\s*(\d+)", resp, re.I)
    stream_id = m.group(1) if m else "1"
    print(f"Stream-ID: {stream_id}\n")

    r.request("PLAY", f"rtsp://{a.host}:{a.port}/stream={stream_id}")

    print(f"Receiving for {a.seconds:.0f} seconds ...\n")
    t_play = time.time()
    t_first = None
    t_first_ts = None
    rtp.settimeout(2.0)
    ts = bytearray()
    packets = 0
    first = None
    sizes = []
    deadline = time.time() + a.seconds
    while time.time() < deadline:
        try:
            data, _ = rtp.recvfrom(65535)
        except socket.timeout:
            continue
        packets += 1
        sizes.append(len(data))
        if first is None:
            first = data
            t_first = time.time() - t_play
        if len(data) < 12 or (data[0] >> 6) != 2:
            continue
        hlen = 12 + 4 * (data[0] & 0x0F)
        if data[0] & 0x10 and len(data) >= hlen + 4:      # Extension
            hlen += 4 + 4 * ((data[hlen + 2] << 8) | data[hlen + 3])
        if t_first_ts is None and len(data) > hlen:
            t_first_ts = time.time() - t_play
        ts += data[hlen:]

    try:
        r.request("TEARDOWN", f"rtsp://{a.host}:{a.port}/stream={stream_id}")
    except Exception:
        pass
    r.close()
    rtp.close()
    rtcp.close()

    print("=" * 70)
    print("RESULT")
    print("=" * 70)
    print(f"RTP packets received : {packets}")
    if t_first is not None:
        print(f"First packet after   : {t_first:.2f} s")
    if t_first_ts is not None:
        print(f"First payload after  : {t_first_ts:.2f} s")
        if t_first_ts > 1.5:
            print("  -> That is long. During a channel scan a TV often waits")
            print("     only about one second per frequency.")
    if sizes:
        print(f"Packet sizes         : smallest {min(sizes)}, "
              f"largest {max(sizes)}, "
              f"average {sum(sizes)//len(sizes)} bytes")
        if max(sizes) <= 16:
            print("  -> These are bare RTP headers without content. The "
                  "Server is sending, but has nothing to send.")
    print(f"Payload              : {len(ts)} bytes "
          f"({len(ts) // 188} TS packets)")

    if packets == 0:
        print("\nNOT A SINGLE packet arrived.")
        print("The server confirmed the request but sends nothing.")
        print("Possible causes: it cannot tune to the source, "
              "or it sends to another address.")
        return 2

    if first:
        pt = first[1] & 0x7F
        print(f"RTP payload type     : {pt}"
              f"{'  (33 = MPEG-2 TS, korrekt)' if pt == 33 else '  (erwartet: 33)'}")

    with open(out_name, "wb") as f:
        f.write(ts)
    print(f"Raw stream saved     : {out_name}")

    hist = pid_histogram(bytes(ts))
    print(f"\nPIDs in the stream ({len(hist)} different):")
    for pid, n in sorted(hist.items(), key=lambda x: -x[1])[:25]:
        label = PID_NAMES.get(pid, "")
        print(f"  PID {pid:<5} {n:>6} packets   {label}")

    pat = parse_pat(bytes(ts))
    print(f"\nPAT: {len(pat)} programme(s)")
    if not pat:
        print("  NO PAT found. Without it a TV finds nothing -")
        print("  neither channel names nor picture and sound.")
    for prog, pmt_pid in pat.items():
        print(f"  Programme {prog}  ->  PMT on PID {pmt_pid}")
        for pid, stype in parse_pmt(bytes(ts), pmt_pid):
            print(f"      PID {pid:<5} {stype}")

    sdt = parse_sdt(bytes(ts))
    print(f"\nSDT: {len(sdt)} entry/entries")
    if not sdt:
        print("  NO SDT found - so the channels stay nameless.")
    for sid, prov, name in sdt:
        print(f"  Service {sid}: {name!r}  (provider: {prov!r})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
