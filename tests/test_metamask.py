import json

from django.contrib.auth import get_user_model
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

        # Reusing the same signature must fail after nonce rotation
        replay_res = self.client.post(
            verify_url,
            data=json.dumps({"address": self.address, "signature": sig}),
            content_type="application/json",
        )
        self.assertEqual(replay_res.status_code, 401)

    def test_link_existing_user_and_sign_in(self):
        alice = User.objects.create_user(username="alice", password="blockshare123")
        ensure_wallet(alice)
        self.client.login(username="alice", password="blockshare123")

        nonce_url = reverse("metamask_nonce")
        link_url = reverse("metamask_link")
        verify_url = reverse("metamask_verify")

        nonce_res = self.client.get(f"{nonce_url}?address={self.address}&mode=link")
        sig = self._sign(nonce_res.json()["message"])

        link_res = self.client.post(
            link_url,
            data=json.dumps({"address": self.address, "signature": sig}),
            content_type="application/json",
        )
        self.assertEqual(link_res.status_code, 200)
        alice.profile.refresh_from_db()
        self.assertEqual(alice.profile.metamask_address, self.address)

        self.client.logout()
        login_nonce = self.client.get(f"{nonce_url}?address={self.address}&mode=login").json()["message"]
        login_sig = self._sign(login_nonce)
        login_res = self.client.post(
            verify_url,
            data=json.dumps({"address": self.address, "signature": login_sig}),
            content_type="application/json",
        )
        self.assertEqual(login_res.status_code, 200)
        self.assertEqual(login_res.json()["username"], "alice")