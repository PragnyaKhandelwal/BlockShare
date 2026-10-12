"""Consistent hashing ring used to place chunk replicas on storage nodes.

Each node is mapped to many points ("virtual nodes") on a 64-bit ring. A chunk
is placed on the first R *distinct* nodes found walking clockwise from the
chunk's own position. Adding or removing a node therefore only moves the
chunks in the arcs next to it (~1/N of the data) instead of reshuffling all of
them as `hash % N` would.
"""
from __future__ import annotations

import bisect
import hashlib


def _point(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:8], "big")


class HashRing:
    def __init__(self, node_ids: list[str], vnodes: int = 64):
        self.vnodes = vnodes
        self._ring: list[tuple[int, str]] = sorted(
            (_point(f"{node_id}#{v}"), node_id) for node_id in node_ids for v in range(vnodes)
        )
        self._points = [p for p, _ in self._ring]
        self.node_ids = sorted(set(node_ids))

    def preference_list(self, key_hex: str, n: int | None = None) -> list[str]:
        """Distinct nodes in ring order starting at the key's position."""
        if not self._ring:
            return []
        n = min(n or len(self.node_ids), len(self.node_ids))
        pos = int(key_hex[:16], 16)
        i = bisect.bisect(self._points, pos)
        out: list[str] = []
        for k in range(len(self._ring)):
            node_id = self._ring[(i + k) % len(self._ring)][1]
            if node_id not in out:
                out.append(node_id)
                if len(out) == n:
                    break
        return out

    def ownership(self) -> dict[str, float]:
        """Fraction of the key space owned (as primary) by every node."""
        if not self._ring:
            return {}
        share = {n: 0 for n in self.node_ids}
        space = 1 << 64
        for idx, (point, node_id) in enumerate(self._ring):
            prev = self._ring[idx - 1][0]
            share[node_id] += (point - prev) % space
        return {n: round(100 * v / space, 1) for n, v in share.items()}
