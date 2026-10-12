"""SHA-256 Merkle tree identical to the one verified by FileRegistry.verifyChunk().

* leaves  = SHA-256 digests of the file chunks (in order)
* parent  = SHA-256(min(a, b) || max(a, b))   (sorted pairs, so proofs need no left/right bits)
* an odd node at the end of a level is promoted unchanged to the next level
"""
from __future__ import annotations

import hashlib


def _parent(a: bytes, b: bytes) -> bytes:
    return hashlib.sha256(min(a, b) + max(a, b)).digest()


def build_levels(leaves: list[bytes]) -> list[list[bytes]]:
    if not leaves:
        leaves = [hashlib.sha256(b"").digest()]
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        level = levels[-1]
        nxt = []
        for i in range(0, len(level), 2):
            nxt.append(level[i] if i + 1 == len(level) else _parent(level[i], level[i + 1]))
        levels.append(nxt)
    return levels


def merkle_root(leaves_hex: list[str]) -> str:
    return build_levels([bytes.fromhex(h) for h in leaves_hex])[-1][0].hex()


def merkle_proof(leaves_hex: list[str], index: int) -> list[str]:
    levels = build_levels([bytes.fromhex(h) for h in leaves_hex])
    proof = []
    for level in levels[:-1]:
        sibling = index ^ 1
        if sibling < len(level):
            proof.append(level[sibling].hex())
        index //= 2
    return proof


def verify_proof(leaf_hex: str, proof_hex: list[str], root_hex: str) -> bool:
    h = bytes.fromhex(leaf_hex)
    for p in proof_hex:
        h = _parent(h, bytes.fromhex(p))
    return h.hex() == root_hex
