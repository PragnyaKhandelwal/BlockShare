from django.contrib import admin

from .models import OperationMetric, SystemConfig


@admin.register(SystemConfig)
class SystemConfigAdmin(admin.ModelAdmin):
    list_display = ("mode", "chunk_size", "replication_factor", "auto_repair", "chain_id", "updated_at")


@admin.register(OperationMetric)
class OperationMetricAdmin(admin.ModelAdmin):
    list_display = ("op", "mode", "duration_ms", "ok", "created_at")
    list_filter = ("mode", "op", "ok")
