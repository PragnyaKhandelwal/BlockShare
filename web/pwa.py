"""Progressive Web App endpoints: web manifest, service worker and offline page.

The service worker is served from the site root (not /static/) so that its scope
covers every page. Its cache version is derived from the content of the files it
precaches, so any change to the CSS/JS/icons ships a new worker automatically.
"""
import hashlib
import json

from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import get_template
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import cache_control

APP_NAME = "BlockShare"
APP_DESCRIPTION = "Distributed file storage with switchable Django or smart-contract coordination."
BACKGROUND = "#0b1222"

# static files the worker caches up front (paths relative to STATIC_URL)
PRECACHE_STATIC = [
    "css/app.css",
    "js/app.js",
    "img/favicon.svg",
    "img/icon.svg",
    "img/icon-192.png",
    "img/icon-512.png",
]


def _version():
    digest = hashlib.sha256()
    for path in PRECACHE_STATIC:
        found = finders.find(path)
        if found:
            with open(found, "rb") as fh:
                digest.update(fh.read())
    for name in ("pwa/sw.js", "pwa/offline.html"):
        digest.update(get_template(name).template.source.encode())
    return digest.hexdigest()[:12]


def manifest(request):
    icon = lambda path, size, purpose="any", kind="image/png": {  # noqa: E731
        "src": static(path), "sizes": size, "type": kind, "purpose": purpose}
    shortcut = lambda name, url: {"name": name, "short_name": name, "url": url,  # noqa: E731
                                  "icons": [icon("img/icon-192.png", "192x192")]}
    data = {
        "id": "/",
        "name": f"{APP_NAME} — Distributed File Storage",
        "short_name": APP_NAME,
        "description": APP_DESCRIPTION,
        "start_url": reverse("dashboard") + "?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": BACKGROUND,
        "theme_color": BACKGROUND,
        "categories": ["productivity", "utilities"],
        "icons": [
            icon("img/icon-192.png", "192x192"),
            icon("img/icon-512.png", "512x512"),
            icon("img/icon-maskable-512.png", "512x512", "maskable"),
            icon("img/icon.svg", "any", "any", "image/svg+xml"),
        ],
        "shortcuts": [
            shortcut("Upload", reverse("upload")),
            shortcut("My Files", reverse("files")),
            shortcut("Transfers", reverse("transfers")),
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json", json_dumps_params={"indent": 2})


@cache_control(no_cache=True, max_age=0)
def service_worker(request):
    body = get_template("pwa/sw.js").render({
        "version": _version(),
        "offline_url": reverse("offline"),
        "static_prefix": settings.STATIC_URL if settings.STATIC_URL.startswith("/") else "/" + settings.STATIC_URL,
        "precache": json.dumps([reverse("offline")] + [static(p) for p in PRECACHE_STATIC]),
    })
    return HttpResponse(body, content_type="application/javascript; charset=utf-8")


def offline(request):
    return render(request, "pwa/offline.html")
