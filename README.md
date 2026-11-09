# m3u2satip

*[Deutsch](README.de.md)*

**Turns IPTV M3U playlists into a SAT>IP cable network for TVs with a SAT>IP client** – such as Panasonic TVs with "DVB via IP".

Your TV believes it is connected to a cable network: it runs a normal channel scan, shows the programme guide, zaps, records to its USB disk. Behind the scenes m3u2satip takes the channels from

- **source 1:** an M3U playlist with **multicast** addresses (`udp://` / `rtp://`), e.g. the channel list of an ISP that offers open IPTV multicast (built and tested with [Init7](https://www.init7.net/) in Switzerland), and
- **source 2 (optional):** an M3U playlist with **HLS** channels (`https://…m3u8`), e.g. from [iptv-org](https://github.com/iptv-org/iptv), remuxed by ffmpeg without transcoding,

and presents them as a DVB-C network with virtual frequencies, PAT/PMT/SDT/NIT, time (TDT/TOT) and an EIT programme guide from XMLTV.

```
TV (SAT>IP client) --RTSP:554-->  m3u2satip  <--multicast------  source 1 (ISP)
                   <--RTP--------            <--HLS / ffmpeg---  source 2 (playlist)
                                     |        <--XMLTV----------  programme guide
                                     +-- control panel http://<host>:55000/
```

## What it does

- **Channel scan in minutes:** up to 8 channels share one virtual frequency (`--bundle-size 8`), like a real cable operator. 555 channels fit on 71 frequencies between 114 and 674 MHz.
- **Only what is watched is fetched:** during a scan the bridge builds all tables itself and joins at most one stream per frequency; when watching, exactly one multicast group (or one ffmpeg) per TV.
- **Programme guide** from XMLTV (now/next and two days ahead), **time** for recordings.
- **Several TVs** at the same time; three were tested.
- **Restarts are invisible:** running sessions are saved and resumed; the picture pauses for a moment and continues, no new scan.
- **TVs are recognised by MAC address**, register themselves and keep working when DHCP gives them a new IP.
- **Control panel** in the browser (German/English): health lights in plain words, which TV watches what, log, channel list, sources editable with a "Check" button, restart without password.
- **Strict SAT>IP behaviour where TVs need it:** continuous TS continuity counters, empty CAT, RTCP APP and DESCRIBE per SAT>IP spec 1.2.2 – some TVs (e.g. Panasonic TX-58DX800W) silently drop anything else. A per-device "legacy" profile keeps older behaviour for TVs that were happy with it.

## What it does not do

- **No encrypted channels.** Providers with DRM (in Switzerland e.g. Sunrise, Swisscom blue TV, Salt) cannot be used – and there is no workaround.
- **No server-side recording.** Recording is done by the TV itself (one tuner: switching channels during a recording asks to stop it). Programming recordings on a server would be a job for e.g. Tvheadend + a DLNA server – ideas welcome.
- **Free HLS channels come and go.** A part of any public playlist is always dead or geo-blocked; the bridge checks the list in the background and only offers channels that delivered at least once.

## Requirements

- Linux (tested on Ubuntu 24.04), **Python 3** (tested with 3.12), no third-party Python packages.
- **ffmpeg** – only for source 2 (HLS).
- A **multicast IPTV source** that reaches your LAN. Your router must forward multicast (IGMP proxy). If the Internet works but no channel has a picture, restart the router first.
- A TV with a **SAT>IP client**: Panasonic calls it "DVB via IP". The bridge pretends to be a DVB-C server.
- Port **554** (RTSP) and **55000** (HTTP) free on the machine. No other SAT>IP server may run there (Tvheadend's or minisatip's SAT>IP server: switch it off).
- CPU: roughly a third of one core per watching TV.

## Install

```bash
sudo useradd --system --home /opt/m3u2satip m3u2satip
sudo git clone https://github.com/Hotdobb/m3u2satip /opt/m3u2satip
sudo chown -R m3u2satip: /opt/m3u2satip
sudo apt install ffmpeg            # only needed for source 2 (HLS)
sudo cp /opt/m3u2satip/satipbridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now satipbridge
```

Then open **http://\<host\>:55000/** and set your sources under *Sources & technology* (the defaults are Init7's channel list, the TV7 XMLTV guide and iptv-org's Arabic playlist). Press *Check*, *Save*, and let the bridge restart.

Watch the log with `journalctl -u satipbridge -f`.

Running it by hand for a try (port 554 needs root or `CAP_NET_BIND_SERVICE`):

```bash
sudo python3 satipbridge.py --source init7 --bundle-size 8
```

## On the TV

Panasonic (menu names vary by model): *Setup → Tuning → DVB-via-IP → Server selection* – pick the bridge, then run an *Auto tuning*. Channels from source 1 get numbers from 1, channels from source 2 from 301.

A scan takes about a minute on some TVs and up to ten on others (the TX-58DX800W checks every channel thoroughly).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Internet works, but *no* channel has a picture | The router stopped forwarding multicast. Restart the router. The panel shows "Source 1 delivers nothing". |
| Bridge keeps restarting, log says port 554 is in use | Another SAT>IP server took the port (e.g. `minisatip` re-enabled by a package update): `sudo systemctl mask --now minisatip.service minisatip.path` |
| A TV finds the server but no channels, or scans extremely slowly | Set its profile to *strict* in the panel (default for new devices). |
| After changing a source, channels are in the wrong place | Expected – run a new channel scan on the TV. |
| A channel above 301 shows nothing | Free HLS channel offline or geo-blocked. The bridge keeps retrying. |

## Files

| File | |
|---|---|
| `satipbridge.py` | the bridge |
| `satipepg.py` | XMLTV → EIT programme guide |
| `panel.html` | control panel |
| `satip_probe.py` | test tool: talks to any SAT>IP server like a TV and shows what arrives |
| `satipbridge.service` | systemd unit (template) |
| `tests/` | offline tests: `python3 -m unittest discover -s tests -v` |

State written next to the script: `devices.json` (TVs), `hls_state.json` (checked HLS channels), `sessions.json` (sessions to resume), `settings.json` (sources from the panel), `pmt_cache.json` (sampled PIDs).

Code comments explain *why* things are done the way they are – most of them describe a fault that was found on a real TV.

## License

GPL-3.0-or-later – see [LICENSE](LICENSE). You may use, change and pass it on; if you pass on a changed version, its source code must be open under the same licence, so everyone benefits from improvements.

## Credits

- Programme guide data: [mathewmeconry/TV7_EPG_Data](https://github.com/mathewmeconry/TV7_EPG_Data)
- HLS playlists: [iptv-org/iptv](https://github.com/iptv-org/iptv)
- SAT>IP Protocol Specification 1.2.2 (SES)

Panasonic, VIERA, Init7, SAT>IP and other names are trademarks of their owners; this project is not affiliated with them.
