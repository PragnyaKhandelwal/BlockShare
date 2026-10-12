"""System-level operations: switching the coordination mode, cluster maintenance."""
from __future__ import annotations

from django.utils import timezone

from .models import SystemConfig


def set_blockchain_mode(enabled: bool) -> dict:
    """The one-click switch between centralized and decentralized coordination.

    OFF -> ON : contracts are deployed if needed, storage nodes are registered in
                NodeRegistry and every file's state (owner, versions, grants) is
                anchored on-chain. From then on the contracts are the authority.
    ON -> OFF : the Django database (kept in sync as a mirror) becomes the
                authority again; files are flagged for re-anchoring next time.
    """
    config = SystemConfig.get()
    if config.blockchain_enabled == enabled:
        return {"changed": False}
    report: dict = {"changed": True}
    if enabled:
        from coordination.blockchain import ensure_chain_ready

        SystemConfig.objects.filter(pk=config.pk).update(blockchain_enabled=True)
        try:
            report.update(ensure_chain_ready())
        except Exception:
            SystemConfig.objects.filter(pk=config.pk).update(blockchain_enabled=False)
            raise
    else:
        from coordination.blockchain import mark_all_dirty

        mark_all_dirty()
        SystemConfig.objects.filter(pk=config.pk).update(blockchain_enabled=False)
    SystemConfig.objects.filter(pk=config.pk).update(mode_changed_at=timezone.now())
    return report


def update_settings(chunk_size: int | None = None, replication_factor: int | None = None,
                    auto_repair: bool | None = None):
    fields = {}
    if chunk_size:
        fields["chunk_size"] = max(64 * 1024, min(int(chunk_size), 16 * 1024 * 1024))
    if replication_factor:
        fields["replication_factor"] = max(1, min(int(replication_factor), 5))
    if auto_repair is not None:
        fields["auto_repair"] = bool(auto_repair)
    if fields:
        SystemConfig.objects.filter(pk=SystemConfig.get().pk).update(**fields)
    return SystemConfig.get()
