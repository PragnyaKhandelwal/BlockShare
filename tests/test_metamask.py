import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import Client, TestCase
from django.urls import reverse
from eth_account import Account
from eth_account.messages import encode_defunct

from accounts.wallet import ensure_wallet

User = get_user_model()


class MetaMaskAuthTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.wallet = Account.create()
        self.address = self.wallet.address

    def _sign(self, text: str) -> str:
        return self.wallet.sign_message(encode_defunct(text=text)).signature.hex()

    def test_first_time_metamask_signin_and_replay_protection(self):
        nonce_url = reverse("metamask_nonce")
        verify_url = reverse("metamask_verify")

        res = self.client.get(f"{nonce_url}?address={self.address}&mode=login")
        self.assertEqual(res.status_code, 200)
        message = res.json()["message"]

        sig = self._sign(message)
        verify_res = self.client.post(
            verify_url,
            data=json.dumps({"address": self.address, "signature": sig}),
            content_type="application/json",
        )
        self.assertEqual(verify_res.status_code, 200)
        body = verify_res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["metamask_address"], self.address)
        self.assertNotEqual(body["custodial_address"], self.address)
        self.assertNotIn("token", body)

        # Reusing the same signature must fail because the session nonce was consumed
        replay_res = self.client.post(
            verify_url,
            data=json.dumps({"address": self.address, "signature": sig}),
            content_type="application/json",
        )
        self.assertEqual(replay_res.status_code, 401)

    def test_unlink_refused_without_usable_password_and_allowed_with_password(self):
        nonce_url = reverse("metamask_nonce")
        verify_url = reverse("metamask_verify")
        link_url = reverse("metamask_link")

        # 1. Sign in as a new MetaMask-only user (unusable password)
        msg = self.client.get(f"{nonce_url}?address={self.address}&mode=login").json()["message"]
        self.client.post(
            verify_url,
            data=json.dumps({"address": self.address, "signature": self._sign(msg)}),
            content_type="application/json",
        )

        # Attempting to unlink must be refused with 400 so the user isn't locked out
        unlink_res = self.client.post(
            link_url,
            data=json.dumps({"unlink": True}),
            content_type="application/json",
        )
        self.assertEqual(unlink_res.status_code, 400)
        self.assertIn("no password", unlink_res.json()["error"].lower())

        # 2. A user with a password CAN unlink
        alice = User.objects.create_user(username="alice", password="blockshare123")
        ensure_wallet(alice)
        other_wallet = Account.create()
        alice_client = Client()
        alice_client.login(username="alice", password="blockshare123")

        link_msg = alice_client.get(f"{nonce_url}?address={other_wallet.address}&mode=link").json()["message"]
        link_sig = other_wallet.sign_message(encode_defunct(text=link_msg)).signature.hex()
        alice_client.post(
            link_url,
            data=json.dumps({"address": other_wallet.address, "signature": link_sig}),
            content_type="application/json",
        )

        ok_unlink = alice_client.post(
            link_url,
            data=json.dumps({"unlink": True}),
            content_type="application/json",
        )
        self.assertEqual(ok_unlink.status_code, 200)
        alice.profile.refresh_from_db()
        self.assertIsNone(alice.profile.metamask_address)

    def test_third_party_nonce_request_cannot_interrupt_signin_or_linking(self):
        alice = User.objects.create_user(username="alice", password="blockshare123")
        profile = ensure_wallet(alice)
        profile.metamask_address = self.address
        profile.save(update_fields=["metamask_address"])

        nonce_url = reverse("metamask_nonce")
        verify_url = reverse("metamask_verify")

        # Real user requests a login nonce
        msg = self.client.get(f"{nonce_url}?address={self.address}&mode=login").json()["message"]

        # Attacker in a separate session repeatedly requests nonces for alice's address
        attacker = Client()
        attacker.get(f"{nonce_url}?address={self.address}&mode=login")
        attacker.get(f"{nonce_url}?address={self.address}&mode=login")

        # Real user's signature still succeeds
        res = self.client.post(
            verify_url,
            data=json.dumps({"address": self.address, "signature": self._sign(msg)}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["username"], "alice")

    def test_concurrent_first_signin_integrity_error_returns_clean_409(self):
        nonce_url = reverse("metamask_nonce")
        verify_url = reverse("metamask_verify")

        msg = self.client.get(f"{nonce_url}?address={self.address}&mode=login").json()["message"]
        sig = self._sign(msg)

        with patch("django.contrib.auth.models.UserManager.create_user", side_effect=IntegrityError("duplicate")):
            res = self.client.post(
                verify_url,
                data=json.dumps({"address": self.address, "signature": sig}),
                content_type="application/json",
            )
        self.assertEqual(res.status_code, 409)
        self.assertIn("error", res.json())
