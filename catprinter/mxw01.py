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

# The printer seems to ignore jobs that are too short, so pad them with blank rows.
MIN_ROWS = 90

WAIT_FOR_RESPONSE_TIMEOUT_S = 10
WAIT_FOR_PRINT_COMPLETE_TIMEOUT_S = 60

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


def img_to_rows(img):
    rows = [bytes(byte_encode(row)) for row in img]
    blank = bytes(PRINT_WIDTH // 8)
    rows.extend(blank for _ in range(MIN_ROWS - len(rows)))
    return rows


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


async def print_mxw01(client, img, energy):
    responses = Responses()
    await client.start_notify(NOTIFY_CHARACTERISTIC_UUID, responses.receive)

    await check_status(client, responses)

    intensity = energy_to_intensity(energy)
    await send_cmd(client, CMD_PRINT_INTENSITY, [intensity])

    rows = img_to_rows(img)
    n = len(rows)
    await send_cmd(client, CMD_PRINT, [n & 0xff, n >> 8, 0x30, PRINT_MODE_MONOCHROME])
    resp = await responses.wait(CMD_PRINT, WAIT_FOR_RESPONSE_TIMEOUT_S)
    if len(resp) > 6 and resp[6] != 0:
        raise RuntimeError(f"Printer rejected the print request: {resp.hex(' ')}")

    logger.info(f"⏳ Sending {n} rows at intensity {intensity}...")
    for row in rows:
        await client.write_gatt_char(DATA_CHARACTERISTIC_UUID, row, response=False)
        await asyncio.sleep(0.005)

    await send_cmd(client, CMD_PRINT_DATA_FLUSH, [0x00])
    logger.info("⏳ Waiting for printer to finish...")
    await responses.wait(CMD_PRINT_COMPLETE, WAIT_FOR_PRINT_COMPLETE_TIMEOUT_S)
    logger.info("✅ Done printing.")
