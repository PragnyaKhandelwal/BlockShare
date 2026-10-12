"""blockshare-cli - talk to the blockchain and the storage nodes directly, without Django.

Demonstrates the "advanced blockchain mode" property: if the Django
coordinator is down, file metadata, ownership and the storage-node directory
are still available on-chain, and the chunks are still on the nodes.

    python cli/blockshare_cli.py --api http://127.0.0.1:8000 config      # cache contract addresses
    python cli/blockshare_cli.py nodes                                      # NodeRegistry (on-chain)
    python cli/blockshare_cli.py file 0x<fileId>                            # FileRegistry + FileSharing
    python cli/blockshare_cli.py get 0x<fileId> -o out.bin                  # trustless download
    python cli/blockshare_cli.py verify local.bin 0x<fileId>                # compare with on-chain root
    python cli/blockshare_cli.py ping 127.0.0.1:9101                        # BSP/1 round trip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coordination.merkle import merkle_root  # noqa: E402
from storage_node.client import NodeClient, NodeError  # noqa: E402

CONFIG = Path.home() / ".blockshare-cli.json"
PERMS = {0: "NONE", 1: "READ", 2: "WRITE", 3: "OWNER"}


def load_abi(name):
    return json.loads((ROOT / "coordination" / "contracts" / f"{name}.json").read_text())["abi"]


def load_config(args) -> dict:
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if args.rpc:
        cfg["rpc"] = args.rpc
    cfg.setdefault("rpc", "http://127.0.0.1:8545")
    return cfg


def connect(cfg):
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(cfg["rpc"], request_kwargs={"timeout": 20}))
    c = cfg.get("contracts") or {}
    if not c.get("FileRegistry"):
        sys.exit("No contract addresses known. Run `config --api <django url>` once, or pass --rpc and use `config --set`.")
    return w3, {name: w3.eth.contract(address=Web3.to_checksum_address(addr), abi=load_abi(name))
                for name, addr in c.items() if addr}


def b32(hex_id: str) -> bytes:
    return bytes.fromhex(hex_id.removeprefix("0x").rjust(64, "0"))


def cmd_config(args):
    cfg = load_config(args)
    if args.api:
        with urllib.request.urlopen(args.api.rstrip("/") + "/api/chain/", timeout=10) as r:
            info = json.loads(r.read())
        cfg["contracts"] = info["contracts"]
        cfg["chain_id"] = info["chain_id"]
        if not args.rpc:
            cfg["rpc"] = info["rpc_proxy"]
    for item in args.set or []:
        k, _, v = item.partition("=")
        cfg.setdefault("contracts", {})[k] = v
    CONFIG.write_text(json.dumps(cfg, indent=2))
    print(json.dumps(cfg, indent=2))
    print(f"saved to {CONFIG}")


def cmd_nodes(args):
    _, c = connect(load_config(args))
    for n in c["NodeRegistry"].functions.getNodes().call():
        print(f"{n[0]:<10} {n[1]:<24} {'active' if n[2] else 'inactive'}")


def cmd_file(args):
    _, c = connect(load_config(args))
    fid = b32(args.file_id)
    owner, name, latest, created, deleted = c["FileRegistry"].functions.getFile(fid).call()
    print(f"name        {name}\nowner       {owner}\nversions    {latest}\ndeleted     {deleted}")
    for v in range(1, latest + 1):
        root, manifest, size, chunks, ts, author = c["FileRegistry"].functions.getVersion(fid, v).call()
        print(f"  v{v}: {size} bytes, {chunks} chunks, root {root.hex()[:16]}..., manifest {manifest.hex()[:16]}..., by {author}")
    users, perms = c["FileSharing"].functions.getGrantees(fid).call()
    for u, p in zip(users, perms):
        print(f"  grant {u}: {PERMS[p]}")


def _fetch_blob(endpoints: list[str], sha: str) -> bytes:
    for ep in endpoints:
        try:
            data = NodeClient(ep, timeout=10).get(bytes.fromhex(sha))
        except NodeError as exc:
            print(f"    {ep}: {exc}", file=sys.stderr)
            continue
        if data is not None and hashlib.sha256(data).hexdigest() == sha:
            return data
        print(f"    {ep}: missing or corrupted replica of {sha[:12]}", file=sys.stderr)
    raise SystemExit(f"blob {sha} not available on any node")


def cmd_get(args):
    _, c = connect(load_config(args))
    fid = b32(args.file_id)
    record = c["FileRegistry"].functions.getFile(fid).call()
    version = args.version or record[2]
    root, manifest_hash, size, chunks, _, _ = c["FileRegistry"].functions.getVersion(fid, version).call()
    nodes = [n[1] for n in c["NodeRegistry"].functions.getNodes().call() if n[2]]
    print(f"on-chain: {record[1]} v{version}, {size} bytes, {chunks} chunks; {len(nodes)} storage nodes in NodeRegistry")

    manifest = json.loads(_fetch_blob(nodes, manifest_hash.hex()))
    print(f"manifest {manifest_hash.hex()[:16]}... fetched and verified")
    out = bytearray()
    for ch in manifest["chunks"]:
        endpoints = ch.get("nodes", []) + [n for n in nodes if n not in ch.get("nodes", [])]
        out += _fetch_blob(endpoints, ch["sha256"])
        print(f"  chunk {ch['index']:>4} ok ({ch['size']} bytes)", end="\r")
    computed = merkle_root([ch["sha256"] for ch in manifest["chunks"]])
    if computed != root.hex():
        raise SystemExit(f"\nMerkle root mismatch! chain={root.hex()} computed={computed}")
    Path(args.output).write_bytes(out)
    print(f"\nMerkle root matches the blockchain ({computed[:16]}...). Wrote {len(out)} bytes to {args.output}")


def cmd_verify(args):
    _, c = connect(load_config(args))
    fid = b32(args.file_id)
    version = args.version or c["FileRegistry"].functions.getFile(fid).call()[2]
    data = Path(args.path).read_bytes()
    size = args.chunk_size * 1024
    leaves = [hashlib.sha256(data[i:i + size]).hexdigest() for i in range(0, max(len(data), 1), size)] or \
        [hashlib.sha256(b"").hexdigest()]
    root = merkle_root(leaves)
    ok = c["FileRegistry"].functions.verifyRoot(fid, version, b32(root)).call()
    print(f"local root {root}\nFileRegistry.verifyRoot(v{version}) -> {ok}")
    print("VALID - identical to the registered version" if ok else "MODIFIED - does not match the registered version")


def cmd_ping(args):
    client = NodeClient(args.endpoint, timeout=5)
    for i in range(args.count):
        try:
            print(f"BSP PING {args.endpoint}: rtt={client.ping()} ms")
        except NodeError as exc:
            print(exc)


def main():
    p = argparse.ArgumentParser(prog="blockshare-cli", description=__doc__.split("\n\n")[0])
    p.add_argument("--rpc", help="JSON-RPC URL (local node or https://<site>/rpc)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("config", help="fetch/cache contract addresses")
    s.add_argument("--api", help="BlockShare base URL, e.g. http://127.0.0.1:8000")
    s.add_argument("--set", nargs="*", help="Name=0xAddress overrides")
    s.set_defaults(fn=cmd_config)
    sub.add_parser("nodes", help="list storage nodes from NodeRegistry").set_defaults(fn=cmd_nodes)
    s = sub.add_parser("file", help="show on-chain state of a file")
    s.add_argument("file_id")
    s.set_defaults(fn=cmd_file)
    s = sub.add_parser("get", help="download a file straight from the nodes, verified against the chain")
    s.add_argument("file_id")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--version", type=int)
    s.set_defaults(fn=cmd_get)
    s = sub.add_parser("verify", help="check a local copy against the on-chain Merkle root")
    s.add_argument("path")
    s.add_argument("file_id")
    s.add_argument("--version", type=int)
    s.add_argument("--chunk-size", type=int, default=1024, help="chunk size in KB used at upload")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("ping", help="BSP/1 ping a storage node")
    s.add_argument("endpoint")
    s.add_argument("-c", "--count", type=int, default=4)
    s.set_defaults(fn=cmd_ping)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
