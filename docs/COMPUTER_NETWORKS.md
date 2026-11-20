# BlockShare — Computer Networks Report

> Module: **distributed storage network (data plane)**
> Code: [`storage_node/`](../storage_node) (servers + protocol), [`storage/`](../storage) (placement, replication, monitoring)

## 1. Network architecture

```
                 HTTPS (browser, REST)                 JSON-RPC over HTTP
   Users  ───────────────────────────►  Django  ───────────────────────────►  EVM chain
                                          │
                                          │ BSP/1 over TCP  (port 9000/910x)
                     ┌────────────────────┼────────────────────┐
                     ▼                    ▼                    ▼
                  Node A ◄──── UDP ───► Node B ◄──── UDP ───► Node C      (gossip heartbeats, port 10000/1010x)
                     │  ── TCP replication pipeline ──►  │ ──►  │
                  HTTP/1.1 :9300 (health, stats, metrics) on every node
```

Deployments:

| Setup | How |
|---|---|
| one laptop, processes | `python scripts/dev.py` — nodes on 127.0.0.1:9101-9103 |
| one laptop, separate hosts | `docker compose up` — each node is a container with its own IP (172.28.0.11-13) on a bridge network |
| real LAN | run `python -m storage_node --node-id node-b --advertise-host 192.168.1.102 --peers 192.168.1.101:10000` on each PC and add it on the *Storage Nodes* page |
| cloud | one container, all nodes on loopback (Render) |

## 2. BSP/1 — the BlockShare Storage Protocol (application layer, over TCP)

Implemented from scratch in [`storage_node/protocol.py`](../storage_node/protocol.py).
Every request and response is a **72-byte fixed header** + payload, network byte order:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | magic | `"BSP1"` — rejects stray connections |
| 4 | 1 | version | 1 |
| 5 | 1 | opcode | request op; response sets bit `0x80` |
| 6 | 1 | flags | `0x01 FORWARD` = payload carries a replication list |
| 7 | 1 | status | `OK, NOT_FOUND, BAD_CHECKSUM, HASH_MISMATCH, UNAUTHORIZED, BAD_REQUEST, SERVER_ERROR, UNAVAILABLE` |
| 8 | 4 | request id | lets a client match responses (multiple requests per connection supported) |
| 12 | 32 | key | SHA-256 of the chunk = **content address** |
| 44 | 8 | length | payload length (uint64) → message framing on a byte stream |
| 52 | 4 | CRC-32 | transport-level integrity of the payload |
| 56 | 16 | auth tag | truncated HMAC-SHA256 (cluster key) over the header fields |

Opcodes: `PING, PUT, GET, DELETE, HAS, STATS, LIST, CORRUPT (fault injection), ADMIN (simulate crash / latency)`.

Design points worth discussing:

* **Framing** — TCP is a byte stream; the fixed header + length field delimits messages (`recv_exact`, `readexactly`).
* **Two levels of integrity** — CRC-32 catches corruption in transit; SHA-256(payload) == key catches anything else end-to-end.
* **Authentication** — mutating ops (`PUT, DELETE, CORRUPT, ADMIN`) need a valid HMAC tag; reads are open, so knowing a chunk's hash works as a capability (like IPFS).
* **Nagle disabled** (`TCP_NODELAY`) on the client to keep small control messages snappy.
* **Foreign traffic** — the node reads the 4-byte magic before anything else. Anything that isn't `BSP1` is dropped; if it looks like an HTTP request (cloud load balancers and port scanners probe every open port with `HEAD /`), the node answers `400 Bad Request` with a one-line explanation, counts it in `foreign_connections`, and logs a summary at most once a minute.

### Incoming BSP read timeouts

Storage nodes close stalled incoming connections instead of waiting indefinitely.
The limits are node settings (seconds), independent of Django's outgoing client
timeout:

| Phase | Default | CLI flag | Environment variable |
|---|---|---|---|
| Waiting for the first byte of a request, including between requests | 30 s | `--idle-timeout` | `NODE_IDLE_TIMEOUT_S` |
| Completing the 72-byte header after its first byte | 10 s | `--header-timeout` | `NODE_HEADER_TIMEOUT_S` |
| No progress while receiving the payload | 30 s | `--payload-idle-timeout` | `NODE_PAYLOAD_IDLE_TIMEOUT_S` |

Values must be positive and finite. CLI flags override environment variables;
`--cluster` applies the limits to every node. Docker Compose passes the variables
to its storage containers; local development inherits exported variables.

The header has a fixed deadline, so sending occasional header bytes does not
extend it. The payload timer resets whenever another nonempty piece arrives:
a slow upload can take longer than 30 seconds overall and still succeed. There
is no total payload-duration limit in this change; a client that keeps supplying
bytes within the idle limit can continue until the existing frame-size limit.
Request dispatch, disk access, downstream replication and response writes are
outside these incoming-read limits. The wire format, 72-byte header, CRC/HMAC
validation, and multiple-frames-per-connection behavior are unchanged. An
incomplete frame is closed without a BSP response and is never dispatched.

After a timeout, `/stats` exposes `metrics.bsp_idle_timeouts`,
`metrics.bsp_header_timeouts`, or `metrics.bsp_payload_timeouts`. The existing
Prometheus endpoint exports them as `blockshare_node_bsp_*_timeouts`; like the
other Counter fields, each appears after its first event. `/health` is unchanged.
A clean EOF does not count as a timeout.

For a manual local demonstration, run a standalone node:

```bash
python -m storage_node --node-id timeout-demo --port 9501 --udp-port 10501 --http-port 9701 --header-timeout 3
```

In another terminal, send only the magic prefix:

```bash
python -c "import socket; s=socket.create_connection(('127.0.0.1',9501)); s.settimeout(5); s.sendall(b'BSP1'); print(repr(s.recv(1))); s.close()"
curl http://127.0.0.1:9701/stats
```

The socket prints `b''` when the server closes it after the header deadline, and
the stats show a header-timeout event. This demonstrates connection cleanup,
not a throughput benchmark.

### Replication pipeline (chain replication)

A `PUT` with `FLAG_FORWARD` carries `[uint16 len][json list of host:port][chunk]`.
The first node stores the chunk, forwards it to the next node with the
remaining list, waits for its acknowledgement, and replies with
`stored_on = [A, B, …]`. The client therefore sends each chunk **once** and
the replicas are created node-to-node (the same idea as the HDFS write
pipeline). If the head is unreachable the controller retries with a new head;
if a downstream hop fails it reports a partial success and the controller
fills the gap.

## 3. UDP gossip heartbeats (failure detection between nodes)

Each node sends a JSON datagram every 2 s to every peer it knows:

```json
{"t":"hb","id":"node-a","seq":118,"ts":1760280000.1,"tcp":"172.28.0.11:9000",
 "udp":"172.28.0.11:10000","chunks":42,"bytes":40213504,"peers":["172.28.0.12:10000","172.28.0.13:10000"]}
```

* **Membership by gossip** — the `peers` list spreads knowledge of new nodes; node-b and node-c are only configured with node-a, yet all three discover each other.
* **Liveness** — a peer is *alive* if heard from within 3 intervals, *suspect* within 6, otherwise *dead*.
* **Packet-loss estimate** — gaps in the sequence numbers count as lost datagrams (UDP gives no delivery guarantee); a restart is detected when `seq` goes backwards.

The gossip view of every node is shown on the *Storage Nodes* page.

## 4. Monitoring, RTT and failure detection (coordinator side)

[`storage/monitor.py`](../storage/monitor.py) probes every node with a BSP
`PING` (application-level RTT over TCP) plus `STATS`. Two consecutive
failures mark a node **DOWN**; after a 15 s grace period the cluster is
**re-replicated** automatically. RTT samples are plotted per node.

## 5. Placement: consistent hashing

[`storage/ring.py`](../storage/ring.py): each node owns 64 virtual points on a
64-bit ring; a chunk goes to the first *R* distinct nodes clockwise from
`SHA-256(chunk)`. Adding a 4th node moves only ~25 % of keys (test
`test_adding_a_node_moves_only_a_fraction_of_keys`), whereas `hash mod N` would move ~75 %.
The ring share of each node is displayed on the *Storage Nodes* page.

## 6. Data transfer

* **Upload** — chunks are pipelined to the nodes by a thread pool (default 4 parallel transfers), at most 2×workers chunks in flight to bound memory.
* **Download** — a sliding window prefetches the next chunks in parallel while streaming the file to the browser in order (`StreamingHttpResponse`).
* **Failover & read repair** — replicas are tried in order (UP first, lowest RTT); a missing or corrupted replica is skipped and immediately rewritten from the good copy.
* **Deduplication** — content addressing means an identical chunk already stored R times is not sent again.
* **Frame log** — every request/response exchange (op, node, bytes, RTT, status, pipeline) is stored per transfer and shown on *Transfers → #id*; throughput is plotted per transfer.

### Upload history: contact attempts versus replicas

Upload details distinguish direct coordinator attempts (`node`, `contacted`),
planned pipeline members (`pipeline`), and returned storage acknowledgements
(`stored_on`). A downstream replica is not automatically a direct coordinator
target. Failed head attempts remain visible, and partial acknowledgements must
not be treated as full pipeline success.

`DEDUP` is a local decision with `contacted: false`; its `existing_replicas`
list comes from metadata, not a new network probe. Older saved JSON is not
rewritten. The summaries use only the retained log (the last 500 events,
including manifest operations); missing historical information is not rebuilt
from current placement. No protocol, replication, or download behavior changes.

## 7. HTTP/1.1 server written by hand

Each node runs a minimal HTTP/1.1 responder on raw asyncio streams (request
line parsing, `Content-Length`, `Connection: close`):
`GET /health` (200/503), `GET /stats` (JSON), `GET /metrics` (Prometheus text format).
Docker health checks and browsers use it, e.g. `curl http://localhost:9301/stats`.

## 8. Network fault emulation (for the demo)

From *Storage Nodes* an administrator can, per node:

* **Simulate crash** — the node stops serving data and stops heartbeating (peers mark it *suspect* then *dead*; the monitor marks it DOWN; downloads fail over; auto-repair restores R copies).
* **Add latency** — artificial per-request delay; watch RTT graphs and transfer times change.
* **Drain** — stop placing new chunks on a node.

And on a file page: **corrupt a replica** to show detection (Merkle root mismatch) and self-healing.

## 9. Measured on a laptop (loopback, 1 MB chunks, R = 2)

| Transfer | Avg throughput |
|---|---|
| Upload (chunk + hash + 2× pipeline replication) | ~50–80 Mbit/s |
| Download (parallel prefetch, SHA-256 verified) | ~640 Mbit/s |
| BSP PING RTT | ~1–2 ms (first connection ~8 ms) |

## 10. Tests

`tests/test_network.py`: header size and round trip, CRC detection, bad magic,
forward payload encoding, consistent-hashing stability and minimal movement,
real 3-node cluster: ping/stats, 3-hop replication pipeline, content-address
enforcement, HMAC rejection, corrupt/delete, simulated crash, UDP gossip
discovery, HTTP `/health` and `/metrics`.
