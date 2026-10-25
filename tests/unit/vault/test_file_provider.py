"""Fournisseur `file` (nokker/secrets/file.py) : le `vault.bin` du premier lancement, repris tel quel."""
from __future__ import annotations

import json
import os
import stat

import pytest
from cryptography.fernet import Fernet

from nokker.local.setup import LocalVault, VaultError
from nokker.secrets.base import SecretsError
from nokker.secrets.file import META_KEY, FileSecrets
from tests.unit.vault.fakes import FAKE_A, FAKE_B, FAKE_C, fernet_key


@pytest.fixture
def key(monkeypatch):
    k = fernet_key()
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", k)
    return k


def test_round_trip_and_list_without_values(tmp_path, key):
    s = FileSecrets(tmp_path / "vault.bin")
    info = s.set("GITHUB_TOKEN", FAKE_A)
    assert info.name == "GITHUB_TOKEN" and info.updated_at is not None
    s.set("db.prod-pw", FAKE_B)
    assert s.get("GITHUB_TOKEN") == FAKE_A and s.get("absent") is None
    listed = s.list()
    assert [i.name for i in listed] == ["GITHUB_TOKEN", "db.prod-pw"]
    assert all(i.updated_at is not None for i in listed)
    assert FAKE_A not in repr(listed) and FAKE_A not in json.dumps([i.as_dict() for i in listed])
    raw = (tmp_path / "vault.bin").read_bytes()
    assert FAKE_A.encode() not in raw and b"GITHUB_TOKEN" not in raw  # chiffré d'un bloc, noms compris
    assert stat.S_IMODE(os.stat(tmp_path / "vault.bin").st_mode) == 0o600
    assert s.delete("GITHUB_TOKEN") and not s.delete("GITHUB_TOKEN")
    assert [i.name for i in s.list()] == ["db.prod-pw"]


def test_same_file_and_format_as_the_first_launch_vault(tmp_path, key):
    """Pas de second format : un `vault.bin` d'avant X4 ({nom: valeur}) se relit sans conversion, et ce qu'on écrit
    reste un objet {clé: chaîne} lisible par le code d'avant."""
    path = tmp_path / "vault.bin"
    path.write_bytes(Fernet(key.encode()).encrypt(json.dumps({"router": FAKE_A, "byok:perso": FAKE_B}).encode()))
    system = FileSecrets(path, scope="system")
    user = FileSecrets(path, scope="user")
    assert system.get("router") == FAKE_A and [i.name for i in system.list()] == ["byok:perso", "router"]
    assert user.list() == []
    user.set("router", FAKE_C)  # même nom, autre portée : aucune collision
    assert system.get("router") == FAKE_A and user.get("router") == FAKE_C
    raw = json.loads(Fernet(key.encode()).decrypt(path.read_bytes()))
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items())
    assert raw["router"] == FAKE_A and raw["user:router"] == FAKE_C and META_KEY in raw
    assert FAKE_C not in raw[META_KEY]  # les dates seulement
    with pytest.raises(SecretsError):
        system.set("user:x", FAKE_A)  # préfixe réservé à la portée user


def test_local_vault_of_setup_is_the_system_scope(tmp_path, key):
    vault = LocalVault(tmp_path / "vault.bin")
    vault.put("byok:x", FAKE_A)
    assert FileSecrets(tmp_path / "vault.bin", scope="system").get("byok:x") == FAKE_A
    assert vault.names() == ["byok:x"]


def test_no_key_refuses_to_write_and_empty_vault_reads_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("NOKKER_LOCAL_SECRET_KEY", raising=False)
    s = FileSecrets(tmp_path / "vault.bin")  # pas de fichier de clé configuré
    assert s.list() == [] and s.get("X") is None
    with pytest.raises(SecretsError) as e:
        s.set("X", FAKE_A)
    assert e.value.code == "unavailable" and FAKE_A not in str(e.value)
    assert VaultError is SecretsError  # les appelants d'avant X4 attrapent la même erreur


def test_local_key_file_created_0600_only_for_a_new_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("NOKKER_LOCAL_SECRET_KEY", raising=False)
    s = FileSecrets(tmp_path / "vault.bin", key_file=tmp_path / "vault.key")
    s.set("X", FAKE_A)
    assert stat.S_IMODE(os.stat(tmp_path / "vault.key").st_mode) == 0o600
    assert FileSecrets(tmp_path / "vault.bin", key_file=tmp_path / "vault.key").get("X") == FAKE_A
    # Coffre existant, clé perdue : jamais une clé neuve à côté (il deviendrait illisible sans bruit).
    (tmp_path / "vault.key").unlink()
    with pytest.raises(SecretsError, match="sa clé manque"):
        FileSecrets(tmp_path / "vault.bin", key_file=tmp_path / "vault.key").set("Y", FAKE_B)
    assert not (tmp_path / "vault.key").exists()


def test_wrong_key_is_an_error_not_an_empty_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", fernet_key())
    FileSecrets(tmp_path / "vault.bin").set("X", FAKE_A)
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", fernet_key())
    with pytest.raises(SecretsError, match="illisible"):
        FileSecrets(tmp_path / "vault.bin").list()


@pytest.mark.parametrize("name", ["", "a b", "../x", "x/y", "-x", "secret:x", "é", "x" * 129, "a:b"])
def test_bad_names_refused(tmp_path, key, name):
    with pytest.raises(SecretsError) as e:
        FileSecrets(tmp_path / "vault.bin").set(name, FAKE_A)
    assert e.value.code == "bad_name"


@pytest.mark.parametrize("value", ["", None, 42, "x" * (64 * 1024 + 1)])
def test_bad_values_refused(tmp_path, key, value):
    with pytest.raises(SecretsError) as e:
        FileSecrets(tmp_path / "vault.bin").set("X", value)
    assert e.value.code == "bad_value"
