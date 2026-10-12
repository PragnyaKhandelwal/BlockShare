"""Django settings for BlockShare. Everything deploy-specific comes from environment variables."""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent
VAR_DIR = Path(os.environ.get("BLOCKSHARE_VAR_DIR", BASE_DIR / "var"))
VAR_DIR.mkdir(parents=True, exist_ok=True)


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-blockshare-secret-key-change-me")
DEBUG = env_bool("DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
    CSRF_TRUSTED_ORIGINS.append(f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "core",
    "accounts",
    "storage",
    "files",
    "coordination",
    "api",
    "web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "blockshare.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.system",
            ],
        },
    },
]

WSGI_APPLICATION = "blockshare.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{VAR_DIR / 'blockshare.sqlite3'}", conn_max_age=60
    )
}
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 20

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = VAR_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Uploads are streamed to a temp file and chunked from there, never held whole in memory.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_TEMP_DIR = None
BLOCKSHARE_MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "BlockShare REST API",
    "DESCRIPTION": "Distributed file sharing with switchable centralized (Django) or "
                   "decentralized (smart contract) coordination.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
}

# ----------------------------------------------------------------- BlockShare
# Storage nodes seeded at bootstrap: "id@host:port[/udp_port[/http_port]]", comma separated
BLOCKSHARE_NODES = os.environ.get(
    "BLOCKSHARE_NODES",
    "node-a@127.0.0.1:9101/10101/9301,node-b@127.0.0.1:9102/10102/9302,node-c@127.0.0.1:9103/10103/9303",
)
CLUSTER_KEY = os.environ.get("CLUSTER_KEY", "blockshare-dev-cluster-key").encode()
NODE_TIMEOUT_S = float(os.environ.get("NODE_TIMEOUT_S", "8"))
DEFAULT_CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", str(1024 * 1024)))
DEFAULT_REPLICATION_FACTOR = int(os.environ.get("REPLICATION_FACTOR", "2"))
TRANSFER_WORKERS = int(os.environ.get("TRANSFER_WORKERS", "4"))

BLOCKCHAIN_RPC_URL = os.environ.get("BLOCKCHAIN_RPC_URL", "http://127.0.0.1:8545")
# Hardhat / Anvil development account #0 - only ever valid on a local dev chain.
BLOCKCHAIN_OPERATOR_KEY = os.environ.get(
    "BLOCKCHAIN_OPERATOR_KEY", "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)
BLOCKCHAIN_DEFAULT_ON = env_bool("BLOCKCHAIN_DEFAULT_ON", False)
BLOCKCHAIN_EXPLORER_TX_URL = os.environ.get("BLOCKCHAIN_EXPLORER_TX_URL", "")
WALLET_ENCRYPTION_KEY = os.environ.get("WALLET_ENCRYPTION_KEY", "")
WALLET_FUNDING_ETH = float(os.environ.get("WALLET_FUNDING_ETH", "1"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"web3": {"level": "WARNING"}, "urllib3": {"level": "WARNING"}},
}

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", True)
    CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", True)
