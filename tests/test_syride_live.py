"""Tests for the live-stream decoders, using packets from the 2026-10-08 capture.

Position bytes are replaced with a made-up location so no real coordinates live in git.
"""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
import syride_live as sl  # noqa: E402


def gps_packet(ts=1791468604, lat=520000000, lon=45000000, spd=1, alt=593, hdg=0):
    return struct.pack("<IiiHHHH", ts, lat, lon, spd, alt, hdg, 0)


class GpsTest(unittest.TestCase):
    def test_decode(self):
        fix = sl.decode_gps(gps_packet())
        self.assertEqual(fix.time.isoformat(), "2026-10-08T14:10:04+00:00")
        self.assertAlmostEqual(fix.lat, 52.0)
        self.assertAlmostEqual(fix.lon, 4.5)
        self.assertEqual(fix.alt_m, 593)

    def test_real_header_layout(self):
        # First 4 bytes of a real packet: 3c a4 c7 6a -> 14:10:04 UTC
        real = bytes.fromhex("3ca4c76a") + gps_packet()[4:]
        self.assertEqual(sl.decode_gps(real).time.strftime("%H:%M:%S"), "14:10:04")

    def test_wrong_length(self):
        with self.assertRaises(ValueError):
            sl.decode_gps(b"\x00" * 19)


class VarioTest(unittest.TestCase):
    def test_values_from_capture(self):
        self.assertEqual(sl.decode_vario(bytes.fromhex("30010000")), 0.0)
        self.assertEqual(sl.decode_vario(bytes.fromhex("3001ffec")), -0.2)
        self.assertEqual(sl.decode_vario(bytes.fromhex("3001fff6")), -0.1)

    def test_bad_header(self):
        with self.assertRaises(ValueError):
            sl.decode_vario(bytes.fromhex("31010000"))


if __name__ == "__main__":
    unittest.main()
