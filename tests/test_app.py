"""Full-stack integration tests: services, coordinators, REST API and pages against a real node cluster.

Blockchain-mode tests run when a dev chain is reachable at BLOCKSHARE_TEST_RPC
(CI starts `npx hardhat node`; locally `python scripts/dev.py` does).
"""
import os
import unittest
import json
from unittest.mock import patch

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

    def restore_scenario(self):
        original = os.urandom(150_000)
        file = self.upload(self.alice, original)
        source = file.current
        services.upload_version(self.alice, file, SimpleUploadedFile("v2", b"edited report"))
        services.share(self.alice, file, "bob", "write")
        with self.assertRaises(CoordinationError):
            services.restore_version(self.bob, file, 1)
        with self.assertRaises(CoordinationError):
            services.restore_version(self.charlie, file, 1)

        with patch("storage.controller.StorageController.upload", side_effect=AssertionError("must reuse chunks")):
            restored = services.restore_version(self.alice, file, 1)
        file.refresh_from_db()
        self.assertEqual((restored.number, file.latest_version, file.versions.count()), (3, 3, 3))
        self.assertEqual(restored.author, self.alice)
        self.assertEqual(restored.comment, "Restored from v1")
        self.assertEqual(restored.merkle_root, source.merkle_root)
        self.assertNotEqual(restored.manifest_hash, source.manifest_hash)
        self.assertEqual(list(restored.chunks.values_list("sha256", flat=True)),
                         list(source.chunks.values_list("sha256", flat=True)))
        self.assertEqual(services.read_all(self.bob, file), original)
        self.assertEqual(services.read_all(self.alice, file, 1), original)
        self.assertEqual(services.read_all(self.alice, file, 2), b"edited report")

        from storage.controller import StorageController
        ctrl = StorageController()
        manifest, _ = ctrl.fetch(restored.manifest_hash, ctrl.locations([restored.manifest_hash])[restored.manifest_hash])
        self.assertEqual(json.loads(manifest)["version"], 3)
        self.assertEqual(json.loads(manifest)["merkle_root"], source.merkle_root)
        return file


class ConventionalModeTests(BaseCase):
    def test_restore_preserves_history_and_reuses_chunks(self):
        self.restore_scenario()

    def test_restore_rejects_invalid_current_and_tampered_versions(self):
        file = self.upload(self.alice, b"original")
        services.upload_version(self.alice, file, SimpleUploadedFile("v2", b"edited"))
        for number in (0, -1, None, True, "bad", 999, 2):
            with self.subTest(number=number), self.assertRaises(services.ServiceError):
                services.restore_version(self.alice, file, number)
        file.versions.filter(number=1).update(merkle_root="0" * 64)
        with self.assertRaisesMessage(services.ServiceError, "Integrity violation"):
            services.restore_version(self.alice, file, 1)
        file.refresh_from_db()
        self.assertEqual(file.latest_version, 2)
        self.assertEqual(file.versions.count(), 2)

    def test_restore_failure_does_not_change_latest_or_history(self):
        file = self.upload(self.alice, b"original")
        services.upload_version(self.alice, file, SimpleUploadedFile("v2", b"edited"))
        from storage.controller import StorageError
        for target, error in (
            ("storage.controller.StorageController.fetch", StorageError("source unavailable")),
            ("storage.controller.StorageController.store_blob", StorageError("manifest unavailable")),
            ("coordination.conventional.ConventionalCoordinator.add_version", CoordinationError("registration failed")),
        ):
            with self.subTest(target=target), patch(target, side_effect=error):
                with self.assertRaises((services.ServiceError, CoordinationError)):
                    services.restore_version(self.alice, file, 1)
            file.refresh_from_db()
            self.assertEqual(file.latest_version, 2)
            self.assertEqual(file.versions.count(), 2)
            self.assertEqual(services.read_all(self.alice, file), b"edited")

    def test_append_uses_latest_number_even_with_a_stale_file_object(self):
        file = self.upload(self.alice, b"original")
        stale = type(file).objects.get(pk=file.pk)
        services.upload_version(self.alice, file, SimpleUploadedFile("v2", b"edited"))
        # The caller still holds latest_version=1, as it would in an overlapping request.
        self.assertEqual(stale.latest_version, 1)
        self.assertEqual(services.restore_version(self.alice, stale, 1).number, 3)
        new = services.upload_version(self.alice, stale, SimpleUploadedFile("v4", b"newer"))
        self.assertEqual(new.number, 4)
        file.refresh_from_db()
        self.assertEqual(services.read_all(self.alice, file), b"newer")

    def test_restore_web_and_api(self):
        file = self.upload(self.alice, b"original")
        services.upload_version(self.alice, file, SimpleUploadedFile("v2", b"edited"))
        url = f"/files/{file.pk}/restore/"
        detail = f"/files/{file.pk}/"
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(detail + "?v=1"), "Restore v1")
        self.assertNotContains(self.client.get(detail), "Restore v2")
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url, {"version": "bad"})
        self.assertEqual(file.versions.count(), 2)
        response = self.client.post(url, {"version": 1}, follow=True)
        self.assertContains(response, "Restored v1 as v3")
        self.assertContains(response, "Download v3")
        services.share(self.alice, file, "bob", "write")
        self.client.force_login(self.bob)
        self.assertNotContains(self.client.get(detail + "?v=1"), "Restore v1")
        self.client.post(url, {"version": 1})
        self.assertEqual(file.versions.count(), 3)

        api = APIClient()
        api.force_authenticate(self.bob)
        endpoint = f"/api/files/{file.pk}/restore/"
        self.assertEqual(api.post(endpoint, {"version": 1}, format="json").status_code, 403)
        api.force_authenticate(self.alice)
        for payload in ({}, {"version": 0}, {"version": "bad"}, {"version": 999}, {"version": 3}):
            self.assertEqual(api.post(endpoint, payload, format="json").status_code, 400)
        response = api.post(endpoint, {"version": 2}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["number"], 4)
        download = api.get(f"/api/files/{file.pk}/download/")
        self.assertEqual(b"".join(download.streaming_content), b"edited")

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

    def test_public_landing_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Two ways to coordinate the same cluster")
        self.assertEqual(self.client.get("/dashboard/").status_code, 302)  # requires login

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
        self.assertEqual(self.client.get("/").status_code, 302)  # signed in -> dashboard
        for url in ["/dashboard/", "/files/", f"/files/{fid}/", "/upload/", "/shared-with-me/", "/versions/",
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

    def test_restore_registers_new_version_on_chain(self):
        file = self.restore_scenario()
        report = services.verify_file(self.alice, file)
        self.assertTrue(report["on_chain"]["root_valid"])
        self.assertTrue(all(report["on_chain"]["chunks"]))
        self.assertTrue(file.current.tx_hash)

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


class PwaTests(TransactionTestCase):
    """Installable-app endpoints: manifest, root-scoped service worker, offline page."""

    def test_manifest_is_installable(self):
        r = self.client.get("/manifest.webmanifest")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/manifest+json")
        data = r.json()
        self.assertEqual(data["display"], "standalone")
        self.assertEqual(data["scope"], "/")
        sizes = {i["sizes"] for i in data["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        self.assertTrue(any(i["purpose"] == "maskable" for i in data["icons"]))
        for icon in data["icons"]:
            self.assertTrue(icon["src"].startswith("/static/"))

    def test_service_worker_is_served_from_root(self):
        r = self.client.get("/sw.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r["Content-Type"])
        self.assertIn("no-cache", r["Cache-Control"])
        body = r.content.decode()
        self.assertNotIn("{{", body)
        self.assertIn('"/offline/"', body)
        self.assertIn("/static/css/app.css", body)

    def test_offline_page_is_public(self):
        r = self.client.get("/offline/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "You're offline")

    def test_pages_link_the_manifest(self):
        r = self.client.get("/login/")
        self.assertContains(r, 'rel="manifest"')
        self.assertContains(r, "apple-touch-icon")
