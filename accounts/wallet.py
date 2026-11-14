"""Custodial wallet management (key generation, encryption at rest) and MetaMask auth."""
import base64
import hashlib

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import IntegrityError
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from .models import Profile


def _fernet() -> Fernet:
    secret = settings.WALLET_ENCRYPTION_KEY or settings.SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(("blockshare-wallet:" + secret).encode()).digest())
    return Fernet(key)


def ensure_wallet(user) -> Profile:
    try:
        return user.profile
    except Profile.DoesNotExist:
        pass
    acct = Account.create()
    token = _fernet().encrypt(acct.key.hex().encode()).decode()
    try:
        return Profile.objects.create(user=user, eth_address=acct.address, encrypted_private_key=token)
    except IntegrityError:  # created concurrently
        return Profile.objects.get(user=user)


def account_for(user):
    profile = ensure_wallet(user)
    key = _fernet().decrypt(profile.encrypted_private_key.encode()).decode()
    return Account.from_key(key)


def user_for_address(address: str):
    from django.contrib.auth import get_user_model

    if not address:
        return None
    return get_user_model().objects.filter(profile__eth_address__iexact=address).first()


def user_for_metamask_address(address: str):
    from django.contrib.auth import get_user_model

    if not address:
        return None
    return get_user_model().objects.filter(profile__metamask_address__iexact=address).first()


def build_metamask_challenge(address: str, nonce: str, action: str = "Sign in to") -> str:
    checksum_addr = Web3.to_checksum_address(address)
    return (
        f"{action} BlockShare\n\n"
        f"Wallet: {checksum_addr}\n"
        f"Nonce: {nonce}"
    )


def verify_metamask_signature(
    address: str, signature: str, nonce: str, action: str = "Sign in to"
) -> bool:
    try:
        checksum_addr = Web3.to_checksum_address(address)
        message_text = build_metamask_challenge(checksum_addr, nonce, action=action)
        message = encode_defunct(text=message_text)
        recovered = Account.recover_message(message, signature=signature)
        return Web3.to_checksum_address(recovered) == checksum_addr
    except Exception:
        return False