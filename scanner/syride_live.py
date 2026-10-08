"""
Decoders for the Sys'Nav XL live BLE stream (Plan A).

Reverse-engineered from a capture on 2026-10-08, firmware 1.26, instrument at
rest. Field meanings marked "likely" still need a capture in flight to confirm.

    0000eff1  notify, 1 Hz, 20 bytes, little-endian    -> GpsFix
    0000efe2  indicate, 2 Hz, 4 bytes, header 30 01    -> vario (cm/s)

Usage:
    python scanner/syride_live.py captures/<file>.jsonl          # print decoded stream
    python scanner/syride_live.py captures/<file>.jsonl --csv    # CSV for a spreadsheet
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

GPS_CHAR = "0000eff1-0000-1000-8000-00805f9b34fb"
VARIO_CHAR = "0000efe2-0000-1000-8000-00805f9b34fb"

_GPS = struct.Struct("<IiiHHHH")  # 20 bytes


@dataclass
class GpsFix:
    time: datetime          # bytes 0-3   u32 Unix seconds (confirmed against capture clock)
    lat: float              # bytes 4-7   i32 degrees x 1e7 (confirmed)
    lon: float              # bytes 8-11  i32 degrees x 1e7 (confirmed)
    speed_raw: int          # bytes 12-13 u16 likely ground speed; unit unknown (0-6 at rest)
    alt_m: int              # bytes 14-15 u16 likely GPS altitude in metres (matched local terrain)
    heading_raw: int        # bytes 16-17 u16 likely track/heading in degrees (noisy at rest)
    reserved: int           # bytes 18-19 always 0 so far


def decode_gps(data: bytes) -> GpsFix:
    if len(data) != _GPS.size:
        raise ValueError(f"GPS packet must be {_GPS.size} bytes, got {len(data)}")
    ts, lat, lon, spd, alt, hdg, rsv = _GPS.unpack(data)
    return GpsFix(
        time=datetime.fromtimestamp(ts, tz=timezone.utc),
        lat=lat / 1e7,
        lon=lon / 1e7,
        speed_raw=spd,
        alt_m=alt,
        heading_raw=hdg,
        reserved=rsv,
    )


def decode_vario(data: bytes) -> float:
    """Returns vertical speed in m/s. Packet: 0x30 0x01 then int16 big-endian cm/s."""
    if len(data) != 4 or data[:2] != b"\x30\x01":
        raise ValueError(f"unexpected vario packet: {data.hex()}")
    return struct.unpack(">h", data[2:4])[0] / 100.0


def iter_capture(path: str):
    """Yields (kind, iso_time, decoded) for every known packet in a capture file."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("type") != "notify":
                continue
            raw = bytes.fromhex(rec["hex"])
            if rec["char"] == GPS_CHAR:
                yield "gps", rec["t"], decode_gps(raw)
            elif rec["char"] == VARIO_CHAR:
                yield "vario", rec["t"], decode_vario(raw)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Decode a Sys'Nav XL live-stream capture.")
    p.add_argument("capture", help="captures/<file>.jsonl written by ble_scanner.py explore")
    p.add_argument("--csv", action="store_true", help="output CSV rows instead of text")
    args = p.parse_args(argv)

    if args.csv:
        print("kind,received,time,lat,lon,alt_m,speed_raw,heading_raw,vario_ms")
    for kind, received, value in iter_capture(args.capture):
        if kind == "gps":
            d = asdict(value)
            if args.csv:
                print(f"gps,{received},{d['time'].isoformat()},{d['lat']:.7f},{d['lon']:.7f},"
                      f"{d['alt_m']},{d['speed_raw']},{d['heading_raw']},")
            else:
                print(f"{received[11:23]}  GPS   {d['time']:%H:%M:%S}  "
                      f"alt {d['alt_m']} m  speed {d['speed_raw']}  hdg {d['heading_raw']}")
        else:
            if args.csv:
                print(f"vario,{received},,,,,,,{value:.2f}")
            else:
                print(f"{received[11:23]}  VARIO {value:+.2f} m/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
