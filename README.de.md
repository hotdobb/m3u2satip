# m3u2satip

*[English](README.md)*

**Macht aus IPTV-M3U-Listen ein SAT>IP-Kabelnetz für Fernseher mit SAT>IP-Empfang** – zum Beispiel Panasonic-Fernseher mit „DVB via IP“.

Der Fernseher glaubt, er hänge an einem Kabelnetz: normaler Sendersuchlauf, Programmführer, Umschalten, Aufnahme auf seine USB-Platte. Im Hintergrund holt m3u2satip die Sender aus

- **Quelle 1:** einer M3U-Liste mit **Multicast**-Adressen (`udp://` / `rtp://`), zum Beispiel der Senderliste eines Anbieters mit offenem IPTV-Multicast (gebaut und getestet mit [Init7](https://www.init7.net/) in der Schweiz), und
- **Quelle 2 (optional):** einer M3U-Liste mit **HLS**-Sendern (`https://…m3u8`), zum Beispiel von [iptv-org](https://github.com/iptv-org/iptv); ffmpeg setzt sie ohne Umrechnung um,

und bietet sie als DVB-C-Netz mit erfundenen Frequenzen an, mit PAT/PMT/SDT/NIT, Uhrzeit (TDT/TOT) und einem EIT-Programmführer aus XMLTV.

```
Fernseher (SAT>IP) --RTSP:554-->  m3u2satip  <--Multicast------  Quelle 1 (Anbieter)
                   <--RTP--------            <--HLS / ffmpeg---  Quelle 2 (Liste)
                                     |        <--XMLTV----------  Programmführer
                                     +-- Bedienpult http://<rechner>:55000/
```

## Was es kann

- **Suchlauf in Minuten:** bis zu 8 Sender teilen sich eine erfundene Frequenz (`--bundle-size 8`), wie bei einem echten Kabelanbieter. 555 Sender passen auf 71 Frequenzen zwischen 114 und 674 MHz.
- **Geholt wird nur, was geschaut wird:** beim Suchlauf baut die Brücke alle Tabellen selbst und holt höchstens einen Strom je Frequenz; beim Fernsehen genau eine Multicast-Gruppe (oder ein ffmpeg) je Fernseher.
- **Programmführer** aus XMLTV (jetzt/danach und zwei Tage voraus), **Uhrzeit** für Aufnahmen.
- **Mehrere Fernseher** gleichzeitig; getestet mit drei.
- **Neustarts merkt man kaum:** laufende Sitzungen werden gesichert und fortgesetzt; das Bild stockt kurz und läuft weiter, kein neuer Suchlauf.
- **Fernseher werden an der MAC-Adresse erkannt**, tragen sich selbst ein und laufen weiter, wenn DHCP ihnen eine neue IP gibt.
- **Bedienpult** im Browser (Deutsch/Englisch): Gesundheitsampel in Klartext, welcher Fernseher was schaut, Protokoll, Senderliste, Quellen bearbeiten mit „Prüfen“-Knopf, Neustart ohne Passwort.
- **Strenges SAT>IP, wo Fernseher es brauchen:** fortlaufende TS-Paketzähler, leere CAT, RTCP-APP und DESCRIBE nach SAT>IP-Norm 1.2.2 – manche Fernseher (z. B. Panasonic TX-58DX800W) verwerfen sonst still alles. Ein Profil „alt“ pro Gerät behält das frühere Verhalten für Fernseher, die damit zufrieden waren.

## Was es nicht kann

- **Keine verschlüsselten Sender.** Anbieter mit Kopierschutz (in der Schweiz z. B. Sunrise, Swisscom blue TV, Salt) gehen nicht – und dafür gibt es keinen Ausweg.
- **Keine Aufnahme auf dem Server.** Aufgenommen wird am Fernseher selbst (ein Tuner: Umschalten während einer Aufnahme fragt, ob sie abgebrochen werden soll). Aufnahmen auf einem Server wären eine Aufgabe für z. B. Tvheadend + einen DLNA-Server – Ideen willkommen.
- **Freie HLS-Sender kommen und gehen.** Ein Teil jeder öffentlichen Liste ist immer tot oder regional gesperrt; die Brücke prüft die Liste im Hintergrund und bietet nur Sender an, die mindestens einmal geliefert haben.

## Voraussetzungen

- Linux (getestet mit Ubuntu 24.04), **Python 3** (getestet mit 3.12), keine fremden Python-Pakete.
- **ffmpeg** – nur für Quelle 2 (HLS).
- Eine **Multicast-IPTV-Quelle**, die im Heimnetz ankommt. Der Router muss Multicast weiterreichen (IGMP-Proxy). Geht das Internet, aber kein Sender hat Bild: zuerst den Router neu starten.
- Ein Fernseher mit **SAT>IP-Empfang**: bei Panasonic heißt das „DVB via IP“. Die Brücke gibt sich als DVB-C-Server aus.
- Port **554** (RTSP) und **55000** (HTTP) frei auf dem Rechner. Dort darf kein anderer SAT>IP-Server laufen (den von Tvheadend oder minisatip abschalten).
- Rechenleistung: etwa ein Drittel eines Prozessorkerns je schauendem Fernseher.

## Installation

```bash
sudo useradd --system --home /opt/m3u2satip m3u2satip
sudo git clone https://github.com/Hotdobb/m3u2satip /opt/m3u2satip
sudo chown -R m3u2satip: /opt/m3u2satip
sudo apt install ffmpeg            # nur für Quelle 2 (HLS) nötig
sudo cp /opt/m3u2satip/satipbridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now satipbridge
```

Dann **http://\<rechner\>:55000/** öffnen und unter *Quellen & Technik* die Quellen eintragen (voreingestellt sind die Init7-Senderliste, der TV7-Programmführer und die arabische Liste von iptv-org). *Prüfen*, *Speichern*, die Brücke neu starten lassen.

Protokoll mitlesen: `journalctl -u satipbridge -f`.

Zum Ausprobieren von Hand (Port 554 braucht root oder `CAP_NET_BIND_SERVICE`):

```bash
sudo python3 satipbridge.py --source init7 --bundle-size 8
```

## Am Fernseher

Panasonic (Menünamen je nach Modell): *Setup → Sendersuchlauf → DVB-via-IP → Serverauswahl* – die Brücke auswählen, dann *automatischen Suchlauf* starten. Sender aus Quelle 1 bekommen die Nummern ab 1, Sender aus Quelle 2 ab 301.

Ein Suchlauf dauert bei manchen Fernsehern etwa eine Minute, bei anderen bis zu zehn (der TX-58DX800W prüft jeden Sender gründlich).

## Störungen

| Anzeichen | Ursache / Abhilfe |
|---|---|
| Internet geht, aber *kein* Sender hat Bild | Der Router reicht kein Multicast mehr weiter. Router neu starten. Das Pult meldet „Quelle 1 liefert nichts“. |
| Brücke startet dauernd neu, Protokoll: Port 554 belegt | Ein anderer SAT>IP-Server hat den Port (z. B. `minisatip`, durch ein Update wieder eingeschaltet): `sudo systemctl mask --now minisatip.service minisatip.path` |
| Fernseher findet den Server, aber keine Sender, oder sucht extrem langsam | Im Pult sein Profil auf *streng* stellen (Standard für neue Geräte). |
| Nach dem Ändern einer Quelle liegen Sender falsch | Erwartet – am Fernseher neu suchen. |
| Ein Sender ab 301 zeigt nichts | Freier HLS-Sender offline oder gesperrt. Die Brücke versucht es selbst weiter. |

## Dateien

| Datei | |
|---|---|
| `satipbridge.py` | die Brücke |
| `satipepg.py` | XMLTV → EIT-Programmführer |
| `panel.html` | Bedienpult |
| `satip_probe.py` | Prüfwerkzeug: spricht mit jedem SAT>IP-Server wie ein Fernseher und zeigt, was ankommt |
| `satipbridge.service` | systemd-Dienst (Vorlage) |
| `tests/` | Tests ohne Fernseher und Netz: `python3 -m unittest discover -s tests -v` |

Neben dem Skript legt die Brücke ihren Zustand ab: `devices.json` (Fernseher), `hls_state.json` (geprüfte HLS-Sender), `sessions.json` (fortzusetzende Sitzungen), `settings.json` (Quellen aus dem Pult), `pmt_cache.json` (abgetastete PIDs).

Die Kommentare im Code (Englisch) erklären, *warum* etwas so gemacht ist – die meisten beschreiben einen Fehler, der an einem echten Fernseher gefunden wurde.

## Lizenz

GPL-3.0 oder neuer – siehe [LICENSE](LICENSE). Du darfst es nutzen, ändern und weitergeben; wer eine veränderte Fassung weitergibt, muss deren Quellcode unter derselben Lizenz offenlegen – so profitieren alle von Verbesserungen.

## Dank

- Programmführer-Daten: [mathewmeconry/TV7_EPG_Data](https://github.com/mathewmeconry/TV7_EPG_Data)
- HLS-Listen: [iptv-org/iptv](https://github.com/iptv-org/iptv)
- SAT>IP Protocol Specification 1.2.2 (SES)
- Entwickelt zusammen mit [Claude](https://claude.ai) (Anthropic), das einen großen Teil des Codes und der Tests geschrieben hat; geführt, an echten Fernsehern getestet und entschieden hat Hotdobb.

Panasonic, VIERA, Init7, SAT>IP und andere Namen sind Marken ihrer Inhaber; dieses Projekt steht in keiner Verbindung zu ihnen.
