"""BSP/1 - the BlockShare Storage Protocol.

A small binary, length-prefixed application protocol that runs over TCP
between the coordinator (Django), the storage nodes and the CLI.

Every message (request or response) is a fixed 72-byte header followed by an
optional payload:

    offset size field
    ------ ---- -----------------------------------------------------------
         0    4 magic        b"BSP1"
         4    1 version      protocol version (1)
         5    1 opcode       request opcode; responses set the 0x80 bit
         6    1 flags        FLAG_FORWARD = payload carries a replication list
         7    1 status       0 in requests, a Status code in responses
         8    4 request_id   echoed back so clients can match responses
        12   32 key          SHA-256 of the chunk (content address)
        44    8 length       payload length in bytes (uint64)
        52    4 crc32        CRC-32 of the payload (transport integrity)
        56   16 auth         truncated HMAC-SHA256 over the header fields,
                             required for mutating operations
    ------ ---- -----------------------------------------------------------
    total   72  (network byte order / big-endian)

Integrity is checked at two layers: CRC-32 catches transport corruption of
a frame, and the content address (SHA-256 of the payload must equal `key`)
guarantees end-to-end chunk integrity.
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import json
import socket
import struct
import zlib
from dataclasses import dataclass, field

MAGIC = b"BSP1"
VERSION = 1
HEADER = struct.Struct("!4sBBBBI32sQI16s")
HEADER_SIZE = HEADER.size  # 72
MAX_PAYLOAD = 64 * 1024 * 1024
RESPONSE_BIT = 0x80
EMPTY_KEY = b"\x00" * 32
EMPTY_AUTH = b"\x00" * 16

FLAG_FORWARD = 0x01


class Op(enum.IntEnum):
    PING = 0x01
    PUT = 0x02
    GET = 0x03
    DELETE = 0x04
    HAS = 0x05
    STATS = 0x06
    LIST = 0x07
    CORRUPT = 0x08   # fault injection for integrity demos
    ADMIN = 0x09     # runtime control (simulate failure / recovery, latency)


class Status(enum.IntEnum):
    OK = 0
    NOT_FOUND = 1
    BAD_CHECKSUM = 2
    HASH_MISMATCH = 3
    UNAUTHORIZED = 4
    BAD_REQUEST = 5
    SERVER_ERROR = 6
    UNAVAILABLE = 7


# Operations that change state must carry a valid HMAC tag.
AUTHENTICATED_OPS = {Op.PUT, Op.DELETE, Op.CORRUPT, Op.ADMIN}


class ProtocolError(Exception):
    pass


@dataclass
class Frame:
    opcode: int
    key: bytes = EMPTY_KEY
    payload: bytes = b""
    flags: int = 0
    status: int = 0
    request_id: int = 0
    crc: int = field(default=0, repr=False)
    auth: bytes = field(default=EMPTY_AUTH, repr=False)

    @property
    def is_response(self) -> bool:
        return bool(self.opcode & RESPONSE_BIT)

    @property
    def op(self) -> Op:
        return Op(self.opcode & ~RESPONSE_BIT)

    @property
    def key_hex(self) -> str:
        return self.key.hex()

    def json(self):
        return json.loads(self.payload.decode() or "null")


def _auth_tag(cluster_key: bytes, opcode: int, flags: int, request_id: int, key: bytes, length: int, crc: int) -> bytes:
    msg = struct.pack("!BBI32sQI", opcode, flags, request_id, key, length, crc)
    return hmac.new(cluster_key, msg, hashlib.sha256).digest()[:16]


def encode(frame: Frame, cluster_key: bytes | None = None) -> bytes:
    if len(frame.key) != 32:
        raise ProtocolError("key must be 32 bytes")
    payload = frame.payload or b""
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    auth = EMPTY_AUTH
    if cluster_key and not frame.is_response:
        auth = _auth_tag(cluster_key, frame.opcode, frame.flags, frame.request_id, frame.key, len(payload), crc)
    header = HEADER.pack(MAGIC, VERSION, frame.opcode, frame.flags, frame.status, frame.request_id,
                         frame.key, len(payload), crc, auth)
    return header + payload


def decode_header(raw: bytes) -> Frame:
    magic, version, opcode, flags, status, request_id, key, length, crc, auth = HEADER.unpack(raw)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ProtocolError(f"unsupported version {version}")
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"payload too large ({length} bytes)")
    f = Frame(opcode=opcode, key=key, flags=flags, status=status, request_id=request_id, crc=crc, auth=auth)
    f.payload = b""
    f._length = length  # type: ignore[attr-defined]
    return f


def check_payload(frame: Frame) -> bool:
    return (zlib.crc32(frame.payload) & 0xFFFFFFFF) == frame.crc


def verify_auth(frame: Frame, cluster_key: bytes) -> bool:
    expected = _auth_tag(cluster_key, frame.opcode, frame.flags, frame.request_id, frame.key, len(frame.payload), frame.crc)
    return hmac.compare_digest(expected, frame.auth)


# ---------------------------------------------------------------- forwarding
# A PUT with FLAG_FORWARD carries the remaining replication pipeline in front
# of the chunk bytes:  [uint16 json_len][json list of "host:port"][chunk bytes]

def pack_forward(targets: list[str], data: bytes) -> bytes:
    meta = json.dumps(targets).encode()
    return struct.pack("!H", len(meta)) + meta + data


def unpack_forward(payload: bytes) -> tuple[list[str], bytes]:
    (n,) = struct.unpack("!H", payload[:2])
    return json.loads(payload[2:2 + n].decode()), payload[2 + n:]


# ------------------------------------------------------------ blocking I/O

def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(min(n - len(buf), 1 << 20))
        if not part:
            raise ConnectionError("connection closed by peer")
        buf.extend(part)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> Frame:
    frame = decode_header(recv_exact(sock, HEADER_SIZE))
    frame.payload = recv_exact(sock, frame._length) if frame._length else b""  # type: ignore[attr-defined]
    if not check_payload(frame):
        raise ProtocolError("CRC mismatch")
    return frame


# ------------------------------------------------------------ asyncio I/O

async def read_frame(reader, prefix: bytes = b"") -> Frame:
    """Read one frame; `prefix` holds header bytes the caller has already consumed."""
    frame = decode_header(prefix + await reader.readexactly(HEADER_SIZE - len(prefix)))
    frame.payload = await reader.readexactly(frame._length) if frame._length else b""  # type: ignore[attr-defined]
    return frame


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    host, _, port = endpoint.rpartition(":")
    return host, int(port)
