"""Prepare a BlockShare deployment: database, users, storage nodes, contracts, demo data.

Idempotent - safe to run on every container start.
"""
import io
import os
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand

from accounts.wallet import ensure_wallet
from core.models import SystemConfig
from storage.models import StorageNode

DEMO_USERS = ("alice", "bob", "charlie")


def parse_nodes(spec: str):
    """'node-a@host:9101/10101/9301,...' -> [(id, host, port, udp, http)]"""
    out = []
    for item in filter(None, (s.strip() for s in spec.split(","))):
        node_id, _, rest = item.partition("@")
        addr, *ports = rest.split("/")
        host, _, port = addr.rpartition(":")
        udp = int(ports[0]) if len(ports) > 0 and ports[0] else None
        http = int(ports[1]) if len(ports) > 1 and ports[1] else None
        out.append((node_id, host, int(port), udp, http))
    return out


def demo_document() -> bytes:
    lines = ["BlockShare - Modular Distributed File-Sharing System", "=" * 52, ""]
    para = ("Files are split into chunks, hashed with SHA-256, distributed over storage nodes "
            "with a consistent-hashing ring and replicated through a TCP pipeline. The control "
            "plane is either Django (conventional mode) or Ethereum smart contracts (blockchain mode). ")
    for i in range(1, 9000):
        lines.append(f"[{i:05d}] {para}")
    return "\n".join(lines).encode()


class Command(BaseCommand):
    help = "Migrate, create users, register storage nodes, deploy contracts and seed demo data."

    def add_arguments(self, parser):
        parser.add_argument("--no-demo", action="store_true", help="do not create demo users/files")
        parser.add_argument("--wait", type=int, default=int(os.environ.get("BOOTSTRAP_WAIT_S", "30")),
                            help="seconds to wait for storage nodes / blockchain")

    def handle(self, *args, **opts):
        call_command("migrate", interactive=False, verbosity=0)
        self.stdout.write("database migrated")
        config = SystemConfig.get()
        User = get_user_model()

        admin_password = os.environ.get("ADMIN_PASSWORD", "admin12345")
        admin, created = User.objects.get_or_create(username="admin", defaults={
            "is_staff": True, "is_superuser": True, "email": "admin@blockshare.local"})
        if created:
            admin.set_password(admin_password)
            admin.save()
            self.stdout.write("created superuser 'admin'")
        ensure_wallet(admin)

        demo = not opts["no_demo"] and os.environ.get("DEMO_SEED", "1") not in ("0", "false", "False")
        if demo:
            for name in DEMO_USERS:
                user, created = User.objects.get_or_create(username=name, defaults={"email": f"{name}@blockshare.local"})
                if created:
                    user.set_password(os.environ.get("DEMO_PASSWORD", "blockshare123"))
                    user.save()
                ensure_wallet(user)

        for node_id, host, port, udp, http in parse_nodes(settings.BLOCKSHARE_NODES):
            node, created = StorageNode.objects.update_or_create(
                node_id=node_id, defaults={"host": host, "port": port, "udp_port": udp, "http_port": http})
            if created:
                self.stdout.write(f"registered storage node {node}")

        from storage.monitor import check_nodes

        deadline = time.time() + opts["wait"]
        while True:
            nodes = check_nodes()
            up = [n for n in nodes if n.is_up]
            if len(up) == len(nodes) or time.time() > deadline:
                break
            time.sleep(1)
        self.stdout.write(f"storage nodes up: {len(up)}/{len(nodes)}")

        self._setup_chain(config, opts["wait"])

        if demo and not os.environ.get("SKIP_DEMO_FILES") and up:
            self._seed_files()
        self.stdout.write(self.style.SUCCESS("bootstrap complete"))

    def _setup_chain(self, config, wait):
        from coordination.blockchain import ensure_chain_ready
        from coordination.eth import get_chain

        chain = get_chain()
        deadline = time.time() + wait
        while not chain.ping() and time.time() < deadline:
            time.sleep(1)
        if not chain.ping():
            self.stdout.write(self.style.WARNING(
                f"blockchain RPC {settings.BLOCKCHAIN_RPC_URL} unreachable - running in Conventional mode only"))
            return
        report = ensure_chain_ready()
        self.stdout.write(f"blockchain ready on chain {get_chain().chain_id}: {report}")
        for user in get_user_model().objects.all():
            chain.fund(ensure_wallet(user).eth_address)
        if settings.BLOCKCHAIN_DEFAULT_ON and not SystemConfig.get().blockchain_enabled:
            from core.services import set_blockchain_mode

            set_blockchain_mode(True)
            self.stdout.write("blockchain mode enabled")

    def _seed_files(self):
        from files import services
        from files.models import StoredFile

        User = get_user_model()
        alice = User.objects.filter(username="alice").first()
        if alice is None or StoredFile.objects.exists():
            return
        try:
            doc = services.upload_file(alice, SimpleUploadedFile(
                "blockshare-overview.txt", demo_document(), content_type="text/plain"),
                comment="demo file created at bootstrap")
            services.share(alice, doc, "bob", "read")
            notes = io.BytesIO(b"# Lab notes\n\nChunk size, replication factor and coordination mode "
                               b"can be changed by the admin at runtime.\n")
            services.upload_file(alice, SimpleUploadedFile("lab-notes.md", notes.getvalue(),
                                                           content_type="text/markdown"))
            self.stdout.write("seeded demo files for alice (one shared with bob)")
        except Exception as exc:  # demo data is optional
            self.stdout.write(self.style.WARNING(f"could not seed demo files: {exc}"))
