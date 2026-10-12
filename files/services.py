"""Use-cases of BlockShare, shared by the web UI and the REST API.

    upload -> chunk + hash -> place & replicate on nodes -> Merkle root
           -> manifest blob -> coordinator.register_file (DB row or smart contract)
"""
from __future__ import annotations

import json
import mimetypes
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from accounts.wallet import ensure_wallet
from coordination import get_coordinator
from coordination.base import PERM_OWNER, PERM_READ, PERM_WRITE, CoordinationError
from coordination.merkle import merkle_proof, merkle_root
from storage.controller import StorageController, StorageError

from .models import Chunk, FileVersion, StoredFile, Transfer


class ServiceError(Exception):
    pass


def _check_size(uploaded):
    limit = settings.BLOCKSHARE_MAX_UPLOAD_MB * 1024 * 1024
    if uploaded.size > limit:
        raise ServiceError(f"File is larger than the {settings.BLOCKSHARE_MAX_UPLOAD_MB} MB limit of this deployment.")


def _store(file: StoredFile, number: int, uploaded, ctrl: StorageController):
    """Chunk, distribute and describe one version. Returns (upload_result, root, manifest_hash)."""
    uploaded.seek(0)
    result = ctrl.upload(uploaded)
    leaves = [c.sha256 for c in result.chunks]
    root = merkle_root(leaves)
    nodes = ctrl.nodes
    manifest = {
        "format": "blockshare-manifest/1",
        "file_id": file.ensure_chain_id(),
        "name": file.name,
        "version": number,
        "size": result.size,
        "chunk_size": ctrl.chunk_size,
        "merkle_root": root,
        "chunks": [
            {"index": c.index, "sha256": c.sha256, "size": c.size,
             "nodes": [nodes[n].endpoint for n in c.nodes if n in nodes]}
            for c in result.chunks
        ],
    }
    manifest_hash, _ = ctrl.store_blob(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
    return result, root, manifest_hash


def _log_transfer(user, file, number, direction, mode, start, result=None, ctrl=None, status="ok", error="",
                  size=0, chunks=0):
    return Transfer.objects.create(
        user=user, file=file, version_number=number, direction=direction, mode=mode, status=status,
        bytes=result.size if result else size, chunks=len(result.chunks) if result else chunks,
        deduplicated=result.deduplicated if result else 0,
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
        detail=ctrl.log.items[-500:] if ctrl else [], error=error,
    )


def upload_file(user, uploaded, comment: str = "") -> StoredFile:
    _check_size(uploaded)
    coord = get_coordinator()  # fail fast if the chosen coordinator is unavailable
    ensure_wallet(user)
    start = time.perf_counter()
    ctrl = StorageController()
    file = StoredFile(name=uploaded.name[:255], owner=user, coordinator=coord.mode,
                      mime_type=uploaded.content_type or mimetypes.guess_type(uploaded.name)[0] or "")
    try:
        result, root, manifest_hash = _store(file, 1, uploaded, ctrl)
    except StorageError as exc:
        _log_transfer(user, None, 1, Transfer.UPLOAD, coord.mode, start, ctrl=ctrl, status="failed", error=str(exc))
        raise ServiceError(str(exc)) from exc

    with transaction.atomic():
        file.latest_version = 1
        file.save()
        version = FileVersion.objects.create(
            file=file, number=1, size=result.size, chunk_size=ctrl.chunk_size, chunk_count=len(result.chunks),
            merkle_root=root, manifest_hash=manifest_hash, author=user, comment=comment[:200],
        )
        Chunk.objects.bulk_create([Chunk(version=version, index=c.index, sha256=c.sha256, size=c.size)
                                   for c in result.chunks])
    try:
        coord.register_file(user, file, version)
    except CoordinationError:
        file.delete()  # orphaned chunks are removed by garbage collection
        raise
    _log_transfer(user, file, 1, Transfer.UPLOAD, coord.mode, start, result, ctrl,
                  status="partial" if result.under_replicated else "ok")
    return file


def upload_version(user, file: StoredFile, uploaded, comment: str = "") -> FileVersion:
    _check_size(uploaded)
    coord = get_coordinator()
    coord.require(user, file, PERM_WRITE)
    start = time.perf_counter()
    ctrl = StorageController()
    number = file.latest_version + 1
    try:
        result, root, manifest_hash = _store(file, number, uploaded, ctrl)
    except StorageError as exc:
        _log_transfer(user, file, number, Transfer.UPLOAD, coord.mode, start, ctrl=ctrl, status="failed",
                      error=str(exc))
        raise ServiceError(str(exc)) from exc
    with transaction.atomic():
        version = FileVersion.objects.create(
            file=file, number=number, size=result.size, chunk_size=ctrl.chunk_size,
            chunk_count=len(result.chunks), merkle_root=root, manifest_hash=manifest_hash,
            author=user, comment=comment[:200],
        )
        Chunk.objects.bulk_create([Chunk(version=version, index=c.index, sha256=c.sha256, size=c.size)
                                   for c in result.chunks])
    try:
        coord.add_version(user, file, version)
    except CoordinationError:
        version.delete()
        raise
    file.latest_version = number
    file.save(update_fields=["latest_version", "updated_at"])
    _log_transfer(user, file, number, Transfer.UPLOAD, coord.mode, start, result, ctrl,
                  status="partial" if result.under_replicated else "ok")
    return version


def get_version(file: StoredFile, number: int | None) -> FileVersion:
    version = file.versions.filter(number=number or file.latest_version).first()
    if version is None:
        raise ServiceError("Unknown version")
    return version


def open_download(user, file: StoredFile, number: int | None = None):
    """Authorize, check the manifest against the registered root, and return a byte generator."""
    coord = get_coordinator()
    coord.require(user, file, PERM_READ)
    version = get_version(file, number)
    chunks = list(version.chunks.values_list("index", "sha256"))
    computed = merkle_root([sha for _, sha in chunks])
    registered = coord.registered_root(file, version.number)
    if computed != registered:
        raise ServiceError("Integrity violation: the chunk manifest does not match the registered Merkle root.")
    ctrl = StorageController()

    def stream():
        start = time.perf_counter()
        sent, status, error = 0, "ok", ""
        try:
            for data in ctrl.iter_chunks(chunks):
                sent += len(data)
                yield data
        except StorageError as exc:
            status, error = "failed", str(exc)
            raise
        except GeneratorExit:
            status = "aborted"
            raise
        finally:
            _log_transfer(user, file, version.number, Transfer.DOWNLOAD, coord.mode, start, ctrl=ctrl,
                          status=status, error=error, size=sent, chunks=len(chunks))

    return version, stream(), {"root": registered, "source": coord.source, "mode": coord.mode}


def read_all(user, file, number=None) -> bytes:
    _, gen, _ = open_download(user, file, number)
    return b"".join(gen)


def verify_file(user, file: StoredFile, number: int | None = None) -> dict:
    """Deep integrity check of every replica, compared against the coordinator's registered root."""
    coord = get_coordinator()
    coord.require(user, file, PERM_READ)
    version = get_version(file, number)
    start = time.perf_counter()
    ctrl = StorageController()
    chunks = list(version.chunks.values_list("index", "sha256"))
    rows = ctrl.deep_verify(chunks)
    expected = [sha for _, sha in chunks]

    served = []
    for row in rows:
        present = [r for r in row["replicas"] if r["actual"]]
        served.append(present[0]["actual"] if present else row["expected"])
        row["healthy"] = any(r["state"] == "ok" for r in row["replicas"])
        row["status"] = "ok" if all(r["state"] == "ok" for r in row["replicas"]) and row["replicas"] else (
            "degraded" if row["healthy"] else "lost")
    served_root = merkle_root(served)
    registered = coord.registered_root(file, version.number)

    on_chain = None
    if coord.mode == "blockchain":
        on_chain = {"root_valid": coord.verify_root(file, version.number, served_root), "chunks": []}
        for i, row in enumerate(rows[:64]):
            leaf = served[i]
            on_chain["chunks"].append(bool(coord.verify_chunk(file, version.number, leaf, merkle_proof(served, i))))
        for i, ok in enumerate(on_chain["chunks"]):
            rows[i]["proof_valid"] = ok

    _log_transfer(user, file, version.number, Transfer.VERIFY, coord.mode, start, ctrl=ctrl,
                  size=sum(c.size for c in version.chunks.all()), chunks=len(chunks))
    return {
        "file": file, "version": version, "mode": coord.mode, "source": coord.source,
        "registered_root": registered, "served_root": served_root,
        "expected_root": merkle_root(expected),
        "status": "VALID" if served_root == registered else "MODIFIED",
        "recoverable": all(r["healthy"] for r in rows),
        "rows": rows, "on_chain": on_chain, "checked_at": timezone.now(),
    }


def find_user(username: str):
    user = get_user_model().objects.filter(username__iexact=username.strip()).first()
    if user is None:
        raise ServiceError(f"No user called '{username}'.")
    return user


def share(owner, file, username: str, permission: str):
    if permission not in (PERM_READ, PERM_WRITE):
        raise ServiceError("Permission must be read or write.")
    return get_coordinator().share_file(owner, file, find_user(username), permission)


def revoke(owner, file, username: str):
    get_coordinator().revoke_access(owner, file, find_user(username))


def transfer_ownership(owner, file, username: str):
    get_coordinator().transfer_ownership(owner, file, find_user(username))


def delete(owner, file):
    get_coordinator().delete_file(owner, file)


def accessible_file(user, file_id) -> tuple[StoredFile, str]:
    file = StoredFile.objects.filter(pk=file_id, deleted=False).select_related("owner").first()
    if file is None:
        raise ServiceError("File not found.")
    perm = get_coordinator().check_permission(user, file)
    if perm == "none":
        raise ServiceError("File not found or access denied.")
    return file, perm


__all__ = ["ServiceError", "PERM_OWNER"]
