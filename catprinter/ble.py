import asyncio
import contextlib
import errno
import sys
import uuid
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.scanner import AdvertisementData
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
# In MacOS capture exception trying Linux library import and set a flag
try:
    from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
except ImportError:
    BleakClientBlueZDBus = None
from catprinter import logger
from catprinter.cmds import cmds_print_img
from catprinter.att import LeAttClient
from catprinter.mxw01 import print_mxw01

# For some reason, bleak reports the 0xaf30 service on my macOS, while it reports
# 0xae30 (which I believe is correct) on my Raspberry Pi. This hacky workaround
# should cover both cases.

POSSIBLE_SERVICE_UUIDS = [
    "0000ae30-0000-1000-8000-00805f9b34fb",
    "0000af30-0000-1000-8000-00805f9b34fb",
]

TX_CHARACTERISTIC_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
RX_CHARACTERISTIC_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"

PRINTER_READY_NOTIFICATION = b"\x51\x78\xae\x01\x01\x00\x00\x00\xff"

SCAN_TIMEOUT_S = 10

# Wait time after sending each chunk of data through BLE.
WAIT_AFTER_EACH_CHUNK_S = 0.02

# Wait for printer done event timeout.
WAIT_FOR_PRINTER_DONE_TIMEOUT = 30

# The first connection attempt often fails while the printer wakes up from its idle sleep.
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY_S = 2


async def scan(name: Optional[str], timeout: int):
    autodiscover = not name
    if autodiscover:
        logger.info("⏳ Trying to auto-discover a printer...")
    else:
        logger.info(f"⏳ Looking for a BLE device named {name}...")

    def filter_fn(device: BLEDevice, adv_data: AdvertisementData):
        if autodiscover:
            return any(
                uuid in adv_data.service_uuids for uuid in POSSIBLE_SERVICE_UUIDS
            )
        else:
            return device.name == name

    device = await BleakScanner.find_device_by_filter(
        filter_fn,
        timeout=timeout,
    )
    if device is None:
        raise RuntimeError(
            "Unable to find printer, make sure it is turned on and in range"
        )
    logger.info(f"✅ Got it. Address: {device}")
    return device


def chunkify(data, chunk_size):
    return (data[i : i + chunk_size] for i in range(0, len(data), chunk_size))


MXW01_NAMES = ("MXW01",)


async def get_device(device: Optional[str]):
    # See if we were passed a string that smells like an UUID or MAC address.
    address = None
    if device:
        with contextlib.suppress(ValueError):
            address = str(uuid.UUID(device))
        if device.count(":") == 5 and device.replace(":", "").isalnum():
            address = device

    if address:
        # Scan for it anyway: we need the advertised name to tell the model apart.
        found = await BleakScanner.find_device_by_address(address, timeout=SCAN_TIMEOUT_S)
        if found is None:
            raise RuntimeError(
                f"Unable to find printer {address}, make sure it is turned on and in range"
            )
        return found

    return await scan(device, timeout=SCAN_TIMEOUT_S)


def notification_receiver_factory(event):
    def notification_receiver(sender, data):
        logger.debug(f"📡 Received notification: {data}")
        if data == PRINTER_READY_NOTIFICATION:
            event.set()

    return notification_receiver


async def wait_for_printer_ready(event):
    logger.info("⏳ Done printing. Waiting for printer to be ready...")
    await event.wait()
    logger.info("✅ Printer is ready, disconnecting...")


def connect_failure_hint(error):
    if isinstance(error, OSError) and error.errno == errno.ECONNREFUSED:
        # Only an explicit refusal points at another device holding the printer.
        return ("The printer refused the connection. It only accepts one connection at a "
                "time: close the phone app (or turn off the phone's Bluetooth) and try again.")
    return ("Could not connect to the printer. Make sure it is on and close to the computer; "
            "if it has been idle for a few minutes it may be asleep, so power-cycle it.")


async def connect_with_retries(make_client):
    '''Returns a connected client, retrying a few times. Raises ConnectionError.'''
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        client = make_client()
        try:
            await client.connect()
            return client
        except (OSError, asyncio.TimeoutError, BleakError) as e:
            error = e
            with contextlib.suppress(Exception):
                await client.disconnect()
            logger.warning(
                f"⚠️ Connection attempt {attempt}/{CONNECT_ATTEMPTS} failed: "
                f"{e or type(e).__name__}")
            if attempt < CONNECT_ATTEMPTS:
                await asyncio.sleep(CONNECT_RETRY_DELAY_S * attempt)
    raise ConnectionError(connect_failure_hint(error))


async def run_mxw01(ble_device, img, energy: int, gray_levels=None):
    # On Linux, BlueZ insists on connecting to the MXW01 over BR/EDR (see att.py),
    # so we bypass it and open the LE connection ourselves.
    if sys.platform.startswith("linux"):
        def make_client():
            return LeAttClient(ble_device.address)
    else:
        def make_client():
            return BleakClient(ble_device)
    try:
        client = await connect_with_retries(make_client)
    except ConnectionError as e:
        logger.error(f"🛑 {e}")
        return
    try:
        logger.info(f"✅ Connected; MTU: {client.mtu_size}")
        await print_mxw01(client, img, energy, gray_levels)
    except (OSError, RuntimeError, asyncio.TimeoutError) as e:
        logger.error(f"🛑 {e or 'Timed out waiting for the printer.'}")
    finally:
        await client.disconnect()


async def run_ble(img, energy: int, device: Optional[str], gray_levels=None):
    '''Prints img (boolean, True = black). gray_levels (0 = white ... 15 = black) is used
    instead on printers with a grayscale mode.'''
    try:
        ble_device = await get_device(device)
    except RuntimeError as e:
        logger.error(f"🛑 {e}")
        return
    logger.info(f"⏳ Connecting to {ble_device}...")

    if ble_device.name in MXW01_NAMES:
        logger.info("ℹ️ Detected an MXW01 printer.")
        await run_mxw01(ble_device, img, energy, gray_levels)
        return

    if gray_levels is not None:
        logger.warning("⚠️ This printer has no grayscale mode; printing dithered instead.")

    try:
        client = await connect_with_retries(lambda: BleakClient(ble_device))
    except ConnectionError as e:
        logger.error(f"🛑 {e}")
        return
    try:
        # XXX: BlueZ incorrectly reports a fixed MTU of 23; force MTU negotiation manually.
        # https://bleak.readthedocs.io/en/latest/api/client.html#bleak.BleakClient.mtu_size
        # Only use this library on Linux, not MacOS
        if BleakClientBlueZDBus and isinstance(client, BleakClientBlueZDBus):
            await client._acquire_mtu()

        logger.info(f"✅ Connected: {client.is_connected}; MTU: {client.mtu_size}")

        data = cmds_print_img(img, energy=energy)
        logger.info(f"✅ Generated BLE commands: {len(data)} bytes")
        chunk_size = client.mtu_size - 3
        event = asyncio.Event()

        receive_notification = notification_receiver_factory(event)

        await client.start_notify(RX_CHARACTERISTIC_UUID, receive_notification)

        logger.info(
            f"⏳ Sending {len(data)} bytes of data in chunks of {chunk_size} bytes..."
        )
        for i, chunk in enumerate(chunkify(data, chunk_size)):
            await client.write_gatt_char(TX_CHARACTERISTIC_UUID, chunk)
            await asyncio.sleep(WAIT_AFTER_EACH_CHUNK_S)

        try:
            await asyncio.wait_for(
                wait_for_printer_ready(event), timeout=WAIT_FOR_PRINTER_DONE_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error("🛑 Timed out while waiting for printer done event. Exiting.")
    finally:
        await client.disconnect()
