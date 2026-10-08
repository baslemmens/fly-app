"""Unit tests for the parts of the scanner that don't need a Bluetooth radio.

Run: python -m unittest discover -s tests
"""

import sys
import types
import unittest
from pathlib import Path

# Allow the tests to run where bleak isn't installed (e.g. CI without Bluetooth).
if "bleak" not in sys.modules:
    try:
        import bleak  # noqa: F401
    except ImportError:
        fake = types.ModuleType("bleak")
        fake.BleakClient = object
        fake.BleakScanner = object
        backends = types.ModuleType("bleak.backends")
        characteristic = types.ModuleType("bleak.backends.characteristic")
        characteristic.BleakGATTCharacteristic = object
        sys.modules.update({
            "bleak": fake,
            "bleak.backends": backends,
            "bleak.backends.characteristic": characteristic,
        })

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scanner"))
import ble_scanner as bs  # noqa: E402


class LineAssemblerTest(unittest.TestCase):
    def test_sentence_split_over_packets(self):
        a = bs.LineAssembler()
        self.assertEqual(a.feed(b"$LK8EX1,101300,"), [])
        self.assertEqual(a.feed(b"99999,-12,25,1000,"), [])
        self.assertEqual(a.feed(b"*0A\r\n$PRS,"), ["$LK8EX1,101300,99999,-12,25,1000,*0A"])
        self.assertEqual(a.feed(b"17E5A\r\n"), ["$PRS,17E5A"])

    def test_binary_resets_buffer(self):
        a = bs.LineAssembler()
        a.feed(b"$GPGGA,123")
        self.assertEqual(a.feed(bytes([0xFF, 0x00])), [])
        self.assertEqual(a.feed(b"\n"), [])


class NmeaTest(unittest.TestCase):
    def test_is_nmea(self):
        self.assertTrue(bs.is_nmea("$GPRMC,1,2"))
        self.assertFalse(bs.is_nmea("hello"))

    def test_checksum(self):
        good = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
        self.assertTrue(bs.nmea_checksum_ok(good))
        self.assertFalse(bs.nmea_checksum_ok(good[:-2] + "00"))
        self.assertIsNone(bs.nmea_checksum_ok("$PRS,17E5A"))


class HelperTest(unittest.TestCase):
    def test_to_ascii(self):
        self.assertEqual(bs.to_ascii(b"A\x00B"), "A.B")

    def test_name_match(self):
        self.assertTrue(bs.looks_like_name("Syride XL", bs.DEFAULT_NAME_HINTS))
        self.assertTrue(bs.looks_like_name("NavXL20251169", bs.DEFAULT_NAME_HINTS))
        self.assertFalse(bs.looks_like_name(None, bs.DEFAULT_NAME_HINTS))

    def test_syride_match_by_service_and_mfr(self):
        self.assertTrue(bs.looks_like_syride(None, ["0000EFF0-0000-1000-8000-00805F9B34FB"], {}))
        self.assertTrue(bs.looks_like_syride(None, [], {0xEEFF: b"Syride"}))
        self.assertTrue(bs.looks_like_syride("Nav_XL20251169", [], {0x000D: b"Syride\x00"}))
        self.assertTrue(bs.looks_like_name("Nav_XL20251169", bs.DEFAULT_NAME_HINTS))
        self.assertFalse(bs.looks_like_syride("iPhone", ["180a"], {0x004C: b"\x12\x02"}))

    def test_short_uuid(self):
        self.assertEqual(bs.short("00002a37-0000-1000-8000-00805f9b34fb"), "2a37")
        self.assertEqual(bs.short("6e400003-b5a3-f393-e0a9-e50e24dcca9e"), "6e400003")

    def test_cli_parses(self):
        args = bs.build_parser().parse_args(["explore", "--name", "syride", "--duration", "5"])
        self.assertEqual(args.duration, 5.0)


if __name__ == "__main__":
    unittest.main()
