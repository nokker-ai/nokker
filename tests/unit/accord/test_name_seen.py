"""ACCORD — nom donné par la personne et dernière connexion (migration 094) : nom NFC, ≤ 40, sans caractère de
contrôle ni de format, vide = « ACCORD <key8> » ; PATCH sur un appareil actif seulement ; `last_seen_at` posée au
`ready`, au plus une fois par minute ; une panne d'écriture ne coupe jamais l'appareil."""
from __future__ import annotations

import pytest

from nokker.accord import http as accord_http
from nokker.accord.hub import SEEN_EVERY_S
from nokker.accord.store import NAME_MAX, clean_name
from tests.unit.accord.test_hub import NOW, connect, device, make, pair
from tests.unit.accord.test_wiring import FakeHandler


@pytest.mark.parametrize("raw,out", [
    ("  Bureau  ", "Bureau"),
    ("Café", "Café"),            # NFD → NFC
    ("", None), ("   ", None), (None, None),
    ("x" * NAME_MAX, "x" * NAME_MAX),
])
def test_clean_name_accepts(raw, out):
    assert clean_name(raw) == out


@pytest.mark.parametrize("raw", ["x" * (NAME_MAX + 1), "a\nb", "a\tb", "a\x00b", "a‮b", "a​b", "a b", 42,
                                 ["Bureau"]])
def test_clean_name_refuses(raw):
    with pytest.raises(ValueError):
        clean_name(raw)


def test_default_name_then_rename_then_reset():
    hub, _, _, _ = make()
    dev = pair(hub, device())
    j = hub.store.get(dev.device_id).to_json()
    assert j["name"] == f"ACCORD {dev.key8}" and j["name_is_default"] is True and j["last_seen_at"] is None
    assert hub.rename(dev.device_id, " Salon ").to_json()["name"] == "Salon"
    assert hub.status()["devices"][0]["name"] == "Salon"
    assert hub.rename(dev.device_id, "").to_json()["name_is_default"] is True


def test_patch_route():
    hub, _, _, _ = make()
    dev = pair(hub, device())
    url = f"/v1/local/accord/devices/{dev.device_id}"
    h = FakeHandler({"name": "Cuisine"})
    accord_http.handle(h, "PATCH", url, hub)
    assert h.sent[-1][0] == 200 and h.sent[-1][1]["name"] == "Cuisine" and h.sent[-1][1]["device_id"] == dev.device_id
    for body in ({"name": "a\nb"}, {"name": "x" * 41}, {}, ["Cuisine"], None):
        h = FakeHandler(body)
        accord_http.handle(h, "PATCH", url, hub)
        assert h.sent[-1][0] == 400 and h.sent[-1][1]["error"]["code"] == "bad_name"
    assert hub.store.get(dev.device_id).name == "Cuisine"  # un refus ne touche à rien
    h = FakeHandler({"name": "X"})
    accord_http.handle(h, "PATCH", "/v1/local/accord/devices/acc_" + "0" * 12, hub)
    assert h.sent[-1][0] == 404
    assert hub.revoke(dev.device_id)
    accord_http.handle(h, "PATCH", url, hub)
    assert h.sent[-1][0] == 404  # révoqué : plus de renommage
    assert hub.store.get(dev.device_id).name == "Cuisine"


def test_last_seen_on_ready_at_most_once_a_minute():
    hub, _, clock, _ = make()
    d = device()
    dev = pair(hub, d)
    connect(hub, d)
    assert hub.store.get(dev.device_id).last_seen_at == NOW
    clock.t += SEEN_EVERY_S - 1
    connect(hub, d)
    assert hub.store.get(dev.device_id).last_seen_at == NOW  # moins d'une minute : pas réécrite
    clock.t += 2
    connect(hub, d)
    assert hub.store.get(dev.device_id).last_seen_at == clock.t


def test_a_failing_touch_never_blocks_ready():
    hub, _, _, _ = make()
    d = device()
    pair(hub, d)

    def boom(*a):
        raise RuntimeError("base indisponible")

    hub.store.touch = boom
    connect(hub, d)  # `ready` quand même
