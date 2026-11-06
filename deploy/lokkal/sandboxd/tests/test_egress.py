import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import egress  # noqa: E402


@pytest.mark.parametrize("ip", ["10.250.90.2", "172.18.0.1", "192.168.1.1", "127.0.0.1", "169.254.169.254",
                                "100.64.0.7", "::1", "fd00::1", "::ffff:10.0.0.1", "0.0.0.0"])
def test_bloque(ip):
    assert egress.is_blocked(ip)


@pytest.mark.parametrize("ip", ["151.101.0.223", "1.1.1.1", "2606:4700::1111"])
def test_public(ip):
    assert not egress.is_blocked(ip)


# ─────────────── hôtes ssh déclarés (docs/en/ops.md) ───────────────
import asyncio  # noqa: E402
import json  # noqa: E402


def _allow(tmp_path, monkeypatch, hosts, own=()):
    f = tmp_path / "ssh-hosts.json"
    f.write_text(json.dumps({"v": 1, "hosts": hosts}))
    monkeypatch.setattr(egress, "ALLOW_FILE", str(f))
    monkeypatch.setattr(egress, "OWN_NETWORKS", [egress.ipaddress.ip_network(n) for n in own])
    egress._allow_cache.update(key=None, hosts=frozenset())
    return f


def _connect(host, port):
    try:
        return asyncio.run(egress.resolve_connect(host, port))
    except egress.Denied as e:
        return (e.status, str(e))


def test_hote_declare_prive_ouvert_adresse_et_port(tmp_path, monkeypatch):
    _allow(tmp_path, monkeypatch, [{"address": "192.168.1.20", "port": 22}, {"address": "100.89.156.12", "port": 2222},
                                   {"address": "10.0.0.5", "port": 22}])
    assert _connect("192.168.1.20", 22) == "192.168.1.20"
    assert _connect("100.89.156.12", 2222) == "100.89.156.12"
    assert _connect("[10.0.0.5]", 22) == "10.0.0.5"
    # même adresse, autre port ; autre adresse du même réseau : refusés
    assert _connect("192.168.1.20", 2222)[0] == 403
    assert _connect("100.89.156.12", 22)[0] == 403
    assert _connect("192.168.1.21", 22)[0] == 403
    assert _connect("192.168.1.20", 443)[0] == 403     # privé non déclaré sur ce port : règle d'avant


def test_port_22_non_declare_refuse_meme_public(tmp_path, monkeypatch):
    _allow(tmp_path, monkeypatch, [{"address": "203.0.113.5", "port": 22}])
    assert _connect("1.1.1.1", 22) == (403, "ssh vers 1.1.1.1:22 refusé : hôte non déclaré dans hosts.yaml "
                                            "(adresse et port)")
    assert _connect("203.0.113.5", 22) == "203.0.113.5"   # Internet déclaré : même règle
    assert _connect("1.1.1.1", 443) == "1.1.1.1"           # HTTPS public : inchangé


def test_jamais_joignable_meme_declare(tmp_path, monkeypatch):
    _allow(tmp_path, monkeypatch, [{"address": a, "port": 22} for a in
                                   ("169.254.169.254", "127.0.0.1", "0.0.0.0", "::1", "fe80::1", "224.0.0.1",
                                    "172.20.0.5", "::ffff:127.0.0.1")], own=["172.20.0.0/16"])
    for a in ("169.254.169.254", "127.0.0.1", "0.0.0.0", "::1", "fe80::1", "224.0.0.1", "172.20.0.5",
              "::ffff:127.0.0.1"):
        st, msg = _connect(a, 22)
        assert st == 403 and "jamais joignable" in msg, a


def test_nom_declare_resolu_et_verifie(tmp_path, monkeypatch):
    _allow(tmp_path, monkeypatch, [{"address": "Box.Lan", "port": 22}, {"address": "meta.lan", "port": 22}])

    async def fake(h):
        return {"box.lan": ["192.168.1.30"], "meta.lan": ["169.254.169.254"]}[h.lower()]
    monkeypatch.setattr(egress, "_resolve", fake)
    assert _connect("box.lan", 22) == "192.168.1.30"       # casse ignorée
    assert _connect("meta.lan", 22)[0] == 403              # un nom déclaré ne mène pas aux métadonnées


def test_liste_absente_illisible_ou_mal_formee_echec_ferme(tmp_path, monkeypatch):
    f = _allow(tmp_path, monkeypatch, [])
    for content in ("{", "[]", '{"hosts": "x"}', '{"hosts": [{"address": "192.168.1.20", "port": "22"}]}',
                    '{"hosts": [{"address": "-oProxyCommand=x", "port": 22}]}',
                    '{"hosts": [{"address": "192.168.1.20", "port": 70000}]}'):
        f.write_text(content)
        egress._allow_cache.update(key=None)
        assert egress.declared_hosts() == frozenset() or ("192.168.1.20", 22) not in egress.declared_hosts(), content
        assert _connect("192.168.1.20", 22)[0] == 403, content
    f.unlink()
    egress._allow_cache.update(key=None)
    assert _connect("192.168.1.20", 22)[0] == 403


def test_liste_relue_quand_elle_change(tmp_path, monkeypatch):
    f = _allow(tmp_path, monkeypatch, [])
    assert _connect("192.168.1.20", 22)[0] == 403
    f2 = tmp_path / "n.json"
    f2.write_text(json.dumps({"hosts": [{"address": "192.168.1.20", "port": 22}]}))
    f2.replace(f)                                          # remplacement atomique, comme le noyau
    assert _connect("192.168.1.20", 22) == "192.168.1.20"


def test_reseaux_du_container_lus_dans_proc():
    nets = egress._own_networks()
    assert all(isinstance(n, (egress.ipaddress.IPv4Network, egress.ipaddress.IPv6Network)) for n in nets)
