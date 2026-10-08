#!/usr/bin/env python3
"""
Turn an XCTrack-mode capture from the Sys'Nav XL into an IGC file plus flight stats.

Input:  captures/<file>.jsonl written by `ble_scanner.py record` (or `explore` in XCTrack mode)
Output: captures/<file>.igc and a stats summary (printed, and as JSON with --json)

    python scanner/nmea_to_igc.py captures/<file>.jsonl
    python scanner/nmea_to_igc.py captures/<file>.jsonl --pilot "Bas Lemmens" --glider "Ozone Rush 6"

What goes into each IGC B record (one per GPS second):
    time, lat, lon     from $GNGGA (date from $GNRMC)
    pressure altitude  ISA altitude from the latest $LK8EX1 pressure (QNE, as IGC expects)
    GNSS altitude      from $GNGGA

Stats: duration, track distance, straight-line distance, max/min altitude, max climb and
max sink (vario averaged over 5 s, from $LK8EX1), and time spent turning left/right.
The IGC file is unsigned (no G record): fine for logbooks and viewers, not for contests.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# NMEA parsing
# ---------------------------------------------------------------------------


def checksum_ok(sentence: str) -> bool:
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, given = sentence[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(given[:2], 16)
    except ValueError:
        return False


def fields(sentence: str) -> list[str]:
    return sentence.split("*", 1)[0].split(",")


def nmea_coord(value: str, hemi: str) -> float | None:
    """ddmm.mmmm / dddmm.mmmm + N/S/E/W -> signed decimal degrees."""
    if not value or not hemi:
        return None
    dot = value.index(".") if "." in value else len(value)
    deg = int(value[: dot - 2])
    minutes = float(value[dot - 2:])
    dec = deg + minutes / 60.0
    return -dec if hemi in "SW" else dec


def nmea_time(value: str) -> time | None:
    if len(value) < 6:
        return None
    frac = float(value[6:]) if len(value) > 6 else 0.0
    return time(int(value[0:2]), int(value[2:4]), int(value[4:6]), int(round(frac * 1e6)) % 1_000_000)


def pressure_to_alt(pa: float) -> float:
    """ISA pressure altitude in metres (reference 1013.25 hPa)."""
    return 44330.769 * (1.0 - (pa / 101325.0) ** 0.190263)


# ---------------------------------------------------------------------------
# Track building
# ---------------------------------------------------------------------------


@dataclass
class Fix:
    t: datetime
    lat: float
    lon: float
    gps_alt: float | None
    baro_alt: float | None
    valid: bool


@dataclass
class VarioSample:
    t: datetime
    ms: float


@dataclass
class Track:
    fixes: list[Fix] = field(default_factory=list)
    vario: list[VarioSample] = field(default_factory=list)
    bad_checksums: int = 0
    sentences: int = 0


def read_lines(path: Path):
    """Yields NMEA sentences from a capture (.jsonl) or a plain .nmea/.txt log."""
    with path.open(encoding="utf-8") as fh:
        if path.suffix == ".jsonl":
            for raw in fh:
                rec = json.loads(raw)
                if rec.get("type") == "line" and rec.get("nmea"):
                    yield rec["text"]
        else:
            for raw in fh:
                raw = raw.strip()
                if raw.startswith("$"):
                    yield raw


def build_track(sentences) -> Track:
    tr = Track()
    day: date | None = None
    pressure_alt: float | None = None
    last_fix_time: datetime | None = None
    pending: list[tuple[time, list[str]]] = []  # GGA seen before the first RMC gave us a date

    def add_gga(t: time, f: list[str]) -> None:
        nonlocal last_fix_time
        stamp = datetime.combine(day, t, tzinfo=timezone.utc)
        if last_fix_time and stamp < last_fix_time - timedelta(hours=12):
            stamp += timedelta(days=1)  # crossed midnight UTC
        lat, lon = nmea_coord(f[2], f[3]), nmea_coord(f[4], f[5])
        if lat is None or lon is None:
            return
        if last_fix_time and stamp <= last_fix_time:
            return  # duplicate second
        quality = int(f[6] or 0)
        gps_alt = float(f[9]) if f[9] else None
        tr.fixes.append(Fix(stamp, lat, lon, gps_alt, pressure_alt, quality > 0))
        last_fix_time = stamp

    for s in sentences:
        tr.sentences += 1
        if not checksum_ok(s):
            tr.bad_checksums += 1
            continue
        f = fields(s)
        kind = f[0][3:] if f[0].startswith(("$GN", "$GP")) else f[0][1:]
        if kind == "RMC" and len(f) > 9 and f[9]:
            day = date(2000 + int(f[9][4:6]), int(f[9][2:4]), int(f[9][0:2]))
            for t, g in pending:
                add_gga(t, g)
            pending.clear()
        elif kind == "GGA" and len(f) > 9:
            t = nmea_time(f[1])
            if t is None:
                continue
            if day is None:
                pending.append((t, f))
            else:
                add_gga(t, f)
        elif kind == "LK8EX1" and len(f) > 3:
            if f[1] and f[1] != "999999":
                pressure_alt = pressure_to_alt(float(f[1]))
            if f[3] and f[3] != "9999" and last_fix_time:
                tr.vario.append(VarioSample(last_fix_time, int(f[3]) / 100.0))

    # The XL sends LK8EX1 just after GGA, so the first fix(es) have no pressure yet:
    # back-fill them with the first known pressure altitude.
    first_baro = next((fx.baro_alt for fx in tr.fixes if fx.baro_alt is not None), None)
    for fx in tr.fixes:
        if fx.baro_alt is not None:
            break
        fx.baro_alt = first_baro
    return tr


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def haversine_m(a: Fix, b: Fix) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def bearing_deg(a: Fix, b: Fix) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


@dataclass
class Stats:
    start_utc: str
    end_utc: str
    duration_s: int
    fixes: int
    track_km: float
    straight_km: float
    max_alt_m: float | None
    min_alt_m: float | None
    max_climb_ms: float | None
    max_sink_ms: float | None
    turning_left_pct: float
    turning_right_pct: float
    straight_pct: float


def compute_stats(tr: Track, turn_rate_dps: float = 8.0, min_speed_ms: float = 0.8) -> Stats:
    fx = [f for f in tr.fixes if f.valid]
    if len(fx) < 2:
        raise ValueError("not enough valid GPS fixes for a track")

    track = sum(haversine_m(a, b) for a, b in zip(fx, fx[1:]))
    alts = [f.baro_alt if f.baro_alt is not None else f.gps_alt for f in fx]
    alts = [a for a in alts if a is not None]

    # Vario: one value per second (mean of that second's samples), then a 5 s moving average.
    per_second: dict[datetime, list[float]] = {}
    for v in tr.vario:
        per_second.setdefault(v.t, []).append(v.ms)
    secs = [sum(vs) / len(vs) for _, vs in sorted(per_second.items())]
    window = 5
    smooth = [sum(secs[i:i + window]) / window for i in range(len(secs) - window + 1)]

    # Turning: signed heading change per second between consecutive moving fixes,
    # using a 3 s course to suppress GPS jitter.
    left = right = straight = 0
    step = 3
    for i in range(step, len(fx) - step):
        a, b, c = fx[i - step], fx[i], fx[i + step]
        dt = (c.t - a.t).total_seconds()
        if dt <= 0 or haversine_m(a, c) / dt < min_speed_ms:
            continue  # standing still: heading is noise
        d = (bearing_deg(b, c) - bearing_deg(a, b) + 540) % 360 - 180
        rate = d / (dt / 2)
        if rate <= -turn_rate_dps:
            left += 1
        elif rate >= turn_rate_dps:
            right += 1
        else:
            straight += 1
    moving = left + right + straight or 1

    return Stats(
        start_utc=fx[0].t.isoformat(),
        end_utc=fx[-1].t.isoformat(),
        duration_s=int((fx[-1].t - fx[0].t).total_seconds()),
        fixes=len(fx),
        track_km=round(track / 1000, 3),
        straight_km=round(haversine_m(fx[0], fx[-1]) / 1000, 3),
        max_alt_m=round(max(alts), 1) if alts else None,
        min_alt_m=round(min(alts), 1) if alts else None,
        max_climb_ms=round(max(smooth), 2) if smooth else None,
        max_sink_ms=round(min(smooth), 2) if smooth else None,
        turning_left_pct=round(100 * left / moving, 1),
        turning_right_pct=round(100 * right / moving, 1),
        straight_pct=round(100 * straight / moving, 1),
    )


# ---------------------------------------------------------------------------
# IGC writer
# ---------------------------------------------------------------------------


def igc_lat(v: float) -> str:
    hemi = "N" if v >= 0 else "S"
    v = abs(v)
    deg = int(v)
    mmm = round((v - deg) * 60000)
    if mmm == 60000:
        deg, mmm = deg + 1, 0
    return f"{deg:02d}{mmm:05d}{hemi}"


def igc_lon(v: float) -> str:
    hemi = "E" if v >= 0 else "W"
    v = abs(v)
    deg = int(v)
    mmm = round((v - deg) * 60000)
    if mmm == 60000:
        deg, mmm = deg + 1, 0
    return f"{deg:03d}{mmm:05d}{hemi}"


def igc_alt(v: float | None) -> str:
    if v is None:
        return "00000"
    v = int(round(v))
    return f"-{abs(v):04d}" if v < 0 else f"{v:05d}"


def write_igc(tr: Track, out: Path, pilot: str, glider: str) -> int:
    fx = tr.fixes
    first = fx[0].t
    lines = [
        "AXXXFLYAPP",
        f"HFDTEDATE:{first:%d%m%y},01",
        f"HFPLTPILOTINCHARGE:{pilot}",
        f"HFGTYGLIDERTYPE:{glider}",
        "HFGIDGLIDERID:",
        "HFDTMGPSDATUM:WGS84",
        "HFRFWFIRMWAREVERSION:XCTrack-mode NMEA via fly-app",
        "HFRHWHARDWAREVERSION:Syride SYS'Nav XL",
        "HFFTYFRTYPE:fly-app,nmea_to_igc",
        "HFGPSRECEIVER:Syride SYS'Nav XL internal",
        "HFPRSPRESSALTSENSOR:Syride SYS'Nav XL internal",
        "HFALGALTGPS:GEO",
        "HFALPALTPRESSURE:ISA",
    ]
    for f in fx:
        lines.append(f"B{f.t:%H%M%S}{igc_lat(f.lat)}{igc_lon(f.lon)}{'A' if f.valid else 'V'}"
                     f"{igc_alt(f.baro_alt)}{igc_alt(f.gps_alt)}")
    out.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return len(fx)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def fmt_duration(s: int) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Convert an XCTrack-mode capture to IGC + stats.")
    p.add_argument("capture", type=Path, nargs="?", help="captures/<file>.jsonl (or a plain NMEA log)")
    p.add_argument("--latest", action="store_true", help="use the newest captures/*record*.jsonl")
    p.add_argument("--out", type=Path, help="IGC output path (default: next to the capture)")
    p.add_argument("--pilot", default="Bas Lemmens")
    p.add_argument("--glider", default="")
    p.add_argument("--json", action="store_true", help="also write <capture>.stats.json")
    p.add_argument("--turn-rate", type=float, default=8.0,
                   help="deg/s that counts as turning (default 8, thermalling is ~15-20; use ~4 for a walk)")
    args = p.parse_args(argv)
    if args.latest:
        captures = sorted((Path(__file__).resolve().parent.parent / "captures").glob("*record*.jsonl"))
        if not captures:
            print("No captures/*record*.jsonl found. Record one first.")
            return 1
        args.capture = captures[-1]
    if args.capture is None:
        p.error("give a capture file or --latest")

    tr = build_track(read_lines(args.capture))
    if len(tr.fixes) < 2:
        print(f"Only {len(tr.fixes)} GPS fixes in {args.capture}; nothing to convert.")
        return 1

    out = args.out or args.capture.with_suffix(".igc")
    n = write_igc(tr, out, args.pilot, args.glider)
    st = compute_stats(tr, turn_rate_dps=args.turn_rate)

    print(f"IGC written: {out} ({n} B records, {tr.bad_checksums} bad checksums "
          f"of {tr.sentences} sentences)")
    print(f"  Duration        {fmt_duration(st.duration_s)}  ({st.start_utc[11:19]}-{st.end_utc[11:19]} UTC)")
    print(f"  Track distance  {st.track_km:.2f} km   straight line {st.straight_km:.2f} km")
    print(f"  Altitude        {st.min_alt_m} - {st.max_alt_m} m (pressure, ISA)")
    print(f"  Max climb/sink  {st.max_climb_ms:+.2f} / {st.max_sink_ms:+.2f} m/s (5 s average)")
    print(f"  Turning         left {st.turning_left_pct}%  right {st.turning_right_pct}%  "
          f"straight {st.straight_pct}%  (while moving)")
    if args.json:
        js = args.capture.with_suffix(".stats.json")
        js.write_text(json.dumps(asdict(st), indent=2), encoding="utf-8")
        print(f"Stats JSON: {js}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
