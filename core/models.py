from django.conf import settings
from django.db import models


class SystemConfig(models.Model):
    """Singleton holding the cluster-wide configuration, including the coordination mode."""

    MODE_CONVENTIONAL = "conventional"
    MODE_BLOCKCHAIN = "blockchain"

    blockchain_enabled = models.BooleanField(default=False)
    chunk_size = models.PositiveIntegerField(default=1024 * 1024)
    replication_factor = models.PositiveSmallIntegerField(default=2)
    auto_repair = models.BooleanField(default=True)

    chain_id = models.PositiveIntegerField(null=True, blank=True)
    node_registry_address = models.CharField(max_length=42, blank=True)
    file_registry_address = models.CharField(max_length=42, blank=True)
    file_sharing_address = models.CharField(max_length=42, blank=True)
    contracts_deployed_at = models.DateTimeField(null=True, blank=True)

    mode_changed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "system configuration"

    def __str__(self):
        return f"SystemConfig(mode={self.mode})"

    @property
    def mode(self) -> str:
        return self.MODE_BLOCKCHAIN if self.blockchain_enabled else self.MODE_CONVENTIONAL

    @classmethod
    def get(cls) -> "SystemConfig":
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "chunk_size": settings.DEFAULT_CHUNK_SIZE,
                "replication_factor": settings.DEFAULT_REPLICATION_FACTOR,
            },
        )
        return obj


class OperationMetric(models.Model):
    """Latency of every coordination operation - used to compare the two modes."""

    op = models.CharField(max_length=40)
    mode = models.CharField(max_length=20)
    duration_ms = models.FloatField()
    ok = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
