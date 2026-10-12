"""Full-stack integration tests: services, coordinators, REST API and pages against a real node cluster.

Blockchain-mode tests run when a dev chain is reachable at BLOCKSHARE_TEST_RPC
(CI starts `npx hardhat node`; locally `python scripts/dev.py` does).
"""
import os
import unittest

import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from coordination.base import CoordinationError
from coordination.merkle import merkle_proof, merkle_root, verify_proof
from core.models import SystemConfig
from files import services
from storage.models import StorageNode
from storage.monitor import check_nodes

from .cluster import CLUSTER_KEY, Cluster

TEST_RPC = os.environ.get("BLOCKSHARE_TEST_RPC", "http://127.0.0.1:8545")


def chain_available() -> bool:
    try:
        r = requests.post(TEST_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=2)
        return "result" in r.json()
    except Exception:
        return False


class MerkleTests(unittest.TestCase):
    def test_proofs_for_every_leaf(self):
        import hashlib

        for n in (1, 2, 3, 5, 8, 13):
            leaves = [hashlib.sha256(bytes([i])).hexdigest() for i in range(n)]
            root = merkle_root(leaves)
            for i, leaf in enumerate(leaves):
                self.assertTrue(verify_proof(leaf, merkle_proof(leaves, i), root))
            self.assertFalse(verify_proof(hashlib.sha256(b"x").hexdigest(), merkle_proof(leaves, 0), root) and n > 1)


@override_settings(CLUSTER_KEY=CLUSTER_KEY, TRANSFER_WORKERS=3)
class BaseCase(TransactionTestCase):
    cluster = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cluster = Cluster(3).start()

    @classmethod
    def tearDownClass(cls):
        cls.cluster.stop()
        super().tearDownClass()

    def setUp(self):
        for n in self.cluster.nodes:
            StorageNode.objects.create(node_id=n.node_id, host="127.0.0.1", port=n.port, udp_port=n.udp_port)
        SystemConfig.objects.update_or_create(pk=1, defaults={"chunk_size": 64 * 1024, "replication_factor": 2})
        check_nodes()
        User = get_user_model()
        self.alice = User.objects.create_user("alice", password="pw-alice-123")
        self.bob = User.objects.create_user("bob", password="pw-bob-123")
        self.charlie = User.objects.create_user("charlie", password="pw-charlie-123")

    def upload(self, user, data, name="report.bin"):
        return services.upload_file(user, SimpleUploadedFile(name, data))

    def scenario(self):
        """The same user story must work under either coordinator."""
        data = os.urandom(300_000)  # 5 chunks of 64 KB
        f = self.upload(self.alice, data)
        self.assertEqual(f.current.chunk_count, 5)
        self.assertEqual(services.read_all(self.alice, f), data)
        with self.assertRaises(CoordinationError):
            services.read_all(self.bob, f)

        services.share(self.alice, f, "bob", "read")
        self.assertEqual(services.read_all(self.bob, f), data)
        with self.assertRaises(CoordinationError):  # bob may not re-share
            services.share(self.bob, f, "charlie", "read")
        with self.assertRaises(CoordinationError):  # read-only cannot write
            services.upload_version(self.bob, f, SimpleUploadedFile("v2", b"v2"))

        services.share(self.alice, f, "bob", "write")
        v2 = services.upload_version(self.bob, f, SimpleUploadedFile("v2", b"version two"))
        self.assertEqual(v2.number, 2)
        f.refresh_from_db()
        self.assertEqual(services.read_all(self.alice, f), b"version two")
        self.assertEqual(services.read_all(self.alice, f, 1), data)

        services.revoke(self.alice, f, "bob")
        with self.assertRaises(CoordinationError):
            services.read_all(self.bob, f)

        services.transfer_ownership(self.alice, f, "charlie")
        f.refresh_from_db()
        self.assertEqual(f.owner, self.charlie)
        with self.assertRaises(CoordinationError):
            services.read_all(self.alice, f)
        self.assertEqual(services.read_all(self.charlie, f), b"version two")

        report = services.verify_file(self.charlie, f, 1)
        self.assertEqual(report["status"], "VALID")
        return f, data


class ConventionalModeTests(BaseCase):
    def test_full_user_story(self):
        f, _ = self.scenario()
        self.assertEqual(f.coordinator, "conventional")

    def test_replicas_and_deduplication(self):
        data = os.urandom(200_000)
        f1 = self.upload(self.alice, data, "a.bin")
        from storage.models import ChunkLocation

        for chunk in f1.current.chunks.all():
            self.assertEqual(ChunkLocation.objects.filter(sha256=chunk.sha256).count(), 2)
        f2 = self.upload(self.bob, data, "copy.bin")  # same content -> nothing re-sent
        self.assertEqual(f2.transfers.first().deduplicated, f2.current.chunk_count)

    def test_tamper_detection_and_self_healing(self):
        data = os.urandom(150_000)
        f = self.upload(self.alice, data)
        from storage.controller import StorageController

        chunk = f.current.chunks.first()
        holder = StorageController().locations([chunk.sha256])[chunk.sha256][0]
        StorageController().corrupt_replica(chunk.sha256, holder.node_id)
        report = services.verify_file(self.alice, f)
        self.assertEqual(report["status"], "MODIFIED")
        self.assertTrue(report["recoverable"])
        self.assertEqual(services.read_all(self.alice, f), data)  # failover + read repair
        self.assertEqual(services.verify_file(self.alice, f)["status"], "VALID")

    def test_node_failure_and_repair(self):
        data = os.urandom(260_000)
        f = self.upload(self.alice, data)
        from storage.controller import StorageController
        from storage_node.client import NodeClient

        victim = self.cluster.nodes[0]
        NodeClient(victim.tcp_endpoint, CLUSTER_KEY).admin(offline=True)
        try:
            check_nodes()
            check_nodes()
            self.assertEqual(StorageNode.objects.get(node_id=victim.node_id).status, "down")
            self.assertEqual(services.read_all(self.alice, f), data)
            report = StorageController().repair()
            self.assertEqual(report["lost"], [])
        finally:
            NodeClient(victim.tcp_endpoint, CLUSTER_KEY).admin(offline=False)

    def test_rest_api_and_pages(self):
        token = Token.objects.create(user=self.alice)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        r = api.post("/api/files/upload/", {"file": SimpleUploadedFile("api.txt", b"hello api" * 1000)},
                     format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        fid = r.json()["id"]
        r = api.post(f"/api/files/{fid}/share/", {"username": "bob", "permission": "read"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = api.get(f"/api/files/{fid}/download/")
        self.assertEqual(b"".join(r.streaming_content), b"hello api" * 1000)
        self.assertEqual(api.get("/api/files/").json()[0]["id"], fid)

        self.client.force_login(self.alice)
        for url in ["/", "/files/", f"/files/{fid}/", "/upload/", "/shared-with-me/", "/versions/",
                    "/transfers/", "/nodes/", "/architecture/", "/api/docs/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


@unittest.skipUnless(chain_available(), f"no dev blockchain at {TEST_RPC}")
@override_settings(BLOCKCHAIN_RPC_URL=TEST_RPC)
class BlockchainModeTests(BaseCase):
    def setUp(self):
        super().setUp()
        from coordination.blockchain import ensure_chain_ready
        from core.services import set_blockchain_mode

        ensure_chain_ready(force_redeploy=True)
        set_blockchain_mode(True)

    def test_full_user_story_on_chain(self):
        f, _ = self.scenario()
        from coordination.models import ChainTransaction

        functions = set(ChainTransaction.objects.filter(file=f, ok=True).values_list("function", flat=True))
        self.assertTrue({"registerFile", "shareFile", "addVersion", "revokeAccess", "transferOwnership"} <= functions)
        report = services.verify_file(self.charlie, f, 2)
        self.assertTrue(report["on_chain"]["root_valid"])
        self.assertTrue(all(report["on_chain"]["chunks"]))

    def test_contract_is_the_authority(self):
        """Even if Django's mirror is edited, the chain decides who may read."""
        f = self.upload(self.alice, b"secret" * 1000)
        from files.models import Share

        Share.objects.create(file=f, user=self.bob, permission="read")  # forged DB row
        with self.assertRaises(CoordinationError):
            services.read_all(self.bob, f)

    def test_switching_modes_anchors_conventional_state(self):
        from core.services import set_blockchain_mode

        set_blockchain_mode(False)
        f = self.upload(self.alice, b"made while chain was off" * 100)
        services.share(self.alice, f, "bob", "read")
        set_blockchain_mode(True)
        f.refresh_from_db()
        self.assertTrue(f.on_chain)
        self.assertEqual(services.read_all(self.bob, f), b"made while chain was off" * 100)
