from .models import SystemConfig


def system(request):
    try:
        config = SystemConfig.get()
    except Exception:  # database not migrated yet
        return {}
    wallet = None
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        wallet = getattr(getattr(user, "profile", None), "eth_address", None)
    return {"system": config, "wallet_address": wallet}
