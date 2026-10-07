'''CUPS backend: lets CUPS (and anything that prints through it, including other
computers on the network) print on a cat printer.

CUPS runs it as `catprinter job-id user title copies options [file]` with the
printer's URI in $DEVICE_URI, e.g. catprinter:/48:0F:57:44:BF:3C or
catprinter:/auto. (A single slash: with "//", CUPS would read the address as
host:port and reject it.) The job arrives either as CUPS raster (from a queue using
cups/catprinter.ppd) or as raw ZPL (from a raw queue).

Run without arguments, it lists the device class for `lpinfo -v`.
'''
import asyncio
import logging
import os
import sys

import cv2
import numpy as np

from catprinter import logger
from catprinter.ble import run_ble
from catprinter.cmds import PRINT_WIDTH
from catprinter.cupsraster import is_cups_raster, read_pages
from catprinter.img import floyd_steinberg_dither
from catprinter.zpl import zpl_to_img

# Exit codes from <cups/backend.h>.
CUPS_BACKEND_OK = 0
CUPS_BACKEND_FAILED = 1
CUPS_BACKEND_CANCEL = 5
# Printer off, asleep or out of paper: CUPS keeps the job and tries again later.
CUPS_BACKEND_RETRY = 6

PAGE_GAP = 24
# Blank rows kept above and below the content of each page.
TRIM_MARGIN = 8
# Pixels lighter than this count as blank when trimming.
BLANK_THRESHOLD = 250
DEFAULT_ENERGY = 0xffff


class CupsLogHandler(logging.Handler):
    '''Prefixes messages the way CUPS expects them on stderr.'''

    PREFIXES = {logging.DEBUG: "DEBUG", logging.INFO: "INFO",
                logging.WARNING: "WARNING", logging.ERROR: "ERROR"}

    def emit(self, record):
        prefix = self.PREFIXES.get(record.levelno, "ERROR")
        sys.stderr.write(f"{prefix}: {record.getMessage()}\n")
        sys.stderr.flush()


def parse_options(text):
    options = {}
    for item in text.split():
        key, _, value = item.partition("=")
        options[key] = value.strip("'\"") if value else "true"
    return options


def device_from_uri(uri):
    '''catprinter:/48:0F:57:44:BF:3C -> "48:0F:57:44:BF:3C"; catprinter:/auto -> None.'''
    target = uri.partition(":")[2].strip("/")
    return None if target in ("", "auto") else target


def prepare_page(gray, tone):
    '''Fits a raster page to the head and converts it for printing.

    Returns (boolean image, gray levels or None), or None for a blank page.
    '''
    h, w = gray.shape
    if w != PRINT_WIDTH:
        # Pages wider than the head (A4, Letter) are shrunk to the paper width.
        gray = cv2.resize(gray, (PRINT_WIDTH, max(1, round(h * PRINT_WIDTH / w))),
                          interpolation=cv2.INTER_AREA)
    ink_rows = np.flatnonzero((gray < BLANK_THRESHOLD).any(axis=1))
    if not len(ink_rows):
        return None
    top = max(0, ink_rows[0] - TRIM_MARGIN)
    bottom = min(gray.shape[0], ink_rows[-1] + 1 + TRIM_MARGIN)
    gray = gray[top:bottom]

    if tone == "Dither":
        dithered = floyd_steinberg_dither(gray.astype(np.float32))
        return dithered <= 127, None
    if tone == "Gray":
        return gray < 128, (255 - gray.astype(np.uint8)) >> 4
    return gray < 128, None


def stack(images, gap=PAGE_GAP):
    out = []
    for img in images:
        out += [img, np.zeros((gap,) + img.shape[1:], dtype=img.dtype)]
    return np.vstack(out[:-1])


def build_job(data, options):
    '''Turns the job's bytes into (boolean image, gray levels or None).'''
    if is_cups_raster(data):
        tone = options.get("CatTone", "Threshold")
        prepared = [p for p in (prepare_page(page, tone) for page in read_pages(data)) if p]
        if not prepared:
            raise ValueError("The job has no printable content.")
        img = stack([p[0] for p in prepared])
        gray = stack([p[1] for p in prepared]) if tone == "Gray" else None
        return img, gray
    if data.lstrip()[:3] == b"^XA":
        layout = options.get("CatZplLayout", "reflow")
        return zpl_to_img(data.decode("utf-8", errors="replace"), PRINT_WIDTH, layout), None
    raise ValueError("Unsupported job format: expected CUPS raster or ZPL.")


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) == 1:
        print('direct catprinter "Unknown" "Cat Printer (Bluetooth LE)"')
        return CUPS_BACKEND_OK
    if len(argv) not in (6, 7):
        sys.stderr.write("Usage: catprinter job-id user title copies options [file]\n")
        return CUPS_BACKEND_FAILED

    logger.setLevel(logging.INFO)
    logger.handlers[:] = [CupsLogHandler()]
    options = parse_options(argv[5])
    device = device_from_uri(os.environ.get("DEVICE_URI", "catprinter:/auto"))

    if len(argv) == 7:
        with open(argv[6], "rb") as f:
            data = f.read()
        # Backends that read a file (raw queues) produce the copies themselves.
        copies = max(1, int(argv[4]) if argv[4].isdigit() else 1)
    else:
        data = sys.stdin.buffer.read()
        copies = 1

    try:
        img, gray = build_job(data, options)
    except ValueError as e:
        logger.error(f"🛑 {e}")
        return CUPS_BACKEND_CANCEL
    if copies > 1:
        img = stack([img] * copies)
        gray = stack([gray] * copies) if gray is not None else None

    energy = DEFAULT_ENERGY
    if options.get("CatEnergy", "").isdigit():
        energy = int(options["CatEnergy"]) * 0xffff // 100
    logger.info(f"Printing {img.shape[0]} rows")
    ok = asyncio.run(run_ble(img, energy=energy, device=device, gray_levels=gray))
    return CUPS_BACKEND_OK if ok else CUPS_BACKEND_RETRY


if __name__ == "__main__":
    sys.exit(main())
