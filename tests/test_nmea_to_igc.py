"""Tests for nmea_to_igc with a synthetic walk: 60 s straight north, then a clockwise circle."""

import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
import nmea_to_igc as n2i  # noqa: E402

LAT0, LON0 = 52.0, 4.5  # made-up start point
M_PER_DEG_LAT = 111_195.0
R = 8.0  # circle radius, m


def with_checksum(body: str) -> str:
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"${body}*{c:02X}"


def ddmm(v: float, lat: bool) -> tuple[str, str]:
    hemi = ("N" if v >= 0 else "S") if lat else ("E" if v >= 0 else "W")
    v = abs(v)
    d = int(v)
    m = (v - d) * 60
    return (f"{d:02d}{m:09.6f}" if lat else f"{d:03d}{m:09.6f}"), hemi


def walk_sentences():
    """1.5 m/s: 60 s due north, then clockwise circles of radius 8 m (~10.7 deg/s) for 83 s."""
    t0 = datetime(2026, 10, 8, 14, 30, 0, tzinfo=timezone.utc)
    pts = []
    for s in range(60):
        pts.append((LAT0 + 1.5 * s / M_PER_DEG_LAT, LON0))
    clat, clon = pts[-1][0], LON0 + R / (M_PER_DEG_LAT * math.cos(math.radians(LAT0)))
    omega = 1.5 / R  # rad/s
    for s in range(1, 84):
        ang = math.pi - omega * s  # start west of centre, move clockwise (north -> east)
        dy, dx = R * math.sin(ang) * -1, R * math.cos(ang)
        pts.append((clat + -dy / M_PER_DEG_LAT,
                    clon + dx / (M_PER_DEG_LAT * math.cos(math.radians(LAT0)))))
    out = []
    for i, (lat, lon) in enumerate(pts):
        t = t0 + timedelta(seconds=i)
        la, lh = ddmm(lat, True)
        lo, oh = ddmm(lon, False)
        out.append(with_checksum(f"GNRMC,{t:%H%M%S}.000,A,{la},{lh},{lo},{oh},2.9,0.0,{t:%d%m%y},,,D,V"))
        out.append(with_checksum(f"GNGGA,{t:%H%M%S}.000,{la},{lh},{lo},{oh},2,20,0.6,600.0,M,46.1,M,,"))
        pa = 94052 - (i // 10)                  # slow climb: about 0.8 m per 10 s
        vario = 250 if 70 <= i < 80 else -10    # a 10 s "thermal" of +2.5 m/s
        for _ in range(4):
            out.append(with_checksum(f"LK8EX1,{pa},99999,{vario},99,1085"))
    return out


class ParseTest(unittest.TestCase):
    def test_coord_and_pressure(self):
        self.assertAlmostEqual(n2i.nmea_coord("4644.839200", "N"), 46 + 44.8392 / 60, places=6)
        self.assertAlmostEqual(n2i.nmea_coord("01311.560746", "W"), -(13 + 11.560746 / 60), places=6)
        self.assertAlmostEqual(n2i.pressure_to_alt(101325), 0.0, places=3)
        self.assertAlmostEqual(n2i.pressure_to_alt(94052), 623.7, delta=1.0)  # XL said 623.0

    def test_real_sentence_checksum(self):
        self.assertTrue(n2i.checksum_ok("$LK8EX1,94052,99999,-9,99,1085*24"))
        self.assertFalse(n2i.checksum_ok("$LK8EX1,94052,99999,-9,99,1085*25"))


class WalkTest(unittest.TestCase):
    def setUp(self):
        self.track = n2i.build_track(walk_sentences())

    def test_track(self):
        self.assertEqual(len(self.track.fixes), 143)
        self.assertEqual(self.track.bad_checksums, 0)
        self.assertAlmostEqual(self.track.fixes[0].baro_alt, 623.7, delta=1.0)

    def test_stats(self):
        st = n2i.compute_stats(self.track)
        self.assertEqual(st.duration_s, 142)
        self.assertAlmostEqual(st.track_km, 0.213, delta=0.01)  # 1.5 m/s * 142 s
        self.assertAlmostEqual(st.max_climb_ms, 2.5, delta=0.01)
        self.assertAlmostEqual(st.max_sink_ms, -0.1, delta=0.01)
        self.assertGreater(st.turning_right_pct, 45)  # circles are ~58 % of the walk
        self.assertLess(st.turning_left_pct, 5)

    def test_igc(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "walk.igc"
            n = n2i.write_igc(self.track, out, "Test Pilot", "Test Wing")
            lines = out.read_text().splitlines()
        self.assertEqual(n, 143)
        self.assertEqual(lines[0], "AXXXFLYAPP")
        self.assertIn("HFDTEDATE:081026,01", lines)
        b = [l for l in lines if l.startswith("B")]
        self.assertEqual(len(b), 143)
        self.assertTrue(all(len(l) == 35 for l in b))
        self.assertEqual(b[0], "B1430005200000N00430000EA0062400600")

    def test_igc_coords(self):
        self.assertEqual(n2i.igc_lat(46.747320), "4644839N")
        self.assertEqual(n2i.igc_lon(13.192679), "01311561E")
        self.assertEqual(n2i.igc_lat(-0.5), "0030000S")
        self.assertEqual(n2i.igc_alt(-12), "-0012")


if __name__ == "__main__":
    unittest.main()
