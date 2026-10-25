"""Fournisseur `openbao` contre un faux OpenBao (httpx.MockTransport) : KV v2 sous `nokker/`, AppRole depuis des
fichiers, relogin une fois sur 403, coffre scellé. La version contre un vrai OpenBao : tests/integration/."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.secrets.base import SecretsError
from nokker.secrets.openbao import OpenBaoSecrets, OpenBaoSession, http_allowed, session_from_config
from tests.unit.vault.fakes import FAKE_A, FAKE_B


class FakeBao:
    """Le strict nécessaire de l'API : login AppRole, KV v2 (data, metadata, list), santé, scellement."""

    def __init__(self) -> None:
        self.kv: dict[str, dict] = {}
        self.tokens: set[str] = set()
        self.sealed = False
        self.logins = 0
        self.requests: list[httpx.Request] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/v1/sys/health":
            return httpx.Response(503 if self.sealed else 200, json={"initialized": True, "sealed": self.sealed})
        if self.sealed:
            return httpx.Response(503, json={"errors": ["Vault is sealed"]})
        if path == "/v1/auth/approle/login":
            body = json.loads(req.content)
            if body != {"role_id": "role-x", "secret_id": "sid-" + "x"}:
                return httpx.Response(400, json={"errors": ["invalid role or secret ID"]})
            self.logins += 1
            tok = f"tok-{self.logins}"
            self.tokens.add(tok)
            return httpx.Response(200, json={"auth": {"client_token": tok, "lease_duration": 3600}})
        if req.headers.get("X-Vault-Token") not in self.tokens:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        if not path.startswith("/v1/nokker/"):
            return httpx.Response(403, json={"errors": ["permission denied"]})
        kind, _, key = path[len("/v1/nokker/"):].partition("/")
        if kind == "metadata" and req.url.params.get("list") == "true":
            prefix = key.rstrip("/") + "/"
            keys = sorted({k[len(prefix):].split("/")[0] + ("/" if "/" in k[len(prefix):] else "")
                           for k in self.kv if k.startswith(prefix)})
            return httpx.Response(200, json={"data": {"keys": keys}}) if keys else httpx.Response(404, json={})
        if kind == "data" and req.method == "POST":
            self.kv[key] = {"value": json.loads(req.content)["data"]["value"],
                            "time": "2026-10-25T10:11:12.123456789Z"}
            return httpx.Response(200, json={"data": {"created_time": self.kv[key]["time"], "version": 1}})
        entry = self.kv.get(key)
        if entry is None:
            return httpx.Response(404, json={"errors": []})
        if kind == "data":
            return httpx.Response(200, json={"data": {"data": {"value": entry["value"]}}})
        if kind == "metadata" and req.method == "DELETE":
            del self.kv[key]
            return httpx.Response(204)
        return httpx.Response(200, json={"data": {"updated_time": entry["time"]}})


@pytest.fixture
def bao(tmp_path):
    fake = FakeBao()
    (tmp_path / "role").write_text("role-x\n")
    (tmp_path / "sid").write_text("sid-" + "x\n")
    session = OpenBaoSession(address="http://openbao:8200", role_id_file=str(tmp_path / "role"),
                             secret_id_file=str(tmp_path / "sid"),
                             client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return fake, session


def test_round_trip_list_and_layout(bao):
    fake, session = bao
    user, system = OpenBaoSecrets(session, scope="user"), OpenBaoSecrets(session, scope="system")
    assert user.set("GITHUB_TOKEN", FAKE_A).updated_at is not None
    system.set("byok:perso", FAKE_B)
    assert set(fake.kv) == {"user/GITHUB_TOKEN", "system/byok:perso"}  # nokker/data/<portée>/<NOM>
    assert user.get("GITHUB_TOKEN") == FAKE_A and system.get("byok:perso") == FAKE_B and user.get("byok") is None
    listed = user.list()
    assert [i.name for i in listed] == ["GITHUB_TOKEN"] and listed[0].updated_at.year == 2026
    assert FAKE_A not in repr(listed) and FAKE_A not in repr(user) and FAKE_A not in repr(session)
    assert user.delete("GITHUB_TOKEN") and not user.delete("GITHUB_TOKEN") and user.list() == []
    assert fake.logins == 1  # un login pour toute la séquence, partagé par les deux portées


def test_relogin_once_on_403_then_denied(bao):
    fake, session = bao
    user = OpenBaoSecrets(session)
    user.set("X", FAKE_A)
    fake.tokens.clear()  # jeton expiré ou révoqué
    assert user.get("X") == FAKE_A and fake.logins == 2
    # Policy qui refuse (montage hors nokker/) : relogin une fois, puis erreur `denied`, sans valeur dans le message.
    other = OpenBaoSecrets(session, mount="ailleurs")
    with pytest.raises(SecretsError) as e:
        other.get("X")
    assert e.value.code == "denied" and fake.logins == 3


def test_sealed_vault_is_a_clear_error(bao):
    fake, session = bao
    user = OpenBaoSecrets(session)
    user.set("X", FAKE_A)
    fake.sealed = True
    for op in (lambda: user.get("X"), user.list, lambda: user.set("Y", FAKE_B)):
        with pytest.raises(SecretsError) as e:
            op()
        assert e.value.code == "sealed" and "scellé" in str(e.value)
    assert session.health() == {"reachable": True, "initialized": True, "sealed": True}


def test_unreachable_and_bad_credentials(tmp_path):
    def down(req):
        raise httpx.ConnectError("refusé")

    s = OpenBaoSession(address="http://127.0.0.1:1", auth="token", token_file=str(tmp_path / "absent"),
                       client=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(SecretsError) as e:
        OpenBaoSecrets(s).get("X")
    assert e.value.code == "config"  # fichier de jeton absent : dit lequel, pas de valeur
    (tmp_path / "absent").write_text("t-" + "1")
    with pytest.raises(SecretsError) as e:
        OpenBaoSecrets(s).get("X")
    assert e.value.code == "unavailable" and "injoignable" in str(e.value)
    assert s.health()["reachable"] is False


def test_http_only_on_loopback_or_compose_service():
    assert http_allowed("http://127.0.0.1:8200") and http_allowed("http://localhost:8200")
    assert http_allowed("http://openbao:8200") and http_allowed("http://[::1]:8200")
    assert not http_allowed("http://bao.example.ch:8200") and not http_allowed("http://10.0.0.5:8200")
    with pytest.raises(SecretsError):
        OpenBaoSession(address="http://bao.example.ch:8200")
    OpenBaoSession(address="http://bao.example.ch:8200", allow_http=True)
    OpenBaoSession(address="https://bao.example.ch")
    with pytest.raises(SecretsError):
        OpenBaoSession(address="")


def test_compose_environment_wins_and_credentials_are_files(tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_OPENBAO_ADDR", "http://openbao:8200")
    monkeypatch.setenv("NOKKER_OPENBAO_ROLE_ID_FILE", str(tmp_path / "role"))
    monkeypatch.setenv("NOKKER_OPENBAO_SECRET_ID_FILE", str(tmp_path / "sid"))
    s = session_from_config({"address": "https://autre.example"})
    assert s.address == "http://openbao:8200" and s.auth == "approle"
    assert s.role_id_file == str(tmp_path / "role") and s.secret_id_file == str(tmp_path / "sid")
