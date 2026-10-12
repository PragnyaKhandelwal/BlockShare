from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("files", views.FileViewSet, basename="file")
router.register("nodes", views.NodeViewSet, basename="node")
router.register("transfers", views.TransferViewSet, basename="transfer")

urlpatterns = [
    path("auth/token/", obtain_auth_token, name="api-token"),
    path("auth/register/", views.register, name="api-register"),
    path("auth/me/", views.me, name="api-me"),
    path("shared-with-me/", views.shared_with_me, name="api-shared"),
    path("system/", views.system, name="api-system"),
    path("system/<slug:task>/", views.maintenance, name="api-maintenance"),
    path("stats/", views.stats, name="api-stats"),
    path("chain/", views.chain_config, name="api-chain"),
    path("", include(router.urls)),
]
