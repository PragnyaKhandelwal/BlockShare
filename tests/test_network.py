"""Computer-networks layer: BSP/1 framing, authentication, replication pipeline, gossip, HTTP."""
import json
import time
import unittest
import urllib.request

from storage_node.client import NodeClient, NodeError
from storage_node.protocol import (
    FLAG_FORWARD, HEADER_SIZE, Frame, Op, ProtocolError, Status, check_payload, decode_header, encode,
    pack_forward, sha256, unpack_forward, verify_auth,
)
from storage.ring import HashRing

from .cluster import CLUSTER_KEY, Cluster


class ProtocolTests(unittest.TestCase):
    def test_header_is_72_bytes_and_roundtrips(self):
        data = b"hello"
        raw = encode(Frame(opcode=Op.PUT, key=sha256(data), payload=data, request_id=7), CLUSTER_KEY)
        self.assertEqual(HEADER_SIZE, 72)
        self.assertEqual(len(raw), 72 + len(data))
        frame = decode_header(raw[:HEADER_SIZE])
        frame.payload = raw[HEADER_SIZE:]
        self.assertEqual(frame.op, Op.PUT)
        self.assertEqual(frame.request_id, 7)
        self.assertTrue(check_payload(frame))
        self.assertTrue(verify_auth(frame, CLUSTER_KEY))
        self.assertFalse(verify_auth(frame, b"other-key"))

    def test_crc_detects_corrupted_payload(self):
        raw = bytearray(encode(Frame(opcode=Op.PUT, key=sha256(b"abc"), payload=b"abc")))
        raw[-1] ^= 0xFF
        frame = decode_header(bytes(raw[:HEADER_SIZE]))
        frame.payload = bytes(raw[HEADER_SIZE:])
        self.assertFalse(check_payload(frame))

    def test_bad_magic_rejected(self):
        raw = b"XXXX" + encode(Frame(opcode=Op.PING))[4:HEADER_SIZE]
        with self.assertRaises(ProtocolError):
            decode_header(raw)

    def test_forward_payload(self):
        targets, data = unpack_forward(pack_forward(["10.0.0.2:9000", "10.0.0.3:9000"], b"chunk"))
        self.assertEqual(targets, ["10.0.0.2:9000", "10.0.0.3:9000"])
        self.assertEqual(data, b"chunk")


class RingTests(unittest.TestCase):
    def test_preference_list_is_distinct_and_stable(self):
        ring = HashRing(["node-a", "node-b", "node-c"])
        pl = ring.preference_list("ab" * 32, 2)
        self.assertEqual(len(set(pl)), 2)
        self.assertEqual(pl, HashRing(["node-c", "node-a", "node-b"]).preference_list("ab" * 32, 2))

    def test_adding_a_node_moves_only_a_fraction_of_keys(self):
        keys = [sha256(str(i).encode()).hex() for i in range(2000)]
        before = HashRing(["node-a", "node-b", "node-c"])
        after = HashRing(["node-a", "node-b", "node-c", "node-d"])
        moved = sum(before.preference_list(k, 1) != after.preference_list(k, 1) for k in keys)
        self.assertLess(moved / len(keys), 0.40)  # ~25% expected, hash % N would move ~75%
        self.assertAlmostEqual(sum(after.ownership().values()), 100, delta=0.5)


class ClusterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = Cluster(3).start()
        cls.eps = [n.tcp_endpoint for n in cls.cluster.nodes]

    @classmethod
    def tearDownClass(cls):
        cls.cluster.stop()

    def client(self, i, key=CLUSTER_KEY):
        return NodeClient(self.eps[i], key, timeout=5)

    def test_ping_and_stats(self):
        self.assertGreater(self.client(0).ping(), 0)
        self.assertEqual(self.client(0).stats()["node_id"], "node-a")

    def test_replication_pipeline_stores_on_every_hop(self):
        data = b"pipeline" * 5000
        res = self.client(0).put(data, forward_to=self.eps[1:])
        self.assertEqual(sorted(res["stored_on"]), ["node-a", "node-b", "node-c"])
        for i in range(3):
            self.assertEqual(self.client(i).get(sha256(data)), data)

    def test_content_address_is_enforced(self):
        resp = self.client(0).request(Op.PUT, sha256(b"something else"), b"payload")
        self.assertEqual(resp.status, Status.HASH_MISMATCH)

    def test_mutations_require_cluster_key(self):
        with self.assertRaises(NodeError) as ctx:
            self.client(0, key=b"intruder").put(b"x")
        self.assertEqual(ctx.exception.status, Status.UNAUTHORIZED)

    def test_corrupt_and_delete(self):
        data = b"tamper me" * 100
        c = self.client(1)
        c.put(data)
        self.assertTrue(c.corrupt(sha256(data)))
        self.assertNotEqual(sha256(c.get(sha256(data))), sha256(data))
        self.assertTrue(c.delete(sha256(data)))
        self.assertIsNone(c.get(sha256(data)))

    def test_simulated_failure(self):
        c = self.client(2)
        c.admin(offline=True)
        with self.assertRaises(NodeError) as ctx:
            c.ping()
        self.assertEqual(ctx.exception.status, Status.UNAVAILABLE)
        c.admin(offline=False)
        self.assertGreater(c.ping(), 0)

    def test_udp_gossip_discovers_peers(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            peers = self.client(0).stats()["peers"]
            if len(peers) == 2 and all(p["status"] == "alive" for p in peers):
                break
            time.sleep(0.2)
        self.assertEqual(sorted(p["node_id"] for p in peers), ["node-b", "node-c"])

    def test_http_probe_on_bsp_port_is_answered_and_ignored(self):
        """Load balancers / port scanners send HTTP to every open port; the node must cope."""
        import socket

        host, port = self.eps[0].rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=5) as s:
            s.sendall(b"HEAD / HTTP/1.1\r\nHost: probe\r\nUser-Agent: Render/1.0\r\n\r\n")
            reply = s.recv(1024)
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"))
        self.assertIn(b"BSP/1", reply)
        self.assertGreaterEqual(self.client(0).stats()["metrics"].get("foreign_connections", 0), 1)
        self.assertGreater(self.client(0).ping(), 0)  # still serving BSP

    def test_node_counts_operations(self):
        self.client(0).ping()
        metrics = self.client(0).stats()["metrics"]
        self.assertGreaterEqual(metrics.get("op_ping", 0), 1)
        self.assertGreaterEqual(metrics.get("connections", 0), 1)


class HttpEndpointTests(unittest.TestCase):
    def test_minimal_http_server(self):
        import asyncio
        import tempfile
        import threading

        from storage_node.server import StorageNode

        from .cluster import free_port

        port = free_port()
        loop = asyncio.new_event_loop()
        with tempfile.TemporaryDirectory() as tmp:
            node = StorageNode("node-h", tmp, host="127.0.0.1", port=free_port(), udp_port=free_port(),
                               http_port=port, advertise_host="127.0.0.1")
            ready = threading.Event()

            def run():
                asyncio.set_event_loop(loop)
                loop.run_until_complete(node.start())
                ready.set()
                loop.run_forever()

            t = threading.Thread(target=run, daemon=True)
            t.start()
            ready.wait(5)
            try:
                body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).read())
                self.assertEqual(body, {"node_id": "node-h", "healthy": True})
                metrics = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
                self.assertIn("blockshare_node_chunks", metrics)
            finally:
                asyncio.run_coroutine_threadsafe(node.stop(), loop).result(5)
                loop.call_soon_threadsafe(loop.stop)
                t.join(5)


if __name__ == "__main__":
    unittest.main()
