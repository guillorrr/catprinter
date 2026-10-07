![Cat Printer](./media/hackoclock.jpg)

Cat printer is a portable thermal printer sold on AliExpress for around $20.

This repository contains Python code for talking to the cat printer over Bluetooth Low Energy (BLE). The code has been reverse engineered from the [official Android app](https://play.google.com/store/apps/details?id=com.frogtosea.iprint&hl=en_US&gl=US).

> **Fork note:** this fork of [rbaron/catprinter](https://github.com/rbaron/catprinter) adds support for the **MXW01** printer. See [MXW01 support](#mxw01-support) below.

## Supported models

| Model | Protocol | Notes |
|---|---|---|
| GB01, GB02, GB03, GT01 | `0x51 0x78` commands on `0xae01` | Original upstream support. |
| **MXW01** | `0x22 0x21` commands on `0xae01`, image data on `0xae03` | Added in this fork. Detected automatically from its advertised name. Supports 16-level grayscale. |

It can also be installed as a **CUPS printer**, so any program (and other computers on the network, including Windows) can print to it. See [Printing from CUPS and Windows](#printing-from-cups-and-windows).

This fork can also print **ZPL labels** (the Zebra label language Mercado Libre uses for its product and shipping labels). See [ZPL labels](#zpl-labels-mercado-libre).

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
usage: print.py [-h] [-l {debug,info,warn,error}]
                [-b {mean-threshold,floyd-steinberg,atkinson,halftone,none}]
                [-s] [-d DEVICE] [-g] [--zpl-layout {reflow,scale}]
                [-e ENERGY]
                filename

prints an image or a ZPL label on your cat thermal printer

positional arguments:
  filename

options:
  -h, --help            show this help message and exit
  -l {debug,info,warn,error}, --log-level {debug,info,warn,error}
  -b {mean-threshold,floyd-steinberg,atkinson,halftone,none}, --img-binarization-algo {mean-threshold,floyd-steinberg,atkinson,halftone,none}
                        Which image binarization algorithm to use. If 'none'
                        is used, no binarization will be used. In this case
                        the image has to have a width of 384 px.
  -s, --show-preview    If set, displays the final image and asks the user for
                        confirmation before printing.
  -d DEVICE, --device DEVICE
                        The printer's Bluetooth Low Energy (BLE) address (MAC
                        address on Linux; UUID on macOS) or advertisement name
                        (e.g.: "GT01", "GB02", "GB03", "MXW01"). If omitted,
                        the the script will try to auto discover the printer
                        based on its advertised BLE services.
  -g, --grayscale       Print with 16 real gray levels instead of dithered
                        black and white. Only the MXW01 supports it; other
                        models fall back to dithering. Ignored for ZPL labels.
  --zpl-layout {reflow,scale}
                        How to fit ZPL labels wider than the paper (e.g. 10 cm
                        shipping labels): "reflow" rebuilds them as a single
                        column, keeping text and QR codes at their original
                        size; "scale" shrinks them as drawn.
  -e ENERGY, --energy ENERGY
                        Thermal energy. Between 0x0000 (light) and 0xffff
                        (darker, default).
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

## Grayscale

`-g/--grayscale` uses the MXW01's 4 bits-per-dot mode: 16 real burn levels instead of dithered black and white, which looks much better for photos. Other models ignore it and print dithered as usual.

```bash
$ ./print.py -g photo.jpg
```

## Reliability

- The connection is retried up to 3 times: the first attempt often fails while the printer wakes up. It falls asleep after 5-6 idle minutes; if it is not found at all, press its button.
- The wait for the "print complete" notification scales with the job length (15 s + 1 s per 15 rows), so long jobs are not cut off.
- Image rows are sent in as few BLE packets as the negotiated MTU allows.

These ideas come from [Aelieth/catprinter-linux](https://github.com/Aelieth/catprinter-linux), a Rust daemon that exposes the printer to CUPS as an IPP Everywhere printer.

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
3. `0xA9` print request with `[rows lo, rows hi, 0x30, mode]` (mode `0x00` = 1 bit per pixel, `0x02` = 4 bits per pixel). The printer acknowledges it.
4. Image rows to `0xae03`. 1 bpp: 48 bytes per row (384 px), LSB first, `1` = black. 4 bpp: 192 bytes per row, level `0` = white to `15` = black, even pixels in the high nibble. Jobs shorter than 90 rows are padded with blank rows.
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

# ZPL labels (Mercado Libre)

`print.py` also accepts ZPL files (anything starting with `^XA`), such as the `.txt` labels Mercado Libre gives you for a Zebra printer. They are drawn locally by [`catprinter/zpl.py`](catprinter/zpl.py); nothing is sent to an online service.

```bash
$ ./print.py etiqueta.txt
```

Supported commands: `^XA`/`^XZ`, `^LH`, `^FO`, `^FD`/`^FS`, `^FH`, `^CI28`, `^A0`, `^FB`, `^GB`, `^GFA` (including ZPL compression), `^BY`/`^BC` (Code 128) and `^BQ` (QR code). Anything else is ignored.

ZPL works in 203 dpi dots, the same resolution as these printers, so:

- **Labels that fit** (e.g. product labels) are printed 1:1, and barcodes keep their exact module width so they still scan. Files with several stickers side by side are split into columns and printed one after the other.
- **Labels wider than the paper** (e.g. 10 cm Flex shipping labels) are, by default, **rebuilt as a single 48 mm column** (`--zpl-layout reflow`). Text and QR codes keep their original size; fields that sat side by side go one under the other when they don't fit together, a logo next to text stays next to it, and vertical rules are dropped. A 10x15 cm Flex label comes out at about 4.8x12 cm.
- `--zpl-layout scale` shrinks the label as drawn instead (about 48% for a 10 cm label), which keeps the layout but makes the text very small.

The text uses the closest installed condensed bold font to Zebra's `^A0` (Liberation Sans Narrow, Nimbus Sans Narrow or DejaVu Sans Condensed).

# Printing from CUPS and Windows

[`cups/`](cups/) turns the printer into a regular CUPS printer on Linux. The computer running CUPS talks to the printer over Bluetooth; everything else, including other computers on the network, prints to CUPS.

```bash
$ sudo ./cups/install.sh 48:0F:57:44:BF:3C --share   # your printer's address, or "auto"
```

It installs the package in `/opt/catprinter` (with its own virtualenv), the backend in `/usr/lib/cups/backend/catprinter` and two queues:

| Queue | Takes | Use it for |
|---|---|---|
| `CatPrinter` | Anything CUPS can print: PDF, images, text, office documents | Printing from any application or computer |
| `CatPrinter-ZPL` | Raw ZPL | Mercado Libre (or any Zebra) labels, exactly as downloaded |

```bash
$ lp -d CatPrinter photo.jpg -o CatTone=Gray
$ lp -d CatPrinter-ZPL etiqueta.txt
```

`CatPrinter` options:

| Option | Values | Notes |
|---|---|---|
| Paper (`PageSize`) | 48x50, 48x100 (default), 48x150, 48x297 mm, custom lengths, **A4 / Letter** | Blank space is trimmed. A4 and Letter pages are shrunk to the 48 mm width, so applications lay out a normal page. |
| Tone (`CatTone`) | **Threshold** (text, labels, barcodes), Dither (photos), Gray (16 levels, MXW01 only) | |
| Darkness (`CatEnergy`) | 60, 80, **100** | |

`CatPrinter-ZPL` takes `-o CatZplLayout=scale` to shrink wide labels instead of rebuilding them.

If the printer is off or asleep the backend tells CUPS to retry, so the job waits in the queue (`printer-error-policy=retry-job`) and prints once the printer is back. `sudo ./cups/install.sh --uninstall` removes everything.

## From Windows

With `--share`, both queues are published on the local network (port 631; if `ufw` is active, `sudo ufw allow 631/tcp`).

- **Documents and images:** *Settings → Bluetooth & devices → Printers & scanners → Add device → Add manually → Select a shared printer by name*, enter `http://<linux-host>:631/printers/CatPrinter` and pick the **Microsoft IPP Class Driver** (or *Generic → MS Publisher Imagesetter*). Choose the 48 mm paper sizes, or A4 to get a page shrunk to the roll.
- **ZPL labels:** add `http://<linux-host>:631/printers/CatPrinter-ZPL` the same way but with the **Generic / Text Only** driver, which passes the text through, and print the label `.txt` from Notepad.

The Windows side has not been tested yet; the Linux side (CUPS raster and ZPL through the backend) has.

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
