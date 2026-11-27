"""Check HEAD on the wire: HTTP clients may otherwise hide an erroneous body."""
import socket
import unittest
from unittest.mock import patch

from .cluster import Cluster, free_port


class HttpHeadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = Cluster(1, http_port=free_port()).start()
        cls.node = cls.cluster.nodes[0]

    @classmethod
    def tearDownClass(cls):
        cls.cluster.stop()

    def request(self, method, path):
        with socket.create_connection(("127.0.0.1", self.node.http_port), timeout=5) as sock:
            sock.sendall(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            parts = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                parts.append(data)
        header, body = b"".join(parts).split(b"\r\n\r\n", 1)
        lines = header.decode().split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:])
        return int(lines[0].split()[1]), headers, body

    def assert_matches_get(self, path, status):
        get_status, get_headers, get_body = self.request("GET", path)
        head_status, head_headers, head_body = self.request("HEAD", path)
        self.assertEqual((get_status, head_status), (status, status))
        self.assertEqual(head_body, b"")
        self.assertEqual(head_headers, get_headers)
        self.assertEqual(int(head_headers["Content-Length"]), len(get_body))
        self.assertGreater(len(get_body), 0)
        self.assertEqual(head_headers["Connection"], "close")

    def test_healthy_health_routes_and_query_string(self):
        for path in ("/", "/health", "/health?probe=1"):
            with self.subTest(path=path):
                self.assert_matches_get(path, 200)

    def test_unhealthy_health_routes_keep_503_without_body(self):
        self.node.offline = True
        try:
            for path in ("/", "/health"):
                self.assert_matches_get(path, 503)
        finally:
            self.node.offline = False

    def test_stats_and_metrics(self):
        # Freeze the dynamic stats representation, not HTTP handling.
        with patch.object(self.node, "stats", return_value=self.node.stats()):
            self.assert_matches_get("/stats", 200)
        self.assert_matches_get("/metrics", 200)

    def test_unknown_path_keeps_404_without_body(self):
        self.assert_matches_get("/missing", 404)

    def test_other_methods_keep_405_and_their_existing_body(self):
        for method in ("POST", "PUT", "DELETE"):
            status, headers, body = self.request(method, "/health")
            self.assertEqual(status, 405)
            self.assertEqual(body, b"method not allowed\n")
            self.assertEqual(int(headers["Content-Length"]), len(body))
