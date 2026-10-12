from django.contrib import admin

from .models import Chunk, FileVersion, Share, StoredFile, Transfer


class VersionInline(admin.TabularInline):
    model = FileVersion
    extra = 0
    fields = ("number", "size", "chunk_count", "merkle_root", "tx_hash", "author")
    readonly_fields = fields


@admin.register(StoredFile)
class StoredFileAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "latest_version", "coordinator", "on_chain", "deleted", "updated_at")
    list_filter = ("coordinator", "on_chain", "deleted")
    inlines = [VersionInline]


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ("id", "direction", "file", "user", "mode", "bytes", "duration_ms", "status", "created_at")
    list_filter = ("direction", "mode", "status")


admin.site.register(Share)
admin.site.register(Chunk)
