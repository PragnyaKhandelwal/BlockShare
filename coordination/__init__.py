"""Pluggable control plane - see coordination/base.py."""


def get_coordinator():
    """Return the coordinator for the currently selected system mode."""
    from core.models import SystemConfig

    if SystemConfig.get().blockchain_enabled:
        from .blockchain import BlockchainCoordinator

        return BlockchainCoordinator()
    from .conventional import ConventionalCoordinator

    return ConventionalCoordinator()
