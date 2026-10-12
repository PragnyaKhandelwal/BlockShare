"""Storage controller: the bridge between the coordinator and the data plane.

Responsibilities
* split uploads into fixed-size chunks and hash them (SHA-256 = content address)
* choose replica nodes with the consistent-hashing ring
* push every chunk through a replication pipeline (client -> n1 -> n2 ...)
* fetch chunks in parallel for downloads, verify them, fail over to other
  replicas and repair corrupted replicas on the fly ("read repair")
* anti-entropy repair / garbage collection for the whole cluster
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction

from core.models import SystemConfig
from storage_node.client import NodeClient, NodeError

from .models import ChunkLocation, StorageNode
from .ring import HashRing

log = logging.getLogger(__name__)


class StorageError(Exception):
    pass


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_chunks(fileobj, chunk_size: int):
    """Yield successive chunks; an empty file still yields one (empty) chunk."""
    first = True
    while True:
        data = fileobj.read(chunk_size)
        if not data:
            if first:
                yield b""
            return
        first = False
        yield data


@dataclass
class ChunkResult:
    index: int
    sha256: str
    size: int
    nodes: list[str]
    deduplicated: bool = False


@dataclass
class UploadResult:
    chunks: list[ChunkResult]
    size: int
    deduplicated: int
    under_replicated: int
    exchanges: list[dict] = field(default_factory=list)


class TransferLog:
    """Thread-safe list of network exchanges (shown as the frame log of a transfer)."""

    def __init__(self):
        self.items: list[dict] = []
        self._lock = threading.Lock()

    def add(self, node_id: str, client: NodeClient | None, **extra):
        entry = {"node": node_id}
        if client is not None and client.last is not None:
            entry.update(client.last.as_dict())
        entry.update(extra)
        with self._lock:
            self.items.append(entry)


class StorageController:
    def __init__(self, replication_factor: int | None = None):
        config = SystemConfig.get()
        self.r = max(1, replication_factor or config.replication_factor)
        self.chunk_size = config.chunk_size
        self.nodes: dict[str, StorageNode] = {n.node_id: n for n in StorageNode.objects.all()}
        self.log = TransferLog()

    # ------------------------------------------------------------ helpers
    def client(self, node: StorageNode) -> NodeClient:
        return NodeClient(node.endpoint, settings.CLUSTER_KEY, settings.NODE_TIMEOUT_S)

    def writable_nodes(self) -> list[StorageNode]:
        return [n for n in self.nodes.values() if n.is_active and n.status != StorageNode.STATUS_DOWN]

    def ring(self) -> HashRing:
        return HashRing([n.node_id for n in self.writable_nodes()])

    def placement(self, key_hex: str) -> list[StorageNode]:
        return [self.nodes[i] for i in self.ring().preference_list(key_hex)]

    def locations(self, hashes: list[str]) -> dict[str, list[StorageNode]]:
        """Replica locations for many blobs, best candidates first (up, then lowest RTT)."""
        out: dict[str, list[StorageNode]] = {h: [] for h in hashes}
        rows = ChunkLocation.objects.filter(sha256__in=set(hashes)).values_list("sha256", "node__node_id")
        for sha, node_id in rows:
            node = self.nodes.get(node_id)
            if node:
                out[sha].append(node)

        def rank(n: StorageNode):
            return (0 if n.status == StorageNode.STATUS_UP else 1 if n.status == StorageNode.STATUS_UNKNOWN else 2,
                    n.last_rtt_ms if n.last_rtt_ms is not None else 1e9)

        for sha in out:
            out[sha].sort(key=rank)
        return out

    def _record_locations(self, results: list[tuple[str, int, set[str]]]):
        with transaction.atomic():
            for sha, size, node_ids in results:
                for node_id in node_ids:
                    node = self.nodes.get(node_id)
                    if node:
                        ChunkLocation.objects.get_or_create(sha256=sha, node=node, defaults={"size": size})

    # ------------------------------------------------------------ writing
    def _replicate(self, data: bytes, key_hex: str, already: set[str], candidates: list[StorageNode],
                   index: int | None = None) -> set[str]:
        """Store `data` on enough nodes to reach R replicas using a chained PUT pipeline."""
        need = self.r - len(already)
        pool = [n for n in candidates if n.node_id not in already]
        stored: set[str] = set()
        key = bytes.fromhex(key_hex)
        while need > 0 and pool:
            batch, pool = pool[:need], pool[need:]
            head, rest = batch[0], batch[1:]
            client = self.client(head)
            try:
                res = client.put(data, forward_to=[n.endpoint for n in rest], key=key)
                got = set(res.get("stored_on", [])) & {n.node_id for n in batch}
                self.log.add(head.node_id, client, chunk=index, pipeline=[n.node_id for n in batch],
                             stored_on=sorted(got))
            except NodeError as exc:
                self.log.add(head.node_id, client, chunk=index, error=str(exc))
                got = set()
                pool = rest + pool  # head is unreachable - retry the others with a new head
            stored |= got
            need = self.r - len(already) - len(stored)
        return stored

    def upload(self, fileobj, chunk_size: int | None = None) -> UploadResult:
        chunk_size = chunk_size or self.chunk_size
        if not self.writable_nodes():
            raise StorageError("No storage nodes are available")
        workers = max(1, settings.TRANSFER_WORKERS)
        results: dict[int, ChunkResult] = {}
        pending: dict = {}
        total = dedup = 0

        def drain(block_until: int):
            while len(pending) > block_until:
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in done:
                    idx, sha, size, already = pending.pop(fut)
                    stored = fut.result() | already
                    if not stored:
                        raise StorageError(f"Chunk #{idx} could not be stored on any node")
                    results[idx] = ChunkResult(idx, sha, size, sorted(stored))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, data in enumerate(read_chunks(fileobj, chunk_size)):
                sha = sha256_hex(data)
                total += len(data)
                existing = self.locations([sha])[sha]
                already = {n.node_id for n in existing if n.status != StorageNode.STATUS_DOWN}
                if len(already) >= self.r:  # content-addressed deduplication
                    dedup += 1
                    results[index] = ChunkResult(index, sha, len(data), sorted(already), deduplicated=True)
                    self.log.add(sorted(already)[0], None, op="DEDUP", chunk=index, key=sha[:16],
                                 bytes_sent=0, status="SKIPPED")
                    continue
                fut = pool.submit(self._replicate, data, sha, already, self.placement(sha), index)
                pending[fut] = (index, sha, len(data), already)
                drain(workers * 2)  # bound memory: at most 2*workers chunks in flight
            drain(0)

        chunks = [results[i] for i in sorted(results)]
        self._record_locations([(c.sha256, c.size, set(c.nodes)) for c in chunks if not c.deduplicated])
        under = sum(1 for c in chunks if len(c.nodes) < self.r)
        return UploadResult(chunks, total, dedup, under, self.log.items)

    def store_blob(self, data: bytes) -> tuple[str, list[str]]:
        sha = sha256_hex(data)
        already = {n.node_id for n in self.locations([sha])[sha] if n.status != StorageNode.STATUS_DOWN}
        stored = self._replicate(data, sha, already, self.placement(sha)) | already
        if not stored:
            raise StorageError("Manifest could not be stored on any node")
        self._record_locations([(sha, len(data), stored)])
        return sha, sorted(stored)

    # ------------------------------------------------------------ reading
    def fetch(self, sha: str, candidates: list[StorageNode], index: int | None = None) -> tuple[bytes, str]:
        """Fetch a blob from the first healthy replica, verifying its hash; repair bad replicas."""
        key = bytes.fromhex(sha)
        bad: list[StorageNode] = []
        for node in candidates:
            client = self.client(node)
            try:
                data = client.get(key)
            except NodeError as exc:
                self.log.add(node.node_id, client, chunk=index, error=str(exc))
                continue
            if data is None:
                self.log.add(node.node_id, client, chunk=index, error="replica missing")
                bad.append(node)
                continue
            if sha256_hex(data) != sha:
                self.log.add(node.node_id, client, chunk=index, error="hash mismatch - corrupted replica")
                bad.append(node)
                continue
            self.log.add(node.node_id, client, chunk=index)
            for b in bad:  # read repair
                c = self.client(b)
                try:
                    c.delete(key)
                    c.put(data, key=key)
                    self.log.add(b.node_id, c, chunk=index, op="READ_REPAIR")
                except NodeError:
                    pass
            return data, node.node_id
        raise StorageError(f"Chunk {sha[:12]}... is unavailable on every replica")

    def iter_chunks(self, chunks: list[tuple[int, str]], window: int | None = None):
        """Yield chunk bytes in order while prefetching `window` chunks in parallel."""
        locs = self.locations([sha for _, sha in chunks])
        window = window or max(1, settings.TRANSFER_WORKERS)
        queue = deque(chunks)
        inflight = deque()
        with ThreadPoolExecutor(max_workers=window) as pool:
            while queue or inflight:
                while queue and len(inflight) < window:
                    idx, sha = queue.popleft()
                    inflight.append(pool.submit(self.fetch, sha, locs[sha], idx))
                data, _ = inflight.popleft().result()
                yield data

    # ------------------------------------------------------------ integrity
    def deep_verify(self, chunks: list[tuple[int, str]]) -> list[dict]:
        """Check every replica of every chunk. Returns one row per chunk."""
        locs = self.locations([sha for _, sha in chunks])

        def check(idx, sha):
            row = {"index": idx, "expected": sha, "replicas": []}
            for node in locs[sha]:
                client = self.client(node)
                try:
                    data = client.get(bytes.fromhex(sha))
                    if data is None:
                        state, actual = "missing", None
                    else:
                        actual = sha256_hex(data)
                        state = "ok" if actual == sha else "corrupted"
                except NodeError:
                    state, actual = "unreachable", None
                row["replicas"].append({"node": node.node_id, "state": state, "actual": actual})
                self.log.add(node.node_id, client, chunk=idx, op="VERIFY", result=state)
            return row

        with ThreadPoolExecutor(max_workers=max(1, settings.TRANSFER_WORKERS)) as pool:
            rows = list(pool.map(lambda c: check(*c), chunks))
        return rows

    def corrupt_replica(self, sha: str, node_id: str) -> bool:
        node = self.nodes[node_id]
        return self.client(node).corrupt(bytes.fromhex(sha))

    # ------------------------------------------------------------ maintenance
    def referenced_blobs(self) -> dict[str, int]:
        from files.models import Chunk, FileVersion

        refs = dict(Chunk.objects.filter(version__file__deleted=False).values_list("sha256", "size"))
        for manifest in FileVersion.objects.filter(file__deleted=False).values_list("manifest_hash", flat=True):
            refs.setdefault(manifest, 0)
        return refs

    def repair(self) -> dict:
        """Anti-entropy: bring every referenced blob back to R healthy replicas."""
        start = time.perf_counter()
        refs = self.referenced_blobs()
        locs = self.locations(list(refs))
        report = {"checked": len(refs), "repaired": 0, "replicas_added": 0, "lost": [], "bytes": 0}
        new_locations = []
        for sha, holders in locs.items():
            healthy, data = [], None
            for node in holders:
                if node.status == StorageNode.STATUS_DOWN:
                    continue
                client = self.client(node)
                try:
                    if client.has(bytes.fromhex(sha)):
                        healthy.append(node)
                    else:
                        ChunkLocation.objects.filter(sha256=sha, node=node).delete()
                except NodeError:
                    continue
            if len(healthy) >= self.r:
                continue
            try:
                data, _ = self.fetch(sha, healthy)
            except StorageError:
                report["lost"].append(sha)
                continue
            stored = self._replicate(data, sha, {n.node_id for n in healthy}, self.placement(sha))
            if stored:
                report["repaired"] += 1
                report["replicas_added"] += len(stored)
                report["bytes"] += len(data) * len(stored)
                new_locations.append((sha, len(data), stored))
        self._record_locations(new_locations)
        report["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
        return report

    def garbage_collect(self) -> dict:
        refs = self.referenced_blobs()
        orphans = ChunkLocation.objects.exclude(sha256__in=list(refs)).select_related("node")
        removed = 0
        for loc in orphans:
            try:
                self.client(loc.node).delete(bytes.fromhex(loc.sha256))
            except NodeError:
                continue
            loc.delete()
            removed += 1
        return {"removed_replicas": removed}
