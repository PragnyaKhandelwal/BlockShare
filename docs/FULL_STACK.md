# BlockShare — Full-Stack Development Report

> Course: Full Stack Development Lab (24B15CS314)
> Stack: **Django 5.1 · Django REST Framework · PostgreSQL/SQLite · HTML/CSS/JavaScript · Chart.js · Docker**

## 1. Layered architecture

```
Browser (templates + JS)        REST clients / CLI
          │  HTTPS                    │  Token auth
          ▼                           ▼
   web/ (views, forms)          api/ (DRF viewsets, serializers, OpenAPI)
          └──────────────┬────────────┘
                         ▼
                files/services.py      ← use-cases shared by UI and API
                         │
          ┌──────────────┼───────────────────────┐
          ▼              ▼                       ▼
  coordination/     storage/controller.py    accounts/wallet.py
  (pluggable        (chunking, placement,    (custodial keys)
   control plane)    replication)
          │              │
   Django DB / chain   storage nodes (TCP)
```

Django apps:

| App | Responsibility |
|---|---|
| `core` | system configuration (mode, chunk size, R), metrics, bootstrap & monitor commands |
| `accounts` | user profile with Ethereum wallet |
| `files` | files, versions, chunks, shares, transfers + the service layer |
| `storage` | storage nodes, replica locations, health samples, controller, monitor, hash ring |
| `coordination` | `Coordinator` interface, conventional + blockchain implementations, web3 client, Merkle tree |
| `api` | REST API |
| `web` | server-rendered UI |

## 2. Database schema

```
auth_user 1─1 accounts_profile(eth_address, encrypted_private_key)
auth_user 1─* files_storedfile(id uuid, chain_id bytes32, name, owner, latest_version, coordinator, on_chain, deleted)
files_storedfile 1─* files_fileversion(number, size, chunk_size, chunk_count, merkle_root, manifest_hash, author, tx_hash)
files_fileversion 1─* files_chunk(index, sha256, size)
files_storedfile 1─* files_share(user, permission, granted_by, tx_hash)
files_transfer(user, file, direction, mode, bytes, chunks, deduplicated, duration_ms, status, detail JSON)
storage_storagenode(node_id, host, port, udp_port, http_port, status, last_rtt_ms, chunk_count, peers JSON, ...)
storage_chunklocation(sha256, node)          ← replica map (content addressed, shared across files)
storage_nodehealthsample(node, ok, rtt_ms)
coordination_chaintransaction(tx_hash, contract, function, sender, user, file, block, gas_used, latency_ms, ok)
core_systemconfig(singleton: blockchain_enabled, chunk_size, replication_factor, auto_repair, contract addresses)
core_operationmetric(op, mode, duration_ms)  ← conventional vs blockchain comparison
```

## 3. Pages

| URL | Page |
|---|---|
| `/login`, `/register` | authentication (a wallet is created on registration) |
| `/` | dashboard: stats, data per node chart, recent files/transfers/transactions |
| `/files`, `/files/<id>` | my files; file detail with control-plane state, version selector, chunk map, Merkle proofs, sharing, transfer, new version, integrity report |
| `/upload` | drag & drop upload with live progress (XHR) |
| `/shared-with-me` | files other users granted to me |
| `/versions` | version timeline |
| `/transfers`, `/transfers/<id>` | throughput chart, per-transfer BSP frame log |
| `/nodes` | topology (SVG), RTT chart, gossip view, node control |
| `/blockchain` | chain explorer: contracts, blocks, decoded events, wallets, gas and latency comparison |
| `/architecture` | Mermaid diagrams, mode comparison, subject mapping |
| `/settings` | admin control panel: mode switch, chunk size, R, repair, GC, chain sync |
| `/api/docs` | Swagger UI · `/admin` Django admin |

## 4. REST API (excerpt — full spec at `/api/docs/`)

| Method & path | Description |
|---|---|
| `POST /api/auth/register/` | create user (+ wallet), returns token |
| `POST /api/auth/token/` | obtain token |
| `GET /api/files/` | my files (from the active coordinator) |
| `POST /api/files/upload/` | multipart upload |
| `GET /api/files/{id}/` | detail incl. versions and shares |
| `GET /api/files/{id}/download/?version=n` | streamed download (`X-BlockShare-Merkle-Root` header) |
| `POST /api/files/{id}/versions/` | upload a new version |
| `POST /api/files/{id}/share/` · `/revoke/` · `/transfer-ownership/` | sharing & ownership |
| `POST /api/files/{id}/verify/` | deep integrity check |
| `GET /api/files/{id}/chunks/` | chunk map |
| `GET /api/shared-with-me/` | files shared with me |
| `GET /api/nodes/` · `POST /api/nodes/{id}/simulate/` | cluster (admin: add/remove/simulate) |
| `GET /api/transfers/` | transfer log |
| `GET/PATCH /api/system/` | mode & storage policy (PATCH = admin) |
| `POST /api/system/repair/` · `/gc/` · `/sync-chain/` | maintenance (admin) |
| `GET /api/stats/` · `GET /api/chain/` | statistics, public chain config |

Example:

```bash
TOKEN=$(curl -s -X POST -d username=alice -d password=blockshare123 http://localhost:8000/api/auth/token/ | jq -r .token)
curl -H "Authorization: Token $TOKEN" -F file=@report.pdf http://localhost:8000/api/files/upload/
curl -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' \
     -d '{"username":"bob","permission":"read"}' http://localhost:8000/api/files/<id>/share/
```

## 5. Engineering practices

* **Service layer** — UI and API call the same functions (`files/services.py`), so behaviour is identical.
* **Strategy pattern** — `get_coordinator()` returns `ConventionalCoordinator` or `BlockchainCoordinator`; nothing else changes when the mode flips.
* **Streaming** — uploads are spooled to disk by Django and chunked; downloads use `StreamingHttpResponse` with parallel prefetch; memory stays bounded.
* **Security** — CSRF protection, password validation, session + token auth, per-file authorization on every request, admin-only operations, wallet keys encrypted at rest, HMAC-authenticated node protocol, read-only RPC proxy, `DEBUG=false` and secure cookies in production.
* **Configuration** via environment variables (12-factor); Postgres via `DATABASE_URL`.
* **Tests** — 24 Python tests (`python manage.py test tests`) + 10 contract tests; GitHub Actions CI runs contracts, backend (against a real Hardhat chain) and the Docker build.
* **Deployment** — Dockerfile (all-in-one with supervisord), docker-compose (7 containers on a private network), Render blueprint.
