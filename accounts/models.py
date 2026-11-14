import secrets

from django.conf import settings
from django.db import models


def generate_nonce() -> str:
    return secrets.token_hex(16)


class Profile(models.Model):
    """Every BlockShare user gets a custodial Ethereum wallet.

    In Blockchain mode the user's own key signs the transactions, so the smart
    contracts see the *user* (msg.sender) as the owner / sharer of a file - the
    Django server is not a privileged party on-chain.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    eth_address = models.CharField(max_length=42, unique=True)
    encrypted_private_key = models.TextField()
    metamask_address = models.CharField(max_length=42, unique=True, null=True, blank=True, db_index=True)
    login_nonce = models.CharField(max_length=64, default=generate_nonce)
    created_at = models.DateTimeField(auto_now_add=True)

    def rotate_nonce(self) -> str:
        self.login_nonce = generate_nonce()
        self.save(update_fields=["login_nonce"])
        return self.login_nonce

    def __str__(self):
        return f"{self.user.username} ({self.eth_address})"