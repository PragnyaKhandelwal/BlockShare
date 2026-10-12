"""Content-addressed on-disk chunk store used by every storage node."""
from __future__ import annotations

import os
import threading
from pathlib import Path


class ChunkStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._count = 0
        self._bytes = 0
        for p in self._iter_files():
            self._count += 1
            self._bytes += p.stat().st_size

    def _path(self, key_hex: str) -> Path:
        # two-level fan-out keeps directories small: ab/cd/abcd...
        return self.root / key_hex[:2] / key_hex[2:4] / key_hex

    def _iter_files(self):
        for p in self.root.glob("??/??/*"):
            if p.is_file() and not p.name.endswith(".tmp"):
                yield p

    def put(self, key_hex: str, data: bytes) -> bool:
        """Store a chunk atomically. Returns False if it was already present."""
        path = self._path(key_hex)
        if path.exists():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{threading.get_ident()}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        try:
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            return False
        with self._lock:
            self._count += 1
            self._bytes += len(data)
        return True

    def get(self, key_hex: str) -> bytes | None:
        try:
            return self._path(key_hex).read_bytes()
        except FileNotFoundError:
            return None

    def has(self, key_hex: str) -> bool:
        return self._path(key_hex).exists()

    def delete(self, key_hex: str) -> bool:
        path = self._path(key_hex)
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            return False
        with self._lock:
            self._count -= 1
            self._bytes -= size
        return True

    def corrupt(self, key_hex: str) -> bool:
        """Flip bits in a stored chunk (used to demonstrate tamper detection)."""
        path = self._path(key_hex)
        if not path.exists():
            return False
        data = bytearray(path.read_bytes() or b"\x00")
        data[0] ^= 0xFF
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        return True

    def keys(self) -> list[str]:
        return [p.name for p in self._iter_files()]

    @property
    def count(self) -> int:
        return self._count

    @property
    def bytes_used(self) -> int:
        return self._bytes
