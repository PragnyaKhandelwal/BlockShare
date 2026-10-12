"""Failure detection for the storage cluster.

The monitor probes every node with a BSP PING (measuring the TCP round-trip
time) followed by STATS (chunk count, bytes, the node's own UDP gossip view of
its peers). A node that fails `FAILURE_THRESHOLD` consecutive probes is marked
DOWN; once it has been down for `REPAIR_GRACE_S` the controller re-replicates
the chunks it held onto the surviving nodes.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.models import SystemConfig
from storage_node.client import NodeClient, NodeError

from .models import NodeHealthSample, StorageNode

log = logging.getLogger(__name__)

FAILURE_THRESHOLD = 2
REPAIR_GRACE_S = 15
SAMPLES_KEPT = 120
_failures: dict[str, int] = {}


def probe(node: StorageNode) -> dict:
    client = NodeClient(node.endpoint, settings.CLUSTER_KEY, timeout=3)
    try:
        rtt = client.ping()
        stats = client.stats()
        return {"ok": True, "rtt": rtt, "stats": stats}
    except NodeError as exc:
        return {"ok": False, "error": str(exc)}


def check_nodes() -> list[StorageNode]:
    nodes = list(StorageNode.objects.all())
    if not nodes:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
        results = list(pool.map(probe, nodes))
    now = timezone.now()
    for node, res in zip(nodes, results):
        NodeHealthSample.objects.create(node=node, ok=res["ok"], rtt_ms=res.get("rtt"))
        if res["ok"]:
            _failures[node.node_id] = 0
            stats = res["stats"]
            if node.status != StorageNode.STATUS_UP:
                log.info("node %s is UP (rtt %.2f ms)", node.node_id, res["rtt"])
            node.status = StorageNode.STATUS_UP
            node.down_since = None
            node.last_seen = now
            node.last_rtt_ms = res["rtt"]
            node.chunk_count = stats.get("chunks", 0)
            node.bytes_used = stats.get("bytes", 0)
            node.peers = stats.get("peers", [])
            node.node_metrics = stats.get("metrics", {})
            node.latency_ms = stats.get("latency_ms", 0)
            node.simulated_failure = stats.get("offline", False)
        else:
            _failures[node.node_id] = _failures.get(node.node_id, 0) + 1
            node.last_rtt_ms = None
            if "UNAVAILABLE" in res.get("error", ""):
                node.simulated_failure = True
            if _failures[node.node_id] >= FAILURE_THRESHOLD or node.status == StorageNode.STATUS_UNKNOWN:
                if node.status != StorageNode.STATUS_DOWN:
                    log.warning("node %s is DOWN: %s", node.node_id, res.get("error"))
                    node.down_since = now
                node.status = StorageNode.STATUS_DOWN
        node.save()
        stale = node.samples.values_list("id", flat=True)[SAMPLES_KEPT:]
        NodeHealthSample.objects.filter(id__in=list(stale)).delete()
    return nodes


def needs_repair(nodes: list[StorageNode]) -> bool:
    cutoff = timezone.now() - timedelta(seconds=REPAIR_GRACE_S)
    return any(n.status == StorageNode.STATUS_DOWN and n.down_since and n.down_since < cutoff
               and n.replicas.exists() for n in nodes)


def run_forever(interval: float = 5.0):
    from .controller import StorageController

    last_repair = 0.0
    last_chain_check = 0.0
    while True:
        try:
            nodes = check_nodes()
            config = SystemConfig.get()
            if config.auto_repair and needs_repair(nodes) and time.time() - last_repair > 30:
                report = StorageController().repair()
                last_repair = time.time()
                log.info("auto-repair: %s", report)
            if config.blockchain_enabled and time.time() - last_chain_check > 30:
                from coordination.blockchain import ensure_chain_ready

                ensure_chain_ready()
                last_chain_check = time.time()
        except Exception:  # keep monitoring no matter what
            log.exception("monitor iteration failed")
        time.sleep(interval)
