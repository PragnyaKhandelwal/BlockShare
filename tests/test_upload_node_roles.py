"""Upload-only history: direct attempts versus pipeline and replica evidence."""
import copy
import io
import os

from django.test import SimpleTestCase
from files import services
from files.models import Transfer
from storage.controller import StorageController, sha256_hex
from .test_app import BaseCase

class TransferRoleTests(SimpleTestCase):
    def test_legacy_records_are_not_rewritten_or_expanded_into_direct_contacts(self):
        detail = [
            {"node": "a", "op": "PUT", "endpoint": "host:1", "pipeline": ["a", "b", "c"], "stored_on": ["a", "b"]},
            {"node": "c", "op": "DEDUP", "status": "SKIPPED"},
            {"node": "unknown"}, None,
        ]
        original = copy.deepcopy(detail)
        transfer = Transfer(direction="upload", detail=detail)
        self.assertEqual(transfer.nodes_used, ["a"])
        self.assertEqual(transfer.acknowledged_nodes, ["a", "b"])
        self.assertEqual(transfer.existing_replica_nodes, [])
        self.assertFalse(transfer.log_entries[1]["contacted"])
        self.assertEqual(transfer.detail, original)


    def test_new_dedup_metadata_is_not_a_network_exchange_or_acknowledgement(self):
        transfer = Transfer(direction="upload", detail=[{"node": "a", "op": "DEDUP", "contacted": False,
                                     "existing_replicas": ["a", "b"]}])
        self.assertEqual(transfer.nodes_used, [])
        self.assertEqual(transfer.acknowledged_nodes, [])
        self.assertEqual(transfer.existing_replica_nodes, ["a", "b"])


    def test_empty_or_incomplete_history_has_no_invented_nodes(self):
        for detail in (None, [], {}, [None, "invalid", {"pipeline": ["a", "b"]}]):
            transfer = Transfer(detail=detail)
            self.assertEqual(transfer.nodes_used, [])
            self.assertEqual(transfer.acknowledged_nodes, [])


class TransferLoggingTests(BaseCase):
    def transfer(self, ctrl, direction="upload"):
        return Transfer.objects.create(user=self.alice, direction=direction, mode="conventional", detail=ctrl.log.items)


    def test_pipeline_head_is_the_only_direct_contact(self):
        ctrl = StorageController(replication_factor=3)
        nodes = list(ctrl.nodes.values())
        data = os.urandom(500)
        stored = ctrl._replicate(data, sha256_hex(data), set(), nodes, index=0)
        self.assertEqual(stored, {n.node_id for n in nodes})
        transfer = self.transfer(ctrl)
        self.assertEqual(transfer.nodes_used, [nodes[0].node_id])
        self.assertEqual(set(transfer.acknowledged_nodes), stored)
        self.assertEqual(transfer.detail[0]["pipeline"], [n.node_id for n in nodes])
        for node in nodes:
            self.assertEqual(ctrl.client(node).get(bytes.fromhex(sha256_hex(data))), data)


    def test_downstream_failure_does_not_turn_plans_into_acknowledgements(self):
        ctrl = StorageController(replication_factor=3)
        nodes = list(ctrl.nodes.values())
        victim = ctrl.client(nodes[1])
        victim.admin(offline=True)
        try:
            data = os.urandom(500)
            stored = ctrl._replicate(data, sha256_hex(data), set(), nodes, index=0)
            transfer = self.transfer(ctrl)
            self.assertEqual(stored, {nodes[0].node_id})
            self.assertEqual(transfer.nodes_used, [nodes[0].node_id])
            self.assertEqual(transfer.acknowledged_nodes, [nodes[0].node_id])
            self.assertEqual(len(transfer.detail[0]["pipeline"]), 3)
            self.assertTrue(transfer.detail[0]["forward_errors"])
        finally:
            victim.admin(offline=False)


    def test_failed_head_attempt_is_distinct_from_replica_storage(self):
        ctrl = StorageController(replication_factor=3)
        nodes = list(ctrl.nodes.values())
        victim = ctrl.client(nodes[0])
        victim.admin(offline=True)
        try:
            data = os.urandom(500)
            stored = ctrl._replicate(data, sha256_hex(data), set(), nodes, index=0)
            transfer = self.transfer(ctrl)
            self.assertEqual(transfer.nodes_used, [nodes[0].node_id, nodes[1].node_id])
            self.assertEqual(set(transfer.acknowledged_nodes), stored)
            self.assertNotIn(nodes[0].node_id, stored)
            self.assertEqual(transfer.detail[0]["pipeline"], [n.node_id for n in nodes])
        finally:
            victim.admin(offline=False)


    def test_dedup_records_existing_locations_without_claiming_contacts(self):
        data = os.urandom(100_000)
        self.upload(self.alice, data)
        ctrl = StorageController()
        result = ctrl.upload(io.BytesIO(data))
        self.assertEqual(result.deduplicated, len(result.chunks))
        transfer = self.transfer(ctrl)
        self.assertEqual(transfer.nodes_used, [])
        self.assertEqual(transfer.acknowledged_nodes, [])
        self.assertTrue(transfer.existing_replica_nodes)
        for entry in transfer.detail:
            self.assertEqual(entry["op"], "DEDUP")
            self.assertFalse(entry["contacted"])
            self.assertEqual(len(entry["existing_replicas"]), 2)


    def test_service_persistence_and_ui_labels(self):
        file = self.upload(self.alice, b"transfer observability" * 100)
        upload = file.transfers.get(direction="upload")
        original = copy.deepcopy(upload.detail)
        self.assertTrue(upload.nodes_used)
        self.assertTrue(upload.acknowledged_nodes)
        self.assertEqual(services.read_all(self.alice, file), b"transfer observability" * 100)
        download = file.transfers.get(direction="download")
        self.assertTrue(download.nodes_used)
        self.assertFalse(any("served" in e or "failover_from" in e for e in download.detail))
        self.client.force_login(self.alice)
        page = self.client.get(f"/transfers/{upload.pk}/")
        self.assertContains(page, "Coordinator contact attempts")
        self.assertContains(page, "Storage acknowledgements")
        self.assertContains(page, "Planned pipeline")
        self.assertNotContains(self.client.get(f"/transfers/{download.pk}/"), "Upload node roles")
        self.assertContains(self.client.get("/transfers/"), "direct coordinator contact attempts")
        upload.refresh_from_db()
        self.assertEqual(upload.detail, original)


