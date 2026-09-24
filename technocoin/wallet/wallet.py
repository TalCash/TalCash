"""A TechnoCoin wallet: one 24-word passphrase, any number of addresses.

The wallet file (JSON) keeps the passphrase encrypted with your password. The
list of addresses is stored in the clear so they can be shown without the
password; anything that uses a key (signing, adding an address) re-derives the
addresses and checks them against that list first.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.params import NETWORKS, NetworkParams
from ..core.tx import Output, Transfer
from ..crypto import hd, keys, mnemonic
from ..crypto.address import decode_address, encode_address, payload_from_public_key
from . import keystore
from .keystore import MODERATE, KdfStrength

FORMAT = "technocoin-wallet"
VERSION = 1


class WalletError(Exception):
    pass


@dataclass(frozen=True)
class WalletAddress:
    index: int
    address: str


class Wallet:
    def __init__(
        self,
        path: Path,
        params: NetworkParams,
        encrypted: dict,
        addresses: list[WalletAddress],
        created: int,
    ) -> None:
        self.path = Path(path)
        self.params = params
        self._encrypted = encrypted
        self._addresses = addresses
        self.created = created

    # --- creating and opening ------------------------------------------------

    @classmethod
    def create(
        cls,
        path: Path | str,
        params: NetworkParams,
        password: str,
        *,
        words: int = 24,
        extra_passphrase: str = "",
        strength: KdfStrength = MODERATE,
    ) -> tuple["Wallet", str]:
        """Make a new wallet. Returns it and the passphrase the user must write down."""
        phrase = mnemonic.generate(words)
        return cls._new(Path(path), params, phrase, password, extra_passphrase, strength), phrase

    @classmethod
    def restore(
        cls,
        path: Path | str,
        params: NetworkParams,
        phrase: str,
        password: str,
        *,
        extra_passphrase: str = "",
        strength: KdfStrength = MODERATE,
    ) -> "Wallet":
        """Rebuild a wallet from its passphrase."""
        mnemonic.mnemonic_to_entropy(phrase)  # raises ValueError explaining what's wrong
        return cls._new(Path(path), params, mnemonic.normalize(phrase), password, extra_passphrase, strength)

    @classmethod
    def _new(
        cls, path: Path, params: NetworkParams, phrase: str, password: str, extra: str, strength: KdfStrength
    ) -> "Wallet":
        if path.exists():
            raise WalletError(f"{path} already exists; refusing to overwrite a wallet")
        secret = json.dumps({"mnemonic": phrase, "extra_passphrase": extra}).encode("utf-8")
        encrypted = keystore.encrypt(secret, password, strength)
        seed = mnemonic.to_seed(phrase, extra)
        wallet = cls(path, params, encrypted, [WalletAddress(0, _derive_address(seed, 0, params))], int(time.time()))
        wallet.save()
        return wallet

    @classmethod
    def load(cls, path: Path | str) -> "Wallet":
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise WalletError(f"no wallet at {path}") from None
        if data.get("format") != FORMAT or data.get("version") != VERSION:
            raise WalletError(f"{path} is not a TechnoCoin wallet file (version {VERSION})")
        if data.get("network") not in NETWORKS:
            raise WalletError(f"unknown network {data.get('network')!r} in {path}")
        addresses = [WalletAddress(int(a["index"]), a["address"]) for a in data["addresses"]]
        return cls(path, NETWORKS[data["network"]], data["encrypted"], addresses, int(data["created"]))

    def save(self) -> None:
        """Write atomically: a crash mid-write never leaves a half-written wallet."""
        data = {
            "format": FORMAT,
            "version": VERSION,
            "network": self.params.name,
            "created": self.created,
            "addresses": [{"index": a.index, "address": a.address} for a in self._addresses],
            "encrypted": self._encrypted,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    # --- reading ------------------------------------------------------------

    @property
    def addresses(self) -> list[WalletAddress]:
        return list(self._addresses)

    def _secret(self, password: str) -> tuple[str, str]:
        data = json.loads(keystore.decrypt(self._encrypted, password))
        return data["mnemonic"], data["extra_passphrase"]

    def _verified_seed(self, password: str) -> bytes:
        phrase, extra = self._secret(password)
        seed = mnemonic.to_seed(phrase, extra)
        for entry in self._addresses:
            if _derive_address(seed, entry.index, self.params) != entry.address:
                raise WalletError(f"address #{entry.index} in the wallet file does not match its keys (file edited?)")
        return seed

    def reveal_passphrase(self, password: str) -> str:
        return self._secret(password)[0]

    def check_password(self, password: str) -> None:
        self._verified_seed(password)

    # --- using keys ---------------------------------------------------------

    def new_address(self, password: str) -> WalletAddress:
        seed = self._verified_seed(password)
        index = max(a.index for a in self._addresses) + 1
        entry = WalletAddress(index, _derive_address(seed, index, self.params))
        self._addresses.append(entry)
        self.save()
        return entry

    def private_key(self, password: str, index: int) -> bytes:
        if index not in {a.index for a in self._addresses}:
            raise WalletError(f"the wallet has no address #{index}")
        return hd.wallet_private_key(self._verified_seed(password), index)

    def sign_transfer(
        self,
        password: str,
        *,
        index: int,
        nonce: int,
        fee: int,
        outputs: list[tuple[str, int]],
        memo: bytes = b"",
    ) -> Transfer:
        """Build and sign a transfer from address #index. `outputs` are (text address, base units)."""
        private_key = self.private_key(password, index)
        prefix = self.params.address_prefix
        unsigned = Transfer(
            network_id=self.params.network_id,
            sender_public_key=keys.public_key(private_key),
            nonce=nonce,
            fee=fee,
            outputs=tuple(Output(decode_address(address, prefix), amount) for address, amount in outputs),
            memo=memo,
        )
        return unsigned.sign(private_key)


def _derive_address(seed: bytes, index: int, params: NetworkParams) -> str:
    public_key = keys.public_key(hd.wallet_private_key(seed, index))
    return encode_address(payload_from_public_key(public_key), params.address_prefix)
