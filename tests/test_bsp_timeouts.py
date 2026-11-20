"""Real local socket tests for stalled BSP reads; no cloud services needed."""
import json
import select
import socket
import time
import unittest
import urllib.request

from storage_node.client import NodeClient
from storage_node.protocol import Frame, Op, Status, encode, recv_frame, sha256
from storage_node.server import StorageNode

from .cluster import CLUSTER_KEY, Cluster, free_port


class BspTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = Cluster(1, http_port=free_port(), idle_timeout=1.0,
                              header_timeout=1.0, payload_idle_timeout=1.0).start()
        cls.node = cls.cluster.nodes[0]

    @classmethod
    def tearDownClass(cls):
        cls.cluster.stop()

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.node.port), timeout=5)

    def client(self):
        return NodeClient(self.node.tcp_endpoint, CLUSTER_KEY, timeout=5)

    def counter(self, phase):
        return self.client().stats()["metrics"].get(f"bsp_{phase}_timeouts", 0)

    def test_silent_connection_expires_and_node_recovers(self):
        before = self.counter("idle")
        with self.connect() as sock:
            self.assertEqual(sock.recv(1), b"")
        self.assertEqual(self.counter("idle"), before + 1)
        self.assertGreaterEqual(self.client().ping(), 0)

    def test_incomplete_magic_and_header_expire_without_dispatch(self):
        raw = encode(Frame(opcode=Op.PING, request_id=7))
        for partial in (b"B", b"BSP1", raw[:20]):
            with self.subTest(partial=partial):
                before = self.counter("header")
                ping_count = self.client().stats()["metrics"].get("op_ping", 0)
                with self.connect() as sock:
                    sock.sendall(partial)
                    self.assertEqual(sock.recv(1), b"")
                self.assertEqual(self.counter("header"), before + 1)
                self.assertEqual(self.client().stats()["metrics"].get("op_ping", 0), ping_count)

    def test_trickling_header_cannot_reset_its_deadline(self):
        before = self.counter("header")
        with self.connect() as sock:
            sock.sendall(b"BSP1")
            deadline = time.monotonic() + 4
            closed = False
            # Keep making progress, but never supply the full 72-byte header.
            while time.monotonic() < deadline:
                readable, _, _ = select.select([sock], [], [], 0.15)
                if readable:
                    self.assertEqual(sock.recv(1), b"")
                    closed = True
                    break
                try:
                    sock.sendall(b"\0")
                except (BrokenPipeError, ConnectionResetError):
                    closed = True
                    break
            self.assertTrue(closed, "header deadline was extended by trickled bytes")
        self.assertEqual(self.counter("header"), before + 1)

    def test_incomplete_payload_expires_without_storing_partial_data(self):
        data = b"partial payload test"
        key = sha256(data)
        raw = encode(Frame(opcode=Op.PUT, key=key, payload=data), CLUSTER_KEY)
        before = self.counter("payload")
        puts = self.client().stats()["metrics"].get("op_put", 0)
        with self.connect() as sock:
            sock.sendall(raw[:75])  # complete header, only three payload bytes
            self.assertEqual(sock.recv(1), b"")
        self.assertEqual(self.counter("payload"), before + 1)
        self.assertEqual(self.client().stats()["metrics"].get("op_put", 0), puts)
        self.assertFalse(self.client().has(key))

    def test_payload_progress_can_exceed_the_total_idle_interval(self):
        data = b"slow valid payload" * 4
        key = sha256(data)
        raw = encode(Frame(opcode=Op.PUT, key=key, payload=data, request_id=42), CLUSTER_KEY)
        before = self.counter("payload")
        with self.connect() as sock:
            sock.sendall(raw[:72])
            # Total duration exceeds one second, but each gap is below one second.
            for offset in range(0, len(data), 10):
                time.sleep(0.2)
                sock.sendall(data[offset:offset + 10])
            response = recv_frame(sock)
            self.assertEqual(response.status, Status.OK)
            self.assertEqual(response.request_id, 42)
        self.assertEqual(self.counter("payload"), before)
        self.assertEqual(self.client().get(key), data)

    def test_fragmented_header_and_multiple_frames_on_one_connection(self):
        first = encode(Frame(opcode=Op.PING, request_id=1))
        second = encode(Frame(opcode=Op.PING, request_id=2))
        with self.connect() as sock:
            sock.sendall(first[:2])
            time.sleep(0.1)
            sock.sendall(first[2:30])
            time.sleep(0.1)
            sock.sendall(first[30:] + second)
            for request_id in (1, 2):
                response = recv_frame(sock)
                self.assertEqual(response.status, Status.OK)
                self.assertEqual(response.request_id, request_id)
            before = self.counter("idle")
            self.assertEqual(sock.recv(1), b"")
            self.assertEqual(self.counter("idle"), before + 1)

    def test_clean_disconnect_is_not_counted_as_timeout(self):
        before = {phase: self.counter(phase) for phase in ("idle", "header", "payload")}
        with self.connect() as sock:
            sock.sendall(b"BSP1")
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(sock.recv(1), b"")
        self.assertEqual({phase: self.counter(phase) for phase in before}, before)

    def test_request_processing_is_not_subject_to_read_timeouts(self):
        self.node.latency_ms = 1200
        try:
            self.assertGreaterEqual(self.client().ping(), 0)
        finally:
            self.node.latency_ms = 0

    def test_timeout_counters_are_exposed_without_changing_health(self):
        for phase, data in (("idle", b""), ("header", b"BSP1"),
                            ("payload", encode(Frame(opcode=Op.PING, payload=b"abc"))[:73])):
            with self.subTest(phase=phase):
                before = self.counter(phase)
                with self.connect() as sock:
                    if data:
                        sock.sendall(data)
                    self.assertEqual(sock.recv(1), b"")
                base = f"http://127.0.0.1:{self.node.http_port}"
                with urllib.request.urlopen(base + "/stats", timeout=5) as response:
                    self.assertEqual(json.load(response)["metrics"][f"bsp_{phase}_timeouts"], before + 1)
                with urllib.request.urlopen(base + "/metrics", timeout=5) as response:
                    self.assertIn(f'blockshare_node_bsp_{phase}_timeouts{{node="node-a"}} {before + 1}',
                                  response.read().decode())
        with urllib.request.urlopen(base + "/health", timeout=5) as response:
            self.assertEqual(json.load(response), {"node_id": "node-a", "healthy": True})

    def test_invalid_timeout_configuration_is_rejected(self):
        for name in ("idle_timeout", "header_timeout", "payload_idle_timeout"):
            for value in (0, -1, float("inf"), float("nan")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    StorageNode("invalid", self.cluster.tmp.name, **{name: value})
