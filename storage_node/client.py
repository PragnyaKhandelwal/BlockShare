"""Blocking BSP/1 client used by the Django coordinator, the monitor and the CLI."""
from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from dataclasses import dataclass

from .protocol import (
    EMPTY_KEY, FLAG_FORWARD, Frame, Op, Status, encode, pack_forward, parse_endpoint, recv_frame, sha256,
)

_request_ids = itertools.count(1)
_id_lock = threading.Lock()


def _next_id() -> int:
    with _id_lock:
        return next(_request_ids) & 0xFFFFFFFF


class NodeError(Exception):
    def __init__(self, endpoint: str, status: int | None, message: str = ""):
        self.endpoint = endpoint
        self.status = status
        name = Status(status).name if status is not None else "NETWORK_ERROR"
        super().__init__(f"{endpoint}: {name} {message}".strip())


@dataclass
class Exchange:
    """One request/response round trip - shown in the Transfers 'frame log'."""
    op: str
    endpoint: str
    key: str
    bytes_sent: int
    bytes_received: int
    rtt_ms: float
    status: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class NodeClient:
    def __init__(self, endpoint: str, cluster_key: bytes | None = None, timeout: float = 10.0):
        self.endpoint = endpoint
        self.host, self.port = parse_endpoint(endpoint)
        self.cluster_key = cluster_key
        self.timeout = timeout
        self.last: Exchange | None = None

    def request(self, op: Op, key: bytes = EMPTY_KEY, payload: bytes = b"", flags: int = 0) -> Frame:
        frame = Frame(opcode=int(op), key=key, payload=payload, flags=flags, request_id=_next_id())
        data = encode(frame, self.cluster_key)
        start = time.perf_counter()
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.sendall(data)
                resp = recv_frame(sock)
        except (OSError, ConnectionError) as exc:
            self.last = Exchange(op.name, self.endpoint, key.hex()[:16], len(payload), 0,
                                 round((time.perf_counter() - start) * 1000, 2), "NETWORK_ERROR")
            raise NodeError(self.endpoint, None, str(exc)) from exc
        rtt = (time.perf_counter() - start) * 1000
        self.last = Exchange(op.name, self.endpoint, key.hex()[:16], len(payload), len(resp.payload),
                             round(rtt, 2), Status(resp.status).name)
        if resp.request_id != frame.request_id:
            raise NodeError(self.endpoint, None, "response id mismatch")
        return resp

    def _ok(self, resp: Frame, allow_not_found=False) -> bool:
        if resp.status == Status.OK:
            return True
        if allow_not_found and resp.status == Status.NOT_FOUND:
            return False
        raise NodeError(self.endpoint, resp.status)

    # ------------------------------------------------------------ operations
    def ping(self) -> float:
        start = time.perf_counter()
        self._ok(self.request(Op.PING, payload=b"x" * 32))
        return round((time.perf_counter() - start) * 1000, 2)

    def put(self, data: bytes, forward_to: list[str] | None = None, key: bytes | None = None) -> dict:
        key = key or sha256(data)
        if forward_to:
            resp = self.request(Op.PUT, key, pack_forward(forward_to, data), FLAG_FORWARD)
        else:
            resp = self.request(Op.PUT, key, data)
        self._ok(resp)
        return resp.json()

    def get(self, key: bytes) -> bytes | None:
        resp = self.request(Op.GET, key)
        return resp.payload if self._ok(resp, allow_not_found=True) else None

    def has(self, key: bytes) -> bool:
        return self._ok(self.request(Op.HAS, key), allow_not_found=True)

    def delete(self, key: bytes) -> bool:
        return self._ok(self.request(Op.DELETE, key), allow_not_found=True)

    def corrupt(self, key: bytes) -> bool:
        return self._ok(self.request(Op.CORRUPT, key), allow_not_found=True)

    def stats(self) -> dict:
        resp = self.request(Op.STATS)
        self._ok(resp)
        return resp.json()

    def list_keys(self) -> list[str]:
        resp = self.request(Op.LIST)
        self._ok(resp)
        return resp.json()

    def admin(self, **cmd) -> dict:
        resp = self.request(Op.ADMIN, payload=json.dumps(cmd).encode())
        self._ok(resp)
        return resp.json()
