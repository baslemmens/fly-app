# fly-app

Direct Link: pull paragliding flights straight off a Syride Sys'Nav XL over Bluetooth,
without going through Syride's servers.

This repo starts with **Phase 0: reconnaissance**, a read-only BLE scanner for the Mac.
It never writes to the instrument.

## Setup (Mac, VS Code)

1. Clone and open the folder in VS Code; install the recommended extensions when asked.
2. Create the environment: **Terminal → Run Task… → Setup: create venv and install**
   (or `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`).
3. Select the interpreter `.venv/bin/python` if VS Code doesn't pick it up.
4. Bluetooth permission: the first run triggers a macOS prompt. If nothing shows up,
   allow **Visual Studio Code** (or Terminal) under
   System Settings → Privacy & Security → Bluetooth.

## Before you scan

- Switch the XL on with Bluetooth enabled.
- Close the Syride app or turn Bluetooth off on your phone: the XL probably accepts
  only one connection at a time.

## Run it

From the Run and Debug panel (F5), or the terminal:

| Goal | Command |
| --- | --- |
| Find the XL | `python scanner/ble_scanner.py scan` |
| Didn't find it? See every device | `python scanner/ble_scanner.py scan --all --timeout 15` |
| Map services and log everything | `python scanner/ble_scanner.py explore --duration 120` |
| Same, by address, with raw hex | `python scanner/ble_scanner.py explore --address <UUID> --duration 300 --raw` |

`explore` writes two files to `captures/` (git-ignored, they contain GPS positions):

- `<time>_<device>.gatt.json`: every service and characteristic, with properties and read values
- `<time>_<device>.jsonl`: one line per event: raw notifications (hex + ascii), reassembled
  text lines with an NMEA flag and checksum check, and a summary

Try `explore` twice: once with the XL idle, once with external-sensor mode on.
Write what you find in `docs/protocol-notes.md`.

## Decode a capture

`python scanner/syride_live.py captures/<file>.jsonl` prints the decoded GPS fixes and vario
values from the live stream (add `--csv` for a spreadsheet). Format details are in
`docs/protocol-notes.md`.

## Tests

`python -m unittest discover -s tests -v` (also in the VS Code Testing panel).

## Roadmap

See the project doc: Direct Link – Sys'Nav XL flight pipeline.
Next: Phase 1 (send commands, compare against a SYS PC Tool IGC), then the iOS app.
