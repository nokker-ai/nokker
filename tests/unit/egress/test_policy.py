"""Politique pure (nokker/egress/policy.py) : déclarations, substitution par emplacement, refus, caviardage par
morceaux."""
from __future__ import annotations

import base64
import json

import pytest

from nokker.egress.policy import (
    Grant,
    Redactor,
    Refused,
    Request,
    SecretRule,
    parse_rules,
    substitute,
)

V = "tok_0123456789abcdef"


def _grant(**rules: dict) -> Grant:
    g = Grant(session="cs_1", tenant="tn_a")
    for name, d in rules.items():
        g.secrets[name] = (SecretRule.from_dict(name, d), V if name == "T" else f"{name.lower()}-value-123")
    return g


G = _grant(T={"hosts": ["api.example.com", "*.forge.example"], "headers": ["authorization"], "query": ["key"],
              "fields": ["auth.token", "token"]})


def test_declarations_refusees():
    with pytest.raises(ValueError, match="au moins un hôte"):
        SecretRule.from_dict("T", {"headers": ["authorization"]})
    with pytest.raises(ValueError, match="au moins un emplacement"):
        SecretRule.from_dict("T", {"hosts": ["a.example"]})
    with pytest.raises(ValueError, match="transport"):
        SecretRule.from_dict("T", {"hosts": ["a.example"], "headers": ["proxy-authorization"]})
    with pytest.raises(ValueError, match="nom de secret invalide"):
        SecretRule.from_dict("../x", {"hosts": ["a.example"], "headers": ["x"]})
    with pytest.raises(ValueError, match="clés inconnues"):
        SecretRule.from_dict("T", {"hosts": ["a.example"], "headers": ["x"], "everywhere": True})
    with pytest.raises(ValueError, match="invalide"):
        SecretRule.from_dict("T", {"hosts": ["*"], "headers": ["x"]})
    assert [r.name for r in parse_rules({"B": {"hosts": ["b.example"], "query": ["k"]},
                                         "A": {"hosts": ["A.Example"], "headers": ["X-K"]}})] == ["A", "B"]


def test_hotes_exacts_et_suffixe():
    r = G.secrets["T"][0]
    assert r.host_allowed("api.example.com") and r.host_allowed("API.example.com.")
    assert r.host_allowed("git.forge.example") and not r.host_allowed("forge.example")
    assert not r.host_allowed("api.example.com.evil.example") and not r.host_allowed("xapi.example.com")


def test_en_tete_avec_ponctuation_finale():
    req, ev, _ = substitute(G, "api.example.com", Request("GET", "/", [("Authorization", "Bearer secret:T")]))
    assert req.headers == [("Authorization", f"Bearer {V}")]
    assert [e.as_dict() for e in ev] == [{"event": "substitution", "secret": "T", "host": "api.example.com",
                                          "where": "header:Authorization"}]


def test_formulaire_et_requete():
    body = b"token=secret%3AT&other=x"
    req, _, _ = substitute(G, "api.example.com", Request("POST", "/p?key=secret:T", [
        ("Content-Type", "application/x-www-form-urlencoded"), ("Content-Length", str(len(body)))], body))
    assert req.target == f"/p?key={V}" and req.body == f"token={V}&other=x".encode()
    assert ("Content-Length", str(len(req.body))) in req.headers


def test_reference_dans_le_chemin_refusee():
    with pytest.raises(Refused) as e:
        substitute(G, "api.example.com", Request("GET", "/u/secret:T", []))
    assert e.value.code == "location_not_allowed"


def test_reference_dans_une_cle_json_refusee():
    body = json.dumps({"secret:T": 1}).encode()
    with pytest.raises(Refused):
        substitute(G, "api.example.com", Request("POST", "/", [("Content-Type", "application/json")], body))


def test_nom_inconnu_dans_un_corps_passe_tel_quel():
    body = b"la doc parle de secret:AUTRE ici"
    req, ev, _ = substitute(G, "api.example.com", Request("POST", "/", [("Content-Type", "text/plain")], body))
    assert req.body == body and ev == []


def test_nom_inconnu_dans_un_en_tete_refuse():
    with pytest.raises(Refused) as e:
        substitute(G, "api.example.com", Request("GET", "/", [("Authorization", "Bearer secret:AUTRE")]))
    assert e.value.code == "secret_unknown" and e.value.secret == "AUTRE"


def test_corps_non_inspecte_ne_bloque_pas_les_en_tetes():
    req, _, _ = substitute(G, "api.example.com", Request("PUT", "/", [("Authorization", "secret:T")], None))
    assert req.body is None and req.headers == [("Authorization", V)]


def test_basic_redecoupe():
    b = base64.b64encode(b"x-access-token:secret:T").decode()
    req, _, extra = substitute(G, "git.forge.example", Request("GET", "/", [("Authorization", f"Basic {b}")]))
    sent = req.headers[0][1][6:]
    assert base64.b64decode(sent) == f"x-access-token:{V}".encode()
    assert extra == [(sent.encode(), b.encode())]


@pytest.mark.parametrize("cut", range(1, len(V) + 6))
def test_caviardage_valeur_coupee_entre_deux_morceaux(cut):
    data = f'{{"echo": "Bearer {V}", "url": "{V}"}}'.encode()
    r = Redactor(G)
    out = r.feed(data[:cut]) + r.feed(data[cut:]) + r.finish()
    assert out == b'{"echo": "Bearer secret:T", "url": "secret:T"}'
    assert r.hits == {"T": 2}


def test_caviardage_formes_encodees():
    r = Redactor(G)
    assert r.whole(base64.b64encode(V.encode())) == b"secret:T"
    assert r.whole(b"a=" + V.encode() + b"&") == b"a=secret:T&"


def test_valeurs_courtes_non_caviardees():
    g = Grant("s", "t", {"X": (SecretRule.from_dict("X", {"hosts": ["a.example"], "headers": ["x"]}), "ab")})
    assert Redactor(g).whole(b"abc") == b"abc"


def test_repr_sans_valeur():
    assert V not in repr(G) and "T" in repr(G)


def test_grammaire_des_noms_identique_au_coffre():
    from nokker.egress.policy import NAME_RE
    from nokker.secrets.base import NAME_RE as VAULT_NAME_RE

    assert NAME_RE.pattern == VAULT_NAME_RE.pattern


@pytest.mark.parametrize("ip", ["10.250.90.2", "172.18.0.1", "192.168.1.1", "127.0.0.1", "169.254.169.254",
                                "100.64.0.7", "100.89.1.1", "::1", "fd00::1", "::ffff:10.0.0.1", "0.0.0.0",
                                "fe80::1", "224.0.0.251"])
def test_plages_privees_refusees(ip):
    from nokker.egress.proxy import _SSRF, is_blocked

    assert is_blocked(ip)
    assert _SSRF is not None  # la garde SSRF du noyau est bien réutilisée dans l'image du noyau


@pytest.mark.parametrize("ip", ["151.101.0.223", "1.1.1.1", "2606:4700::1111"])
def test_plages_publiques_permises(ip):
    from nokker.egress.proxy import is_blocked

    assert not is_blocked(ip)


def test_basic_url_avec_reference_en_identifiant():
    """`git push https://secret:T@hôte/…` (forme écrite par le modèle au passage réel) : Basic `secret:T`."""
    b = base64.b64encode(b"secret:T").decode()
    req, ev, _ = substitute(G, "git.forge.example", Request("GET", "/", [("Authorization", f"Basic {b}")]))
    assert base64.b64decode(req.headers[0][1][6:]) == f"x-access-token:{V}".encode()
    assert ev[0].where == "header:Authorization"
