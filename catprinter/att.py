'''Minimal GATT client that talks ATT over a raw LE L2CAP socket (Linux only).

Why not BlueZ: the MXW01 advertises the "simultaneous LE and BR/EDR" flag even
though it only answers over LE. BlueZ (at least up to 5.72) takes that at face
value and, when asked to connect, pages it over BR/EDR, which always ends in a
"Page Timeout". Its D-Bus API offers no way to force the LE transport, so we
open the LE connection ourselves through the kernel, which needs no root.

It implements just the subset of the BleakClient interface that the MXW01
code uses: start_notify() and write_gatt_char().
'''
import asyncio
import ctypes
import errno
import socket
import struct

from catprinter import logger

BTPROTO_L2CAP = 0
ATT_CID = 4
BDADDR_LE_PUBLIC = 1

ATT_ERROR_RSP = 0x01
ATT_EXCHANGE_MTU_REQ = 0x02
ATT_EXCHANGE_MTU_RSP = 0x03
ATT_FIND_INFO_REQ = 0x04
ATT_READ_BY_TYPE_REQ = 0x08
ATT_WRITE_REQ = 0x12
ATT_HANDLE_VALUE_NTF = 0x1B
ATT_HANDLE_VALUE_IND = 0x1D
ATT_HANDLE_VALUE_CFM = 0x1E
ATT_WRITE_CMD = 0x52
ATT_ERR_REQUEST_NOT_SUPPORTED = 0x06

UUID_CHARACTERISTIC = 0x2803
UUID_CCCD = 0x2902

DESIRED_MTU = 247
CONNECT_TIMEOUT_S = 20
RESPONSE_TIMEOUT_S = 5

_libc = ctypes.CDLL(None, use_errno=True)


class _SockaddrL2(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("family", ctypes.c_ushort),
        ("psm", ctypes.c_ushort),
        ("bdaddr", ctypes.c_ubyte * 6),
        ("cid", ctypes.c_ushort),
        ("bdaddr_type", ctypes.c_ubyte),
    ]


def _sockaddr(mac):
    # bdaddr_t is stored little-endian.
    raw = bytes(reversed(bytes.fromhex(mac.replace(":", "")))) if mac else bytes(6)
    return _SockaddrL2(socket.AF_BLUETOOTH, 0, (ctypes.c_ubyte * 6)(*raw),
                       ATT_CID, BDADDR_LE_PUBLIC)


def _check(ret, what):
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what} failed: {errno.errorcode.get(err, err)}")


def _uuid128(raw):
    if len(raw) == 2:
        return f"0000{int.from_bytes(raw, 'little'):04x}-0000-1000-8000-00805f9b34fb"
    h = raw[::-1].hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class LeAttClient:
    def __init__(self, address):
        self.address = address
        self.mtu_size = 23
        self._sock = None
        self._reader = None
        self._pending = None
        self._chars = {}  # uuid -> (declaration handle, value handle)
        self._notify_handlers = {}  # value handle -> callback

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    @property
    def is_connected(self):
        return self._sock is not None

    async def connect(self):
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        try:
            local = _sockaddr(None)
            _check(_libc.bind(sock.fileno(), ctypes.byref(local), ctypes.sizeof(local)), "bind")
            remote = _sockaddr(self.address)
            # SO_SNDTIMEO bounds the blocking connect().
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            struct.pack("ll", CONNECT_TIMEOUT_S, 0))
            await asyncio.to_thread(
                lambda: _check(_libc.connect(sock.fileno(), ctypes.byref(remote),
                                             ctypes.sizeof(remote)), "LE connect"))
        except OSError:
            sock.close()
            raise
        sock.setblocking(False)
        self._sock = sock
        self._reader = asyncio.create_task(self._read_loop())

        rsp = await self._request(struct.pack("<BH", ATT_EXCHANGE_MTU_REQ, DESIRED_MTU),
                                  ATT_EXCHANGE_MTU_RSP)
        self.mtu_size = min(DESIRED_MTU, struct.unpack_from("<H", rsp, 1)[0])
        await self._discover_characteristics()

    async def disconnect(self):
        if self._reader:
            self._reader.cancel()
            self._reader = None
        if self._sock:
            self._sock.close()
            self._sock = None

    async def _read_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            pdu = await loop.sock_recv(self._sock, 1024)
            if not pdu:
                logger.debug("📡 ATT link closed")
                return
            op = pdu[0]
            if op in (ATT_HANDLE_VALUE_NTF, ATT_HANDLE_VALUE_IND):
                if op == ATT_HANDLE_VALUE_IND:
                    await loop.sock_sendall(self._sock, bytes([ATT_HANDLE_VALUE_CFM]))
                handle = struct.unpack_from("<H", pdu, 1)[0]
                handler = self._notify_handlers.get(handle)
                if handler:
                    handler(handle, bytearray(pdu[3:]))
            elif op == ATT_EXCHANGE_MTU_REQ:
                await loop.sock_sendall(
                    self._sock, struct.pack("<BH", ATT_EXCHANGE_MTU_RSP, DESIRED_MTU))
            elif op % 2 == 0 and not op & 0x40:
                # A request from the printer we don't serve: tell it so, or the link stalls.
                await loop.sock_sendall(self._sock, struct.pack(
                    "<BBHB", ATT_ERROR_RSP, op, 0, ATT_ERR_REQUEST_NOT_SUPPORTED))
            elif self._pending and not self._pending.done():
                self._pending.set_result(pdu)

    async def _request(self, pdu, expected_op):
        '''Sends a request and returns its response, or None on an ATT error response.'''
        self._pending = asyncio.get_running_loop().create_future()
        await asyncio.get_running_loop().sock_sendall(self._sock, pdu)
        rsp = await asyncio.wait_for(self._pending, RESPONSE_TIMEOUT_S)
        if rsp[0] == ATT_ERROR_RSP:
            return None
        if rsp[0] != expected_op:
            raise RuntimeError(f"Unexpected ATT response: {rsp.hex(' ')}")
        return rsp

    async def _discover_characteristics(self):
        start = 1
        while start <= 0xffff:
            rsp = await self._request(
                struct.pack("<BHHH", ATT_READ_BY_TYPE_REQ, start, 0xffff, UUID_CHARACTERISTIC),
                ATT_READ_BY_TYPE_REQ + 1)
            if rsp is None:
                break
            item_len = rsp[1]
            for i in range(2, len(rsp) - item_len + 1, item_len):
                decl, _props, value = struct.unpack_from("<HBH", rsp, i)
                self._chars[_uuid128(rsp[i + 5:i + item_len])] = (decl, value)
                start = decl + 1

    def _value_handle(self, uuid):
        try:
            return self._chars[uuid.lower()][1]
        except KeyError:
            raise RuntimeError(f"Characteristic {uuid} not found on the printer")

    async def _find_cccd(self, value_handle):
        later = [decl for decl, _ in self._chars.values() if decl > value_handle]
        end = min(later) - 1 if later else 0xffff
        rsp = await self._request(
            struct.pack("<BHH", ATT_FIND_INFO_REQ, value_handle + 1, end),
            ATT_FIND_INFO_REQ + 1)
        if rsp is not None and rsp[1] == 0x01:  # 16-bit UUIDs
            for i in range(2, len(rsp) - 3, 4):
                handle, uuid = struct.unpack_from("<HH", rsp, i)
                if uuid == UUID_CCCD:
                    return handle
        return value_handle + 1

    async def start_notify(self, uuid, callback):
        value_handle = self._value_handle(uuid)
        self._notify_handlers[value_handle] = callback
        cccd = await self._find_cccd(value_handle)
        rsp = await self._request(struct.pack("<BHH", ATT_WRITE_REQ, cccd, 0x0001),
                                  ATT_WRITE_REQ + 1)
        if rsp is None:
            raise RuntimeError(f"Printer refused to enable notifications on {uuid}")

    async def write_gatt_char(self, uuid, data, response=False):
        handle = self._value_handle(uuid)
        if response:
            rsp = await self._request(struct.pack("<BH", ATT_WRITE_REQ, handle) + bytes(data),
                                      ATT_WRITE_REQ + 1)
            if rsp is None:
                raise RuntimeError(f"Write to {uuid} was rejected")
        else:
            await asyncio.get_running_loop().sock_sendall(
                self._sock, struct.pack("<BH", ATT_WRITE_CMD, handle) + bytes(data))
