"""Routes `/v1/local/secrets*` (jeton local, valeur jamais renvoyée), commande `secret`/`vault`, migration."""
from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from nokker.api.server import make_handler
from nokker.cli import main
from nokker.local.api import LOCAL_OWNER, LocalApi
from nokker.secrets.file import FileSecrets
from nokker.secrets.migrate import migrate
from tests.unit.api.test_server import make_kernel
from tests.unit.vault.fakes import FAKE_A, FAKE_B, FAKE_C, MemorySecrets, fernet_key


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", fernet_key())
    monkeypatch.delenv("NOKKER_SECRETS_PROVIDER", raising=False)
    monkeypatch.delenv("NOKKER_OPENBAO_ADDR", raising=False)


def _api(tmp_path) -> LocalApi:
    return LocalApi.from_config({"state_dir": str(tmp_path / "state"), "token_path": str(tmp_path / "tok"),
                                 "code": {"roots": [str(tmp_path)], "backend": "local"}}, port=8742)


def test_routes_need_the_local_token_and_never_return_a_value(tmp_path):
    api = _api(tmp_path)
    auth = f"Bearer {api.token}"
    for method, path, body in (("GET", "/v1/local/secrets", {}), ("PUT", "/v1/local/secrets/GH", {"value": FAKE_A}),
                               ("DELETE", "/v1/local/secrets/GH", {})):
        assert api.handle(method, path, None, body)[0] == 401
        assert api.handle(method, path, "Bearer faux", body)[0] == 401
    status, body = api.handle("PUT", "/v1/local/secrets/GH", auth, {"value": FAKE_A})
    assert status == 200 and body["name"] == "GH" and body["updated_at"] and FAKE_A not in json.dumps(body)
    status, body = api.handle("GET", "/v1/local/secrets", auth, {})
    assert status == 200 and body["provider"] == "file" and [s["name"] for s in body["secrets"]] == ["GH"]
    assert FAKE_A not in json.dumps(body)
    assert api.handle("PUT", "/v1/local/secrets/a%20b", auth, {"value": FAKE_A})[0] == 400
    assert api.handle("PUT", "/v1/local/secrets/GH", auth, {"value": ""})[0] == 400
    assert api.handle("PUT", "/v1/local/secrets/GH", auth, {})[0] == 400
    assert api.handle("DELETE", "/v1/local/secrets/GH", auth, {}) == (200, {"name": "GH", "deleted": True})
    assert api.handle("DELETE", "/v1/local/secrets/GH", auth, {})[0] == 404
    # Les clés du noyau (portée system) ne sont ni listées ni effaçables par ces routes.
    api.setup.vault.put("router", FAKE_B)
    assert api.handle("GET", "/v1/local/secrets", auth, {})[1]["secrets"] == []
    assert api.handle("DELETE", "/v1/local/secrets/router", auth, {})[0] == 404
    assert api.setup.vault.get("router") == FAKE_B


def test_relayed_requests_may_list_but_never_write(tmp_path, monkeypatch):
    api = _api(tmp_path)
    monkeypatch.setattr(api, "relayed_owner", lambda auth: {"sub": "acc"})
    assert api.handle("GET", "/v1/local/secrets", "Bearer x.y.z", {}, relayed=True)[0] == 200
    assert api.handle("PUT", "/v1/local/secrets/GH", "Bearer x.y.z", {"value": FAKE_A}, relayed=True)[0] == 403
    assert api.handle("DELETE", "/v1/local/secrets/GH", "Bearer x.y.z", {}, relayed=True)[0] == 403
    assert api.vault.list() == []


def test_sealed_vault_gives_503_coffre_scelle(tmp_path):
    api = _api(tmp_path)
    sealed = MemorySecrets()
    sealed.fail = "sealed"
    api.vault.user = sealed
    auth = f"Bearer {api.token}"
    for method, path, body in (("GET", "/v1/local/secrets", {}), ("PUT", "/v1/local/secrets/GH", {"value": FAKE_A}),
                               ("DELETE", "/v1/local/secrets/GH", {})):
        status, out = api.handle(method, path, auth, body)
        assert status == 503 and out["error"]["code"] == "sealed" and "scellé" in out["error"]["message"]


def test_openbao_chosen_without_the_vault_profile_is_a_clear_503(tmp_path, monkeypatch):
    """Le compose monte toujours /run/nokker-vault, vide sans le profil : coffre non configuré, pas un crash."""
    monkeypatch.setenv("NOKKER_SECRETS_PROVIDER", "openbao")
    monkeypatch.setenv("NOKKER_OPENBAO_ADDR", "http://openbao:8200")
    monkeypatch.setenv("NOKKER_OPENBAO_ROLE_ID_FILE", str(tmp_path / "vide" / "role_id"))
    monkeypatch.setenv("NOKKER_OPENBAO_SECRET_ID_FILE", str(tmp_path / "vide" / "secret_id"))
    api = _api(tmp_path)  # démarre
    status, body = api.handle("GET", "/v1/local/secrets", f"Bearer {api.token}", {})
    assert status == 503 and body["error"]["code"] == "config" and "non configuré" in body["error"]["message"]
    assert api.setup.view()["models"]["router"]["linked"] is False


def test_real_handler_put_get_delete(tmp_path):
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {api.token: LOCAL_OWNER},
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def req(method, path, token=None, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json",
                                            **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    try:
        assert req("GET", "/v1/local/secrets")[0] == 401
        assert req("PUT", "/v1/local/secrets/GH", body={"value": FAKE_A})[0] == 401
        status, raw = req("PUT", "/v1/local/secrets/GH", api.token, {"value": FAKE_A})
        assert status == 200 and FAKE_A not in raw
        status, raw = req("GET", "/v1/local/secrets", api.token)
        assert status == 200 and '"GH"' in raw and FAKE_A not in raw
        assert req("DELETE", "/v1/local/secrets/GH", api.token)[0] == 200
    finally:
        httpd.shutdown()


# ─────────────────────────────── migration ───────────────────────────────


def test_migration_rereads_is_idempotent_and_never_overwrites_silently(tmp_path):
    src_user, src_sys = FileSecrets(tmp_path / "vault.bin", "user"), FileSecrets(tmp_path / "vault.bin", "system")
    src_user.set("GH", FAKE_A)
    src_user.set("DB", FAKE_B)
    src_sys.set("router", FAKE_C)
    dst_user, dst_sys = MemorySecrets("user"), MemorySecrets("system")
    dst_user.set("DB", FAKE_C)  # déjà là, autre valeur
    pairs = [(src_sys, dst_sys), (src_user, dst_user)]

    dry = migrate(pairs, check=True)
    assert dry.count("would-copy") == 2 and dry.count("divergent") == 1 and dst_user.get("GH") is None

    r1 = migrate(pairs)
    assert {(s, n): st for s, n, st in r1.rows} == {("system", "router"): "copied", ("user", "DB"): "divergent",
                                                    ("user", "GH"): "copied"}
    assert not r1.ok and dst_user.get("DB") == FAKE_C and dst_sys.get("router") == FAKE_C
    assert FAKE_A not in r1.render() and FAKE_B not in r1.render()

    r2 = migrate(pairs)
    assert r2.count("same") == 2 and r2.count("divergent") == 1  # idempotente

    r3 = migrate(pairs, overwrite=True)
    assert r3.ok and r3.count("overwritten") == 1 and dst_user.get("DB") == FAKE_B
    assert src_user.get("DB") == FAKE_B  # la source n'est jamais touchée


# ─────────────────────────────── commande ───────────────────────────────


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"local": {"state_dir": str(tmp_path / "state")}}))
    return str(path)


def test_cli_set_from_stdin_list_rm_never_prints_a_value(cfg, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(FAKE_A + "\n"))
    assert main(["--config", cfg, "secret", "set", "GH"]) == 0
    assert FileSecrets(tmp_path / "state" / "vault.bin").get("GH") == FAKE_A  # retour à la ligne final retiré
    assert main(["--config", cfg, "secret", "list"]) == 0
    assert main(["--config", cfg, "secret", "rm", "GH"]) == 0
    assert main(["--config", cfg, "secret", "rm", "GH"]) == 1
    out = capsys.readouterr()
    assert "GH" in out.out and FAKE_A not in out.out + out.err


def test_cli_value_is_never_an_argument(cfg):
    with pytest.raises(SystemExit):
        main(["--config", cfg, "secret", "set", "GH", FAKE_A])


def test_cli_interactive_prompt_twice(cfg, tmp_path, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    answers = iter([FAKE_A, FAKE_B])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["--config", cfg, "secret", "set", "GH"]) == 1  # deux saisies différentes : rien n'est rangé
    assert FileSecrets(tmp_path / "state" / "vault.bin").get("GH") is None
    answers = iter([FAKE_A, FAKE_A])
    assert main(["--config", cfg, "secret", "set", "GH"]) == 0
    assert FileSecrets(tmp_path / "state" / "vault.bin").get("GH") == FAKE_A
