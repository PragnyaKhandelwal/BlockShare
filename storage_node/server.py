"""BlockShare storage node.

A single asyncio process exposes three network services:

* TCP  (BSP/1)   - chunk data plane: PUT / GET / HAS / DELETE / STATS ...
                   PUTs can be pipelined to further replicas (chain replication,
                   the same idea as the HDFS write pipeline).
* UDP  heartbeat - every node periodically gossips a small JSON heartbeat to its
                   peers; nodes learn about new peers from each other and keep a
                   liveness table (alive / suspect / dead, packet loss estimate).
* HTTP/1.1       - a tiny hand-written status endpoint (/health, /stats, /metrics)
                   for browsers, load balancers and Docker health checks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections import Counter

from .protocol import (
    AUTHENTICATED_OPS, EMPTY_KEY, FLAG_FORWARD, RESPONSE_BIT, Frame, Op, ProtocolError, Status,
    check_payload, encode, pack_forward, parse_endpoint, read_frame, sha256, unpack_forward, verify_auth,
)
from .store import ChunkStore

log = logging.getLogger("storage_node")


class PeerInfo:
    def __init__(self, node_id: str, udp: str):
        self.node_id = node_id
        self.udp = udp
        self.tcp = ""
        self.last_seen = 0.0
        self.last_seq = 0
        self.received = 0
        self.lost = 0
        self.chunks = 0
        self.bytes = 0

    def as_dict(self, interval: float) -> dict:
        age = time.time() - self.last_seen if self.last_seen else None
        if age is None or age > 6 * interval:
            status = "dead"
        elif age > 3 * interval:
            status = "suspect"
        else:
            status = "alive"
        total = self.received + self.lost
        return {
            "node_id": self.node_id, "udp": self.udp, "tcp": self.tcp, "status": status,
            "last_seen_s": round(age, 2) if age is not None else None,
            "heartbeats": self.received, "lost": self.lost,
            "loss_pct": round(100.0 * self.lost / total, 1) if total else 0.0,
            "chunks": self.chunks, "bytes": self.bytes,
        }


class HeartbeatProtocol(asyncio.DatagramProtocol):
    def __init__(self, node: "StorageNode"):
        self.node = node

    def connection_made(self, transport):
        self.node.udp_transport = transport

    def datagram_received(self, data, addr):
        self.node.on_heartbeat(data, addr)

    def error_received(self, exc):  # e.g. ICMP port unreachable from a dead peer
        log.debug("udp error: %s", exc)


class StorageNode:
    def __init__(self, node_id: str, data_dir: str, host: str = "0.0.0.0", port: int = 9000,
                 udp_port: int | None = None, http_port: int | None = None, advertise_host: str | None = None,
                 peers: list[str] | None = None, cluster_key: bytes | None = None,
                 heartbeat_interval: float = 2.0, latency_ms: int = 0):
        self.node_id = node_id
        self.host = host
        self.port = port
        self.udp_port = udp_port if udp_port is not None else port + 1000
        self.http_port = http_port
        self.advertise_host = advertise_host or (host if host not in ("0.0.0.0", "") else socket.gethostname())
        self.cluster_key = cluster_key
        self.heartbeat_interval = heartbeat_interval
        self.latency_ms = latency_ms
        self.store = ChunkStore(data_dir)
        self.offline = False
        self.started = time.time()
        self.metrics: Counter = Counter()
        self.known_peers: set[str] = {p for p in (peers or []) if p}
        self.peers: dict[str, PeerInfo] = {}
        self.seq = 0
        self.udp_transport = None
        self._servers: list = []
        self._tasks: list = []

    @property
    def tcp_endpoint(self) -> str:
        return f"{self.advertise_host}:{self.port}"

    @property
    def udp_endpoint(self) -> str:
        return f"{self.advertise_host}:{self.udp_port}"

    # ------------------------------------------------------------- lifecycle
    async def start(self):
        loop = asyncio.get_running_loop()
        self._servers.append(await asyncio.start_server(self._handle_tcp, self.host, self.port))
        await loop.create_datagram_endpoint(lambda: HeartbeatProtocol(self), local_addr=(self.host, self.udp_port))
        if self.http_port:
            self._servers.append(await asyncio.start_server(self._handle_http, self.host, self.http_port))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))
        log.info("[%s] BSP/TCP on %s:%s  heartbeat/UDP on %s  HTTP on %s  data=%s",
                 self.node_id, self.host, self.port, self.udp_port, self.http_port, self.store.root)

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for s in self._servers:
            s.close()
        if self.udp_transport:
            self.udp_transport.close()

    # ------------------------------------------------------------- TCP / BSP
    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.metrics["connections"] += 1
        try:
            while True:
                try:
                    frame = await read_frame(reader)
                except asyncio.IncompleteReadError:
                    break
                self.metrics["bytes_in"] += len(frame.payload)
                response = await self.dispatch(frame)
                data = encode(response)
                self.metrics["bytes_out"] += len(response.payload)
                writer.write(data)
                await writer.drain()
        except ProtocolError as exc:
            self.metrics["protocol_errors"] += 1
            log.warning("[%s] protocol error: %s", self.node_id, exc)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def _reply(self, req: Frame, status: Status = Status.OK, payload: bytes = b"", key: bytes | None = None) -> Frame:
        return Frame(opcode=req.opcode | RESPONSE_BIT, key=key if key is not None else req.key,
                     payload=payload, status=int(status), request_id=req.request_id)

    def _json(self, req: Frame, obj, status: Status = Status.OK) -> Frame:
        return self._reply(req, status, json.dumps(obj).encode())

    async def dispatch(self, req: Frame) -> Frame:
        try:
            op = Op(req.opcode)
        except ValueError:
            return self._reply(req, Status.BAD_REQUEST)
        self.metrics[f"op_{op.name.lower()}"] += 1

        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        if not check_payload(req):
            self.metrics["crc_errors"] += 1
            return self._reply(req, Status.BAD_CHECKSUM)
        if op in AUTHENTICATED_OPS and self.cluster_key and not verify_auth(req, self.cluster_key):
            self.metrics["auth_failures"] += 1
            return self._reply(req, Status.UNAUTHORIZED)
        if self.offline and op not in (Op.ADMIN,):
            return self._reply(req, Status.UNAVAILABLE)

        loop = asyncio.get_running_loop()
        key_hex = req.key.hex()

        if op == Op.PING:
            return self._json(req, {"node_id": self.node_id, "ts": time.time(), "echo": len(req.payload)})

        if op == Op.PUT:
            targets, data = ([], req.payload)
            if req.flags & FLAG_FORWARD:
                targets, data = unpack_forward(req.payload)
            if sha256(data) != req.key:
                self.metrics["hash_mismatch"] += 1
                return self._reply(req, Status.HASH_MISMATCH)
            created = await loop.run_in_executor(None, self.store.put, key_hex, data)
            stored_on = [self.node_id]
            failed = []
            if targets:
                downstream, rest = targets[0], targets[1:]
                try:
                    stored_on += await self._forward(downstream, req.key, data, rest)
                except Exception as exc:  # pipeline broken downstream - report partial success
                    self.metrics["forward_failures"] += 1
                    failed.append({"target": downstream, "error": str(exc)})
            return self._json(req, {"stored_on": stored_on, "created": created, "failed": failed})

        if op == Op.GET:
            data = await loop.run_in_executor(None, self.store.get, key_hex)
            if data is None:
                return self._reply(req, Status.NOT_FOUND)
            return self._reply(req, Status.OK, data)

        if op == Op.HAS:
            return self._reply(req, Status.OK if self.store.has(key_hex) else Status.NOT_FOUND)

        if op == Op.DELETE:
            ok = await loop.run_in_executor(None, self.store.delete, key_hex)
            return self._reply(req, Status.OK if ok else Status.NOT_FOUND)

        if op == Op.CORRUPT:
            ok = await loop.run_in_executor(None, self.store.corrupt, key_hex)
            return self._reply(req, Status.OK if ok else Status.NOT_FOUND)

        if op == Op.STATS:
            return self._json(req, self.stats())

        if op == Op.LIST:
            return self._json(req, await loop.run_in_executor(None, self.store.keys))

        if op == Op.ADMIN:
            cmd = req.json() or {}
            if "offline" in cmd:
                self.offline = bool(cmd["offline"])
                log.warning("[%s] simulated %s", self.node_id, "FAILURE" if self.offline else "RECOVERY")
            if "latency_ms" in cmd:
                self.latency_ms = max(0, int(cmd["latency_ms"]))
            return self._json(req, {"offline": self.offline, "latency_ms": self.latency_ms})

        return self._reply(req, Status.BAD_REQUEST)

    async def _forward(self, endpoint: str, key: bytes, data: bytes, rest: list[str]) -> list[str]:
        host, port = parse_endpoint(endpoint)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
        try:
            payload = pack_forward(rest, data) if rest else data
            frame = Frame(opcode=Op.PUT, key=key, payload=payload, flags=FLAG_FORWARD if rest else 0)
            writer.write(encode(frame, self.cluster_key))
            await writer.drain()
            resp = await asyncio.wait_for(read_frame(reader), timeout=30)
            if resp.status != Status.OK:
                raise RuntimeError(f"{endpoint} replied {Status(resp.status).name}")
            self.metrics["forwarded"] += 1
            return resp.json()["stored_on"]
        finally:
            writer.close()

    def stats(self) -> dict:
        return {
            "node_id": self.node_id,
            "tcp": self.tcp_endpoint,
            "udp": self.udp_endpoint,
            "chunks": self.store.count,
            "bytes": self.store.bytes_used,
            "uptime_s": round(time.time() - self.started, 1),
            "offline": self.offline,
            "latency_ms": self.latency_ms,
            "metrics": dict(self.metrics),
            "peers": [p.as_dict(self.heartbeat_interval) for p in sorted(self.peers.values(), key=lambda p: p.node_id)],
        }

    # ------------------------------------------------------------ UDP gossip
    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            if self.offline or not self.udp_transport:
                continue
            self.seq += 1
            msg = json.dumps({
                "t": "hb", "id": self.node_id, "seq": self.seq, "ts": time.time(),
                "tcp": self.tcp_endpoint, "udp": self.udp_endpoint,
                "chunks": self.store.count, "bytes": self.store.bytes_used,
                "peers": sorted(self.known_peers | {self.udp_endpoint}),
            }).encode()
            for endpoint in list(self.known_peers):
                if endpoint == self.udp_endpoint:
                    continue
                try:
                    host, port = parse_endpoint(endpoint)
                    self.udp_transport.sendto(msg, (socket.gethostbyname(host), port))
                    self.metrics["heartbeats_sent"] += 1
                except OSError:
                    self.metrics["heartbeat_send_errors"] += 1

    def on_heartbeat(self, data: bytes, addr):
        try:
            hb = json.loads(data.decode())
        except ValueError:
            return
        if hb.get("t") != "hb" or hb.get("id") == self.node_id or self.offline:
            return
        self.metrics["heartbeats_received"] += 1
        peer = self.peers.get(hb["id"])
        if peer is None:
            peer = self.peers[hb["id"]] = PeerInfo(hb["id"], hb.get("udp", f"{addr[0]}:{addr[1]}"))
            log.info("[%s] discovered peer %s via %s", self.node_id, hb["id"], addr)
        seq = int(hb.get("seq", 0))
        if peer.last_seq and seq > peer.last_seq + 1:
            peer.lost += seq - peer.last_seq - 1
        elif seq < peer.last_seq:  # peer restarted
            peer.lost = 0
            peer.received = 0
        peer.last_seq = seq
        peer.received += 1
        peer.last_seen = time.time()
        peer.tcp = hb.get("tcp", "")
        peer.udp = hb.get("udp", peer.udp)
        peer.chunks = hb.get("chunks", 0)
        peer.bytes = hb.get("bytes", 0)
        # gossip-based membership: learn every peer our peers know about
        for endpoint in [peer.udp, *hb.get("peers", [])]:
            if endpoint and endpoint != self.udp_endpoint:
                self.known_peers.add(endpoint)

    # ------------------------------------------------------- minimal HTTP/1.1
    async def _handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            request_line = raw.split(b"\r\n", 1)[0].decode(errors="replace")
            method, path, _ = (request_line.split(" ") + ["", ""])[:3]
            path = path.split("?", 1)[0]
            if method != "GET":
                status, ctype, body = "405 Method Not Allowed", "text/plain", b"method not allowed\n"
            elif path in ("/", "/health"):
                ok = not self.offline
                status = "200 OK" if ok else "503 Service Unavailable"
                ctype, body = "application/json", json.dumps({"node_id": self.node_id, "healthy": ok}).encode()
            elif path == "/stats":
                status, ctype, body = "200 OK", "application/json", json.dumps(self.stats(), indent=2).encode()
            elif path == "/metrics":
                lines = [f'blockshare_node_chunks{{node="{self.node_id}"}} {self.store.count}',
                         f'blockshare_node_bytes{{node="{self.node_id}"}} {self.store.bytes_used}']
                lines += [f'blockshare_node_{k}{{node="{self.node_id}"}} {v}' for k, v in sorted(self.metrics.items())]
                status, ctype, body = "200 OK", "text/plain; version=0.0.4", ("\n".join(lines) + "\n").encode()
            else:
                status, ctype, body = "404 Not Found", "text/plain", b"not found\n"
            head = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                    f"Server: BlockShare-Node/{self.node_id}\r\nConnection: close\r\n\r\n").encode()
            writer.write(head + body)
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            pass
        finally:
            writer.close()


__all__ = ["StorageNode", "EMPTY_KEY"]
