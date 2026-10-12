from django.conf import settings
from django.db import models


class Profile(models.Model):
    """Every BlockShare user gets a custodial Ethereum wallet.

    In Blockchain mode the user's own key signs the transactions, so the smart
    contracts see the *user* (msg.sender) as the owner / sharer of a file - the
    Django server is not a privileged party on-chain.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    eth_address = models.CharField(max_length=42, unique=True)
    encrypted_private_key = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} ({self.eth_address})"
