"""Blockchain ON: smart contracts are the coordinator.

Ownership, permissions and version history live in FileRegistry / FileSharing.
Every write is a transaction signed with the acting user's own wallet key, so
the contracts (not Django) decide whether e.g. Alice may share a file. Reads
for authorization go to the chain as well; the Django tables are only a cache
used for display (names, sizes, chunk maps).
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from accounts.wallet import account_for, ensure_wallet, user_for_address
from core.models import SystemConfig
from files.models import Share, StoredFile
from storage.models import StorageNode

from .base import PERM_NONE, PERM_OWNER, PERM_READ, PERM_WRITE, Coordinator, CoordinationError, timed
from .eth import ChainTxError, ChainUnavailable, get_chain, to_bytes32

log = logging.getLogger(__name__)

PERM_FROM_CHAIN = {0: PERM_NONE, 1: PERM_READ, 2: PERM_WRITE, 3: PERM_OWNER}
PERM_TO_CHAIN = {PERM_READ: 1, PERM_WRITE: 2}
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class Contracts:
    def __init__(self, chain, config: SystemConfig):
        self.nodes = chain.contract("NodeRegistry", config.node_registry_address)
        self.registry = chain.contract("FileRegistry", config.file_registry_address)
        self.sharing = chain.contract("FileSharing", config.file_sharing_address)


# ----------------------------------------------------------------- lifecycle

def deploy_contracts(config: SystemConfig | None = None) -> SystemConfig:
    chain = get_chain()
    config = config or SystemConfig.get()
    node_registry = chain.deploy("NodeRegistry")
    file_registry = chain.deploy("FileRegistry")
    file_sharing = chain.deploy("FileSharing", file_registry)
    reg = chain.contract("FileRegistry", file_registry)
    chain.transact(chain.operator, reg.functions.setSharingContract(file_sharing),
                   contract="FileRegistry", function="setSharingContract")
    config.chain_id = chain.chain_id
    config.node_registry_address = node_registry
    config.file_registry_address = file_registry
    config.file_sharing_address = file_sharing
    config.contracts_deployed_at = timezone.now()
    config.save()
    # a fresh chain knows nothing - everything has to be (re)anchored
    StoredFile.objects.update(on_chain=False)
    StorageNode.objects.update(on_chain=False)
    log.info("deployed BlockShare contracts on chain %s: %s %s %s", chain.chain_id,
             node_registry, file_registry, file_sharing)
    return config


def ensure_chain_ready(force_redeploy: bool = False) -> dict:
    """Make sure contracts exist (redeploying after a chain reset) and state is anchored."""
    chain = get_chain()
    if not chain.ping():
        raise ChainUnavailable(f"Blockchain RPC {chain.rpc_url} is unreachable")
    config = SystemConfig.get()
    chain._chain_id = None  # the node may have been restarted with another chain id
    redeployed = False
    if (force_redeploy or config.chain_id != chain.chain_id
            or not all(chain.has_code(a) for a in (config.node_registry_address,
                                                   config.file_registry_address,
                                                   config.file_sharing_address))):
        config = deploy_contracts(config)
        redeployed = True
    nodes = sync_nodes(config)
    files = sync_files() if config.blockchain_enabled else {"reconciled": 0}
    return {"redeployed": redeployed, "nodes_registered": nodes, **files}


def sync_nodes(config: SystemConfig | None = None) -> int:
    chain = get_chain()
    c = Contracts(chain, config or SystemConfig.get())
    count = 0
    for node in StorageNode.objects.filter(on_chain=False):
        chain.transact(chain.operator, c.nodes.functions.registerNode(node.node_id, node.endpoint),
                       contract="NodeRegistry", function="registerNode")
        node.on_chain = True
        node.save(update_fields=["on_chain"])
        count += 1
    return count


def sync_files(files=None) -> dict:
    """Reconcile the on-chain state of files with the Django mirror.

    Used when switching from Conventional to Blockchain mode (anchoring the
    state that Django coordinated while the chain was off) and after a chain reset.
    """
    chain = get_chain()
    c = Contracts(chain, SystemConfig.get())
    qs = files if files is not None else StoredFile.objects.filter(on_chain=False).order_by("created_at")
    done, errors = 0, []
    for f in qs:
        try:
            _reconcile(chain, c, f)
            done += 1
        except (ChainTxError, CoordinationError) as exc:
            errors.append(f"{f.name}: {exc}")
            log.warning("could not anchor %s: %s", f.name, exc)
    return {"reconciled": done, "errors": errors}


def mark_all_dirty():
    """Called when leaving Blockchain mode: Django becomes the authority again,
    so every file must be reconciled the next time the chain takes over."""
    StoredFile.objects.update(on_chain=False)


def _wallet(chain, user):
    acct = account_for(user)
    chain.fund(acct.address)
    return acct


def _reconcile(chain, c: Contracts, f: StoredFile):
    fid = to_bytes32(f.chain_id)
    versions = list(f.versions.order_by("number"))
    record = None
    try:
        record = c.registry.functions.getFile(fid).call()  # (owner, name, latest, created, deleted)
    except Exception:
        record = None

    if f.deleted:
        if record and not record[4]:
            owner_user = user_for_address(record[0])
            if owner_user:
                chain.transact(_wallet(chain, owner_user), c.registry.functions.deleteFile(fid),
                               contract="FileRegistry", function="deleteFile", file=f)
        f.on_chain = True
        f.save(update_fields=["on_chain"])
        return
    if not versions:
        return

    owner_acct = _wallet(chain, f.owner)
    if record is None:
        v1 = versions[0]
        receipt = chain.transact(owner_acct, c.registry.functions.registerFile(
            fid, f.name, to_bytes32(v1.merkle_root), to_bytes32(v1.manifest_hash), v1.size, v1.chunk_count),
            contract="FileRegistry", function="registerFile", user=f.owner, file=f)
        v1.tx_hash = _hex(receipt.transactionHash)
        v1.save(update_fields=["tx_hash"])
        chain_owner, chain_latest = owner_acct.address, 1
    else:
        if record[4]:
            raise CoordinationError("file was deleted on-chain")
        chain_owner, chain_latest = record[0], record[2]

    if chain_owner.lower() != owner_acct.address.lower():
        previous = user_for_address(chain_owner)
        if previous is None:
            raise CoordinationError("on-chain owner is not a BlockShare user; cannot reconcile ownership")
        chain.transact(_wallet(chain, previous), c.registry.functions.transferOwnership(fid, owner_acct.address),
                       contract="FileRegistry", function="transferOwnership", user=previous, file=f)

    for v in versions:
        if v.number > chain_latest:
            receipt = chain.transact(owner_acct, c.registry.functions.addVersion(
                fid, to_bytes32(v.merkle_root), to_bytes32(v.manifest_hash), v.size, v.chunk_count),
                contract="FileRegistry", function="addVersion", user=f.owner, file=f)
            v.tx_hash = _hex(receipt.transactionHash)
            v.save(update_fields=["tx_hash"])

    users, perms = c.sharing.functions.getGrantees(fid).call()
    on_chain = {u.lower(): PERM_FROM_CHAIN[p] for u, p in zip(users, perms)}
    wanted = {}
    for share in f.shares.select_related("user"):
        wanted[ensure_wallet(share.user).eth_address.lower()] = (share, share.permission)
    for addr, (share, perm) in wanted.items():
        if on_chain.get(addr) != perm:
            chain.transact(owner_acct, c.sharing.functions.shareFile(fid, share.user.profile.eth_address,
                                                                     PERM_TO_CHAIN[perm]),
                           contract="FileSharing", function="shareFile", user=f.owner, file=f)
    for addr in on_chain:
        if addr not in wanted:
            grantee = user_for_address(addr)
            target = grantee.profile.eth_address if grantee else addr
            chain.transact(owner_acct, c.sharing.functions.revokeAccess(fid, target),
                           contract="FileSharing", function="revokeAccess", user=f.owner, file=f)
    f.on_chain = True
    f.save(update_fields=["on_chain"])


def _hex(tx_hash) -> str:
    return "0x" + bytes(tx_hash).hex()


# --------------------------------------------------------------- coordinator

class BlockchainCoordinator(Coordinator):
    mode = "blockchain"
    label = "Blockchain (smart-contract coordinator)"
    source = "FileRegistry / FileSharing smart contracts"

    def __init__(self):
        self.chain = get_chain()
        config = SystemConfig.get()
        if not config.file_registry_address:
            ensure_chain_ready()
            config = SystemConfig.get()
        if not self.chain.ping():
            raise CoordinationError("Blockchain mode is ON but the blockchain network is unreachable. "
                                    "An administrator can switch back to Conventional mode.")
        self.c = Contracts(self.chain, config)

    def _anchored(self, file):
        if not file.on_chain:
            _reconcile(self.chain, self.c, file)
        return to_bytes32(file.chain_id)

    def _tx(self, user, fn, contract, function, file=None):
        try:
            return self.chain.transact(_wallet(self.chain, user), fn, contract=contract, function=function,
                                       user=user, file=file)
        except ChainTxError as exc:
            raise CoordinationError(f"Smart contract rejected the operation: {exc}") from exc
        except ChainUnavailable as exc:
            raise CoordinationError(f"Blockchain network unreachable: {exc}") from exc

    # --- writes -----------------------------------------------------------
    @timed("register_file")
    def register_file(self, user, file, version):
        receipt = self._tx(user, self.c.registry.functions.registerFile(
            to_bytes32(file.chain_id), file.name, to_bytes32(version.merkle_root),
            to_bytes32(version.manifest_hash), version.size, version.chunk_count),
            "FileRegistry", "registerFile", file)
        version.tx_hash = _hex(receipt.transactionHash)
        version.save(update_fields=["tx_hash"])
        file.on_chain = True
        file.save(update_fields=["on_chain"])

    @timed("add_version")
    def add_version(self, user, file, version):
        fid = self._anchored(file)
        receipt = self._tx(user, self.c.registry.functions.addVersion(
            fid, to_bytes32(version.merkle_root), to_bytes32(version.manifest_hash),
            version.size, version.chunk_count), "FileRegistry", "addVersion", file)
        version.tx_hash = _hex(receipt.transactionHash)
        version.save(update_fields=["tx_hash"])

    @timed("share_file")
    def share_file(self, owner, file, grantee, permission):
        fid = self._anchored(file)
        grantee_addr = ensure_wallet(grantee).eth_address
        receipt = self._tx(owner, self.c.sharing.functions.shareFile(fid, grantee_addr, PERM_TO_CHAIN[permission]),
                           "FileSharing", "shareFile", file)
        share, _ = Share.objects.update_or_create(
            file=file, user=grantee,
            defaults={"permission": permission, "granted_by": owner, "tx_hash": _hex(receipt.transactionHash)},
        )
        return share

    @timed("revoke_access")
    def revoke_access(self, owner, file, grantee):
        fid = self._anchored(file)
        self._tx(owner, self.c.sharing.functions.revokeAccess(fid, ensure_wallet(grantee).eth_address),
                 "FileSharing", "revokeAccess", file)
        Share.objects.filter(file=file, user=grantee).delete()

    @timed("transfer_ownership")
    def transfer_ownership(self, owner, file, new_owner):
        fid = self._anchored(file)
        self._tx(owner, self.c.registry.functions.transferOwnership(fid, ensure_wallet(new_owner).eth_address),
                 "FileRegistry", "transferOwnership", file)
        with transaction.atomic():
            Share.objects.filter(file=file, user=new_owner).delete()
            file.owner = new_owner
            file.save(update_fields=["owner", "updated_at"])

    @timed("delete_file")
    def delete_file(self, owner, file):
        fid = self._anchored(file)
        self._tx(owner, self.c.registry.functions.deleteFile(fid), "FileRegistry", "deleteFile", file)
        file.deleted = True
        file.save(update_fields=["deleted", "updated_at"])

    # --- reads ------------------------------------------------------------
    def _refresh_owner(self, file, chain_owner: str):
        """Keep the Django cache in line with the chain (e.g. after a transfer made from the CLI)."""
        if chain_owner == ZERO_ADDRESS:
            return
        mirror = getattr(getattr(file.owner, "profile", None), "eth_address", "")
        if mirror.lower() != chain_owner.lower():
            user = user_for_address(chain_owner)
            if user:
                file.owner = user
                file.save(update_fields=["owner"])

    @timed("check_permission")
    def check_permission(self, user, file):
        if not file.on_chain:
            self._anchored(file)
        try:
            perm = self.c.sharing.functions.checkPermission(
                to_bytes32(file.chain_id), ensure_wallet(user).eth_address).call()
        except Exception as exc:
            raise CoordinationError(f"Blockchain network unreachable: {exc}") from exc
        return PERM_FROM_CHAIN[perm]

    @timed("list_owned")
    def owned_files(self, user):
        ids = self.c.registry.functions.filesOf(ensure_wallet(user).eth_address).call()
        chain_ids = ["0x" + bytes(i).hex() for i in ids]
        files = list(StoredFile.objects.filter(chain_id__in=chain_ids, deleted=False))
        for f in files:
            if f.owner_id != user.id:
                f.owner = user
                f.save(update_fields=["owner"])
        # files not anchored yet are still shown so nothing "disappears"
        files += list(StoredFile.objects.filter(owner=user, on_chain=False, deleted=False))
        return sorted(set(files), key=lambda f: f.updated_at, reverse=True)

    @timed("list_shared")
    def shared_files(self, user):
        addr = ensure_wallet(user).eth_address
        ids = self.c.sharing.functions.getFilesSharedWith(addr).call()
        chain_ids = ["0x" + bytes(i).hex() for i in ids]
        out = []
        for f in StoredFile.objects.filter(chain_id__in=chain_ids, deleted=False).select_related("owner"):
            perm = PERM_FROM_CHAIN[self.c.sharing.functions.checkPermission(to_bytes32(f.chain_id), addr).call()]
            out.append((f, perm))
        return out

    def file_state(self, file):
        fid = self._anchored(file)
        owner, name, latest, created, deleted = self.c.registry.functions.getFile(fid).call()
        self._refresh_owner(file, owner)
        users, perms = self.c.sharing.functions.getGrantees(fid).call()
        grants = []
        for u, p in zip(users, perms):
            account = user_for_address(u)
            grants.append((account.username if account else u, PERM_FROM_CHAIN[p]))
        return {
            "source": self.source,
            "owner": (user_for_address(owner).username if user_for_address(owner) else owner),
            "owner_address": owner,
            "latest_version": latest,
            "deleted": deleted,
            "grants": grants,
            "registry": self.c.registry.address,
            "sharing": self.c.sharing.address,
        }

    @timed("get_root")
    def registered_root(self, file, version_number):
        fid = self._anchored(file)
        try:
            v = self.c.registry.functions.getVersion(fid, version_number).call()
        except Exception as exc:
            raise CoordinationError(f"Version {version_number} is not registered on-chain: {exc}") from exc
        return bytes(v[0]).hex()

    def verify_root(self, file, version_number, root_hex):
        return self.c.registry.functions.verifyRoot(to_bytes32(file.chain_id), version_number,
                                                    to_bytes32(root_hex)).call()

    def verify_chunk(self, file, version_number, leaf_hex, proof):
        return self.c.registry.functions.verifyChunk(
            to_bytes32(file.chain_id), version_number, to_bytes32(leaf_hex), [to_bytes32(p) for p in proof]).call()
