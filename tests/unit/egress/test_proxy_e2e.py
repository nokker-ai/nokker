"""Injection des secrets à la sortie, de bout en bout : noyau (EgressSecrets) → proxy réel → amont HTTPS local, avec un
« bac à sable » qui est un processus séparé ne recevant que l'environnement rendu par le noyau.

Les cinq exigences du chantier : substitution sur hôte autorisé, refus sur hôte non autorisé, valeur jamais visible
dans le bac à sable (environnement, fichiers, sortie), caviardage de la réponse, secret d'un autre locataire
inaccessible. Plus : le journal ne porte jamais de valeur, un client sans jeton garde le comportement d'origine."""
from __future__ import annotations

import base64
import json
import logging
import socket
import ssl

from nokker.egress.kernel import CA_REL, EgressSecrets, EgressSecretsConfig
from tests.unit.code_sessions.conftest import MemorySecrets
from tests.unit.egress.conftest import egress_block, run_sandbox

ALICE_VALUE = "ghp_alice0123456789abcdefSECRET"
BOB_VALUE = "ghp_bob9876543210zyxwvuSECRET"


def _vault(**values: str) -> MemorySecrets:
    v = MemorySecrets()
    v.values.update(values)
    return v


def _open(egress, tmp_path, session="cs_0000000000000001", tenant="tn_alice", vault=None):
    ws = tmp_path / session
    ws.mkdir(exist_ok=True)
    setup = egress.open_session(session, tenant, vault or _vault(GH_TOKEN=ALICE_VALUE), str(ws), "/workspace")
    assert setup is not None
    # Le bac à sable voit son dossier sur /workspace : on rejoue ce montage en réécrivant le chemin du certificat.
    env = {k: (v.replace("/workspace", str(ws)) if v.startswith("/workspace/") else v) for k, v in setup.env.items()}
    return setup, env, ws


def _url(upstream, host="api.example.test", path="/echo"):
    return f"https://{host}:{upstream.port}{path}"


def test_substitution_sur_hote_autorise(egress, upstream, tmp_path, proxy):
    _, env, _ = _open(egress, tmp_path)
    resp, _, _ = run_sandbox(env, _url(upstream), {"Authorization": "Bearer secret:GH_TOKEN"})
    assert resp["status"] == 200, resp
    assert upstream.seen[-1]["headers"]["authorization"] == f"Bearer {ALICE_VALUE}"
    j = proxy.proxy.journal.since(0)["events"]
    assert {"event": "substitution", "secret": "GH_TOKEN", "host": "api.example.test",
            "where": "header:Authorization"}.items() <= j[0].items()
    assert j[0]["tenant"] == "tn_alice"


def test_substitution_requete_et_corps_json(egress, upstream, tmp_path):
    _, env, _ = _open(egress, tmp_path)
    resp, _, _ = run_sandbox(env, _url(upstream, path="/echo?key=secret:GH_TOKEN&q=1"),
                             {"Content-Type": "application/json"},
                             json.dumps({"auth": {"token": "secret:GH_TOKEN"}, "note": "rien"}))
    assert resp["status"] == 200, resp
    seen = upstream.seen[-1]
    assert seen["path"] == f"/echo?key={ALICE_VALUE}&q=1"
    assert json.loads(seen["body"]) == {"auth": {"token": ALICE_VALUE}, "note": "rien"}


def test_basic_git_reference_dans_le_base64(egress, upstream, tmp_path):
    """git pousse en Basic : `x-access-token:secret:GH_TOKEN` encodé ; le proxy décode, pose, réencode."""
    _, env, _ = _open(egress, tmp_path)
    basic = base64.b64encode(b"x-access-token:secret:GH_TOKEN").decode()
    resp, _, out = run_sandbox(env, _url(upstream), {"Authorization": f"Basic {basic}"})
    assert resp["status"] == 200
    sent = upstream.seen[-1]["headers"]["authorization"]
    assert base64.b64decode(sent[6:]).decode() == f"x-access-token:{ALICE_VALUE}"
    # l'écho de l'en-tête (base64 de la valeur) revient sous sa forme d'origine, la référence
    assert resp["headers"]["X-Echo-Auth"] == f"Basic {basic}"
    assert ALICE_VALUE not in out and sent[6:] not in out


def test_refus_sur_hote_non_autorise(egress, upstream, tmp_path, proxy):
    _, env, _ = _open(egress, tmp_path)
    before = len(upstream.seen)
    resp, _, out = run_sandbox(env, _url(upstream, host="evil.example.test"), {"Authorization": "Bearer secret:GH_TOKEN"})
    assert resp["status"] == 403
    assert "hôte evil.example.test non autorisé pour ce secret" in resp["body"]
    assert len(upstream.seen) == before  # rien n'est parti
    ev = proxy.proxy.journal.since(0)["events"][-1]
    assert ev["event"] == "refused" and ev["where"] == "host_not_allowed" and ev["secret"] == "GH_TOKEN"
    assert ALICE_VALUE not in out


def test_refus_emplacement_non_declare(egress, upstream, tmp_path):
    _, env, _ = _open(egress, tmp_path)
    resp, _, _ = run_sandbox(env, _url(upstream), {"X-Other": "secret:GH_TOKEN"})
    assert resp["status"] == 403 and "emplacement header:X-Other non déclaré" in resp["body"]
    resp, _, _ = run_sandbox(env, _url(upstream), {"Content-Type": "application/json"},
                             json.dumps({"elsewhere": "secret:GH_TOKEN"}))
    assert resp["status"] == 403 and "field:elsewhere" in resp["body"]


def test_valeur_jamais_visible_dans_le_bac_a_sable(egress, upstream, tmp_path):
    setup, env, ws = _open(egress, tmp_path)
    # environnement rendu par le noyau
    assert all(ALICE_VALUE not in v for v in setup.env.values())
    assert setup.env["NOKKER_SECRET_REFS"] == "GH_TOKEN"
    assert ALICE_VALUE not in repr(setup)
    # fichiers déposés dans l'espace de travail : le certificat PUBLIC seulement
    files = [p for p in ws.rglob("*") if p.is_file()]
    assert [p.relative_to(ws).as_posix() for p in files] == ["/".join(CA_REL)]
    assert all(ALICE_VALUE.encode() not in p.read_bytes() for p in files)
    assert b"PRIVATE KEY" not in (ws / CA_REL[0] / CA_REL[1]).read_bytes()
    # sortie d'un processus du bac à sable qui reçoit la valeur en écho (corps, en-tête) et affiche son environnement
    resp, child_env, out = run_sandbox(env, _url(upstream), {"Authorization": "Bearer secret:GH_TOKEN"})
    assert resp["status"] == 200
    assert upstream.seen[-1]["headers"]["authorization"] == f"Bearer {ALICE_VALUE}"
    assert ALICE_VALUE not in out
    assert all(ALICE_VALUE not in v for v in child_env.values())


def test_caviardage_de_la_reponse(egress, upstream, tmp_path, proxy):
    _, env, _ = _open(egress, tmp_path)
    for path in ("/echo", "/gzip"):
        resp, _, out = run_sandbox(env, _url(upstream, path=path), {"Authorization": "Bearer secret:GH_TOKEN"})
        assert resp["status"] == 200, resp
        echoed = json.loads(resp["body"])
        assert echoed["headers"]["authorization"] == "Bearer secret:GH_TOKEN"  # corps : revenu en référence
        assert resp["headers"]["X-Echo-Auth"] == "Bearer secret:GH_TOKEN"  # en-tête : idem
        assert ALICE_VALUE not in out
    red = [e for e in proxy.proxy.journal.since(0)["events"] if e["event"] == "redacted"]
    assert red and all(e["secret"] == "GH_TOKEN" and e["where"] == "response" for e in red)


def test_secret_d_un_autre_locataire_inaccessible(egress, upstream, tmp_path, proxy):
    """Deux locataires, même déclaration de nom : chacun n'obtient que la valeur de SON coffre ; un nom que le coffre
    de bob n'a pas (celui d'alice) est refusé net pour bob."""
    egress.cfg = EgressSecretsConfig.from_config(egress_block(proxy, {
        "GH_TOKEN": {"hosts": ["api.example.test"], "headers": ["authorization"]},
        "ALICE_ONLY": {"hosts": ["api.example.test"], "headers": ["authorization"]}}))
    egress.__post_init__()
    _, env_a, _ = _open(egress, tmp_path, "cs_000000000000000a", "tn_alice",
                        _vault(GH_TOKEN=ALICE_VALUE, ALICE_ONLY="alice-only-value-XYZ"))
    _, env_b, _ = _open(egress, tmp_path, "cs_000000000000000b", "tn_bob", _vault(GH_TOKEN=BOB_VALUE))
    assert env_b["NOKKER_SECRET_REFS"] == "GH_TOKEN"
    resp, _, out = run_sandbox(env_b, _url(upstream), {"Authorization": "Bearer secret:GH_TOKEN"})
    assert resp["status"] == 200 and upstream.seen[-1]["headers"]["authorization"] == f"Bearer {BOB_VALUE}"
    before = len(upstream.seen)
    resp, _, out = run_sandbox(env_b, _url(upstream), {"Authorization": "Bearer secret:ALICE_ONLY"})
    assert resp["status"] == 403 and "secret inconnu pour cette session" in resp["body"]
    assert len(upstream.seen) == before
    assert ALICE_VALUE not in out and "alice-only-value-XYZ" not in out
    # le jeton de bob ne donne jamais la valeur d'alice, même par le caviardage (la réponse de bob ne contient que bob)
    resp, _, _ = run_sandbox(env_a, _url(upstream), {"Authorization": "Bearer secret:ALICE_ONLY"})
    assert resp["status"] == 200 and upstream.seen[-1]["headers"]["authorization"] == "Bearer alice-only-value-XYZ"
    ev = proxy.proxy.journal.since(0, session="cs_000000000000000b")["events"]
    assert all(e["tenant"] == "tn_bob" for e in ev)


def test_session_fermee_jeton_revoque(egress, upstream, tmp_path):
    _, env, _ = _open(egress, tmp_path)
    egress.close_session("cs_0000000000000001")
    resp, _, _ = run_sandbox(env, _url(upstream), {"Authorization": "Bearer secret:GH_TOKEN"})
    assert resp["status"] is None and "407" in resp["body"]  # CONNECT refusé : le jeton révoqué ne sert plus


def test_journal_et_logs_sans_valeur(egress, upstream, tmp_path, proxy, caplog):
    caplog.set_level(logging.DEBUG)
    _, env, _ = _open(egress, tmp_path)
    run_sandbox(env, _url(upstream), {"Authorization": "Bearer secret:GH_TOKEN"})
    run_sandbox(env, _url(upstream, host="evil.example.test"), {"Authorization": "Bearer secret:GH_TOKEN"})
    events = egress.journal()
    assert {e["event"] for e in events} >= {"substitution", "refused", "redacted"}
    assert ALICE_VALUE not in json.dumps(events)
    assert ALICE_VALUE not in caplog.text
    assert "GH_TOKEN" in caplog.text


def test_client_sans_jeton_tunnel_d_origine(proxy, upstream):
    """Sans jeton de session : CONNECT en tunnel opaque, le certificat vu est celui de l'amont (pas de l'AC du proxy)."""
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    s.sendall(f"CONNECT api.example.test:{upstream.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert s.recv(1024).startswith(b"HTTP/1.1 200")
    ctx = ssl.create_default_context(cafile=str(upstream.ca_pem))
    with ctx.wrap_socket(s, server_hostname="api.example.test") as t:
        t.sendall(b"GET /echo HTTP/1.1\r\nHost: api.example.test\r\nAuthorization: Bearer secret:GH_TOKEN\r\n"
                  b"Connection: close\r\n\r\n")
        data = b""
        while chunk := t.recv(65536):
            data += chunk
    assert b"200 OK" in data
    assert upstream.seen[-1]["headers"]["authorization"] == "Bearer secret:GH_TOKEN"  # rien de posé


def test_proxy_injoignable_session_sans_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_EGRESS_CONTROL_KEY", "k" * 40)
    ca = tmp_path / "ca.crt"
    ca.write_text("pem")
    cfg = EgressSecretsConfig.from_config({"enabled": True, "proxy_url": "http://127.0.0.1:9",
                                           "control_url": "http://127.0.0.1:9", "ca_cert": str(ca),
                                           "secrets": {"GH_TOKEN": {"hosts": ["a.example"], "headers": ["x-k"]}}})
    eg = EgressSecrets(cfg)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert eg.open_session("cs_1", "tn_a", _vault(GH_TOKEN=ALICE_VALUE), str(ws), "/workspace") is None
