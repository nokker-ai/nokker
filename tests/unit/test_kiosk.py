"""Kiosque HDMI d'un Lokkal (docs/KIOSK.md, nokker/kiosk.py). Prouve : éteint par défaut ; Lokkal seulement (cloud :
404) ; jeton local du titulaire seulement (faux ou périmé : 401) ; sur la machine seulement (passerelle : 403) ;
lecture seule (405) ; 6 lectures par minute ; Home Assistant : seulement les entités listées ET exposées, jamais
serrure, alarme, garage ni portail (refus au réglage ET à la lecture) ; une source en panne rend null / [], jamais
un 500 ; aucun contenu d'agenda, de mail ni de carte dans la vue."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from nokker import kiosk as kiosk_mod
from nokker.kiosk import READS_PER_MIN, Kiosk

T0 = 1_794_236_400.0  # 09.11.2026 16:00 Europe/Zurich


def _home(states):
    return SimpleNamespace(snapshot=lambda: SimpleNamespace(states={s["entity_id"]: s for s in states}))


STATES = [
    {"entity_id": "input_boolean.nokker_test_lampe", "state": "on", "attributes": {"friendly_name": "Nokker test lampe"}},
    {"entity_id": "input_boolean.nokker_test_prise", "state": "off", "attributes": {}},
    {"entity_id": "sensor.salon", "state": "21.5", "attributes": {"unit_of_measurement": "°C"}},
    {"entity_id": "cover.entree", "state": "closed", "attributes": {"device_class": "garage"}},
    {"entity_id": "lock.porte", "state": "locked", "attributes": {}},
]


def _kiosk(**kw):
    raw = {"enabled": True, "place": {"lat": 46.33, "lon": 6.06, "stops": ["8501000", "8501001"]},
           "ha_entities": ["input_boolean.nokker_test_lampe", "input_boolean.nokker_test_prise", "sensor.salon",
                           "cover.entree", "sensor.absent"]}
    raw.update(kw.pop("raw", {}))
    k = Kiosk.from_config(raw, clock=lambda: T0)
    assert k is not None
    for name, v in kw.items():
        setattr(k, name, v)
    return k


def test_off_by_default_and_bad_config_stays_off():
    assert Kiosk.from_config({}) is None and Kiosk.from_config({"enabled": False}) is None
    assert Kiosk.from_config(None) is None
    for bad in (["lock.porte"], ["alarm_control_panel.maison"], ["cover.garage_door"], ["cover.portail"], ["pas une entité"]):
        assert Kiosk.from_config({"enabled": True, "ha_entities": bad}) is None
    assert Kiosk.from_config({"enabled": True, "place": "Gex"}) is None


def test_view_is_local_filtered_and_has_no_private_content():
    calls = []
    k = _kiosk(home=_home(STATES), weather=lambda lat, lon: {"now_c": None, "hi_c": 12, "lo_c": 4, "code": 3},
               departures=lambda stop, n: (calls.append((stop, n)), [{"line": "F", "minutes": 7 if stop.endswith("0") else 3}])[1])
    v = k.view()
    assert v["now"].startswith("2026-11-09T") and v["tz"] == "Europe/Zurich" and v["presenter"] == {"slot": True}
    assert v["weather"]["hi_c"] == 12
    assert [d["minutes"] for d in v["departures"]] == [3, 7] and calls == [("8501000", 4), ("8501001", 4)]
    assert v["ha"] == [
        {"entity_id": "input_boolean.nokker_test_lampe", "name": "Nokker test lampe", "state": "on",
         "domain": "input_boolean"},
        {"entity_id": "input_boolean.nokker_test_prise", "name": "input_boolean.nokker_test_prise", "state": "off",
         "domain": "input_boolean"},
        {"entity_id": "sensor.salon", "name": "sensor.salon", "state": "21.5", "domain": "sensor", "unit": "°C"},
    ]  # garage (classe posée dans HA) filtré à la lecture ; serrure jamais listée ; entité absente sautée
    assert set(v) == {"now", "tz", "weather", "departures", "ha", "presenter", "night"}  # ni agenda, ni mail, ni carte
    assert v["night"] is True  # mode nuit de l'écran par défaut


def test_night_mode_can_be_turned_off_by_config():
    assert Kiosk.from_config({"enabled": True}).view()["night"] is True
    assert Kiosk.from_config({"enabled": True, "night": False}).view()["night"] is False
    assert Kiosk.from_config({"enabled": True, "night": "non"}).view()["night"] is True  # seul `false` le coupe


def test_sources_down_never_500():
    def boom(*_a):
        raise RuntimeError("panne")

    k = _kiosk(home=SimpleNamespace(snapshot=boom), weather=boom, departures=boom)
    v = k.view()
    assert v["weather"] is None and v["departures"] == [] and v["ha"] == []
    bare = _kiosk(raw={"place": {}})
    assert bare.view()["weather"] is None and bare.view()["departures"] == [] and bare.view()["ha"] == []


def test_reads_are_rate_limited():
    now = [T0]
    k = _kiosk()
    k.clock = lambda: now[0]
    assert all(k.read_allowed() for _ in range(READS_PER_MIN)) and not k.read_allowed()
    now[0] += 61
    assert k.read_allowed()


# ───────────────────────── par le VRAI serveur ─────────────────────────

def _req(port, token=None, method="GET", headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/local/kiosk", method=method, headers=h,
                                 data=b"{}" if method != "GET" else None)
    req.add_unredirected_header("Host", "127.0.0.1:8742")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def serve():
    from nokker.api.server import make_handler
    from tests.unit.api.test_server import make_kernel

    started = []

    def run(keys, local_api=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), keys, local_api=local_api))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd.server_address[1]

    yield run
    for h in started:
        h.shutdown()
    kiosk_mod.configure(None)


def test_lokkal_owner_on_the_machine_reads_the_kiosk(serve, tmp_path):
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.test_local_api import _api

    api = _api(tmp_path)
    port = serve({api.token: LOCAL_OWNER}, local_api=api)
    assert _req(port, api.token)[0] == 404  # drapeau éteint : la route n'existe pas
    kiosk_mod.configure(_kiosk(home=_home(STATES)))
    st, v = _req(port, api.token)
    assert st == 200 and [h["entity_id"] for h in v["ha"]][:1] == ["input_boolean.nokker_test_lampe"]
    assert _req(port, "faux")[0] == 401  # jeton faux, ou ancien jeton local après rotation (session périmée)
    assert _req(port)[0] == 401
    assert _req(port, "us_membre_du_foyer")[0] == 401  # un membre n'a pas le jeton local
    st, out = _req(port, api.token, headers={"X-Nokker-Relay": "1"})
    assert st == 403  # par la passerelle : jamais (jeton local refusé en amont ; jeton relayé : kiosk_on_machine)
    assert _req(port, api.token, method="POST")[0] == 405
    assert _req(port, api.token, method="DELETE")[0] == 405


def test_rotated_local_token_is_refused(serve, tmp_path):
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.test_local_api import _api

    from nokker.local.api import LocalApi

    first = _api(tmp_path)
    old = first.token
    (tmp_path / "tok").unlink()
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(tmp_path / "projets")],
                                                                          "backend": "local"}}, port=8742)
    assert api.token != old
    port = serve({api.token: LOCAL_OWNER}, local_api=api)
    kiosk_mod.configure(_kiosk())
    assert _req(port, old)[0] == 401 and _req(port, api.token)[0] == 200


def test_relayed_owner_gets_kiosk_on_machine():
    out = []
    h = SimpleNamespace(_json=lambda st, body: out.append((st, body)))
    kiosk_mod.configure(_kiosk())
    try:
        kiosk_mod.handle(h, "GET", lokkal=True, via_relay=True, owner=True)
        kiosk_mod.handle(h, "GET", lokkal=False, via_relay=False, owner=True)
    finally:
        kiosk_mod.configure(None)
    assert out[0][0] == 403 and out[0][1]["error"]["code"] == "kiosk_on_machine" and out[1][0] == 404


def test_cloud_never_serves_the_kiosk(serve):
    port = serve({"tok_ana": "us_ana"})
    kiosk_mod.configure(_kiosk())  # même configuré par erreur : sur nokker.ai, 404
    assert _req(port, "tok_ana")[0] == 404 and _req(port)[0] == 404


def test_configure_kiosk_is_lokkal_only():
    from nokker.api.server import configure_kiosk
    from nokker.config import NokkerConfig
    from tests.unit.api.test_server import make_kernel

    cfg = NokkerConfig(kiosk={"enabled": True, "ha_entities": ["input_boolean.nokker_test_lampe"]})
    configure_kiosk(cfg, make_kernel(), None, lokkal=False)
    assert kiosk_mod.service() is None
    configure_kiosk(cfg, make_kernel(), "maison", lokkal=True)
    assert kiosk_mod.service() is not None and kiosk_mod.service().home == "maison"
    configure_kiosk(NokkerConfig(), make_kernel(), None, lokkal=True)
    assert kiosk_mod.service() is None


# ── écran du mur publié (V7, `ambient.screens.enabled`) ──
BASE_KEYS = {"now", "tz", "weather", "departures", "ha", "presenter", "night"}


def test_screen_field_absent_when_screens_are_off():
    v = _kiosk().view()
    assert set(v) == BASE_KEYS and "screen" not in v  # écrans éteints : la vue est celle d'avant


def test_screen_null_when_nothing_is_published_and_failures_never_500():
    k = _kiosk(screen=lambda: None)
    assert k.view()["screen"] is None and set(k.view()) == BASE_KEYS | {"screen"}

    def boom():
        raise RuntimeError("magasin hors service")

    assert _kiosk(screen=boom).view()["screen"] is None
    assert _kiosk(screen=lambda: {"id": "scr_1", "version": 1, "title": "x"}).view()["screen"] is None  # sans cadre : rien


def test_screen_view_has_no_html_and_keeps_the_rest_identical():
    full = {"id": "scr_1", "version": 3, "title": "Courses", "html": "<!doctype html><p>secret</p>",
            "frame_url": "/v1/screens/frame/abc?exp=1&sig=2"}
    k = _kiosk(home=_home(STATES), screen=lambda: full)
    v = k.view()
    assert v["screen"] == {"id": "scr_1", "version": 3, "title": "Courses", "frame_url": "/v1/screens/frame/abc?exp=1&sig=2"}
    assert "secret" not in json.dumps(v) and "<" not in json.dumps(v["screen"])
    base = _kiosk(home=_home(STATES)).view()
    assert {k_: v[k_] for k_ in BASE_KEYS} == base  # heure, tuiles maison, présentateur, nuit : identiques


def test_configure_kiosk_wires_the_published_screen_only_when_screens_are_on():
    from nokker.api.server import configure_kiosk
    from nokker.config import NokkerConfig
    from nokker.screens import wiring as screens_wiring
    from tests.unit.api.test_server import make_kernel

    cfg = NokkerConfig(kiosk={"enabled": True})
    try:
        screens_wiring.configure({})
        configure_kiosk(cfg, make_kernel(), None, lokkal=True)
        assert kiosk_mod.service().screen is None and "screen" not in kiosk_mod.service().view()
        svc = screens_wiring.configure({"screens": {"enabled": True}, "glance": {"enabled": True}}, local=True)
        configure_kiosk(cfg, make_kernel(), None, lokkal=True)
        assert kiosk_mod.service().view()["screen"] is None  # allumé, rien de publié
        # publication de bout en bout (carte approuvée) : la vue du kiosque porte le cadre, sans agenda
        from tests.unit.screens.test_service import SPEC, Cards, ctx
        from nokker.screens import texts as T
        from nokker.tenancy.context import INTERNAL_TENANT

        svc.approvals = Cards()
        svc.clock = lambda: 1_800_000_000.0
        r = svc.tool_source().call(T.TOOL, {**SPEC, "title": "Semaine"}, ctx(sender=None))
        sid = r.content.split("écran ", 1)[1].split(" ", 1)[0]
        svc.bubble_action(INTERNAL_TENANT, {"screen_id": sid}, "publish", None)
        svc.approvals.cards[-1].status = "approved"
        svc.publish_handler()(svc.approvals.cards[-1])
        s = kiosk_mod.service().view()["screen"]
        assert s is not None and s["title"] == "Semaine" and s["version"] == 1 and s["id"] == sid
        assert set(s) == {"id", "version", "title", "frame_url"} and s["frame_url"].startswith("/v1/screens/frame/")
        key, _, q = s["frame_url"][len("/v1/screens/frame/"):].partition("?")
        params = dict(p.split("=") for p in q.split("&"))
        html = svc.frame_html(key, params["exp"], params["sig"])
        assert "pain" in html and "Sur le mur" not in html and "<script" not in html  # pas de rendez-vous au kiosque
    finally:
        screens_wiring.configure({})
        kiosk_mod.configure(None)
