import json
import math

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, Sum
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.wallet import ensure_wallet
from coordination import get_coordinator
from coordination.base import CoordinationError
from coordination.eth import CONTRACT_NAMES, artifact, get_chain
from coordination.merkle import merkle_proof
from coordination.models import ChainTransaction
from core.models import OperationMetric, SystemConfig
from core.services import set_blockchain_mode, update_settings
from files import services
from files.models import FileVersion, StoredFile, Transfer
from storage.controller import StorageController
from storage.models import ChunkLocation, StorageNode
from storage.monitor import check_nodes
from storage.ring import HashRing
from storage_node.client import NodeClient, NodeError

from .forms import NodeForm, RegisterForm, SettingsForm, ShareForm, UploadForm, UsernameForm


NODE_COLORS = ["#4f46e5", "#0891b2", "#d97706", "#16a34a", "#db2777", "#7c3aed", "#0d9488", "#b45309"]


def _coordinator_or_message(request):
    try:
        return get_coordinator()
    except CoordinationError as exc:
        messages.error(request, str(exc))
        return None


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


# ---------------------------------------------------------------- auth

def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        profile = ensure_wallet(user)
        login(request, user)
        messages.success(request, f"Welcome {user.username}! Your wallet address is {profile.eth_address}.")
        return redirect("dashboard")
    return render(request, "registration/register.html", {"form": form})


# ---------------------------------------------------------------- dashboard

@login_required
def dashboard(request):
    coord = _coordinator_or_message(request)
    owned, shared = [], []
    if coord:
        try:
            owned = coord.owned_files(request.user)
            shared = coord.shared_files(request.user)
        except CoordinationError as exc:
            messages.error(request, str(exc))
    nodes = list(StorageNode.objects.all())
    per_node = dict(ChunkLocation.objects.values_list("node__node_id").annotate(b=Sum("size")))
    ctx = {
        "owned_rows": [(f, "owner") for f in owned[:6]],
        "owned_count": len(owned),
        "shared_count": len(shared),
        "nodes": nodes,
        "nodes_up": sum(1 for n in nodes if n.is_up),
        "stored_bytes": ChunkLocation.objects.aggregate(b=Sum("size"))["b"] or 0,
        "transfers": Transfer.objects.filter(user=request.user).select_related("file")[:6],
        "chain_txs": ChainTransaction.objects.select_related("user")[:5],
        "chart": json.dumps({"labels": [n.node_id for n in nodes],
                             "bytes": [per_node.get(n.node_id, 0) for n in nodes]}),
        "coordinator": coord,
    }
    return render(request, "web/dashboard.html", ctx)


# ---------------------------------------------------------------- files

@login_required
def file_list(request):
    coord = _coordinator_or_message(request)
    files = []
    if coord:
        try:
            files = coord.owned_files(request.user)
        except CoordinationError as exc:
            messages.error(request, str(exc))
    return render(request, "web/files.html", {"rows": [(f, "owner") for f in files], "coordinator": coord})


@login_required
def shared_with_me(request):
    coord = _coordinator_or_message(request)
    pairs = []
    if coord:
        try:
            pairs = coord.shared_files(request.user)
        except CoordinationError as exc:
            messages.error(request, str(exc))
    return render(request, "web/shared.html", {"pairs": pairs, "coordinator": coord})


@login_required
def upload(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        if form.is_valid():
            try:
                file = services.upload_file(request.user, form.cleaned_data["file"], form.cleaned_data["comment"])
            except (services.ServiceError, CoordinationError) as exc:
                if _is_ajax(request):
                    return JsonResponse({"ok": False, "error": str(exc)}, status=400)
                messages.error(request, str(exc))
            else:
                t = file.transfers.order_by("-created_at").first()
                msg = (f"Uploaded {file.name}: {file.current.chunk_count} chunk(s), "
                       f"Merkle root {file.current.merkle_root[:16]}...")
                if t and t.deduplicated:
                    msg += f" ({t.deduplicated} chunk(s) deduplicated)"
                messages.success(request, msg)
                url = file_url(file)
                return JsonResponse({"ok": True, "redirect": url}) if _is_ajax(request) else redirect(url)
        elif _is_ajax(request):
            return JsonResponse({"ok": False, "error": form.errors.as_text()}, status=400)
    return render(request, "web/upload.html", {"form": form, "max_mb": settings.BLOCKSHARE_MAX_UPLOAD_MB})


def file_url(file):
    from django.urls import reverse

    return reverse("file_detail", args=[file.pk])


def _load(request, pk):
    try:
        return services.accessible_file(request.user, pk)
    except (services.ServiceError, CoordinationError) as exc:
        messages.error(request, str(exc))
        raise Http404(str(exc))


@login_required
def file_detail(request, pk):
    file, perm = _load(request, pk)
    coord = get_coordinator()
    try:
        number = int(request.GET.get("v") or file.latest_version)
    except ValueError:
        number = file.latest_version
    version = get_object_or_404(FileVersion, file=file, number=number)
    ctrl = StorageController()
    chunks = list(version.chunks.all())
    locs = ctrl.locations([c.sha256 for c in chunks] + [version.manifest_hash])
    leaves = [c.sha256 for c in chunks]
    chunk_rows = [{"chunk": c, "nodes": locs.get(c.sha256, []), "proof": merkle_proof(leaves, c.index)}
                  for c in chunks]
    try:
        state = coord.file_state(file)
    except Exception as exc:  # chain hiccup - still render the page
        state = {"source": coord.source, "error": str(exc)}
    node_ids = [n.node_id for n in ctrl.nodes.values()]
    legend = sorted(ctrl.nodes.values(), key=lambda n: n.node_id)
    for i, n in enumerate(legend):
        n.color = NODE_COLORS[i % len(NODE_COLORS)]
    ctx = {
        "nodes_legend": legend,
        "file": file, "perm": perm, "version": version, "versions": file.versions.all(),
        "chunk_rows": chunk_rows, "manifest_nodes": locs.get(version.manifest_hash, []),
        "state": state, "coordinator": coord, "node_ids": node_ids,
        "share_form": ShareForm(), "user_form": UsernameForm(), "upload_form": UploadForm(),
        "verify": request.session.pop(f"verify:{file.pk}", None),
        "chain_txs": ChainTransaction.objects.filter(file=file)[:10],
        "transfers": file.transfers.select_related("user")[:8],
        "explorer_tx": settings.BLOCKCHAIN_EXPLORER_TX_URL,
        "users": get_user_model().objects.exclude(pk=request.user.pk).order_by("username"),
    }
    return render(request, "web/file_detail.html", ctx)


@login_required
def download(request, pk):
    file, _ = _load(request, pk)
    try:
        version, stream, info = services.open_download(request.user, file, request.GET.get("v"))
    except (services.ServiceError, CoordinationError) as exc:
        messages.error(request, str(exc))
        return redirect("file_detail", pk=pk)
    resp = StreamingHttpResponse(stream, content_type=file.mime_type or "application/octet-stream")
    resp["Content-Disposition"] = f'attachment; filename="{file.name}"'
    resp["Content-Length"] = str(version.size)
    resp["X-BlockShare-Merkle-Root"] = info["root"]
    resp["X-BlockShare-Coordinator"] = info["mode"]
    return resp


def _action(request, pk, fn, success):
    file, _ = _load(request, pk)
    try:
        fn(file)
        messages.success(request, success)
    except (services.ServiceError, CoordinationError) as exc:
        messages.error(request, str(exc))
    return redirect("file_detail", pk=pk)


@login_required
@require_POST
def file_share(request, pk):
    form = ShareForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid share request.")
        return redirect("file_detail", pk=pk)
    u, p = form.cleaned_data["username"], form.cleaned_data["permission"]
    return _action(request, pk, lambda f: services.share(request.user, f, u, p), f"Shared with {u} ({p}).")


@login_required
@require_POST
def file_revoke(request, pk):
    u = request.POST.get("username", "")
    return _action(request, pk, lambda f: services.revoke(request.user, f, u), f"Access of {u} revoked.")


@login_required
@require_POST
def file_transfer(request, pk):
    u = request.POST.get("username", "")
    file, _ = _load(request, pk)
    try:
        services.transfer_ownership(request.user, file, u)
        messages.success(request, f"Ownership of {file.name} transferred to {u}.")
    except (services.ServiceError, CoordinationError) as exc:
        messages.error(request, str(exc))
        return redirect("file_detail", pk=pk)
    return redirect("files")


@login_required
@require_POST
def file_delete(request, pk):
    file, _ = _load(request, pk)
    try:
        services.delete(request.user, file)
        messages.success(request, f"{file.name} deleted.")
    except (services.ServiceError, CoordinationError) as exc:
        messages.error(request, str(exc))
        return redirect("file_detail", pk=pk)
    return redirect("files")


@login_required
@require_POST
def file_new_version(request, pk):
    file, _ = _load(request, pk)
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose a file to upload.")
        return redirect("file_detail", pk=pk)
    try:
        v = services.upload_version(request.user, file, form.cleaned_data["file"], form.cleaned_data["comment"])
        messages.success(request, f"Version {v.number} registered (root {v.merkle_root[:16]}...).")
    except (services.ServiceError, CoordinationError) as exc:
        messages.error(request, str(exc))
    return redirect("file_detail", pk=pk)


@login_required
@require_POST
def file_verify(request, pk):
    file, _ = _load(request, pk)
    try:
        report = services.verify_file(request.user, file, request.POST.get("v"))
    except (services.ServiceError, CoordinationError) as exc:
        messages.error(request, str(exc))
        return redirect("file_detail", pk=pk)
    report["file"] = str(file.pk)
    report["version"] = report["version"].number
    report["checked_at"] = report["checked_at"].isoformat()
    request.session[f"verify:{file.pk}"] = report
    return redirect(f"{file_url(file)}?v={report['version']}#integrity")


@staff_member_required
@require_POST
def file_corrupt(request, pk):
    """Fault injection: flip bits of one replica to demonstrate tamper detection & self-healing."""
    file, _ = _load(request, pk)
    sha, node_id = request.POST.get("sha256", ""), request.POST.get("node", "")
    try:
        ok = StorageController().corrupt_replica(sha, node_id)
        if ok:
            messages.warning(request, f"Replica of chunk {sha[:12]}... on {node_id} was corrupted. "
                                      f"Run 'Verify integrity' to detect it.")
        else:
            messages.error(request, "That node does not hold this chunk.")
    except (KeyError, NodeError) as exc:
        messages.error(request, f"Could not corrupt replica: {exc}")
    return redirect(f"{file_url(file)}?v={request.POST.get('v', '')}#chunks")


@login_required
def versions(request):
    coord = _coordinator_or_message(request)
    files = []
    if coord:
        try:
            files = coord.owned_files(request.user) + [f for f, _ in coord.shared_files(request.user)]
        except CoordinationError as exc:
            messages.error(request, str(exc))
    rows = (FileVersion.objects.filter(file__in=files).select_related("file", "author")
            .order_by("-created_at")[:200])
    return render(request, "web/versions.html", {"versions": rows})


# ---------------------------------------------------------------- transfers

@login_required
def transfers(request):
    qs = Transfer.objects.select_related("user", "file")
    if not request.user.is_staff:
        qs = qs.filter(user=request.user)
    items = list(qs[:100])
    chart = {
        "labels": [t.created_at.strftime("%H:%M:%S") for t in reversed(items[:30])],
        "mbps": [t.throughput_mbps for t in reversed(items[:30])],
        "dir": [t.direction for t in reversed(items[:30])],
    }
    return render(request, "web/transfers.html", {"transfers": items, "chart": json.dumps(chart)})


@login_required
def transfer_detail(request, pk):
    t = get_object_or_404(Transfer, pk=pk)
    if not request.user.is_staff and t.user_id != request.user.id:
        raise Http404
    return render(request, "web/transfer_detail.html", {"t": t})


# ---------------------------------------------------------------- nodes

def _topology(nodes):
    cx, cy, r = 260, 190, 140
    out = []
    for i, n in enumerate(nodes):
        angle = -math.pi / 2 + 2 * math.pi * i / max(1, len(nodes))
        out.append({"node": n, "x": round(cx + r * math.cos(angle)), "y": round(cy + r * math.sin(angle))})
    return out


@login_required
def nodes(request):
    if request.GET.get("refresh"):
        check_nodes()
        return redirect("nodes")
    node_list = list(StorageNode.objects.all())
    counts = dict(ChunkLocation.objects.values_list("node__node_id").annotate(c=Count("id")))
    sizes = dict(ChunkLocation.objects.values_list("node__node_id").annotate(b=Sum("size")))
    ring = HashRing([n.node_id for n in node_list if n.is_active and n.status != StorageNode.STATUS_DOWN])
    ownership = ring.ownership()
    rtt = {}
    for n in node_list:
        samples = list(n.samples.all()[:40])[::-1]
        rtt[n.node_id] = [s.rtt_ms if s.ok else None for s in samples]
    labels = list(range(-len(max(rtt.values(), key=len, default=[])) + 1, 1))
    for n in node_list:
        n.replica_count = counts.get(n.node_id, 0)
        n.replica_bytes = sizes.get(n.node_id, 0)
        n.ring_share = ownership.get(n.node_id, 0)
    return render(request, "web/nodes.html", {
        "nodes": node_list, "topology": _topology(node_list), "form": NodeForm(),
        "rtt_chart": json.dumps({"labels": labels, "series": rtt}),
        "replication_factor": SystemConfig.get().replication_factor,
    })


@staff_member_required
@require_POST
def node_add(request):
    form = NodeForm(request.POST)
    if form.is_valid():
        node = form.save()
        check_nodes()
        if SystemConfig.get().blockchain_enabled:
            try:
                from coordination.blockchain import sync_nodes

                sync_nodes()
            except Exception as exc:
                messages.warning(request, f"Node added but not registered on-chain: {exc}")
        messages.success(request, f"Storage node {node.node_id} added at {node.endpoint}.")
    else:
        messages.error(request, f"Invalid node: {form.errors.as_text()}")
    return redirect("nodes")


@staff_member_required
@require_POST
def node_action(request, pk, op):
    node = get_object_or_404(StorageNode, pk=pk)
    client = NodeClient(node.endpoint, settings.CLUSTER_KEY, 3)
    try:
        if op == "fail":
            client.admin(offline=True)
            messages.warning(request, f"{node.node_id} is simulating a crash (stops serving data and heartbeats).")
        elif op == "recover":
            client.admin(offline=False)
            messages.success(request, f"{node.node_id} recovered.")
        elif op == "latency":
            ms = int(request.POST.get("latency_ms") or 0)
            client.admin(latency_ms=ms)
            messages.info(request, f"{node.node_id}: artificial latency set to {ms} ms.")
        elif op == "toggle":
            node.is_active = not node.is_active
            node.save(update_fields=["is_active"])
            messages.info(request, f"{node.node_id} {'accepts' if node.is_active else 'no longer accepts'} new chunks.")
        elif op == "remove":
            node.delete()
            messages.warning(request, f"{node.node_id} removed. Run 'Repair' to restore the replication factor.")
            return redirect("nodes")
    except NodeError as exc:
        messages.error(request, f"{node.node_id} unreachable: {exc}")
    check_nodes()
    return redirect("nodes")


# ---------------------------------------------------------------- blockchain

def _event_rows(chain, config, limit=40):
    from accounts.wallet import user_for_address

    rows = []
    latest = chain.w3.eth.block_number
    start = max(0, latest - 5000)
    addresses = {"NodeRegistry": config.node_registry_address, "FileRegistry": config.file_registry_address,
                 "FileSharing": config.file_sharing_address}
    names = {f.chain_id: f.name for f in StoredFile.objects.all()}
    for cname, addr in addresses.items():
        if not addr:
            continue
        contract = chain.contract(cname, addr)
        for ev in [e["name"] for e in artifact(cname)["abi"] if e["type"] == "event"]:
            for log in getattr(contract.events, ev)().get_logs(from_block=start):
                args = {}
                for k, v in log["args"].items():
                    if isinstance(v, (bytes, bytearray)):
                        v = "0x" + bytes(v).hex()
                        if k == "fileId" and v in names:
                            v = f"{names[v]} ({v[:10]}...)"
                    elif isinstance(v, str) and v.startswith("0x") and len(v) == 42:
                        u = user_for_address(v)
                        v = f"{u.username} ({v[:8]}...)" if u else v
                    args[k] = v
                rows.append({"block": log["blockNumber"], "contract": cname, "event": ev, "args": args,
                             "tx": "0x" + bytes(log["transactionHash"]).hex(), "idx": log["logIndex"]})
    rows.sort(key=lambda r: (r["block"], r["idx"]), reverse=True)
    return rows[:limit]


@login_required
def blockchain(request):
    config = SystemConfig.get()
    chain = get_chain()
    ctx = {"config": config, "rpc_url": settings.BLOCKCHAIN_RPC_URL, "connected": chain.ping()}
    if ctx["connected"]:
        try:
            w3 = chain.w3
            latest = w3.eth.block_number
            blocks = []
            for n in range(latest, max(-1, latest - 8), -1):
                b = w3.eth.get_block(n)
                blocks.append({"number": n, "hash": "0x" + bytes(b["hash"]).hex(), "txs": len(b["transactions"]),
                               "gas": b["gasUsed"], "ts": b["timestamp"]})
            users = []
            for u in get_user_model().objects.select_related("profile").order_by("username"):
                addr = ensure_wallet(u).eth_address
                users.append({"user": u, "address": addr, "balance": chain.balance_eth(addr)})
            ctx.update({
                "chain_id": chain.chain_id, "latest": latest, "blocks": blocks, "wallets": users,
                "client": w3.client_version,
                "events": _event_rows(chain, config) if config.file_registry_address else [],
                "contracts": [(name, getattr(config, field)) for name, field in
                              zip(CONTRACT_NAMES, ("node_registry_address", "file_registry_address",
                                                   "file_sharing_address"))],
            })
        except Exception as exc:
            ctx["error"] = str(exc)
    ctx["txs"] = ChainTransaction.objects.select_related("user", "file")[:30]
    gas = list(ChainTransaction.objects.filter(ok=True).values("function")
               .annotate(avg=Avg("gas_used"), n=Count("id"), lat=Avg("latency_ms")).order_by("function"))
    ctx["gas"] = gas
    ctx["comparison"] = _comparison()
    return render(request, "web/blockchain.html", ctx)


def _comparison():
    rows = (OperationMetric.objects.filter(ok=True).values("op", "mode")
            .annotate(avg=Avg("duration_ms"), n=Count("id")).order_by("op"))
    ops = {}
    for r in rows:
        ops.setdefault(r["op"], {})[r["mode"]] = {"avg": round(r["avg"], 2), "n": r["n"]}
    labels = sorted(ops)
    return {
        "rows": [(op, ops[op].get("conventional"), ops[op].get("blockchain")) for op in labels],
        "chart": json.dumps({
            "labels": labels,
            "conventional": [ops[o].get("conventional", {}).get("avg") for o in labels],
            "blockchain": [ops[o].get("blockchain", {}).get("avg") for o in labels],
        }),
    }


# ---------------------------------------------------------------- system

@login_required
def architecture(request):
    return render(request, "web/architecture.html")


@staff_member_required
def system_settings(request):
    config = SystemConfig.get()
    form = SettingsForm(request.POST or None, initial={
        "chunk_size_kb": config.chunk_size // 1024, "replication_factor": config.replication_factor,
        "auto_repair": config.auto_repair})
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in ("mode_on", "mode_off"):
                report = set_blockchain_mode(action == "mode_on")
                if action == "mode_on":
                    messages.success(request, "Blockchain mode ON - smart contracts now coordinate ownership, "
                                              f"permissions and versions. Sync report: {report}")
                else:
                    messages.success(request, "Conventional mode ON - Django is the coordinator again.")
            elif action == "save" and form.is_valid():
                update_settings(form.cleaned_data["chunk_size_kb"] * 1024, form.cleaned_data["replication_factor"],
                                form.cleaned_data["auto_repair"])
                messages.success(request, "Settings saved. They apply to new uploads.")
            elif action == "repair":
                check_nodes()
                messages.success(request, f"Repair finished: {StorageController().repair()}")
            elif action == "gc":
                messages.success(request, f"Garbage collection: {StorageController().garbage_collect()}")
            elif action == "sync_chain":
                from coordination.blockchain import ensure_chain_ready

                messages.success(request, f"Blockchain synchronised: {ensure_chain_ready()}")
            elif action == "redeploy":
                from coordination.blockchain import ensure_chain_ready

                messages.success(request, f"Contracts redeployed: {ensure_chain_ready(force_redeploy=True)}")
        except Exception as exc:
            messages.error(request, f"{action} failed: {exc}")
        return redirect("settings")
    return render(request, "web/settings.html", {"form": form, "config": config,
                                                 "chain_ok": get_chain().ping()})
