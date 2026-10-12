import uuid

from django.conf import settings
from django.db import models
from eth_utils import keccak


class StoredFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chain_id = models.CharField(max_length=66, unique=True, editable=False,
                                help_text="bytes32 identifier used by the smart contracts")
    name = models.CharField(max_length=255)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="files")
    mime_type = models.CharField(max_length=120, blank=True)
    latest_version = models.PositiveIntegerField(default=0)
    coordinator = models.CharField(max_length=20, help_text="coordination mode the file was created under")
    on_chain = models.BooleanField(default=False)
    deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    def ensure_chain_id(self) -> str:
        if not self.chain_id:
            self.chain_id = "0x" + keccak(text=f"blockshare:{self.id}").hex()
        return self.chain_id

    def save(self, *args, **kwargs):
        self.ensure_chain_id()
        super().save(*args, **kwargs)

    @property
    def short_id(self) -> str:
        return "F" + self.id.hex[:6].upper()

    @property
    def current(self):
        return self.versions.filter(number=self.latest_version).first()

    @property
    def size(self) -> int:
        cur = self.current
        return cur.size if cur else 0


class FileVersion(models.Model):
    file = models.ForeignKey(StoredFile, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    size = models.BigIntegerField()
    chunk_size = models.PositiveIntegerField()
    chunk_count = models.PositiveIntegerField()
    merkle_root = models.CharField(max_length=64)
    manifest_hash = models.CharField(max_length=64)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    tx_hash = models.CharField(max_length=66, blank=True)
    comment = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["file", "-number"]
        unique_together = [("file", "number")]

    def __str__(self):
        return f"{self.file.name} v{self.number}"


class Chunk(models.Model):
    version = models.ForeignKey(FileVersion, on_delete=models.CASCADE, related_name="chunks")
    index = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)
    size = models.PositiveIntegerField()

    class Meta:
        ordering = ["version", "index"]
        unique_together = [("version", "index")]


class Share(models.Model):
    READ = "read"
    WRITE = "write"
    PERMISSION_CHOICES = [(READ, "Read"), (WRITE, "Read / Write")]

    file = models.ForeignKey(StoredFile, on_delete=models.CASCADE, related_name="shares")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shared_files")
    permission = models.CharField(max_length=10, choices=PERMISSION_CHOICES, default=READ)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    tx_hash = models.CharField(max_length=66, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("file", "user")]


class Transfer(models.Model):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    REPAIR = "repair"
    VERIFY = "verify"
    DIRECTION_CHOICES = [(UPLOAD, "Upload"), (DOWNLOAD, "Download"), (REPAIR, "Repair"), (VERIFY, "Verify")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    file = models.ForeignKey(StoredFile, on_delete=models.SET_NULL, null=True, blank=True, related_name="transfers")
    version_number = models.PositiveIntegerField(null=True, blank=True)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    mode = models.CharField(max_length=20)
    status = models.CharField(max_length=10, default="ok")
    bytes = models.BigIntegerField(default=0)
    chunks = models.PositiveIntegerField(default=0)
    deduplicated = models.PositiveIntegerField(default=0)
    duration_ms = models.FloatField(default=0)
    detail = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def throughput_mbps(self) -> float:
        if not self.duration_ms:
            return 0.0
        return round(self.bytes * 8 / (self.duration_ms / 1000) / 1_000_000, 2)

    @property
    def nodes_used(self) -> list[str]:
        seen = []
        for ex in self.detail or []:
            node = ex.get("node")
            if node and node not in seen:
                seen.append(node)
        return seen
