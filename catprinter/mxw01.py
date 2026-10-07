'''Support for the MXW01 printer.

The MXW01 advertises the same 0xae30 service as the GB01/GB02/GB03/GT01 family,
but speaks a different protocol:

- Commands go to the 0xae01 characteristic, framed as
  0x22 0x21 <cmd> 0x00 <len lo> <len hi> <payload> <crc8(payload)> 0xff.
- Responses arrive as notifications on 0xae02, with the same framing.
- Raw image rows (1 bit per pixel, LSB first, 1 = black) go to 0xae03.

Protocol reference: https://github.com/MaikelChan/CatPrinterBLE
'''
import asyncio

import numpy as np

from catprinter import logger
from catprinter.cmds import PRINT_WIDTH, CHECKSUM_TABLE, byte_encode

CONTROL_CHARACTERISTIC_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_CHARACTERISTIC_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"
DATA_CHARACTERISTIC_UUID = "0000ae03-0000-1000-8000-00805f9b34fb"

CMD_GET_STATUS = 0xA1
CMD_PRINT_INTENSITY = 0xA2
CMD_PRINT = 0xA9
CMD_PRINT_COMPLETE = 0xAA
CMD_PRINT_DATA_FLUSH = 0xAD

PRINT_MODE_MONOCHROME = 0x00
# 4 bits per dot: 16 real burn levels instead of dithered black and white.
PRINT_MODE_GRAYSCALE = 0x02

# The printer seems to ignore jobs that are too short, so pad them with blank rows.
MIN_ROWS = 90
# The A9 print request carries the row count as a 16-bit number.
MAX_ROWS = 0xffff

WAIT_FOR_RESPONSE_TIMEOUT_S = 10
# Waiting for "print complete" scales with the job: the head prints roughly 15 rows per second
# at worst, so a fixed timeout would give up on long jobs that are still printing.
PRINT_COMPLETE_BASE_TIMEOUT_S = 15
PRINT_COMPLETE_ROWS_PER_S = 15
# Pause after each write to the data characteristic.
WAIT_AFTER_EACH_CHUNK_S = 0.008

STATUS_ERRORS = {
    0x01: "no paper",
    0x09: "no paper",
    0x04: "overheated",
    0x08: "low battery",
}


def crc8(data):
    crc = 0
    for b in data:
        crc = CHECKSUM_TABLE[(crc ^ b) & 0xff]
    return crc


def make_cmd(cmd_id, payload):
    payload = bytes(payload)
    return bytes([0x22, 0x21, cmd_id, 0x00, len(payload) & 0xff, len(payload) >> 8]) + \
        payload + bytes([crc8(payload), 0xff])


def energy_to_intensity(energy):
    '''Maps the 0x0000-0xffff energy range used by the other models to 0-100.'''
    return round(energy * 100 / 0xffff)


def print_complete_timeout(n_rows):
    return PRINT_COMPLETE_BASE_TIMEOUT_S + n_rows / PRINT_COMPLETE_ROWS_PER_S


def img_to_rows(img):
    '''Packs a boolean image (True = black) into 1 bpp rows: LSB = leftmost pixel.'''
    rows = [bytes(byte_encode(row)) for row in img]
    blank = bytes(PRINT_WIDTH // 8)
    rows.extend(blank for _ in range(MIN_ROWS - len(rows)))
    return rows


def levels_to_rows(levels):
    '''Packs burn levels (0 = white ... 15 = black) into 4 bpp rows.

    Even pixels go in the high nibble, odd pixels in the low one.
    '''
    levels = np.clip(levels, 0, 15).astype(np.uint8)
    packed = (levels[:, 0::2] << 4) | levels[:, 1::2]
    rows = [row.tobytes() for row in packed]
    blank = bytes(PRINT_WIDTH // 2)
    rows.extend(blank for _ in range(MIN_ROWS - len(rows)))
    return rows


def chunk_rows(rows, max_write):
    '''Groups whole rows into writes as large as the ATT MTU allows.'''
    per_write = max(1, max_write // len(rows[0]))
    return [b"".join(rows[i:i + per_write]) for i in range(0, len(rows), per_write)]


class Responses:
    '''Collects notifications from the printer, keyed by command id.'''

    def __init__(self):
        self._queues = {}

    def _queue(self, cmd_id):
        return self._queues.setdefault(cmd_id, asyncio.Queue())

    def receive(self, sender, data):
        logger.debug(f"📡 Received notification: {bytes(data).hex(' ')}")
        if len(data) < 6 or data[0] != 0x22 or data[1] != 0x21:
            return
        self._queue(data[2]).put_nowait(bytes(data))

    async def wait(self, cmd_id, timeout):
        return await asyncio.wait_for(self._queue(cmd_id).get(), timeout=timeout)


async def send_cmd(client, cmd_id, payload):
    await client.write_gatt_char(
        CONTROL_CHARACTERISTIC_UUID, make_cmd(cmd_id, payload), response=False)


async def check_status(client, responses):
    await send_cmd(client, CMD_GET_STATUS, [0x00])
    resp = await responses.wait(CMD_GET_STATUS, WAIT_FOR_RESPONSE_TIMEOUT_S)
    if len(resp) < 14:
        logger.warning(f"⚠️ Unexpected status response: {resp.hex(' ')}")
        return
    battery, temperature = resp[9], resp[10]
    if resp[12] != 0:
        reason = STATUS_ERRORS.get(resp[13], f"error code 0x{resp[13]:02x}")
        raise RuntimeError(f"Printer reported an error: {reason}")
    logger.info(f"✅ Printer OK. Battery: {battery}; temperature: {temperature}")


async def print_mxw01(client, img, energy, gray_levels=None):
    '''Prints a boolean image (True = black), or 4 bpp burn levels if gray_levels is given.'''
    responses = Responses()
    await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, responses.receive)

    await check_status(client, responses)

    intensity = energy_to_intensity(energy)
    await send_cmd(client, CMD_PRINT_INTENSITY, [intensity])

    if gray_levels is not None:
        rows, mode = levels_to_rows(gray_levels), PRINT_MODE_GRAYSCALE
    else:
        rows, mode = img_to_rows(img), PRINT_MODE_MONOCHROME
    n = len(rows)
    if n > MAX_ROWS:
        raise RuntimeError(f"The image has {n} rows; the printer accepts at most {MAX_ROWS}.")
    await send_cmd(client, CMD_PRINT, [n & 0xff, n >> 8, 0x30, mode])
    resp = await responses.wait(CMD_PRINT, WAIT_FOR_RESPONSE_TIMEOUT_S)
    if len(resp) > 6 and resp[6] != 0:
        raise RuntimeError(f"Printer rejected the print request: {resp.hex(' ')}")

    chunks = chunk_rows(rows, client.mtu_size - 3)
    logger.info(
        f"⏳ Sending {n} rows ({'grayscale' if mode == PRINT_MODE_GRAYSCALE else '1 bit'}) "
        f"at intensity {intensity}, in {len(chunks)} writes...")
    for chunk in chunks:
        await client.write_gatt_char(DATA_CHARACTERISTIC_UUID, chunk, response=False)
        await asyncio.sleep(WAIT_AFTER_EACH_CHUNK_S)

    await send_cmd(client, CMD_PRINT_DATA_FLUSH, [0x00])
    logger.info("⏳ Waiting for printer to finish...")
    await responses.wait(CMD_PRINT_COMPLETE, print_complete_timeout(n))
    logger.info("✅ Done printing.")
