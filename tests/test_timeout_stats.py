"""Effective read-timeout configuration in the existing statistics responses."""
import json
import os
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

from storage_node.client import NodeClient
from storage_node.server import StorageNode
from .cluster import CLUSTER_KEY, Cluster, free_port


class TimeoutStatsTests(unittest.TestCase):
    def test_default_values_and_existing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = StorageNode("defaults", tmp)
            stats = node.stats()
        self.assertEqual(stats["read_timeouts"], {
            "idle_seconds": 30, "header_seconds": 10, "payload_idle_seconds": 30,
        })
        self.assertTrue({"node_id", "tcp", "udp", "chunks", "bytes", "uptime_s",
                         "offline", "latency_ms", "metrics", "peers"} <= stats.keys())

    def test_stats_use_instance_configuration_without_mutating_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            node = StorageNode("custom", tmp, idle_timeout=45, header_timeout=15,
                               payload_idle_timeout=120)
            node.metrics["connections"] = 7
            before = dict(node.metrics)
            with patch.dict(os.environ, {"NODE_IDLE_TIMEOUT_S": "999"}):
                self.assertEqual(node.stats()["read_timeouts"], {
                    "idle_seconds": 45, "header_seconds": 15, "payload_idle_seconds": 120,
                })
            self.assertEqual(dict(node.metrics), before)

    def test_http_and_bsp_report_custom_values_and_health_is_unchanged(self):
        cluster = Cluster(1, http_port=free_port(), idle_timeout=12.5,
                          header_timeout=3.75, payload_idle_timeout=42.5).start()
        try:
            node = cluster.nodes[0]
            expected = {"idle_seconds": 12.5, "header_seconds": 3.75, "payload_idle_seconds": 42.5}
            bsp = NodeClient(node.tcp_endpoint, CLUSTER_KEY).stats()
            base = f"http://127.0.0.1:{node.http_port}"
            with urllib.request.urlopen(base + "/stats", timeout=5) as response:
                http = json.load(response)
            self.assertEqual(bsp["read_timeouts"], expected)
            self.assertEqual(http["read_timeouts"], expected)
            self.assertEqual(http["node_id"], bsp["node_id"])
            with urllib.request.urlopen(base + "/health", timeout=5) as response:
                self.assertEqual(json.load(response), {"node_id": "node-a", "healthy": True})
        finally:
            cluster.stop()
