# BlockShare — Blockchain Development Report

> Module: **Blockchain-based decentralized coordination and file-sharing**
> Code: [`blockchain/`](../blockchain) (Solidity + Hardhat), [`coordination/`](../coordination) (web3.py integration)

## 1. Role of the blockchain in BlockShare

Blockchain is **not** used as an add-on for login or audit logging. It is an
**alternative control plane**. When the administrator switches the system to
*Blockchain mode*, the smart contracts become the authority for:

| Question the system must answer | Conventional mode | Blockchain mode |
|---|---|---|
| Who owns file F? | `files_storedfile.owner` (Django DB) | `FileRegistry.ownerOf(F)` |
| May user U read / write F? | `files_share` rows | `FileSharing.checkPermission(F, U)` |
| Which versions exist, what are their contents? | `files_fileversion` rows | `FileRegistry.getVersion(F, v)` → Merkle root + manifest hash |
| Is the downloaded data authentic? | compare with DB hash | `FileRegistry.verifyRoot()` / `verifyChunk()` (Merkle proof) |
| Which storage nodes exist? | `storage_storagenode` | `NodeRegistry.getNodes()` |

File bytes are **never** stored on-chain — only 32-byte identifiers and hashes.
The chunks stay on the storage nodes in both modes.

```
Blockchain OFF:  centralized coordination  + distributed storage
Blockchain ON:   decentralized coordination + distributed storage
```

## 2. Smart contracts

All contracts target Solidity `0.8.24` (EVM `cancun`, optimizer 200 runs).

### 2.1 `FileRegistry.sol` — ownership, versions, integrity

State per file (`bytes32 fileId`):

```solidity
struct FileRecord { address owner; string name; uint32 latestVersion; uint64 createdAt; bool deleted; }
struct Version    { bytes32 merkleRoot; bytes32 manifestHash; uint64 size; uint32 chunkCount; uint64 timestamp; address author; }
```

| Function | Who may call | Purpose |
|---|---|---|
| `registerFile(id, name, root, manifest, size, chunks)` | anyone (caller becomes owner) | creates version 1 |
| `addVersion(id, root, manifest, size, chunks)` | owner **or** WRITE grantee (asks `FileSharing.canWrite`) | appends to the version chain V1 → V2 → V3 |
| `transferOwnership(id, newOwner)` | current owner | decentralized change of ownership |
| `deleteFile(id)` | owner | soft delete |
| `getFile`, `getVersion`, `ownerOf`, `exists`, `filesOf(owner)` | view | reads |
| `verifyRoot(id, v, root)` | view | whole-file integrity check |
| `verifyChunk(id, v, leaf, proof[])` | view | **Merkle proof** that one chunk belongs to version v |

Events: `FileRegistered`, `VersionAdded`, `OwnershipTransferred`, `FileDeleted` —
together they form the verifiable history of every file (shown in the
in-app *Chain Explorer*).

### 2.2 `FileSharing.sol` — permissions

```solidity
enum Permission { NONE, READ, WRITE, OWNER }
```

| Function | Purpose |
|---|---|
| `shareFile(id, grantee, READ/WRITE)` | owner grants access (`AccessGranted` event) |
| `updatePermission(id, grantee, p)` | change an existing grant |
| `revokeAccess(id, grantee)` | remove access (`AccessRevoked` event) |
| `checkPermission(id, user)` | returns OWNER for the current owner (read live from `FileRegistry`), otherwise the grant |
| `canRead`, `canWrite`, `getGrantees(id)`, `getFilesSharedWith(user)` | views used by the coordinator and the UI |

Because `checkPermission` asks `FileRegistry.ownerOf()` on every call, an
ownership transfer immediately moves sharing rights to the new owner — the
two contracts cannot disagree.

### 2.3 `NodeRegistry.sol` — storage-node directory

Admin-managed list of `{nodeId, endpoint, active}`. It lets a client that
cannot reach Django (see the CLI, §6) discover where data lives.

### 2.4 Merkle tree (shared by Python and Solidity)

* leaf = `SHA-256(chunk)`
* parent = `SHA-256(min(a,b) ‖ max(a,b))` — sorted pairs, so proofs need no left/right flags
* odd node at the end of a level is promoted unchanged

Python: [`coordination/merkle.py`](../coordination/merkle.py) · Solidity: `FileRegistry.verifyChunk`.
The Hardhat tests build trees in JavaScript with the same rules and verify every leaf on-chain.

## 3. Who signs the transactions?

Every BlockShare user gets an Ethereum key pair at registration
([`accounts/wallet.py`](../accounts/wallet.py)); the private key is stored
encrypted (Fernet/AES) in the database. In Blockchain mode **the user's own
key signs** `registerFile`, `shareFile`, `transferOwnership` …, so on-chain
`msg.sender` is Alice, not the server. Consequences:

* the contract — not Django — rejects Bob's attempt to share Alice's file
  (`FileSharing: caller is not the file owner`);
* forging a `Share` row in Django's database does **not** grant access
  (covered by the test `test_contract_is_the_authority`).

An *operator* account deploys the contracts, registers nodes and tops up
user wallets with test ETH for gas.

## 4. Switching modes (state anchoring)

`core/services.py::set_blockchain_mode(True)`:

1. ensure the contracts are deployed (redeploys automatically if the chain was reset);
2. register every storage node in `NodeRegistry`;
3. **reconcile** every file (`coordination/blockchain.py::_reconcile`):
   register it if unknown, replay missing versions, transfer ownership if it
   changed while Django was in charge, and add/revoke grants until the
   on-chain grant set equals Django's.

Switching OFF simply makes Django the authority again (its tables are kept in
sync as a mirror while the chain is in charge) and marks files for
re-reconciliation.

## 5. Measurements (local Hardhat chain, automine)

Gas used per transaction (from the *Chain Explorer → Gas cost* table):

| Call | Avg gas |
|---|---|
| `FileRegistry.registerFile` | ~227,000 |
| `FileRegistry.addVersion` | ~129,000 |
| `FileSharing.shareFile` (first grant) | ~149,000 |
| `FileRegistry.transferOwnership` | ~60,000 |
| `FileSharing.revokeAccess` | ~34,000 |
| `NodeRegistry.registerNode` | ~129,000 |

Coordination latency, conventional vs blockchain (same machine, loopback):

| Operation | Django (ms) | Smart contract (ms) |
|---|---|---|
| `check_permission` | ~0.6 | ~18 (eth_call) |
| `share_file` | ~28 | ~77 (signed tx + receipt) |
| `register_file` | ~0.1 | ~85 |
| `transfer_ownership` | ~62 | ~70 |

Decentralization costs one network round trip per read and a mined
transaction per write — the price of not having to trust the coordinator.
The app records these numbers live (*Chain Explorer → Conventional vs Blockchain*).

## 6. "What if Django goes down?"

[`cli/blockshare_cli.py`](../cli/blockshare_cli.py) needs only a JSON-RPC
endpoint and the storage nodes:

```
python cli/blockshare_cli.py nodes                         # NodeRegistry
python cli/blockshare_cli.py file 0x<fileId>               # owner, versions, grants
python cli/blockshare_cli.py get 0x<fileId> -o copy.bin    # manifest + chunks from nodes, root checked on-chain
python cli/blockshare_cli.py verify copy.bin 0x<fileId>    # FileRegistry.verifyRoot
```

The website also exposes a **read-only JSON-RPC proxy** at `/rpc`
(`eth_call`, `eth_getLogs`, … — never `eth_sendRawTransaction`) so anyone can
audit ownership, grants and roots without trusting BlockShare's database.

## 7. Tests

`blockchain/test/BlockShare.test.js` (Hardhat + chai, 10 tests): node
registry access control, registration, duplicate protection, version chain,
ownership transfer, soft delete, grant/update/revoke, WRITE enforcement
across contracts, Merkle proofs for every leaf and rejection of tampered chunks.
Python integration tests (`tests/test_app.py::BlockchainModeTests`) drive the
real contracts through the whole application.

```
cd blockchain && npm install && npx hardhat test
```

## 8. Deploying to a public testnet

```
cd blockchain
SEPOLIA_RPC_URL=https://... DEPLOYER_PRIVATE_KEY=0x... npx hardhat run scripts/deploy.js --network sepolia
```

Then run BlockShare with `BLOCKCHAIN_RPC_URL=<sepolia rpc>`,
`BLOCKCHAIN_OPERATOR_KEY=<funded key>` and
`BLOCKCHAIN_EXPLORER_TX_URL=https://sepolia.etherscan.io/tx/` — the app deploys
its own contract instance at bootstrap and links every transaction to Etherscan.
