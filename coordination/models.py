from django.conf import settings
from django.db import models


class ChainTransaction(models.Model):
    """Every transaction BlockShare sends to the blockchain (for the explorer & gas analytics)."""

    tx_hash = models.CharField(max_length=66, blank=True, db_index=True)
    contract = models.CharField(max_length=40)
    function = models.CharField(max_length=60)
    sender = models.CharField(max_length=42)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    file = models.ForeignKey("files.StoredFile", on_delete=models.SET_NULL, null=True, blank=True)
    block_number = models.PositiveBigIntegerField(null=True, blank=True)
    gas_used = models.PositiveBigIntegerField(null=True, blank=True)
    latency_ms = models.FloatField(default=0)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
