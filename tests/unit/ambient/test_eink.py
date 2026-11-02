"""Écrans e-ink : rendu à la bonne taille et profondeur, tableau COMMUN seulement (rien de privé), jeton par appareil
refusé une fois révoqué (noyau ET passerelle), appairage TRMNL, cloisonnement entre foyers et locataires."""
from __future__ import annotations

import datetime as dt

import pytest

from nokker.ambient import http as ambient_http
from nokker.ambient.displays import DisplayError, Displays, Membership, MemoryDisplayStore, sha
from nokker.ambient.frame import PRESETS, Board, FrameError, FrameRenderer, screen_size
from nokker.ambient.raster import decode_gray_png, png_info
from nokker.tenancy.context import tenant_scope
from tests.unit.ambient.helpers import handler

NOW = dt.datetime(2026, 11, 2, 7, 30)
ALICE = Membership(account_id="us_aaaaaaaaaaa1", household_id="tp_house_a", household_tenant="tn_aaaaaaaaaaaa",
                   profile="adult")
TEEN = Membership(account_id="us_aaaaaaaaaaa2", household_id="tp_house_a", household_tenant="tn_aaaaaaaaaaaa",
                  profile="teen")
BOB = Membership(account_id="us_bbbbbbbbbbb1", household_id="tp_house_b", household_tenant="tn_bbbbbbbbbbbb",
                 profile="adult")
BOARD = {"brief": "Piscine à 17 h.", "agenda": [{"quand": "09:00", "quoi": "Plombier"},
                                                 {"quand": "12:00", "quoi": "Rendez-vous médical d'Alice",
                                                  "private": True}],
         "courses": ["Lait", {"nom": "Cadeau surprise pour Bob", "private": True}],
         "meteo": {"resume": "Éclaircies", "min": 4, "max": 11}}


def svc(**kw) -> Displays:
    return Displays(store=MemoryDisplayStore(), now_local=lambda: NOW, **kw)


@pytest.fixture(autouse=True)
def _reset():
    yield
    ambient_http.configure(None)


# ─────────────────────── rendu ───────────────────────

@pytest.mark.parametrize("model", sorted(PRESETS))
def test_every_preset_renders_png_of_right_size_and_depth(model):
    w, h, d = PRESETS[model]
    png = FrameRenderer().render(Board.clean(BOARD), w, h, d, now=NOW)
    assert png_info(png) == (w, h, d, 0)  # gris, 1 bit (noir/blanc) ou 2 bits (4 gris)
    _, _, _, px = decode_gray_png(png)
    assert len(px) == w * h and set(px) <= set(range(1 << d)) and 0 in px  # du texte a été dessiné


def test_depth_override_and_custom_bounds():
    assert screen_size("trmnl", depth=2) == (800, 480, 2)
    assert screen_size("custom", 640, 384) == (640, 384, 1)
    for bad in (("custom", 100, 384), ("inconnu", None, None)):
        with pytest.raises(FrameError):
            screen_size(*bad)
    with pytest.raises(FrameError):
        screen_size("trmnl", depth=8)


def test_render_is_deterministic():
    r = FrameRenderer()
    assert r.render(Board.clean(BOARD), 800, 480, 1, now=NOW) == r.render(Board.clean(BOARD), 800, 480, 1, now=NOW)


def test_private_items_never_reach_the_image():
    clean = Board.clean(BOARD)
    assert all("médical" not in a["quoi"] for a in clean.agenda) and "Cadeau surprise pour Bob" not in clean.courses
    without = {**BOARD, "agenda": BOARD["agenda"][:1], "courses": ["Lait"]}
    r = FrameRenderer()
    assert r.render(clean, 800, 480, 1, now=NOW) == r.render(Board.clean(without), 800, 480, 1, now=NOW)
    assert Board.clean({"brief": {"texte": "secret de famille", "private": True}}).brief == ""


def test_secret_is_scrubbed_before_drawing():
    r = FrameRenderer(scrub=lambda s: s.replace("hunter2", "[secret:wifi]"))
    a = r.render(Board.clean({"brief": "Wi-Fi : hunter2"}), 400, 300, 1, now=NOW)
    b = FrameRenderer().render(Board.clean({"brief": "Wi-Fi : [secret:wifi]"}), 400, 300, 1, now=NOW)
    assert a == b


# ─────────────────────── écrans et jetons ───────────────────────

def test_register_list_revoke_and_token_refused_after_revoke():
    d = svc()
    out = d.register(ALICE, {"name": "Entrée", "model": "trmnl"})
    token, did = out["token"], out["display"]["id"]
    assert token.startswith("nkd_") and d.store.rows[did].token_sha256 == sha(token)  # empreinte seule en base
    assert token not in repr(d.store.rows)
    ambient_http.configure(ambient_http.Ambient(displays=d))
    h = handler()
    assert ambient_http.handle_device(h, "GET", f"/v1/eink/{token}.png")
    assert h.status == 200 and h.sent["Content-Type"] == "image/png" and png_info(h.body())[:2] == (800, 480)
    d.revoke(ALICE, did)
    for path, hdr in ((f"/v1/eink/{token}.png", {}), ("/v1/eink/trmnl/api/display", {"Access-Token": token})):
        h = handler(hdr)
        ambient_http.handle_device(h, "GET", path)
        assert h.status == 401
    assert d.list(ALICE)["displays"][0]["revoked"] is True


def test_only_adults_register_or_revoke_and_child_cannot_write_board():
    d = svc()
    with pytest.raises(DisplayError) as e:
        d.register(TEEN, {"model": "trmnl"})
    assert e.value.status == 403
    did = d.register(ALICE, {"model": "trmnl"})["display"]["id"]
    with pytest.raises(DisplayError):
        d.revoke(TEEN, did)
    d.put_board(TEEN, {"courses": ["Pain"]})  # un ado écrit le tableau commun
    with pytest.raises(DisplayError):
        d.put_board(Membership(**{**TEEN.__dict__, "profile": "child"}), {"courses": ["Bonbons"]})


def test_households_are_isolated():
    d = svc()
    ta = d.register(ALICE, {"model": "waveshare_4in2"})["token"]
    tb = d.register(BOB, {"model": "waveshare_4in2"})["token"]
    d.put_board(ALICE, {"brief": "Foyer A : vacances le 20."})
    # B ne voit ni l'écran ni le tableau de A, et ne peut pas révoquer l'écran de A
    assert [x["id"] for x in d.list(BOB)["displays"]] == [d.resolve(tb).id]
    assert d.board(BOB)["board"]["brief"] == ""
    with pytest.raises(DisplayError) as e:
        d.revoke(BOB, d.resolve(ta).id)
    assert e.value.status == 404 and d.resolve(ta) is not None
    # l'image de B est celle d'un tableau vide ; celle de A porte son brief
    empty = FrameRenderer().render(Board(), 400, 300, 1, now=NOW)
    assert d.frame(d.resolve(tb)).png == empty and d.frame(d.resolve(ta)).png != empty
    # le tableau est rangé sous le locataire du foyer (comme la RLS) : lu depuis un autre locataire, rien
    with tenant_scope("tn_bbbbbbbbbbbb"):
        assert d.store.board("tp_house_a").brief == ""


def test_frame_reads_only_the_common_board():
    """L'image ne lit que le tableau du foyer : aucun accès à la mémoire, aux notes ni aux fils d'un membre."""

    class OnlyBoard(MemoryDisplayStore):
        def __getattr__(self, name):  # tout autre accès échouerait
            raise AssertionError(name)

    d = Displays(store=OnlyBoard(), now_local=lambda: NOW)
    tok = d.register(ALICE, {"model": "trmnl"})["token"]
    d.put_board(ALICE, BOARD)
    assert png_info(d.frame(d.resolve(tok)).png)[:2] == (800, 480)


def test_trmnl_setup_then_display_protocol():
    d = svc()
    out = d.register(ALICE, {"model": "trmnl", "mac": "AA:BB:CC:DD:EE:01", "refresh_s": 1800})
    assert "token" not in out and out["setup_window_s"] == 900  # le jeton va à l'appareil seul
    ambient_http.configure(ambient_http.Ambient(displays=d, public_url="https://maison.example.org"))
    h = handler({"ID": "AA:BB:CC:DD:EE:99"})
    ambient_http.handle_device(h, "GET", "/v1/eink/trmnl/api/setup")
    assert h.status == 404
    h = handler({"ID": "aa:bb:cc:dd:ee:01"})
    ambient_http.handle_device(h, "GET", "/v1/eink/trmnl/api/setup")
    body = h.json()
    assert h.status == 200 and body["status"] == 200 and body["api_key"].startswith("nkd_")
    token = body["api_key"]
    h = handler({"ID": "AA:BB:CC:DD:EE:01"})  # une seule fois
    ambient_http.handle_device(h, "GET", "/v1/eink/trmnl/api/setup")
    assert h.status == 404
    h = handler({"ID": "AA:BB:CC:DD:EE:01", "Access-Token": token, "Width": "800", "Height": "480"})
    ambient_http.handle_device(h, "GET", "/v1/eink/trmnl/api/display")
    disp = h.json()
    assert h.status == 200 and disp["status"] == 0 and disp["refresh_rate"] == 1800
    assert disp["image_url"].startswith(f"https://maison.example.org/v1/eink/{token}.png?v=")
    assert disp["filename"] and disp["update_firmware"] is False and disp["reset_firmware"] is False
    h = handler({"Access-Token": token}, b'{"logs": []}')
    ambient_http.handle_device(h, "POST", "/v1/eink/trmnl/api/log")
    assert h.status == 204


def test_setup_window_expires():
    clock = [1000.0]
    d = svc(clock=lambda: clock[0])
    d.register(ALICE, {"model": "trmnl", "mac": "AA:BB:CC:DD:EE:02"})
    clock[0] += 901
    assert d.trmnl_setup("AA:BB:CC:DD:EE:02") is None


def test_bad_inputs():
    d = svc()
    for body in ({"model": "trmnl", "mac": "pas-une-mac"}, {"model": "custom", "width": 10, "height": 10}, "x",
                 {"model": "trmnl", "refresh_s": "souvent"}):
        with pytest.raises(DisplayError):
            d.register(ALICE, body)
    with pytest.raises(DisplayError):
        d.put_board(ALICE, {"agenda": "pas une liste"})
    assert d.resolve("nkd_court") is None and d.resolve("") is None
    for _ in range(10):
        d.register(ALICE, {"model": "trmnl"})
    with pytest.raises(DisplayError) as e:
        d.register(ALICE, {"model": "trmnl"})
    assert e.value.status == 409


def test_member_routes():
    d = svc()
    ambient_http.configure(ambient_http.Ambient(displays=d))
    h = handler(body={"name": "Cuisine", "model": "kindle_600x800"})
    assert ambient_http.handle_member(h, "POST", "/v1/ambient/displays", ALICE)
    assert h.status == 201 and h.json()["display"]["width"] == 600
    h = handler(body=BOARD)
    ambient_http.handle_member(h, "PUT", "/v1/ambient/board", ALICE)
    assert h.status == 200 and h.json()["board"]["version"] == 1
    h = handler()
    ambient_http.handle_member(h, "GET", "/v1/ambient/board", None)
    assert h.status == 403  # pas de foyer
    h = handler()
    ambient_http.handle_member(h, "DELETE", "/v1/ambient/displays/dp_0000000000000000", ALICE)
    assert h.status == 404


def test_routes_absent_when_flag_off():
    ambient_http.configure(None)
    assert not ambient_http.enabled() and not ambient_http.is_ambient_path("/v1/ambient/board")
    assert not ambient_http.is_device_path("/v1/eink/trmnl/api/display")
    assert ambient_http.handle_device(handler(), "GET", "/v1/eink/trmnl/api/display") is False
    assert ambient_http.handle_member(handler(), "GET", "/v1/ambient/board", ALICE) is False
