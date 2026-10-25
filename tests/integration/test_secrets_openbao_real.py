"""Fournisseur `openbao` contre un VRAI OpenBao, jetable : conteneur `openbao/openbao` en mode dev, port aléatoire sur
127.0.0.1, supprimé à la fin. Configuré comme le contrat compose (docs/LOKKAL.md §7.10) : KV v2 `nokker/`, policy
`nokker-core` limitée à nokker/data et nokker/metadata, AppRole lu dans des fichiers.

Lancer : `python -m pytest tests/integration/test_secrets_openbao_real.py -m integration`. Sauté proprement sans
docker ou sans l'image. Ne touche à aucun autre OpenBao de la machine (nom de conteneur et port propres au test).
"""
from __future__ import annotations

import json
import os
import secrets as std_secrets
import shutil
import subprocess
import time
import uuid

import httpx
import pytest

from nokker.local.api import LocalApi
from nokker.secrets.file import FileSecrets
from nokker.secrets.migrate import migrate
from nokker.secrets.openbao import OpenBaoSecrets, OpenBaoSession
from nokker.secrets.vault import Vault
from tests.unit.vault.fakes import FAKE_A, FAKE_B, FAKE_C, fernet_key

pytestmark = pytest.mark.integration
IMAGE = os.environ.get("NOKKER_TEST_OPENBAO_IMAGE", "openbao/openbao:2.4.1")
POLICY = """
path "nokker/data/*"     { capabilities = ["create", "read", "update", "delete"] }
path "nokker/metadata/*" { capabilities = ["read", "list", "delete"] }
"""  # celle de deploy/lokkal/vault/init.sh


def _docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=120).stdout.strip()


@pytest.fixture(scope="module")
def bao(tmp_path_factory):
    if shutil.which("docker") is None:
        pytest.skip("docker absent")
    try:
        _docker("image", "inspect", IMAGE)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        try:
            _docker("pull", IMAGE)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pytest.skip(f"image {IMAGE} indisponible")
    name = f"nokker-x4-bao-test-{uuid.uuid4().hex[:8]}"
    root = "root-" + std_secrets.token_hex(12)  # jeton root du conteneur jetable, jamais écrit ailleurs
    _docker("run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::8200", "--cap-add", "IPC_LOCK",
            "-e", f"BAO_DEV_ROOT_TOKEN_ID={root}", "-e", "BAO_DEV_LISTEN_ADDRESS=0.0.0.0:8200", IMAGE, "server", "-dev")
    try:
        port = _docker("port", name, "8200/tcp").splitlines()[0].rsplit(":", 1)[1]
        addr = f"http://127.0.0.1:{port}"
        admin = httpx.Client(base_url=addr + "/v1", headers={"X-Vault-Token": root}, timeout=5)
        for _ in range(60):
            try:
                if admin.get("/sys/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("OpenBao jetable pas prêt")

        def ok(r):
            assert r.status_code < 300, r.text
            return r

        ok(admin.post("/sys/mounts/nokker", json={"type": "kv", "options": {"version": "2"}}))
        ok(admin.put("/sys/policies/acl/nokker-core", json={"policy": POLICY}))
        ok(admin.post("/sys/auth/approle", json={"type": "approle"}))
        ok(admin.post("/auth/approle/role/nokker-core", json={"token_policies": ["nokker-core"], "token_ttl": "1h",
                                                                    "token_max_ttl": "24h", "secret_id_ttl": "0"}))
        role_id = ok(admin.get("/auth/approle/role/nokker-core/role-id")).json()["data"]["role_id"]
        secret_id = ok(admin.post("/auth/approle/role/nokker-core/secret-id")).json()["data"]["secret_id"]
        d = tmp_path_factory.mktemp("approle")
        for fname, value in (("role-id", role_id), ("secret-id", secret_id)):
            fd = os.open(d / fname, os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(value + "\n")
        yield {"addr": addr, "admin": admin, "role_file": str(d / "role-id"), "sid_file": str(d / "secret-id")}
    finally:
        _docker("rm", "-f", name, check=False)


def _session(bao) -> OpenBaoSession:
    return OpenBaoSession(address=bao["addr"], role_id_file=bao["role_file"], secret_id_file=bao["sid_file"])


def test_round_trip_both_scopes_and_list_without_values(bao):
    s = _session(bao)
    user, system = OpenBaoSecrets(s, "user"), OpenBaoSecrets(s, "system")
    user.set("GH", FAKE_A)
    system.set("byok:perso", FAKE_B)  # « : » dans un nom du noyau
    assert user.get("GH") == FAKE_A and system.get("byok:perso") == FAKE_B and user.get("absent") is None
    listed = user.list()
    assert [i.name for i in listed] == ["GH"] and listed[0].updated_at is not None
    assert [i.name for i in system.list()] == ["byok:perso"]
    raw = bao["admin"].get("/nokker/data/user/GH").json()["data"]["data"]
    assert raw == {"value": FAKE_A}  # disposition du contrat : nokker/data/<portée>/<NOM> = {value}
    user.set("GH", FAKE_C)
    assert user.get("GH") == FAKE_C
    assert user.delete("GH") and not user.delete("GH")
    assert bao["admin"].get("/nokker/metadata/user/GH").status_code == 404  # toutes les versions effacées
    assert system.delete("byok:perso")


def test_policy_is_limited_and_relogin_after_revocation(bao):
    s = _session(bao)
    user = OpenBaoSecrets(s)
    user.set("RL", FAKE_A)
    tok = s._token
    assert bao["admin"].post("/auth/token/revoke", json={"token": tok}).status_code == 204
    assert user.get("RL") == FAKE_A and s._token != tok  # relogin une fois, transparent
    with pytest.raises(Exception) as e:
        OpenBaoSecrets(s, mount="secret").get("RL")  # hors de la policy nokker-core
    assert getattr(e.value, "code", None) == "denied"
    user.delete("RL")


def test_local_api_routes_and_system_scope_on_openbao(bao, tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_SECRETS_PROVIDER", "openbao")
    monkeypatch.setenv("NOKKER_OPENBAO_ADDR", bao["addr"])
    monkeypatch.setenv("NOKKER_OPENBAO_ROLE_ID_FILE", bao["role_file"])
    monkeypatch.setenv("NOKKER_OPENBAO_SECRET_ID_FILE", bao["sid_file"])
    api = LocalApi.from_config({"state_dir": str(tmp_path), "token_path": str(tmp_path / "tok"),
                                "code": {"roots": [str(tmp_path)], "backend": "local"}}, port=8742)
    auth = f"Bearer {api.token}"
    status, body = api.handle("PUT", "/v1/local/secrets/API_X", auth, {"value": FAKE_A})
    assert status == 200 and FAKE_A not in json.dumps(body)
    status, body = api.handle("GET", "/v1/local/secrets", auth, {})
    assert status == 200 and body["provider"] == "openbao" and body["sealed"] is False
    assert [x["name"] for x in body["secrets"]] == ["API_X"] and FAKE_A not in json.dumps(body)
    api.setup.vault.put("router", FAKE_B)  # clé du noyau : dans OpenBao aussi, pas sur le disque
    assert not (tmp_path / "vault.bin").exists()
    assert api.vault.resolver().resolve("secret:API_X") == FAKE_A
    assert api.handle("DELETE", "/v1/local/secrets/API_X", auth, {})[0] == 200
    api.setup.vault.delete("router")


def test_migration_file_to_openbao_and_back(bao, tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", fernet_key())
    s = _session(bao)
    src = [FileSecrets(tmp_path / "vault.bin", "system"), FileSecrets(tmp_path / "vault.bin", "user")]
    src[0].set("machine_token", FAKE_A)
    src[1].set("MIG_1", FAKE_B)
    dst = [OpenBaoSecrets(s, "system"), OpenBaoSecrets(s, "user")]
    r1 = migrate(list(zip(src, dst)))
    assert r1.ok and r1.count("copied") == 2
    r2 = migrate(list(zip(src, dst)))
    assert r2.ok and r2.count("same") == 2  # idempotente
    dst[1].set("MIG_1", FAKE_C)
    r3 = migrate(list(zip(src, dst)))
    assert not r3.ok and r3.count("divergent") == 1 and dst[1].get("MIG_1") == FAKE_C  # jamais écrasée sans option
    back = [FileSecrets(tmp_path / "retour.bin", "system"), FileSecrets(tmp_path / "retour.bin", "user")]
    r4 = migrate(list(zip(dst, back)))
    assert r4.ok and back[0].get("machine_token") == FAKE_A and back[1].get("MIG_1") == FAKE_C
    for p in dst:
        for i in p.list():
            p.delete(i.name)


def test_sealed_vault_routes_503_and_references_fail_clearly(bao, tmp_path, monkeypatch):
    """En dernier : le conteneur de dev reste scellé ensuite (le noyau ne descelle jamais)."""
    monkeypatch.setenv("NOKKER_SECRETS_PROVIDER", "openbao")
    vault = Vault.from_config({"openbao": {"address": bao["addr"], "role_id_file": bao["role_file"],
                                           "secret_id_file": bao["sid_file"]}}, tmp_path)
    vault.set("SEALED_X", FAKE_A)
    assert bao["admin"].post("/sys/seal").status_code == 204
    api = LocalApi.from_config({"state_dir": str(tmp_path), "token_path": str(tmp_path / "tok"),
                                "code": {"roots": [str(tmp_path)], "backend": "local"},
                                "secrets": {"openbao": {"address": bao["addr"], "role_id_file": bao["role_file"],
                                                        "secret_id_file": bao["sid_file"]}}}, port=8742)
    status, body = api.handle("GET", "/v1/local/secrets", f"Bearer {api.token}", {})
    assert status == 503 and body["error"]["code"] == "sealed" and "scellé" in body["error"]["message"]
    with pytest.raises(Exception) as e:
        api.vault.resolver().resolve("secret:SEALED_X")
    assert getattr(e.value, "code", None) == "sealed" and "secret:SEALED_X" in str(e.value)
    assert api.setup.view()["models"]["router"]["linked"] is False  # le premier lancement ne casse pas
