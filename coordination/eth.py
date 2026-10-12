"""Thin web3.py layer: contract artifacts, deployment, signed transactions, funding."""
from __future__ import annotations

import json
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
CONTRACT_NAMES = ("NodeRegistry", "FileRegistry", "FileSharing")


class ChainUnavailable(Exception):
    """The blockchain RPC endpoint cannot be reached."""


class ChainTxError(Exception):
    """A transaction was rejected (reverted) by a smart contract."""


@lru_cache(maxsize=None)
def artifact(name: str) -> dict:
    return json.loads((CONTRACTS_DIR / f"{name}.json").read_text())


def revert_reason(exc: Exception) -> str:
    text = getattr(exc, "message", None) or str(exc)
    for pattern in (r"reason string '([^']+)'", r"execution reverted: ([^\"'\)]+)", r"revert(?:ed)?:? ([^\"'\)]+)"):
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return text


_nonce_locks: dict[str, threading.Lock] = {}
_nonce_guard = threading.Lock()


def _lock_for(address: str) -> threading.Lock:
    with _nonce_guard:
        return _nonce_locks.setdefault(address.lower(), threading.Lock())


class ChainClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
        self.operator = Account.from_key(settings.BLOCKCHAIN_OPERATOR_KEY)
        self._chain_id: int | None = None

    # ------------------------------------------------------------ basics
    def ping(self) -> bool:
        try:
            self.w3.eth.block_number
            return True
        except Exception:
            return False

    @property
    def chain_id(self) -> int:
        if self._chain_id is None:
            try:
                self._chain_id = self.w3.eth.chain_id
            except Exception as exc:
                raise ChainUnavailable(f"Blockchain RPC {self.rpc_url} unreachable: {exc}") from exc
        return self._chain_id

    def has_code(self, address: str) -> bool:
        if not address:
            return False
        try:
            return len(self.w3.eth.get_code(Web3.to_checksum_address(address))) > 0
        except Exception as exc:
            raise ChainUnavailable(str(exc)) from exc

    def contract(self, name: str, address: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=artifact(name)["abi"])

    def balance_eth(self, address: str) -> float:
        return float(Web3.from_wei(self.w3.eth.get_balance(Web3.to_checksum_address(address)), "ether"))

    # ------------------------------------------------------------ writes
    def _send(self, account, tx: dict):
        with _lock_for(account.address):
            tx.setdefault("nonce", self.w3.eth.get_transaction_count(account.address, "pending"))
            tx.setdefault("chainId", self.chain_id)
            signed = account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60, poll_latency=0.1)

    def deploy(self, name: str, *args) -> str:
        art = artifact(name)
        factory = self.w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"])
        with _lock_for(self.operator.address):
            tx = factory.constructor(*args).build_transaction({
                "from": self.operator.address,
                "nonce": self.w3.eth.get_transaction_count(self.operator.address, "pending"),
                "chainId": self.chain_id,
            })
        receipt = self._send(self.operator, tx)
        if receipt.status != 1:
            raise ChainTxError(f"deployment of {name} failed")
        return receipt.contractAddress

    def transact(self, account, fn, *, contract: str, function: str, user=None, file=None):
        """Sign `fn` with `account`, send it, wait for the receipt and log it."""
        from .models import ChainTransaction

        start = time.perf_counter()
        try:
            with _lock_for(account.address):
                tx = fn.build_transaction({
                    "from": account.address,
                    "nonce": self.w3.eth.get_transaction_count(account.address, "pending"),
                    "chainId": self.chain_id,
                })
                signed = account.sign_transaction(tx)
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60, poll_latency=0.1)
        except ContractLogicError as exc:
            reason = revert_reason(exc)
            ChainTransaction.objects.create(contract=contract, function=function, sender=account.address,
                                            user=user, file=file, ok=False, error=reason,
                                            latency_ms=(time.perf_counter() - start) * 1000)
            raise ChainTxError(reason) from exc
        except (ConnectionError, OSError) as exc:
            raise ChainUnavailable(str(exc)) from exc
        except Exception as exc:
            if "revert" in str(exc).lower():
                raise ChainTxError(revert_reason(exc)) from exc
            if "connection" in str(exc).lower() or "timeout" in str(exc).lower():
                raise ChainUnavailable(str(exc)) from exc
            raise
        ok = receipt.status == 1
        ChainTransaction.objects.create(
            tx_hash=Web3.to_hex(receipt.transactionHash), contract=contract, function=function,
            sender=account.address, user=user, file=file, block_number=receipt.blockNumber,
            gas_used=receipt.gasUsed, ok=ok, latency_ms=(time.perf_counter() - start) * 1000,
            error="" if ok else "reverted",
        )
        if not ok:
            raise ChainTxError(f"{contract}.{function} reverted")
        return receipt

    def fund(self, address: str, min_eth: float = 0.2) -> bool:
        """Top up a user wallet from the operator account so it can pay gas."""
        if self.balance_eth(address) >= min_eth:
            return False
        tx = {
            "to": Web3.to_checksum_address(address),
            "value": Web3.to_wei(settings.WALLET_FUNDING_ETH, "ether"),
            "gas": 21000,
            "gasPrice": self.w3.eth.gas_price,
        }
        receipt = self._send(self.operator, tx)
        return receipt.status == 1


_client: ChainClient | None = None
_client_lock = threading.Lock()


def get_chain() -> ChainClient:
    global _client
    with _client_lock:
        if _client is None or _client.rpc_url != settings.BLOCKCHAIN_RPC_URL:
            _client = ChainClient(settings.BLOCKCHAIN_RPC_URL)
        return _client


def to_bytes32(hex_str: str) -> bytes:
    h = hex_str[2:] if hex_str.startswith("0x") else hex_str
    return bytes.fromhex(h.rjust(64, "0"))
