"""Run one storage node, or a small cluster of nodes in one process.

    python -m storage_node --node-id node-a --port 9101 --peers 127.0.0.1:10102,127.0.0.1:10103
    python -m storage_node --cluster 3 --port 9101          # node-a..node-c on 9101..9103

Every option can also be given through environment variables (NODE_ID,
NODE_HOST, NODE_PORT, NODE_UDP_PORT, NODE_HTTP_PORT, NODE_ADVERTISE_HOST,
NODE_PEERS, NODE_DATA_DIR, CLUSTER_KEY, NODE_LATENCY_MS).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import string

from .server import StorageNode


def _env(name, default=None):
    return os.environ.get(name, default)


def main():
    p = argparse.ArgumentParser(description="BlockShare storage node (BSP/1 over TCP + UDP gossip)")
    p.add_argument("--node-id", default=_env("NODE_ID", "node-a"))
    p.add_argument("--host", default=_env("NODE_HOST", "0.0.0.0"), help="bind address")
    p.add_argument("--port", type=int, default=int(_env("NODE_PORT", "9000")), help="BSP/TCP port")
    p.add_argument("--udp-port", type=int, default=int(_env("NODE_UDP_PORT", "0")) or None,
                   help="heartbeat UDP port (default: port + 1000)")
    p.add_argument("--http-port", type=int, default=int(_env("NODE_HTTP_PORT", "0")) or None,
                   help="HTTP status port (default: disabled)")
    p.add_argument("--advertise-host", default=_env("NODE_ADVERTISE_HOST"),
                   help="host name other machines use to reach this node")
    p.add_argument("--peers", default=_env("NODE_PEERS", ""), help="comma separated UDP endpoints of peers")
    p.add_argument("--data-dir", default=_env("NODE_DATA_DIR", "var/nodes"))
    p.add_argument("--cluster-key", default=_env("CLUSTER_KEY", "blockshare-dev-cluster-key"))
    p.add_argument("--latency-ms", type=int, default=int(_env("NODE_LATENCY_MS", "0")),
                   help="artificial per-request delay (network emulation)")
    p.add_argument("--heartbeat", type=float, default=float(_env("NODE_HEARTBEAT_S", "2")))
    p.add_argument("--cluster", type=int, default=int(_env("NODE_CLUSTER", "0")),
                   help="run N nodes (node-a, node-b, ...) on consecutive ports in this process")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = args.cluster_key.encode() if args.cluster_key else None

    nodes: list[StorageNode] = []
    if args.cluster:
        host = args.advertise_host or ("127.0.0.1" if args.host in ("0.0.0.0", "") else args.host)
        udp_base = args.udp_port or args.port + 1000
        udp_eps = [f"{host}:{udp_base + i}" for i in range(args.cluster)]
        for i in range(args.cluster):
            node_id = f"node-{string.ascii_lowercase[i]}"
            nodes.append(StorageNode(
                node_id, os.path.join(args.data_dir, node_id), host=args.host, port=args.port + i,
                udp_port=udp_base + i, http_port=(args.http_port + i) if args.http_port else None,
                advertise_host=host, peers=[e for e in udp_eps if e != udp_eps[i]], cluster_key=key,
                heartbeat_interval=args.heartbeat, latency_ms=args.latency_ms,
            ))
    else:
        data_dir = args.data_dir if args.data_dir.rstrip("/\\").endswith(args.node_id) else os.path.join(args.data_dir, args.node_id)
        nodes.append(StorageNode(
            args.node_id, data_dir, host=args.host, port=args.port, udp_port=args.udp_port,
            http_port=args.http_port, advertise_host=args.advertise_host,
            peers=[x.strip() for x in args.peers.split(",") if x.strip()], cluster_key=key,
            heartbeat_interval=args.heartbeat, latency_ms=args.latency_ms,
        ))

    async def run():
        for n in nodes:
            await n.start()
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
