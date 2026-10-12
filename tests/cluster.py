"""Run a real storage-node cluster inside the test process (own asyncio loop in a thread)."""
import asyncio
import socket
import tempfile
import threading

from storage_node.server import StorageNode

CLUSTER_KEY = b"test-cluster-key"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    def __init__(self, n=3):
        self.tmp = tempfile.TemporaryDirectory()
        self.loop = asyncio.new_event_loop()
        ports = [(free_port(), free_port()) for _ in range(n)]
        udp_eps = [f"127.0.0.1:{u}" for _, u in ports]
        self.nodes = [
            StorageNode(f"node-{chr(97 + i)}", f"{self.tmp.name}/n{i}", host="127.0.0.1", port=tcp, udp_port=udp,
                        advertise_host="127.0.0.1", peers=[e for e in udp_eps if e != udp_eps[i]],
                        cluster_key=CLUSTER_KEY, heartbeat_interval=0.2)
            for i, (tcp, udp) in enumerate(ports)
        ]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.ready = threading.Event()

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def boot():
            for n in self.nodes:
                await n.start()
            self.ready.set()

        self.loop.run_until_complete(boot())
        self.loop.run_forever()

    def start(self):
        self.thread.start()
        self.ready.wait(10)
        return self

    def stop(self):
        async def shutdown():
            for n in self.nodes:
                await n.stop()

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.tmp.cleanup()
