from django.db import models


class StorageNode(models.Model):
    STATUS_UP = "up"
    STATUS_DOWN = "down"
    STATUS_UNKNOWN = "unknown"
    STATUS_CHOICES = [(STATUS_UP, "Up"), (STATUS_DOWN, "Down"), (STATUS_UNKNOWN, "Unknown")]

    node_id = models.CharField(max_length=40, unique=True)
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(help_text="BSP/TCP data port")
    udp_port = models.PositiveIntegerField(null=True, blank=True, help_text="heartbeat (UDP) port")
    http_port = models.PositiveIntegerField(null=True, blank=True, help_text="HTTP status port")
    is_active = models.BooleanField(default=True, help_text="eligible for new chunk placement")

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_UNKNOWN)
    last_rtt_ms = models.FloatField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    down_since = models.DateTimeField(null=True, blank=True)
    chunk_count = models.PositiveIntegerField(default=0)
    bytes_used = models.BigIntegerField(default=0)
    peers = models.JSONField(default=list, blank=True)
    node_metrics = models.JSONField(default=dict, blank=True)
    simulated_failure = models.BooleanField(default=False)
    latency_ms = models.PositiveIntegerField(default=0)
    on_chain = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["node_id"]

    def __str__(self):
        return f"{self.node_id} ({self.endpoint})"

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def udp_endpoint(self) -> str:
        return f"{self.host}:{self.udp_port or self.port + 1000}"

    @property
    def is_up(self) -> bool:
        return self.status == self.STATUS_UP


class NodeHealthSample(models.Model):
    node = models.ForeignKey(StorageNode, on_delete=models.CASCADE, related_name="samples")
    ok = models.BooleanField()
    rtt_ms = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class ChunkLocation(models.Model):
    """Which node holds a replica of which content-addressed blob (chunk or manifest)."""

    sha256 = models.CharField(max_length=64, db_index=True)
    node = models.ForeignKey(StorageNode, on_delete=models.CASCADE, related_name="replicas")
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("sha256", "node")]
