from django.contrib.auth import views as auth_views
from django.urls import path

from . import pwa, views

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("register/", views.register, name="register"),
    path("", views.home, name="home"),
    path("manifest.webmanifest", pwa.manifest, name="manifest"),
    path("sw.js", pwa.service_worker, name="service_worker"),
    path("offline/", pwa.offline, name="offline"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("files/", views.file_list, name="files"),
    path("files/<uuid:pk>/", views.file_detail, name="file_detail"),
    path("files/<uuid:pk>/download/", views.download, name="file_download"),
    path("files/<uuid:pk>/share/", views.file_share, name="file_share"),
    path("files/<uuid:pk>/revoke/", views.file_revoke, name="file_revoke"),
    path("files/<uuid:pk>/transfer/", views.file_transfer, name="file_transfer"),
    path("files/<uuid:pk>/delete/", views.file_delete, name="file_delete"),
    path("files/<uuid:pk>/versions/", views.file_new_version, name="file_new_version"),
    path("files/<uuid:pk>/verify/", views.file_verify, name="file_verify"),
    path("files/<uuid:pk>/corrupt/", views.file_corrupt, name="file_corrupt"),
    path("upload/", views.upload, name="upload"),
    path("shared-with-me/", views.shared_with_me, name="shared"),
    path("versions/", views.versions, name="versions"),
    path("transfers/", views.transfers, name="transfers"),
    path("transfers/<int:pk>/", views.transfer_detail, name="transfer_detail"),
    path("nodes/", views.nodes, name="nodes"),
    path("nodes/add/", views.node_add, name="node_add"),
    path("nodes/<int:pk>/<slug:op>/", views.node_action, name="node_action"),
    path("blockchain/", views.blockchain, name="blockchain"),
    path("architecture/", views.architecture, name="architecture"),
    path("settings/", views.system_settings, name="settings"),
]
