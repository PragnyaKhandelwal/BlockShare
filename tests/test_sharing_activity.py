from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from coordination.base import CoordinationError
from coordination.merkle import merkle_root
from files import services
from files.models import FileVersion, Share, SharingActivity, StoredFile


class SharingActivityTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.alice = users.objects.create_user("alice")
        self.bob = users.objects.create_user("bob")
        self.charlie = users.objects.create_user("charlie")
        self.file = StoredFile.objects.create(name="report.txt", owner=self.alice,
                                              coordinator="conventional", latest_version=1)
        FileVersion.objects.create(file=self.file, number=1, size=0, chunk_size=65536, chunk_count=0,
                                   merkle_root=merkle_root([]), manifest_hash="0" * 64, author=self.alice)

    def test_grant_change_revoke_and_no_op(self):
        services.share(self.alice, self.file, "bob", "read")
        services.share(self.alice, self.file, "bob", "read")
        self.assertEqual(self.file.sharing_activity.count(), 1)
        services.share(self.alice, self.file, "bob", "write")
        services.revoke(self.alice, self.file, "bob")
        events = list(services.sharing_activity(self.alice, self.file))
        self.assertEqual([(e.action, e.previous_permission, e.permission) for e in events], [
            ("revoked", "write", "none"), ("permission_changed", "read", "write"), ("shared", "none", "read")])
        self.assertFalse(Share.objects.filter(file=self.file).exists())
        self.assertTrue(all(e.actor_name == "alice" and e.target_name == "bob" for e in events))

    def test_failed_and_unauthorized_requests_do_not_create_events(self):
        services.share(self.alice, self.file, "bob", "write")
        for call in (
            lambda: services.share(self.bob, self.file, "charlie", "read"),
            lambda: services.revoke(self.bob, self.file, "bob"),
            lambda: services.revoke(self.alice, self.file, "charlie"),
            lambda: services.share(self.alice, self.file, "missing", "read"),
            lambda: services.share(self.alice, self.file, "charlie", "invalid"),
            lambda: services.share(self.alice, self.file, "alice", "read"),
        ):
            with self.assertRaises((CoordinationError, services.ServiceError)):
                call()
        with patch("coordination.conventional.ConventionalCoordinator.share_file",
                   side_effect=CoordinationError("operation failed")):
            with self.assertRaises(CoordinationError):
                services.share(self.alice, self.file, "charlie", "read")
        self.assertEqual(self.file.sharing_activity.count(), 1)

    def test_event_failure_rolls_back_local_permission_changes(self):
        with patch("files.services.SharingActivity.objects.create", side_effect=IntegrityError("log failed")):
            with self.assertRaises(IntegrityError):
                services.share(self.alice, self.file, "bob", "read")
        self.assertFalse(Share.objects.exists())
        services.share(self.alice, self.file, "bob", "read")
        for call in (lambda: services.share(self.alice, self.file, "bob", "write"),
                     lambda: services.revoke(self.alice, self.file, "bob")):
            with patch("files.services.SharingActivity.objects.create", side_effect=IntegrityError("log failed")):
                with self.assertRaises(IntegrityError):
                    call()
            self.assertEqual(Share.objects.get(file=self.file).permission, "read")
            self.assertEqual(self.file.sharing_activity.count(), 1)

    def test_names_survive_account_rename_and_deletion(self):
        services.share(self.alice, self.file, "bob", "read")
        self.bob.username = "renamed-bob"
        self.bob.save()
        self.bob.delete()
        services.transfer_ownership(self.alice, self.file, "charlie")
        self.alice.delete()
        event = SharingActivity.objects.get(file=self.file)
        self.assertEqual((event.actor_name, event.target_name), ("alice", "bob"))
        self.assertIsNone(event.actor)
        self.assertIsNone(event.target)
        self.file.refresh_from_db()
        self.assertEqual(services.sharing_activity(self.charlie, self.file).count(), 1)

    def test_history_visibility_follows_current_ownership(self):
        services.share(self.alice, self.file, "bob", "write")
        with self.assertRaises(CoordinationError):
            services.sharing_activity(self.bob, self.file)
        services.transfer_ownership(self.alice, self.file, "charlie")
        self.file.refresh_from_db()
        with self.assertRaises(CoordinationError):
            services.sharing_activity(self.alice, self.file)
        self.assertEqual(services.sharing_activity(self.charlie, self.file).count(), 1)

    def test_api_and_web_changes_share_one_history(self):
        api = APIClient()
        api.force_authenticate(self.alice)
        url = f"/api/files/{self.file.pk}/"
        self.assertEqual(api.post(url + "share/", {"username": "bob", "permission": "read"}).status_code, 200)
        self.client.force_login(self.alice)
        self.client.post(f"/files/{self.file.pk}/share/", {"username": "bob", "permission": "write"})
        self.assertEqual(api.post(url + "revoke/", {"username": "bob"}).status_code, 200)
        response = api.get(url + "activity/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)
        self.assertEqual(response.json()["results"][0]["action"], "revoked")
        self.assertEqual(api.post(url + "activity/", {}).status_code, 405)
        page = self.client.get(f"/files/{self.file.pk}/")
        self.assertContains(page, "Sharing activity")
        self.assertContains(page, "revoked")
        self.assertContains(page, "permission changes")

    def test_shared_users_cannot_read_activity_and_other_files_are_excluded(self):
        services.share(self.alice, self.file, "bob", "read")
        other = StoredFile.objects.create(name="other.txt", owner=self.alice, coordinator="conventional")
        services.share(self.alice, other, "charlie", "read")
        api = APIClient()
        url = f"/api/files/{self.file.pk}/activity/"
        self.assertIn(api.get(url).status_code, (401, 403))
        for user in (self.bob, self.charlie):
            api.force_authenticate(user)
            self.assertEqual(api.get(url).status_code, 403)
        self.client.force_login(self.bob)
        self.assertNotContains(self.client.get(f"/files/{self.file.pk}/"), 'id="sharing-activity"')
        api.force_authenticate(self.alice)
        self.assertEqual(api.get(url).json()["count"], 1)

    def test_pagination_and_escaped_usernames(self):
        self.bob.username = "<script>alert(1)</script>"
        self.bob.save()
        for i in range(27):
            services.share(self.alice, self.file, self.bob.username, "read" if i % 2 == 0 else "write")
        self.client.force_login(self.alice)
        url = f"/files/{self.file.pk}/"
        page = self.client.get(url)
        self.assertEqual(len(page.context["sharing_activity"]), 25)
        self.assertContains(page, "Older activity")
        self.assertContains(page, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(page, "<script>alert(1)</script>")
        older = self.client.get(url, {"activity_page": 2})
        self.assertEqual(len(older.context["sharing_activity"]), 2)
        self.assertContains(older, "Newer activity")
        api = APIClient()
        api.force_authenticate(self.alice)
        result = api.get(f"/api/files/{self.file.pk}/activity/?limit=5&offset=5").json()
        self.assertEqual(result["count"], 27)
        self.assertEqual(len(result["results"]), 5)


class RemoteSharingActivityTests(TransactionTestCase):
    def test_remote_coordinator_calls_are_not_wrapped_in_a_database_transaction(self):
        # Exercise the external-coordinator boundary without a live chain.
        from coordination.conventional import ConventionalCoordinator

        owner = get_user_model().objects.create_user("owner")
        target = get_user_model().objects.create_user("target")
        file = StoredFile.objects.create(name="remote.txt", owner=owner, coordinator="external")
        coord = ConventionalCoordinator()
        coord.mode = "external"
        share, revoke = coord.share_file, coord.revoke_access

        def checked(fn):
            def call(*args):
                self.assertFalse(connection.in_atomic_block)
                return fn(*args)
            return call

        with patch("files.services.get_coordinator", return_value=coord), \
             patch.object(coord, "share_file", side_effect=checked(share)), \
             patch.object(coord, "revoke_access", side_effect=checked(revoke)):
            services.share(owner, file, target.username, "read")
            services.revoke(owner, file, target.username)
        self.assertEqual(file.sharing_activity.count(), 2)
