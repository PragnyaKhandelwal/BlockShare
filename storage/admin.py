from django.contrib import admin

from .models import ChunkLocation, NodeHealthSample, StorageNode


@admin.register(StorageNode)
class StorageNodeAdmin(admin.ModelAdmin):
    list_display = ("node_id", "endpoint", "status", "last_rtt_ms", "chunk_count", "bytes_used", "is_active")


@admin.register(ChunkLocation)
class ChunkLocationAdmin(admin.ModelAdmin):
    list_display = ("sha256", "node", "size", "created_at")
    list_filter = ("node",)


admin.site.register(NodeHealthSample)
