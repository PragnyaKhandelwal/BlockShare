from django.contrib import admin

from .models import ChainTransaction


@admin.register(ChainTransaction)
class ChainTransactionAdmin(admin.ModelAdmin):
    list_display = ("function", "contract", "user", "block_number", "gas_used", "ok", "created_at")
    list_filter = ("contract", "function", "ok")
