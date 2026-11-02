"""Passerelle `home-bridge` (docs/HOME-WATCH.md §4-§5) contre de faux Frigate, UniFi et Synology : liste blanche,
jeton, opérations nommées, texte seulement, médias signés, et séparation d'avec le noyau."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from tests.unit.homewatch.fakes import BRIDGE_TOKEN, FAKE_WEBP, OTHER_POLICY, PAUSE_POLICY, Foyer


@pytest.fixture
def foyer(tmp_path):
    f = Foyer(tmp_path)
    yield f
    f.close()


def op(foyer, name, payload=None, token=BRIDGE_TOKEN):
    return call(foyer.api_url, f"/v1/op/{name}", "POST", payload or {}, token=token)


def call(url, path, method="GET", body=None, token=BRIDGE_TOKEN):
    req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── configuration ──

def test_plain_secret_refused(tmp_path):
    from nokker.homebridge.config import BridgeConfigError, from_dict
    from nokker.homebridge.watch import Watch

    cfg = from_dict({"allow": [{"host": "192.168.1.1", "ports": [443]}],
                     "watch": {"unifi": {"url": "https://192.168.1.1", "api_key": "en-clair"}}},
                    token=BRIDGE_TOKEN, base_dir=tmp_path)
    with pytest.raises(BridgeConfigError, match="secret:NOM"):
        Watch.build(cfg)


def test_destinations_lan_only(tmp_path):
    from nokker.homebridge.config import Refused, from_dict
    from nokker.homebridge.watch.net import LanHttp, WatchEnv

    cfg = from_dict({"allow": [{"host": "192.168.1.10", "ports": [5000]}, {"host": "nas.local", "ports": [5001]},
                               {"host": "8.8.8.8", "ports": [443]}]}, token=BRIDGE_TOKEN, base_dir=tmp_path)
    cfg.allow.resolve = lambda host: {"nas.local": ["93.184.216.34"]}.get(host, [host])
    env = WatchEnv(cfg)
    env.check_url("http://192.168.1.10:5000/api/review")
    env.check_url("ws://192.168.1.10:5000/api/current")
    for url in ("http://192.168.1.10:5001/", "https://8.8.8.8/", "https://nas.local:5001/", "http://127.0.0.1:5000/",
                "ftp://192.168.1.10:5000/"):
        with pytest.raises(Refused):
            env.check_url(url)
    with pytest.raises(Refused):
        LanHttp(env).request("GET", "https://8.8.8.8/")


def test_secret_files_only(tmp_path):
    from nokker.homebridge.config import BridgeConfigError, from_dict
    from nokker.homebridge.watch.net import WatchEnv

    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "UNIFI_API_KEY").write_text("cle\n")
    env = WatchEnv(from_dict({}, token=BRIDGE_TOKEN, base_dir=tmp_path))
    assert env.secret("secret:UNIFI_API_KEY") == "cle"
    for bad in ("UNIFI_API_KEY", "secret:../home.yaml", "secret:ABSENT"):
        with pytest.raises(BridgeConfigError):
            env.secret(bad)


def test_api_requires_token(foyer):
    assert call(foyer.api_url, "/v1/health", token=None)[0] == 401
    assert op(foyer, "watch.targets", token="x" * 48)[0] == 401
    status, raw = call(foyer.api_url, "/v1/health")
    assert status == 200 and {"cameras.reviews", "network.clients", "nas.status", "watch.action",
                              "watch.targets"} <= set(json.loads(raw)["ops"])


# ── caméras : texte seulement ──

def test_reviews_are_text_only(foyer):
    status, raw = op(foyer, "cameras.reviews", {"after": time.time() - 86400})
    assert status == 200
    items = {i["id"]: i for i in json.loads(raw)["items"]}
    assert {i["niveau"] for i in items.values()} == {"dangereux", "normal", "suspect", "inconnu"}
    assert items["1730500000.1-abc"]["threat_level"] == 2 and items["1730500000.1-abc"]["has_thumb"] is True
    assert items["1730500300.4-jkl"]["has_thumb"] is False  # thumb_path hors de clips/review : ignoré
    text = raw.decode()
    for needle in ("thumb_path", "/media/", "clips/review", ".webp", "RIFF", "data:image"):
        assert needle not in text
    login = [r for r in foyer.frigate.requests if r[0] == "POST"]
    assert login and "password" not in json.dumps(login)


def test_media_thumb_signed(foyer):
    from nokker.homewatch.client import BridgeClient

    c = BridgeClient(base_url=foyer.api_url, token=BRIDGE_TOKEN, media_base_url=foyer.media_url)
    url, _ = c.media_url("thumb", "1730500000.1-abc")
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.read() == FAKE_WEBP
    assert call(url.replace("sig=", "sig=0"), "")[0] == 403  # signature altérée
    expired, _ = BridgeClient(base_url=foyer.api_url, token=BRIDGE_TOKEN, media_base_url=foyer.media_url,
                              clock=lambda: time.time() - 1000).media_url("thumb", "1730500000.1-abc")
    assert call(expired, "")[0] == 403
    other = BridgeClient(base_url=foyer.api_url, token="z" * 48, media_base_url=foyer.media_url)
    assert call(other.media_url("thumb", "1730500000.1-abc")[0], "")[0] == 403  # clé d'un autre jeton
    # le chemin est signé : la même signature ne vaut pas pour le direct
    assert call(url.replace("/thumb/1730500000.1-abc", "/live/portail"), "")[0] == 403
    # les routes média ne sont pas sur l'API, et l'API n'est pas sur l'écoute du foyer
    assert call(foyer.api_url, url[len(foyer.media_url):])[0] == 404
    assert call(foyer.media_url, "/v1/op/watch.targets", "POST", {})[0] == 404


def test_media_live_only_listed_cameras(foyer):
    from nokker.homewatch.client import BridgeClient

    c = BridgeClient(base_url=foyer.api_url, token=BRIDGE_TOKEN, media_base_url=foyer.media_url)
    with urllib.request.urlopen(c.media_url("live", "portail")[0], timeout=10) as r:
        assert r.read().startswith(b"--frame")
    assert call(c.media_url("live", "jardin")[0], "")[0] == 502  # pas dans `live` : refusé par la passerelle


# ── réseau : lecture, et une seule écriture possible ──

def test_unifi_clients_and_pause(foyer):
    status, raw = op(foyer, "network.clients")
    clients = json.loads(raw)["clients"]
    assert status == 200 and {c["mac"] for c in clients} == {"aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"}
    assert json.loads(op(foyer, "watch.targets")[1])["pause"] == ["console_salon"]
    status, raw = op(foyer, "watch.action", {"action": "pause_device", "target": "console_salon"})
    assert status == 200 and json.loads(raw)["changed"] is True
    assert foyer.unifi.policies[PAUSE_POLICY]["enabled"] is True
    put = foyer.unifi.writes[-1]
    assert put[0] == "PUT" and put[1].endswith(PAUSE_POLICY)
    assert set(put[2]) <= {"action", "connectionStateFilter", "description", "destination", "enabled",
                           "ipProtocolScope", "ipsecFilter", "loggingEnabled", "name", "reportFlowEnabled", "schedule",
                           "source"}
    # cible non déclarée, action inconnue : refus, aucune écriture
    n = len(foyer.unifi.writes)
    assert op(foyer, "watch.action", {"action": "pause_device", "target": OTHER_POLICY})[0] == 502
    assert op(foyer, "watch.action", {"action": "delete_rule", "target": "x"})[0] == 400
    assert len(foyer.unifi.writes) == n and foyer.unifi.policies[OTHER_POLICY]["enabled"] is True
    op(foyer, "watch.action", {"action": "resume_device", "target": "console_salon"})
    assert foyer.unifi.policies[PAUSE_POLICY]["enabled"] is False


# ── NAS ──

def test_synology_status_and_backup(foyer):
    status, raw = op(foyer, "nas.status")
    s = json.loads(raw)["systems"][0]
    assert status == 200 and s["kind"] == "synology"
    assert [d["healthy"] for d in s["disks"]] == [True, False]
    assert s["volumes"][0]["used_pct"] == 71.0
    b = s["backups"][0]
    assert b["key"] == "photos" and b["last_result"] == "failed" and b["last_success"] < b["last_run"]
    assert op(foyer, "watch.action", {"action": "run_backup", "target": "photos"})[0] == 200
    assert foyer.syno.backups_started == ["3"]
    assert "mot-de-passe-dsm" not in json.dumps(foyer.syno.requests)


def test_synology_internal_api_off_by_default(tmp_path):
    f = Foyer(tmp_path, internal_api=False)
    try:
        s = json.loads(op(f, "nas.status")[1])["systems"][0]
        assert s["disks"] == [] and "dsm_internal_api" in s["warnings"][0]
        assert f.syno.storage_calls == 0
        assert op(f, "watch.action", {"action": "run_backup", "target": "photos"})[0] == 502
        assert f.syno.backups_started == []
    finally:
        f.close()


def test_truenas_rpc():
    from pathlib import Path

    from nokker.homebridge.config import from_dict
    from nokker.homebridge.watch.nas import TrueNAS
    from nokker.homebridge.watch.net import WatchEnv

    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return {
            "pool.query": [{"name": "tank", "status": "DEGRADED", "healthy": False, "warning": True, "topology": {
                "data": [{"type": "MIRROR", "children": [
                    {"type": "DISK", "disk": "sda", "status": "ONLINE", "stats": {"read_errors": 0}},
                    {"type": "DISK", "disk": "sdb", "status": "FAULTED", "stats": {"checksum_errors": 3}}]}]}}],
            "alert.list": [{"level": "CRITICAL", "formatted": "Pool tank is DEGRADED", "dismissed": False}],
            "cloudsync.query": [{"id": 2, "description": "Photos vers S3", "job": {
                "state": "SUCCESS", "time_finished": {"$date": 1730500000000}}}],
            "replication.query": [{"id": 1, "name": "repli", "state": {"state": "ERROR",
                                                                      "datetime": {"$date": 1730600000000}}}],
        }.get(method, True)

    cfg = WatchEnv(from_dict({"allow": [{"host": "192.168.1.30", "ports": [443]}]}, token=BRIDGE_TOKEN,
                             base_dir=Path("/nonexistent")))
    t = TrueNAS.from_config(cfg, {"url": "wss://192.168.1.30/api/current", "api_key": "secret:TRUENAS_API_KEY",
                                  "backups": {"photos_s3": "cloudsync:2"}}, rpc=rpc)
    s = t.status()
    assert [d["healthy"] for d in s["disks"]] == [True, False] and s["volumes"][0]["healthy"] is False
    assert s["backups"][0]["last_success"] == 1730500000.0 and s["backups"][1]["last_success"] is None
    assert s["warnings"] == ["Pool tank is DEGRADED"]
    t.run_backup("photos_s3")
    assert calls[-1] == ("cloudsync.sync", [2])


def test_bridge_never_loads_core_state():
    """La passerelle ne charge ni base, ni coffre, ni noyau, ni la veille du noyau."""
    code = ("import sys; import nokker.homebridge.server, nokker.homebridge.watch.frigate, "
            "nokker.homebridge.watch.unifi, nokker.homebridge.watch.nas, nokker.homebridge.__main__; "
            "bad = sorted(m for m in sys.modules if m.startswith(('nokker.store', 'nokker.secrets', 'nokker.core', "
            "'nokker.homewatch', 'nokker.api', 'nokker.approvals', 'nokker.memory', 'psycopg'))); "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
