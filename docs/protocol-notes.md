# Sys'Nav XL protocol notes

Working notes for Phase 0–2. Captures live in `captures/` (git-ignored: they contain GPS positions).

## Device (read 2026-10-08)

| Field | Value | Source |
| --- | --- | --- |
| Advertised name | `NavXL20251169` (NavXL + serial) | advertisement |
| Model | `SYSNavXL` | 0x2A24 |
| Serial / hardware rev | `1520251169` | 0x2A25 / 0x2A27 |
| Firmware | `1.26` | 0x2A26 |
| Software rev | `1.0.0` | 0x2A28 |
| Manufacturer data | company ID `0xEEFF`, payload `"Syride"` | advertisement |
| Advertised service | `0000eff0-…` | advertisement |
| Battery | 0x57 = 87 % | 0x2A19 |

macOS address (per-Mac UUID): `746A6606-0B49-4ADB-BE52-FE67AAC01D41`.
The scanner matches the XL by name, service `eff0` or the "Syride" manufacturer data.

## GATT map

| Service | Characteristic | Properties | What we saw (instrument at rest, no special mode) |
| --- | --- | --- | --- |
| 180A Device Information | 2A23–2A2A | read | strings above |
| 180F Battery | 2A19 | read, notify | 87 %, no notifications in 60 s |
| **49535343-fe7d-… (Microchip Transparent UART)** | `…1e4d` | notify, indicate, write, write-no-resp | **silent**: TX from device |
| | `…8841` | write, write-no-resp | RX into device |
| | `…4c8a` | notify, write | control channel, silent |
| efe0 vendor | efe1, efe3–efe6 | indicate, write | silent |
| | **efe2** | indicate | **vario, 2 Hz** |
| eff0 vendor | **eff1** | notify | **GPS fix, 1 Hz** |
| | eff2 | notify | `01` every 20 s |
| | eff3 | notify | `02` every 20 s |
| | eff4 | indicate, write | 4 paged packets once at connect |
| | eff5 | write | — |

## Live stream (Plan A): decoded

Implemented in `scanner/syride_live.py`. Run `python scanner/syride_live.py captures/<file>.jsonl`.

### eff1: GPS fix, 1 Hz, 20 bytes, little-endian

| Bytes | Type | Meaning | Confidence |
| --- | --- | --- | --- |
| 0–3 | u32 | Unix time, seconds (UTC) | confirmed: matches capture clock to the second |
| 4–7 | i32 | latitude × 1e7 | confirmed: matches location |
| 8–11 | i32 | longitude × 1e7 | confirmed: matches location |
| 12–13 | u16 | ground speed, unit unknown | likely: 0–6 at rest |
| 14–15 | u16 | GPS altitude, metres | likely: 593–594, matches local terrain |
| 16–17 | u16 | track/heading, degrees | likely: 0–184, noisy at rest |
| 18–19 | u16 | always 0 | unknown |

### efe2: vario, 2 Hz, 4 bytes

`30 01` header, then int16 big-endian in cm/s. At rest: −0.20 to +0.10 m/s, mean −0.01.

### eff4: settings pages at connect

Four 20-byte packets, first 2 bytes = page index (0000–0003). Page 1–2 hold a rising
curve 664 → 1175, likely the vario audio frequency table. Not needed for flight data.

### eff2 / eff3: status heartbeats

`01` and `02` every 20 s, offset by 10 s. Possibly GPS-fix or flight state; check in flight.

## What's missing for Plan A

- No barometric altitude seen yet. GPS altitude alone is fine for a track, but IGC wants
  pressure altitude too. Check whether another characteristic carries it in flight, or
  derive it by integrating vario.
- Speed and heading units need a moving capture (walk or drive with the XL).
- No NMEA anywhere. External-sensor mode (for XCTrack and similar) not yet tested; it may
  switch the UART service to NMEA.

## Flight download (Plan B)

- The UART service is silent until written to, which fits a command/response protocol:
  the most likely path for downloading stored flights.
- Next: capture the Syride app talking to the XL (Apple PacketLogger with the Bluetooth
  logging profile on the iPhone, or an Android HCI snoop log) while it syncs a flight.
- Ground-truth IGC: download the same flight with SYS PC Tool.

## Captures

| File | What |
| --- | --- |
| `20261008-160724_scan_all.json` | advertisement scan, 11 devices |
| `20261008-161002_NavXL20251169.jsonl` / `.gatt.json` | first connect, 60 s, at rest, default mode |

## Open questions

- Does the XL accept a second BLE connection while the Syride app is connected?
- Does external-sensor mode change the stream?
- Where is barometric altitude?
