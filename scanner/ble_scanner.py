#!/usr/bin/env python3
"""
fly-app BLE scanner - Phase 0 reconnaissance for the Syride Sys'Nav XL.

Two commands:

  scan     List nearby Bluetooth LE devices with their advertisement data.
  explore  Connect to one device, map all services/characteristics, read what
           is readable, subscribe to every notify/indicate characteristic and
           log everything that arrives to captures/ as JSON Lines.

Examples:
  python scanner/ble_scanner.py scan
  python scanner/ble_scanner.py scan --all --timeout 15
  python scanner/ble_scanner.py explore --name syride --duration 120
  python scanner/ble_scanner.py explore --address 1A2B3C4D-... --duration 300

Nothing is written TO the device: this tool only reads and listens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
except ImportError:  # pragma: no cover - only hit when deps are missing
    sys.exit("bleak is not installed. Run: pip install -r requirements.txt")

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_DIR = REPO_ROOT / "captures"

# Name fragments that probably belong to a Syride instrument. Matching is
# case-insensitive. Run `scan --all` once to learn the real advertised name
# and add it here if it differs.
DEFAULT_NAME_HINTS = ("navxl", "syride", "sysnav", "sys'nav", "sys nav", "nav xl")

# Seen on a Sys'Nav XL, 2026-10-08:
#   default mode: name "NavXL<serial>", service 0000eff0-..., mfr 0xEEFF -> b"Syride"
#   XCTrack mode: name "Nav_XL<serial>", no service, mfr 0x000D -> b"Syride\x00",
#                 and a different BLE address.
SYRIDE_SERVICE_UUID = "0000eff0-0000-1000-8000-00805f9b34fb"
SYRIDE_MFR_ID = 0xEEFF


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def to_ascii(data: bytes) -> str:
    """Printable view of raw bytes: printable ASCII kept, everything else '.'."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def looks_like_name(name: str | None, hints: tuple[str, ...]) -> bool:
    if not name:
        return False
    # Ignore separators: default mode advertises "NavXL…", XCTrack mode "Nav_XL…".
    squashed = "".join(c for c in name.lower() if c.isalnum())
    lowered = name.lower()
    return any(h in lowered or "".join(c for c in h if c.isalnum()) in squashed for h in hints)


def looks_like_syride(name: str | None, service_uuids, manufacturer_data,
                      hints: tuple[str, ...] = DEFAULT_NAME_HINTS) -> bool:
    """Match by name, by the XL's service UUID, or by 'Syride' manufacturer data."""
    if looks_like_name(name, hints):
        return True
    if SYRIDE_SERVICE_UUID in [u.lower() for u in (service_uuids or [])]:
        return True
    # Default mode uses company ID 0xEEFF, XCTrack mode 0x000D; both carry "Syride".
    return any(b"syride" in bytes(p).lower() for p in (manufacturer_data or {}).values())


class LineAssembler:
    """
    Reassembles text lines that arrive split across BLE notifications.

    BLE packets are small (often 20 bytes), so one NMEA sentence like
    "$LK8EX1,101300,99999,-12,25,1000,*0A\\r\\n" usually spans several
    notifications. Feed raw chunks in; complete lines come out.
    """

    def __init__(self, max_buffer: int = 4096) -> None:
        self._buf = ""
        self._max = max_buffer

    def feed(self, data: bytes) -> list[str]:
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            # Binary data: drop any half line so it can't merge with garbage.
            self._buf = ""
            return []
        self._buf += text
        parts = self._buf.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self._buf = parts.pop()  # last part is an unfinished line
        if len(self._buf) > self._max:
            self._buf = ""
        return [p for p in parts if p.strip()]


def is_nmea(line: str) -> bool:
    """True for lines shaped like NMEA sentences: $TALKER,...[*HH]."""
    return line.startswith(("$", "!")) and "," in line


def nmea_checksum_ok(line: str) -> bool | None:
    """Checks the *HH checksum. None when the sentence carries no checksum."""
    if "*" not in line:
        return None
    body, _, given = line[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(given[:2], 16)
    except ValueError:
        return False


class CaptureLog:
    """Writes one JSON object per line to captures/<timestamp>_<label>.jsonl."""

    def __init__(self, label: str) -> None:
        CAPTURE_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        self.path = CAPTURE_DIR / f"{stamp}_{safe}.jsonl"
        self._fh = self.path.open("w", encoding="utf-8")

    def write(self, record: dict) -> None:
        record.setdefault("t", now_iso())
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

async def cmd_scan(args: argparse.Namespace) -> int:
    hints = tuple(h.lower() for h in (args.name or DEFAULT_NAME_HINTS))
    print(f"Scanning for {args.timeout:.0f} s ...")
    found = await BleakScanner.discover(timeout=args.timeout, return_adv=True)

    rows = []
    for address, (device, adv) in found.items():
        name = adv.local_name or device.name
        if not args.all and not looks_like_syride(name, adv.service_uuids, adv.manufacturer_data, hints):
            continue
        rows.append((adv.rssi, address, name, adv))

    if not rows:
        print("No matching devices. Is the XL switched on with Bluetooth enabled?")
        print("Tip: run with --all to see every device and find its real name.")
        return 1

    rows.sort(key=lambda r: r[0], reverse=True)  # strongest signal first
    for rssi, address, name, adv in rows:
        print(f"\n{name or '(no name)'}")
        print(f"  address : {address}")
        print(f"  rssi    : {rssi} dBm")
        if adv.service_uuids:
            print("  services: " + ", ".join(adv.service_uuids))
        for company_id, payload in adv.manufacturer_data.items():
            print(f"  mfr data: 0x{company_id:04X} -> {payload.hex(' ')}")
        for uuid, payload in adv.service_data.items():
            print(f"  svc data: {uuid} -> {payload.hex(' ')}")

    print("\nOn macOS the address is a per-Mac UUID, not the device's MAC address.")
    print("Use it with: explore --address <address>")

    if not args.no_save:
        CAPTURE_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = CAPTURE_DIR / f"{stamp}_scan{'_all' if args.all else ''}.json"
        path.write_text(json.dumps([
            {"name": name, "address": address, "rssi": rssi,
             "service_uuids": list(adv.service_uuids),
             "manufacturer_data": {f"0x{k:04X}": v.hex() for k, v in adv.manufacturer_data.items()},
             "service_data": {k: v.hex() for k, v in adv.service_data.items()},
             "tx_power": adv.tx_power}
            for rssi, address, name, adv in rows
        ], indent=2), encoding="utf-8")
        print(f"\nScan saved to {path.relative_to(REPO_ROOT)}")
    return 0


# --------------------------------------------------------------------------
# explore
# --------------------------------------------------------------------------

async def find_target(args: argparse.Namespace):
    if args.address:
        device = await BleakScanner.find_device_by_address(args.address, timeout=args.timeout)
    else:
        hints = tuple(h.lower() for h in (args.name or DEFAULT_NAME_HINTS))
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: looks_like_syride(ad.local_name or d.name, ad.service_uuids,
                                            ad.manufacturer_data, hints),
            timeout=args.timeout,
        )
    return device


async def cmd_explore(args: argparse.Namespace) -> int:
    print("Looking for the device ...")
    device = await find_target(args)
    if device is None:
        print("Device not found. Check it is on, nearby, and not connected to the Syride app.")
        return 1

    label = device.name or device.address
    log = CaptureLog(label)
    print(f"Found {label} ({device.address}). Logging to {log.path.relative_to(REPO_ROOT)}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    def on_disconnect(_client) -> None:
        print("\nDevice disconnected.")
        log.write({"type": "disconnect"})
        stop.set()

    counts: dict[str, int] = {}
    assemblers: dict[str, LineAssembler] = {}

    def make_handler(char: BleakGATTCharacteristic):
        key = char.uuid
        assemblers[key] = LineAssembler()
        counts[key] = 0

        def handler(_sender, data: bytearray) -> None:
            raw = bytes(data)
            counts[key] += 1
            log.write({"type": "notify", "char": key, "len": len(raw),
                       "hex": raw.hex(), "ascii": to_ascii(raw)})
            for line in assemblers[key].feed(raw):
                nmea = is_nmea(line)
                log.write({"type": "line", "char": key, "nmea": nmea,
                           "checksum_ok": nmea_checksum_ok(line) if nmea else None,
                           "text": line})
                if not args.quiet:
                    tag = "NMEA" if nmea else "TEXT"
                    print(f"  [{tag}] {short(key)}  {line}")
            if args.raw and not args.quiet:
                print(f"  [RAW ] {short(key)}  {raw.hex(' ')}")

        return handler

    async with BleakClient(device, disconnected_callback=on_disconnect) as client:
        log.write({"type": "connected", "name": device.name, "address": device.address})
        gatt_map = []
        subscribed = []

        print("\nGATT map")
        for service in client.services:
            print(f"\nService {service.uuid}  {service.description}")
            svc_entry = {"uuid": service.uuid, "description": service.description,
                         "characteristics": []}
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"  Char {char.uuid}  [{props}]  {char.description}")
                entry = {"uuid": char.uuid, "handle": char.handle,
                         "properties": list(char.properties),
                         "description": char.description, "descriptors": []}

                if "read" in char.properties:
                    try:
                        value = bytes(await client.read_gatt_char(char))
                        entry["value_hex"] = value.hex()
                        entry["value_ascii"] = to_ascii(value)
                        print(f"      value: {value.hex(' ')}  |{to_ascii(value)}|")
                    except Exception as exc:  # noqa: BLE001 - log any read failure
                        entry["read_error"] = str(exc)
                        print(f"      read failed: {exc}")

                for desc in char.descriptors:
                    entry["descriptors"].append({"uuid": desc.uuid, "handle": desc.handle})
                    print(f"      Descriptor {desc.uuid}")

                if {"notify", "indicate"} & set(char.properties):
                    try:
                        await client.start_notify(char, make_handler(char))
                        subscribed.append(char.uuid)
                        entry["subscribed"] = True
                    except Exception as exc:  # noqa: BLE001
                        entry["subscribe_error"] = str(exc)
                        print(f"      subscribe failed: {exc}")

                svc_entry["characteristics"].append(entry)
            gatt_map.append(svc_entry)

        log.write({"type": "gatt_map", "services": gatt_map})
        map_path = log.path.with_suffix(".gatt.json")
        map_path.write_text(json.dumps(gatt_map, indent=2), encoding="utf-8")
        print(f"\nGATT map saved to {map_path.relative_to(REPO_ROOT)}")

        if not subscribed:
            print("No notify/indicate characteristics found; nothing to listen to.")
        else:
            print(f"\nListening on {len(subscribed)} characteristic(s) for "
                  f"{args.duration:.0f} s. Press Ctrl+C to stop early.\n")
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.duration)
            except asyncio.TimeoutError:
                pass

            if client.is_connected:
                for uuid in subscribed:
                    try:
                        await client.stop_notify(uuid)
                    except Exception:  # noqa: BLE001 - best effort on shutdown
                        pass

    log.write({"type": "summary", "notifications": counts})
    log.close()
    print("\nSummary (notifications per characteristic):")
    for uuid, n in counts.items():
        print(f"  {uuid}: {n}")
    print(f"\nCapture: {log.path.relative_to(REPO_ROOT)}")
    return 0


# --------------------------------------------------------------------------
# record (XCTrack mode)
# --------------------------------------------------------------------------

UART_TX_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"  # Microchip Transparent UART, device -> us


async def cmd_record(args: argparse.Namespace) -> int:
    """
    Record the XCTrack-mode NMEA stream for --duration seconds.
    Reconnects automatically when the link drops (out of range, XL restarted).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.duration
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    log = CaptureLog(f"record_{args.label}")
    print(f"Recording for {args.duration / 60:.0f} min to {log.path.relative_to(REPO_ROOT)}")
    stats = {"lines": 0, "fixes": 0, "bad_checksum": 0, "connects": 0}
    assembler = LineAssembler()

    def handler(_sender, data: bytearray) -> None:
        raw = bytes(data)
        log.write({"type": "notify", "char": UART_TX_UUID, "len": len(raw), "hex": raw.hex()})
        for line in assembler.feed(raw):
            ok = nmea_checksum_ok(line) if is_nmea(line) else None
            stats["lines"] += 1
            stats["bad_checksum"] += ok is False
            stats["fixes"] += line.startswith(("$GNGGA", "$GPGGA"))
            log.write({"type": "line", "char": UART_TX_UUID, "nmea": is_nmea(line),
                       "checksum_ok": ok, "text": line})

    async def report() -> None:
        while not stop.is_set():
            await asyncio.sleep(30)
            left = max(0, deadline - loop.time())
            print(f"  {now_iso()[11:19]} UTC  fixes {stats['fixes']}  lines {stats['lines']}  "
                  f"reconnects {max(0, stats['connects'] - 1)}  {left / 60:.1f} min left")

    reporter = asyncio.create_task(report())
    try:
        while not stop.is_set() and loop.time() < deadline:
            device = await find_target(args)
            if device is None:
                print("  XL not found (in XCTrack mode and in range?), retrying ...")
                log.write({"type": "not_found"})
                continue
            dropped = asyncio.Event()
            try:
                async with BleakClient(device, disconnected_callback=lambda _c: dropped.set()) as client:
                    stats["connects"] += 1
                    assembler = LineAssembler()
                    log.write({"type": "connected", "name": device.name, "address": device.address})
                    print(f"  connected to {device.name}")
                    await client.start_notify(UART_TX_UUID, handler)
                    waiters = [asyncio.create_task(e.wait()) for e in (stop, dropped)]
                    await asyncio.wait(waiters, timeout=max(0, deadline - loop.time()),
                                       return_when=asyncio.FIRST_COMPLETED)
                    for w in waiters:
                        w.cancel()
            except Exception as exc:  # noqa: BLE001 - keep recording through BLE errors
                print(f"  connection error: {exc}")
                log.write({"type": "error", "error": str(exc)})
            if dropped.is_set() and not stop.is_set():
                print("  link dropped, reconnecting ...")
                log.write({"type": "disconnect"})
    finally:
        stop.set()
        reporter.cancel()
        log.write({"type": "summary", **stats})
        log.close()

    print(f"\nDone: {stats['fixes']} GPS fixes, {stats['lines']} NMEA lines, "
          f"{stats['bad_checksum']} bad checksums, {max(0, stats['connects'] - 1)} reconnects")
    print(f"Capture: {log.path.relative_to(REPO_ROOT)}")
    print(f"Convert: python scanner/nmea_to_igc.py {log.path.relative_to(REPO_ROOT)}")
    return 0 if stats["fixes"] else 1


def short(uuid: str) -> str:
    """Shorten standard 128-bit UUIDs (0000xxxx-0000-1000-8000-00805f9b34fb) to xxxx."""
    u = uuid.lower()
    if u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb"):
        return u[4:8]
    return u[:8]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BLE recon for the Syride Sys'Nav XL (read-only).")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="list nearby BLE devices")
    s.add_argument("--timeout", type=float, default=10.0, help="scan time in seconds (default 10)")
    s.add_argument("--all", action="store_true", help="show every device, not only Syride-like names")
    s.add_argument("--name", action="append", help="name fragment to match (repeatable)")
    s.add_argument("--no-save", action="store_true", help="don't write the scan to captures/")

    e = sub.add_parser("explore", help="connect, map GATT, log all notifications")
    target = e.add_mutually_exclusive_group()
    target.add_argument("--address", help="device address/UUID from `scan`")
    target.add_argument("--name", action="append", help="name fragment to match (repeatable)")
    e.add_argument("--timeout", type=float, default=30.0, help="time to find the device (default 30)")
    e.add_argument("--duration", type=float, default=60.0, help="listen time in seconds (default 60)")
    e.add_argument("--raw", action="store_true", help="also print every raw packet as hex")
    e.add_argument("--quiet", action="store_true", help="log to file only, print nothing per packet")

    r = sub.add_parser("record", help="record the XCTrack-mode NMEA stream, reconnecting on drops")
    rt = r.add_mutually_exclusive_group()
    rt.add_argument("--address", help="device address/UUID from `scan`")
    rt.add_argument("--name", action="append", help="name fragment to match (repeatable)")
    r.add_argument("--timeout", type=float, default=20.0, help="time to find the device per attempt (default 20)")
    r.add_argument("--duration", type=float, default=1200.0, help="recording time in seconds (default 1200)")
    r.add_argument("--label", default="walk", help="label for the capture file name (default walk)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"scan": cmd_scan, "explore": cmd_explore, "record": cmd_record}[args.command]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
