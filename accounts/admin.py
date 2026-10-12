from django.contrib import admin

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "eth_address", "created_at")
    exclude = ("encrypted_private_key",)
    readonly_fields = ("eth_address",)
