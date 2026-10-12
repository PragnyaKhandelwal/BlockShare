import json

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Sum
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from accounts.wallet import ensure_wallet
from coordination import get_coordinator
from coordination.base import CoordinationError
from coordination.models import ChainTransaction
from core.models import OperationMetric, SystemConfig
from core.services import set_blockchain_mode, update_settings
from files import services
from files.models import StoredFile, Transfer
from storage.controller import StorageController
from storage.models import ChunkLocation, StorageNode
from storage.monitor import check_nodes
from storage_node.client import NodeClient, NodeError

from . import serializers as s


def _error(exc, code=status.HTTP_400_BAD_REQUEST):
    return Response({"detail": str(exc)}, status=code)


# ---------------------------------------------------------------- auth

@extend_schema(request=s.RegisterSerializer, responses=s.UserSerializer)
@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    ser = s.RegisterSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    user = get_user_model().objects.create_user(**ser.validated_data)
    ensure_wallet(user)
    token, _ = Token.objects.get_or_create(user=user)
    return Response({**s.UserSerializer(user).data, "token": token.key}, status=status.HTTP_201_CREATED)


@extend_schema(responses=s.UserSerializer)
@api_view(["GET"])
def me(request):
    ensure_wallet(request.user)
    return Response(s.UserSerializer(request.user).data)


# ---------------------------------------------------------------- files

class FileViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                  viewsets.GenericViewSet):
    """Files owned by the caller. Authorization is delegated to the active coordinator."""

    serializer_class = s.FileSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        return StoredFile.objects.filter(deleted=False)

    def _file(self, pk):
        return services.accessible_file(self.request.user, pk)

    def list(self, request):
        try:
            files = get_coordinator().owned_files(request.user)
        except CoordinationError as exc:
            return _error(exc, 503)
        ctx = {"permissions": {f.pk: "owner" for f in files}}
        return Response(s.FileSerializer(files, many=True, context=ctx).data)

    def retrieve(self, request, pk=None):
        try:
            file, perm = self._file(pk)
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc, 404)
        return Response(s.FileDetailSerializer(file, context={"permissions": {file.pk: perm}}).data)

    def destroy(self, request, pk=None):
        try:
            file, _ = self._file(pk)
            services.delete(request.user, file)
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc, 403)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=s.UploadSerializer, responses=s.FileSerializer)
    @action(detail=False, methods=["post"])
    def upload(self, request):
        ser = s.UploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            file = services.upload_file(request.user, ser.validated_data["file"], ser.validated_data.get("comment", ""))
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc)
        return Response(s.FileSerializer(file, context={"permissions": {file.pk: "owner"}}).data,
                        status=status.HTTP_201_CREATED)

    @extend_schema(request=s.UploadSerializer, responses=s.VersionSerializer)
    @action(detail=True, methods=["post"])
    def versions(self, request, pk=None):
        ser = s.UploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            file, _ = self._file(pk)
            version = services.upload_version(request.user, file, ser.validated_data["file"],
                                              ser.validated_data.get("comment", ""))
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc)
        return Response(s.VersionSerializer(version).data, status=status.HTTP_201_CREATED)

    @extend_schema(responses={200: bytes})
    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        try:
            file, _ = self._file(pk)
            version, stream, info = services.open_download(request.user, file, request.query_params.get("version"))
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc, 403)
        resp = StreamingHttpResponse(stream, content_type=file.mime_type or "application/octet-stream")
        resp["Content-Disposition"] = f'attachment; filename="{file.name}"'
        resp["Content-Length"] = str(version.size)
        resp["X-BlockShare-Merkle-Root"] = info["root"]
        resp["X-BlockShare-Coordinator"] = info["mode"]
        return resp

    @extend_schema(request=s.ShareRequestSerializer, responses=s.ShareSerializer)
    @action(detail=True, methods=["post"])
    def share(self, request, pk=None):
        ser = s.ShareRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            file, _ = self._file(pk)
            share = services.share(request.user, file, ser.validated_data["username"], ser.validated_data["permission"])
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc)
        return Response(s.ShareSerializer(share).data)

    @extend_schema(request=s.UsernameSerializer, responses=None)
    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        ser = s.UsernameSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            file, _ = self._file(pk)
            services.revoke(request.user, file, ser.validated_data["username"])
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc)
        return Response({"revoked": ser.validated_data["username"]})

    @extend_schema(request=s.UsernameSerializer, responses=None)
    @action(detail=True, methods=["post"], url_path="transfer-ownership")
    def transfer_ownership(self, request, pk=None):
        ser = s.UsernameSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            file, _ = self._file(pk)
            services.transfer_ownership(request.user, file, ser.validated_data["username"])
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc)
        return Response({"owner": ser.validated_data["username"]})

    @extend_schema(responses=None)
    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        try:
            file, _ = self._file(pk)
            report = services.verify_file(request.user, file, request.data.get("version"))
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc)
        report["file"] = str(report["file"].pk)
        report["version"] = report["version"].number
        return Response(report)

    @extend_schema(responses=None)
    @action(detail=True, methods=["get"])
    def chunks(self, request, pk=None):
        try:
            file, _ = self._file(pk)
            version = services.get_version(file, request.query_params.get("version"))
        except (services.ServiceError, CoordinationError) as exc:
            return _error(exc, 404)
        locs = StorageController().locations(list(version.chunks.values_list("sha256", flat=True)))
        return Response([{**s.ChunkSerializer(c).data, "nodes": [n.node_id for n in locs.get(c.sha256, [])]}
                         for c in version.chunks.all()])


@extend_schema(responses=s.FileSerializer(many=True))
@api_view(["GET"])
def shared_with_me(request):
    try:
        pairs = get_coordinator().shared_files(request.user)
    except CoordinationError as exc:
        return _error(exc, 503)
    files = [f for f, _ in pairs]
    return Response(s.FileSerializer(files, many=True, context={"permissions": {f.pk: p for f, p in pairs}}).data)


# ---------------------------------------------------------------- cluster

class NodeViewSet(viewsets.ModelViewSet):
    queryset = StorageNode.objects.all()
    serializer_class = s.NodeSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdminUser()]

    @action(detail=False, methods=["post"])
    def refresh(self, request):
        check_nodes()
        return Response(s.NodeSerializer(StorageNode.objects.all(), many=True).data)

    @extend_schema(responses=None)
    @action(detail=True, methods=["post"])
    def simulate(self, request, pk=None):
        """Body: {"offline": true|false, "latency_ms": int} - network fault injection."""
        node = self.get_object()
        cmd = {k: request.data[k] for k in ("offline", "latency_ms") if k in request.data}
        try:
            state = NodeClient(node.endpoint, settings.CLUSTER_KEY, 3).admin(**cmd)
        except NodeError as exc:
            return _error(exc, 502)
        check_nodes()
        return Response(state)


class TransferViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = s.TransferSerializer

    def get_queryset(self):
        qs = Transfer.objects.select_related("user", "file")
        return qs if self.request.user.is_staff else qs.filter(user=self.request.user)


@extend_schema(request=s.SystemSerializer, responses=s.SystemSerializer)
@api_view(["GET", "PATCH"])
def system(request):
    if request.method == "PATCH":
        if not request.user.is_staff:
            return _error("Only administrators can change the system configuration.", 403)
        data = request.data
        try:
            if "blockchain_enabled" in data:
                set_blockchain_mode(str(data["blockchain_enabled"]).lower() in ("1", "true", "yes", "on"))
            update_settings(data.get("chunk_size"), data.get("replication_factor"),
                            None if "auto_repair" not in data else str(data["auto_repair"]).lower() in ("1", "true"))
        except Exception as exc:
            return _error(exc, 503)
    return Response(s.SystemSerializer(SystemConfig.get()).data)


@api_view(["POST"])
@permission_classes([IsAdminUser])
def maintenance(request, task):
    ctrl = StorageController()
    if task == "repair":
        return Response(ctrl.repair())
    if task == "gc":
        return Response(ctrl.garbage_collect())
    if task == "sync-chain":
        from coordination.blockchain import ensure_chain_ready

        try:
            return Response(ensure_chain_ready())
        except Exception as exc:
            return _error(exc, 503)
    return _error("unknown task", 404)


@api_view(["GET"])
def stats(request):
    by_mode = (OperationMetric.objects.values("op", "mode").annotate(avg_ms=Avg("duration_ms"), n=Count("id"))
               .order_by("op", "mode"))
    gas = ChainTransaction.objects.filter(ok=True).values("function").annotate(avg_gas=Avg("gas_used"), n=Count("id"))
    return Response({
        "files": StoredFile.objects.filter(deleted=False).count(),
        "nodes_up": StorageNode.objects.filter(status="up").count(),
        "nodes_total": StorageNode.objects.count(),
        "stored_bytes": ChunkLocation.objects.aggregate(b=Sum("size"))["b"] or 0,
        "transfers": Transfer.objects.count(),
        "chain_transactions": ChainTransaction.objects.count(),
        "latency_by_mode": list(by_mode),
        "gas_by_function": list(gas),
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def chain_config(request):
    """Public deployment info for wallets / the CLI (no secrets)."""
    config = SystemConfig.get()
    return Response({
        "mode": config.mode,
        "chain_id": config.chain_id,
        "rpc_proxy": request.build_absolute_uri("/rpc"),
        "contracts": {"NodeRegistry": config.node_registry_address, "FileRegistry": config.file_registry_address,
                      "FileSharing": config.file_sharing_address},
    })


# ---------------------------------------------------------------- read-only JSON-RPC proxy

RPC_READ_METHODS = {
    "eth_chainId", "net_version", "eth_blockNumber", "eth_call", "eth_getLogs", "eth_getBlockByNumber",
    "eth_getBlockByHash", "eth_getTransactionByHash", "eth_getTransactionReceipt", "eth_getBalance",
    "eth_getCode", "eth_gasPrice", "eth_estimateGas", "web3_clientVersion", "eth_getTransactionCount",
}


@csrf_exempt
@require_POST
def rpc_proxy(request):
    """Lets anyone verify on-chain state (ownership, grants, Merkle roots) without trusting Django's database.

    Only read methods are forwarded - the proxy can never be used to send transactions.
    """
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                            status=400)
    calls = payload if isinstance(payload, list) else [payload]
    if len(calls) > 50:
        return JsonResponse({"error": "batch too large"}, status=400)
    for c in calls:
        if c.get("method") not in RPC_READ_METHODS:
            return JsonResponse({"jsonrpc": "2.0", "id": c.get("id"),
                                 "error": {"code": -32601, "message": f"method {c.get('method')} not allowed"}},
                                status=403)
    try:
        upstream = requests.post(settings.BLOCKCHAIN_RPC_URL, json=payload, timeout=15)
    except requests.RequestException as exc:
        return JsonResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": str(exc)}},
                            status=502)
    return JsonResponse(upstream.json(), safe=False)
