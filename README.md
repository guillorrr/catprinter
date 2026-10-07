![Cat Printer](./media/hackoclock.jpg)

Cat printer is a portable thermal printer sold on AliExpress for around $20.

This repository contains Python code for talking to the cat printer over Bluetooth Low Energy (BLE). The code has been reverse engineered from the [official Android app](https://play.google.com/store/apps/details?id=com.frogtosea.iprint&hl=en_US&gl=US).

> **Fork note:** this fork of [rbaron/catprinter](https://github.com/rbaron/catprinter) adds support for the **MXW01** printer. See [MXW01 support](#mxw01-support) below.

## Supported models

| Model | Protocol | Notes |
|---|---|---|
| GB01, GB02, GB03, GT01 | `0x51 0x78` commands on `0xae01` | Original upstream support. |
| **MXW01** | `0x22 0x21` commands on `0xae01`, image data on `0xae03` | Added in this fork. Detected automatically from its advertised name. |

# Installation
```bash
# Clone the repository.
$ git clone git@github.com:guillorrr/catprinter.git
$ cd catprinter
# Create a virtualenv on venv/ and activate it.
$ virtualenv --python=python3 venv
$ source venv/bin/activate
# Install requirements from requirements.txt.
$ pip install -r requirements.txt
```

# Usage
```bash
$ ./print.py --help
usage: print.py [-h] [-l {debug,info,warn,error}] [-b {mean-threshold,floyd-steinberg,atkinson,halftone,none}] [-s] [-d DEVICE] [-e ENERGY]
                filename

prints an image on your cat thermal printer

positional arguments:
  filename

options:
  -h, --help            show this help message and exit
  -l {debug,info,warn,error}, --log-level {debug,info,warn,error}
  -b {mean-threshold,floyd-steinberg,atkinson,halftone,none}, --img-binarization-algo {mean-threshold,floyd-steinberg,atkinson,halftone,none}
                        Which image binarization algorithm to use. If 'none' is used, no binarization will be used. In this case the image has to
                        have a width of 384 px.
  -s, --show-preview    If set, displays the final image and asks the user for confirmation before printing.
  -d DEVICE, --device DEVICE
                        The printer's Bluetooth Low Energy (BLE) address (MAC address on Linux; UUID on macOS) or advertisement name (e.g.:
                        "GT01", "GB02", "GB03", "MXW01"). If omitted, the the script will try to auto discover the printer based on its advertised BLE
                        services.
  -e ENERGY, --energy ENERGY
                        Thermal energy. Between 0x0000 (light) and 0xffff (darker, default).
```

# Example
```bash
% ./print.py --show-preview test.png
⏳ Applying Floyd-Steinberg dithering to image...
✅ Done.
ℹ️ Displaying preview.
🤔 Go ahead with print? [Y/n]?
✅ Read image: (42, 384) (h, w) pixels
✅ Generated BLE commands: 2353 bytes
⏳ Looking for a BLE device named GT01...
✅ Got it. Address: 09480C21-65B5-477B-B475-C797CD0D6B1C: GT01
⏳ Connecting to 09480C21-65B5-477B-B475-C797CD0D6B1C: GT01...
✅ Connected: True; MTU: 104
⏳ Sending 2353 bytes of data in chunks of 101 bytes...
✅ Done.
```


# MXW01 support

The MXW01 advertises the same `0xae30` service as the other models, so it is picked up by auto-discovery, but it speaks a completely different protocol. Sending it the GB/GT commands does nothing.

```bash
$ ./print.py -d 48:0F:57:44:BF:3C muzza.png   # or just: ./print.py muzza.png
⏳ Connecting to 48:0F:57:44:BF:3C: MXW01...
ℹ️ Detected an MXW01 printer.
✅ Connected; MTU: 247
✅ Printer OK. Battery: 85; temperature: 20
⏳ Sending 384 rows at intensity 100...
⏳ Waiting for printer to finish...
✅ Done printing.
```

`-e/--energy` keeps working: the `0x0000`-`0xffff` range is mapped to the MXW01's 0-100 intensity.

## Protocol

Implemented in [`catprinter/mxw01.py`](catprinter/mxw01.py), based on [MaikelChan/CatPrinterBLE](https://github.com/MaikelChan/CatPrinterBLE).

| Characteristic | Use |
|---|---|
| `0xae01` | Commands (write without response) |
| `0xae02` | Responses (notifications) |
| `0xae03` | Raw image rows (write without response) |

Every command and response is framed as `22 21 <cmd> 00 <len lo> <len hi> <payload> <crc8(payload)> ff`, using the same CRC-8 table as the other models.

A print job goes like this:

1. `0xA1` get status: the response carries battery, temperature and errors (no paper, overheated, low battery). The job is aborted if the printer reports an error.
2. `0xA2` set intensity (0-100).
3. `0xA9` print request with `[rows lo, rows hi, 0x30, mode]` (mode `0x00` = 1 bit per pixel). The printer acknowledges it.
4. Image rows to `0xae03`: 48 bytes per row (384 px), LSB first, `1` = black. Jobs shorter than 90 rows are padded with blank rows.
5. `0xAD` flush, then wait for `0xAA` (print complete).

## Linux: why it bypasses BlueZ

On Linux, connecting to the MXW01 through BlueZ (`bleak`, `bluetoothctl connect`) always times out, even with the printer on, in range and not connected to anything else.

The cause is in the printer's advertisement: its flags (`0x0a`) claim *simultaneous LE and BR/EDR* support, but it only answers over LE. BlueZ (checked on 5.72, `select_conn_bearer()` in `src/device.c`) treats it as a dual-mode device and, when asked to connect, pages it over classic BR/EDR. A `btmon` capture shows it clearly:

```
< HCI Command: Create Connection (0x01|0x0005)     Address: 48:0F:57:44:BF:3C
> HCI Event: Connect Complete (0x03)               Status: Page Timeout (0x04)
```

BlueZ's D-Bus API offers no way to force the LE transport (see [bleak#1521](https://github.com/hbldh/bleak/issues/1521)). So on Linux the MXW01 path uses [`catprinter/att.py`](catprinter/att.py): a minimal GATT client that opens an LE L2CAP socket on the ATT channel directly through the kernel, negotiates the MTU, discovers the characteristics and enables notifications. It needs no root and no BlueZ configuration changes. On macOS the regular `bleak` client is used, since CoreBluetooth connects over LE.

The GB/GT models are unaffected: they keep going through `bleak` on every platform.

### Troubleshooting

- Only one central can be connected at a time: close the official app on your phone (or turn its Bluetooth off) before printing.
- To see what the radio is doing, capture HCI traffic while printing: `sudo btmon -w btmon.snoop`, then read it with `btmon -r btmon.snoop`.

# Different Algorithms

**Mean Threshold:**

![Mean threshold](./media/grumpymeanthreshold.png)


**Floyd Steinberg (default):**

![Floyd Steinberg](./media/grumpyfloydsteinbergexample.png)

**Atkinson:**

![Atkinson](./media/grumpyatkinsonexample.png)

**Halftone dithering:**

![Halftone](./media/grumpyhalftone.png)

**None (image must be 384px wide):**

![None](./media/grumpynone.png)
