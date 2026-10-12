"""The coordination abstraction.

BlockShare separates the *control plane* (who owns a file, who may access it,
which versions exist, what their content hash is) from the *data plane* (the
storage nodes holding the chunks). The control plane is pluggable:

    ConventionalCoordinator  - Django + its database is the authority
    BlockchainCoordinator    - smart contracts are the authority

Both implement the interface below, so the rest of the application (views,
REST API, storage controller) never needs to know which one is active.
"""
from __future__ import annotations

import functools
import time

PERM_NONE = "none"
PERM_READ = "read"
PERM_WRITE = "write"
PERM_OWNER = "owner"
PERM_RANK = {PERM_NONE: 0, PERM_READ: 1, PERM_WRITE: 2, PERM_OWNER: 3}


class CoordinationError(Exception):
    """An operation was refused or could not be completed by the coordinator."""


def timed(op: str):
    """Record the latency of a coordination operation (used for the mode comparison)."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            from core.models import OperationMetric

            start = time.perf_counter()
            ok = False
            try:
                result = fn(self, *args, **kwargs)
                ok = True
                return result
            finally:
                OperationMetric.objects.create(op=op, mode=self.mode, ok=ok,
                                               duration_ms=(time.perf_counter() - start) * 1000)

        return wrapper

    return deco


class Coordinator:
    mode = ""
    label = ""
    source = ""  # where the authoritative state lives (shown in the UI)

    # --- writes -----------------------------------------------------------
    def register_file(self, user, file, version):
        raise NotImplementedError

    def add_version(self, user, file, version):
        raise NotImplementedError

    def share_file(self, owner, file, grantee, permission: str):
        raise NotImplementedError

    def revoke_access(self, owner, file, grantee):
        raise NotImplementedError

    def transfer_ownership(self, owner, file, new_owner):
        raise NotImplementedError

    def delete_file(self, owner, file):
        raise NotImplementedError

    # --- reads ------------------------------------------------------------
    def check_permission(self, user, file) -> str:
        raise NotImplementedError

    def owned_files(self, user) -> list:
        raise NotImplementedError

    def shared_files(self, user) -> list[tuple]:
        """[(file, permission)] shared with `user`."""
        raise NotImplementedError

    def file_state(self, file) -> dict:
        raise NotImplementedError

    def registered_root(self, file, version_number: int) -> str:
        raise NotImplementedError

    def verify_root(self, file, version_number: int, root_hex: str) -> bool:
        return self.registered_root(file, version_number) == root_hex

    def verify_chunk(self, file, version_number: int, leaf_hex: str, proof: list[str]):
        """On-chain Merkle proof check; None when the coordinator has no such facility."""
        return None

    # --- helpers ----------------------------------------------------------
    def require(self, user, file, needed: str) -> str:
        perm = self.check_permission(user, file)
        if PERM_RANK[perm] < PERM_RANK[needed]:
            raise CoordinationError(f"You need {needed} permission on this file (you have: {perm}).")
        return perm
