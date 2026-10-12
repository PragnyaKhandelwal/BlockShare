# Demo / viva walkthrough (≈ 10 minutes)

Accounts: `alice`, `bob`, `charlie` (password `blockshare123`), `admin` (password from the deployment).
Use two browsers (or one normal + one private window) to be two users at once.

## 1. Conventional mode — Django coordinates (2 min)
1. Log in as **alice** → *Dashboard*: 3 storage nodes, mode pill says **Conventional**.
2. *Upload* any 3–10 MB file. Point out the progress bar, then the file page:
   chunk map coloured by node, each chunk on R = 2 nodes, Merkle root, *Registration tx: — (Django database record)*.
3. *Transfers → latest*: BSP/1 frame log — `PUT` to node-x with `pipeline node-x → node-y`, RTT per exchange.
4. Share the file with **bob** (read). In bob's window: *Shared With Me* → download.

## 2. Flip the switch — Blockchain mode (3 min)
1. As **admin** → *Control Panel* → **Switch to Blockchain**. The flash message shows the sync report: existing files were anchored on-chain.
2. *Chain Explorer*: contracts, blocks, decoded events `FileRegistered`, `AccessGranted` (with usernames), wallets.
3. As **alice**: upload a new file → its page shows *Registration tx 0x…* and *answered by FileRegistry / FileSharing smart contracts*.
4. Share with bob → transaction signed by **alice's** wallet (see *On-chain history*).
5. Transfer ownership to **charlie** → alice loses access, charlie sees it in *My Files* (`FileRegistry.filesOf`).

Key sentence: *"The storage layer did not change at all — only the control plane moved from Django to the smart contracts."*

## 3. Integrity & self-healing (2 min)
1. As **admin** open a file → *Fault injection*: corrupt chunk #0 on one node.
2. **Verify integrity** → status **MODIFIED**, root of served data ≠ registered root, `verifyChunk()` proof **INVALID** for that chunk, *Recoverable: yes*.
3. **Download** → still correct (failover to the healthy replica + read repair).
4. **Verify** again → **VALID**.

## 4. Network failure (2 min)
1. *Storage Nodes* → **Simulate crash** on node-a. Gossip view of the other nodes turns *suspect → dead*; topology link turns red.
2. Download a file → still works. Upload a file → placed on the remaining nodes.
3. Wait ~20 s (or *Control Panel → Repair*) → chunks re-replicated to restore R copies. **Recover** node-a.
4. Optional: set 200 ms latency on node-b and watch the RTT chart / transfer times.

## 5. Django is down — decentralized access (1 min, local setup)
```
python cli/blockshare_cli.py config --api http://127.0.0.1:8000
# stop Django (Ctrl+C the runserver / docker compose stop web)
python cli/blockshare_cli.py nodes
python cli/blockshare_cli.py get 0x<chain id from the file page> -o copy.bin
```
The CLI reads ownership, versions and the node directory from the chain and pulls the chunks over TCP, checking the Merkle root on-chain.

## 6. Numbers to quote
*Chain Explorer → Conventional vs Blockchain*: e.g. permission check ≈ 0.6 ms (DB) vs ≈ 18 ms (eth_call); share ≈ 28 ms vs ≈ 77 ms (signed tx). Gas: registerFile ≈ 227k, shareFile ≈ 149k, transferOwnership ≈ 60k.
