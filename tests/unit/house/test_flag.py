"""Drapeau `house_room` : éteint (défaut), rien ne change — ni route, ni outil, ni texte vu par le modèle (scène
rejouée octet pour octet) ; allumé sans foyer : éteint ; allumé avec foyer : routes, `ask_house` pour les membres."""
from __future__ import annotations

from types import SimpleNamespace

from nokker.api import server
from nokker.config import NokkerConfig
from nokker.core.turn import TurnInput
from nokker.house import http as house_http
from nokker.house.room import AskHouseSource, HouseRoom
from nokker.household import http as household_http
from nokker.tenancy.context import scoped_thread_id, tenant_scope
from tests.unit.house.conftest import make_world


def _scene(tmp_path, *, configure: bool) -> list:
    w = make_world(tmp_path)
    w.brain.plan["web"] = [("tool", "mail_perso", {}), ("text", "Voilà ton courrier.")]
    if configure:
        household_http.configure(w.hh)
        try:
            assert server.configure_house_room(NokkerConfig(), w.kernel, None) is None
        finally:
            household_http.configure(None)
    with tenant_scope(w.adult.tenant_id):
        w.kernel.run_turn(TurnInput(thread_id=scoped_thread_id(w.adult.tenant_id, "th"), user_text="mon courrier ?",
                                    channel="web", sender=w.adult.account_id))
    return w.brain.seen


def test_flag_off_by_default_and_model_sees_the_same_bytes(tmp_path):
    assert NokkerConfig().house_room is False and NokkerConfig().house_room_settings is None
    assert _scene(tmp_path, configure=False) == _scene(tmp_path, configure=True)
    assert not house_http.enabled()


def test_flag_on_without_household_stays_off():
    kernel = SimpleNamespace(tools=SimpleNamespace(tester_sources=[], refresh=lambda: None))
    household_http.configure(None)
    assert server.configure_house_room(NokkerConfig(house_room=True), kernel, None) is None
    assert kernel.tools.tester_sources == [] and not house_http.enabled()


def test_flag_on_with_household_adds_routes_and_ask_house(tmp_path):
    w = make_world(tmp_path)
    kernel = SimpleNamespace(tools=SimpleNamespace(tester_sources=[], refresh=lambda: None), invariants=w.kernel.invariants)
    household_http.configure(w.hh)
    try:
        room = server.configure_house_room(NokkerConfig(house_room=True), kernel, None)
        assert isinstance(room, HouseRoom) and house_http.enabled()
        (src,) = kernel.tools.tester_sources
        assert isinstance(src, AskHouseSource)
        assert src.available_for(w.adult.tenant_id) and not src.available_for(w.lone.tenant_id)
        assert room.providers == {}  # aucun chantier de la maison branché : agents indisponibles, sauf intendant
        with tenant_scope(w.adult.tenant_id):
            view = room.view(w.adult.account_id)
        assert {a["slug"]: a["available"] for a in view["agents"]} == {
            "intendant": True, "maison": False, "courrier": False, "veille": False, "agenda": False, "memoire": True}
    finally:
        household_http.configure(None)
        house_http.configure(None)


def test_train_registers_the_merged_house_providers_only():
    """Intégration (train 0311) : une ligne par chantier de la maison fusionné — maison (home-assistant), veille
    (home-watch) ; courrier, mémoire et agenda sans fournisseur. Rien sans source construite."""
    from types import SimpleNamespace

    from nokker.api.server import house_room_on, register_house_providers
    from nokker.house import providers

    home, watch = object(), object()
    try:
        register_house_providers(home=home, home_watch=watch)
        assert providers.providers_for(None, None) == {"maison": home, "veille": watch}
        register_house_providers()
        assert providers.providers_for(None, None) == {}
    finally:
        providers.clear()
    assert not house_room_on(SimpleNamespace(house_room=True, household=False))
    assert not house_room_on(SimpleNamespace(house_room=False, household=True))
    assert house_room_on(SimpleNamespace(house_room=True, household=True))
