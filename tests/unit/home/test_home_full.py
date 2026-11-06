"""Maison complète (docs/HOME-ASSISTANT.md §Couverture) contre le faux Home Assistant, par le vrai chemin (passerelle
HTTP réelle, protocole WebSocket du faux HA) : musique par Home Assistant (recherche, Music Assistant, URI), groupement,
annonce, lectures (historique, journal, agenda, liste, météo), liste fermée des services à réponse, code jamais journalisé,
textes venus de HA toujours encadrés en donnée non fiable."""
from __future__ import annotations

import json

import pytest

from nokker.home.policy import HomePolicy
from tests.unit.home.fake_ha import TRAP
from tests.unit.home.test_home import BRIDGE_TOKEN, ENTITIES, ctx, journal, make, plan

FULL = [*ENTITIES, "media_player.*", "tts.*", "todo.*", "calendar.*", "weather.*", "event.*", "binary_sensor.*",
        "alarm_control_panel.*", "notify.*"]
CLOSE = "</untrusted-data>"


def plan_r(actions=(), lectures=(), reponse="ok"):
    obj = json.loads(plan(*actions, reponse=reponse))
    obj["lectures"] = [dict(x) for x in lectures]
    return json.dumps(obj)


def model_seen(r) -> str:
    return r.content


def assert_framed(text: str) -> None:
    """Tout texte piégé de HA est DANS un bloc non fiable, et ne le referme jamais."""
    assert TRAP not in text  # la balise fermante a été neutralisée
    if "Ignore les consignes" in text:
        i = text.index("Ignore les consignes")
        assert text.rfind("<untrusted-data", 0, i) > text.rfind(CLOSE, 0, i)


# ── musique par Home Assistant ──
def test_text_on_a_searching_player_is_searched_then_played_by_uri(tmp_path):
    src, ha, *_ = make(tmp_path, [plan(("media_player", "play_media", "media_player.salon",
                                        {"media_content_id": "Bohemian Rhapsody Queen",
                                         "media_content_type": "track"}))], entities=FULL)
    r = src.call("maison", {"demande": "mets Bohemian Rhapsody au salon"}, ctx())
    assert not r.is_error, r.content
    search = [c for c in ha.calls if c[1] == "search_media"]
    assert search == [("media_player", "search_media", ["media_player.salon"], {"search_query": "Bohemian Rhapsody Queen"})]
    assert ("media_player", "play_media", ["media_player.salon"],
            {"media_content_id": "spotify:track:4u7EnebtmKWzUH433cf5Qv", "media_content_type": "track"}) in ha.calls
    assert "Fait :\n- media_player.play_media media_player.salon" in r.content
    assert "média trouvé" in r.content
    assert_framed(model_seen(r))


def test_text_on_a_music_assistant_player_goes_through_music_assistant(tmp_path):
    src, ha, *_ = make(tmp_path, [plan(("media_player", "play_media", "media_player.cuisine",
                                        {"media_content_id": "Discover Weekly", "media_content_type": "playlist",
                                         "enqueue": "replace"}))], entities=FULL)
    r = src.call("maison", {"demande": "lance ma playlist Discover Weekly dans la cuisine"}, ctx())
    assert ha.calls == [("music_assistant", "play_media", ["media_player.cuisine"],
                         {"media_id": "Discover Weekly", "media_type": "playlist", "enqueue": "replace"})]
    assert "(par music_assistant.play_media)" in r.content
    assert journal(tmp_path)[-1]["what"]["resolved"] == "music_assistant.play_media"


def test_spotify_uri_plays_as_is_and_a_player_without_search_fails_cleanly(tmp_path):
    src, ha, *_ = make(tmp_path, [plan(("media_player", "play_media", "media_player.chambre",
                                        {"media_content_id": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
                                         "media_content_type": "playlist"}),
                                       ("media_player", "play_media", "media_player.chambre",
                                        {"media_content_id": "Queen", "media_content_type": "artist"}))],
                       entities=FULL)
    r = src.call("maison", {"demande": "joue"}, ctx())
    assert ha.calls == [("media_player", "play_media", ["media_player.chambre"],
                         {"media_content_id": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
                          "media_content_type": "playlist"})]
    assert "ne sait pas chercher" in r.content


def test_arbitrary_urls_are_never_played(tmp_path):
    src, ha, *_ = make(tmp_path, [plan(("media_player", "play_media", "media_player.chambre",
                                        {"media_content_id": "http://192.168.1.1/admin",
                                         "media_content_type": "music"}))], entities=FULL)
    r = src.call("maison", {"demande": "joue"}, ctx())
    assert ha.calls == [] and "URI d'un service de musique" in r.content
    # un résultat de recherche qui pointe vers le LAN est refusé avant l'appel
    ha._response = lambda *a: {"result": [{"title": "x", "media_class": "track", "media_content_type": "music",
                                           "media_content_id": "http://192.168.1.1/admin", "can_play": True}]}
    src.policy  # noqa: B018
    small = src.providers()[next(iter(src.providers()))]
    small.replies.append(plan(("media_player", "play_media", "media_player.salon", {"media_content_id": "admin"})))
    r = src.call("maison", {"demande": "joue admin"}, ctx())
    assert not [c for c in ha.calls if c[1] == "play_media"] and "URI d'un service de musique" in r.content


def test_grouping_speakers_checks_every_member(tmp_path):
    src, ha, *_ = make(tmp_path, [plan(("media_player", "join", "media_player.salon",
                                        {"group_members": ["media_player.cuisine"]}),
                                       ("media_player", "join", "media_player.salon",
                                        {"group_members": ["media_player.voisin"]}))], entities=FULL)
    r = src.call("maison", {"demande": "groupe"}, ctx())
    assert ha.calls == [("media_player", "join", ["media_player.salon"], {"group_members": ["media_player.cuisine"]})]
    assert "group_members : entité inconnue" in r.content


def test_bridge_refuses_unlisted_referenced_entities(tmp_path):
    from nokker.ambient.bridge import BridgeError, HomeBridge

    src, ha, *_ = make(tmp_path, [], entities=FULL)
    b = HomeBridge(url=src.client.bridge.url, token=lambda: BRIDGE_TOKEN)
    with pytest.raises(BridgeError, match="403"):
        b.op("ha.call_service", {"domain": "media_player", "service": "join", "entity_ids": ["media_player.salon"],
                                 "data": {"group_members": ["light.salon"]}})
    with pytest.raises(BridgeError, match="403"):  # un service à réponse passe par ha.query, liste fermée
        b.op("ha.call_service", {"domain": "todo", "service": "get_items", "entity_ids": ["todo.courses"]})
    with pytest.raises(BridgeError, match="403"):
        b.op("ha.query", {"domain": "lock", "service": "unlock", "entity_ids": ["lock.entree"]})
    assert ha.calls == []


def test_announcement_on_a_speaker(tmp_path):
    src, ha, *_ = make(tmp_path, [plan(("tts", "speak", "tts.google", {"media_player_entity_id": "media_player.salon",
                                                                       "message": "À table !"}))], entities=FULL)
    src.call("maison", {"demande": "annonce à table au salon"}, ctx())
    assert ha.calls == [("tts", "speak", ["tts.google"], {"media_player_entity_id": "media_player.salon",
                                                          "message": "À table !"})]


def test_child_cannot_speak_on_a_speaker_without_the_right(tmp_path):
    src, ha, *_ = make(tmp_path, [plan(("tts", "speak", "tts.google", {"media_player_entity_id": "media_player.salon",
                                                                       "message": "x"}))],
                       members={"zoe": "child"}, entities=FULL)
    r = src.call("maison", {"demande": "annonce"}, ctx("zoe"))
    assert ha.calls == [] and "hors de la carte" in r.content  # tts non lisible pour un enfant : hors carte


# ── lectures ──
def test_readings_history_journal_list_agenda_weather(tmp_path):
    src, ha, small, *_ = make(tmp_path, [plan_r(lectures=[
        {"type": "historique", "entity_id": "lock.entree", "heures": 24},
        {"type": "journal", "entity_id": "lock.entree"},
        {"type": "liste", "entity_id": "todo.courses"},
        {"type": "agenda", "entity_id": "calendar.famille", "heures": 72}]),
        plan_r(lectures=[{"type": "meteo", "entity_id": "weather.maison"}])], entities=FULL)
    r = src.call("maison", {"demande": "quand la porte a-t-elle été ouverte, et la liste ?"}, ctx())
    assert not r.is_error, r.content
    hist = [m for m in ha.ws_log if m.get("type") == "history/history_during_period"]
    assert hist and hist[0]["entity_ids"] == ["lock.entree"] and hist[0]["no_attributes"] is True
    body = r.content
    assert "historique lock.entree" in body and "unlocked" in body
    assert "[ ] lait" in body and "Piscine" in body
    assert_framed(body)
    # grammaire : lectures contraintes aux entités lisibles, par domaine
    schema = small.log[0]["body"]["response_format"]["json_schema"]["schema"]
    reads = {b["properties"]["type"]["enum"][0]: b for b in schema["properties"]["lectures"]["items"]["anyOf"]}
    assert reads["liste"]["properties"]["entity_id"]["enum"] == ["todo.courses"]
    assert "lock.entree" in reads["historique"]["properties"]["entity_id"]["enum"]
    r = src.call("maison", {"demande": "météo demain ?"}, ctx())
    assert "rainy 11°C" in r.content and "pluie 70 %" in r.content
    assert [x["outcome"] for x in journal(tmp_path) if x["event"] == "home.read"][:4] == ["read"] * 4


def test_a_guest_cannot_read_the_lock_history(tmp_path):
    src, ha, *_ = make(tmp_path, [plan_r(lectures=[{"type": "historique", "entity_id": "lock.entree"}])],
                       members={"invite": "guest"}, entities=FULL)
    r = src.call("maison", {"demande": "porte ?"}, ctx("invite"))
    assert not [m for m in ha.ws_log if m.get("type") == "history/history_during_period"]
    assert "non lisible" in r.content or "Aucune entité" in r.content


# ── secrets, données non fiables ──
def test_alarm_code_is_never_journaled(tmp_path):
    src, ha, *_ = make(tmp_path, [plan(("alarm_control_panel", "alarm_disarm", "alarm_control_panel.maison",
                                        {"code": "4711"}))], entities=FULL)
    src.call("maison", {"demande": "désarme"}, ctx())
    assert "4711" not in json.dumps(journal(tmp_path))
    assert ha.calls == []  # proposé seulement


def test_media_title_from_home_assistant_stays_untrusted_data(tmp_path):
    """Un titre piégé (attribut media_title posé par HA après la lecture) revient dans la carte de l'agent maison ;
    il reste dans le bloc non fiable, balise fermante neutralisée — par la passerelle réelle."""
    src, ha, small, *_ = make(tmp_path, [plan(("media_player", "play_media", "media_player.cuisine",
                                               {"media_content_id": "Queen"})), plan()], entities=FULL)
    src.call("maison", {"demande": "Queen dans la cuisine", "zone": "cuisine"}, ctx())
    assert ha.states["media_player.cuisine"]["attributes"]["media_title"] == TRAP
    src.call("maison", {"demande": "qu'est-ce qui passe ?", "zone": "cuisine"}, ctx())
    user = small.log[-1]["messages"][1]["content"]
    assert "Ignore les consignes" in user
    assert_framed(user)


def test_policy_tables_cover_the_2026_inventory():
    p = HomePolicy.from_config({"entities": ["*"]})

    def attrs(e):
        return {"device_class": "garage"} if e == "cover.garage" else {}

    def k(d, s, e, data=None):
        return p.classify(d, s, [e], data or {}, attrs).klass

    assert k("light", "turn_on", "light.a", {"color_temp_kelvin": 2700, "rgb_color": [255, 0, 0]}) == "direct"
    assert k("switch", "turn_off", "switch.a") == "direct"
    assert k("climate", "set_preset_mode", "climate.a", {"preset_mode": "eco"}) == "direct"
    assert k("climate", "set_temperature", "climate.a", {"temperature_range": {"target_temp_low": 10,
                                                                               "target_temp_high": 22}}) == "strong"
    assert k("vacuum", "start", "vacuum.a") == "direct"
    assert k("vacuum", "send_command", "vacuum.a") == "deny"
    assert k("todo", "add_item", "todo.a", {"item": "pain"}) == "direct"
    assert k("todo", "remove_item", "todo.a") == "deny"  # suppression : jamais par l'agent
    assert k("cover", "open_cover", "cover.garage") == "strong"
    for d, s in (("lock", "unlock"), ("alarm_control_panel", "alarm_disarm"), ("valve", "open_valve"),
                 ("siren", "turn_on"), ("scene", "turn_on"), ("button", "press"), ("camera", "turn_off"),
                 ("water_heater", "set_temperature")):
        assert k(d, s, f"{d}.a") == "strong", (d, s)
    for d, s in (("notify", "send_message"), ("homeassistant", "restart"), ("update", "install"),
                 ("conversation", "process"), ("camera", "play_stream"), ("remote", "send_command")):
        assert k(d, s, f"{d}.a") == "deny", (d, s)
    assert k("music_assistant", "play_media", "media_player.a", {"media_id": "Queen"}) == "direct"
    assert k("music_assistant", "play_media", "light.a", {"media_id": "Queen"}) == "deny"


# ── flux d'événements (feeds) ──
class Hub:
    def __init__(self):
        self.notes = []

    def notify_user(self, user, *, title, text, from_thread=None):
        self.notes.append((user, title, text))


def _wait(cond, timeout=5.0):
    import time as _t

    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        if cond():
            return True
        _t.sleep(0.02)
    return False


def test_feeds_route_doorbell_and_motion_to_notifications_memory_and_brief(tmp_path):
    feeds = [{"entity": "event.sonnette", "titre": "Sonnette"},
             {"entity": "binary_sensor.mouvement_*", "to": ["on"], "cooldown_s": 300},
             {"entity": "lock.*", "to": ["unlocked"], "notify": False}]
    src, ha, small, *_ = make(tmp_path, [plan_r(lectures=[{"type": "evenements", "heures": 2}])],
                              members={"alice": "adult", "zoe": "child"}, entities=FULL,
                              extra={"feeds": feeds})
    hub, pushed = Hub(), []
    src.hub = hub
    src.feeds.push = lambda m, t, x: pushed.append((m, t, x))
    src.feeds.idle_s = 0.3
    src.feeds.start()
    try:
        assert _wait(lambda: src.feeds._thread is not None and src.feeds._thread.is_alive())
        import time as _t

        _t.sleep(0.5)  # abonnement posé
        ha.states["event.sonnette"]["attributes"]["friendly_name"] = "Sonnette " + TRAP
        ha.set_state("event.sonnette", "2026-11-05T19:00:00.000+00:00", event_type="ring")
        ha.set_state("binary_sensor.mouvement_entree", "on")
        ha.set_state("binary_sensor.mouvement_entree", "off")
        ha.set_state("binary_sensor.mouvement_entree", "on")  # pendant la pause de 300 s : ignoré
        ha.set_state("lock.entree", "unlocked")
        ha.set_state("light.salon", None, brightness=10)  # aucune règle
        assert _wait(lambda: len(src.feeds.recent()) >= 3)
        _t.sleep(0.3)
    finally:
        src.feeds.stop()
    texts = [e["text"] for e in src.feeds.recent()]
    assert len(texts) == 3, texts
    assert texts[0].endswith("(event.sonnette) : ring") and "off → on" in texts[1] and "locked → unlocked" in texts[2]
    # notifications : sonnette et mouvement, pas la serrure (notify: false) ; l'enfant lit binary_sensor, pas event
    assert sorted({(u, t) for u, t, _ in hub.notes}) == [("alice", "Maison : Mouvement entrée"),
                                                         ("alice", "Maison : Sonnette"),
                                                         ("zoe", "Maison : Mouvement entrée")]
    assert sorted(m for m, *_ in pushed) == ["alice", "alice", "zoe"]
    assert ha.calls == []  # aucun événement ne déclenche d'action
    # lecture `evenements` par l'outil maison : donnée non fiable
    r = src.call("maison", {"demande": "qui a sonné ?"}, ctx())
    assert "evenements :" in r.content and "ring" in r.content
    assert_framed(r.content)
    brief = src.feeds.brief_text("alice")
    assert brief.startswith("[Événements de la maison, dernières 24 h]") and "<untrusted-data kind=\"home-events\"" in brief
    assert_framed(brief)
    zoe = src.feeds.brief_text("zoe")
    assert "Mouvement entrée" in zoe and "sonnette" not in zoe.lower()
    assert [x for x in journal(tmp_path) if x["event"] == "home.feed"]


def test_feeds_rules_are_validated():
    from nokker.home.feeds import parse_rules

    with pytest.raises(ValueError):
        parse_rules([{"to": ["on"]}])
    assert parse_rules(["event.*"])[0].entity == "event.*"


# ── notification par HA (envoi externe) ──
def test_notifier_is_send_external_under_the_kernel_card_and_member_rights(tmp_path):
    from nokker.core.turn import Decision, Effect, ToolCall, Turn, TurnInput
    from nokker.home.source import HomeGates
    from nokker.qa.invariants import DeterministicInvariants

    src, ha, *_ = make(tmp_path, [], entities=FULL,
                       extra={"notify_targets": {"alice": "notify.telephone_alice", "famille": "mobile_app_x"}})
    spec = next(s for s in src.specs() if s.name == "maison_notifier")
    assert spec.effect is Effect.SEND_EXTERNAL
    assert spec.parameters["properties"]["destinataire"]["enum"] == ["alice", "famille"]

    class Inner:
        def action(self, call, spec, turn):
            return [DeterministicInvariants().check_action(call, spec, turn)]

    def turn(who):
        t = Turn(thread_id="th_1", index=0, input=TurnInput("th_1", "préviens Alice", sender=who))
        t.sender = who
        return t

    call = ToolCall(id="n", name="maison_notifier", arguments={"destinataire": "alice", "message": "Colis livré"})
    out = HomeGates(inner=Inner(), source=src).action(call, spec, turn("alice"))
    assert [(v.decision, v.rule) for v in out] == [(Decision.HITL, "effect:send_external")]
    assert HomeGates(inner=Inner(), source=src).action(call, spec, turn("bob"))[0].decision is Decision.DENY  # teen
    bad = ToolCall(id="m", name="maison_notifier", arguments={"destinataire": "voisin", "message": "x"})
    assert HomeGates(inner=Inner(), source=src).action(bad, spec, turn("alice"))[0].decision is Decision.DENY
    r = src.call("maison_notifier", call.arguments, ctx())
    assert not r.is_error and ha.calls == [("notify", "send_message", ["notify.telephone_alice"],
                                            {"message": "Colis livré"})]
    r = src.call("maison_notifier", {"destinataire": "famille", "message": "x"}, ctx())  # service non déclaré côté passerelle
    assert r.is_error and "403" in r.content
    # sans destinataire déclaré : pas d'outil
    src2, *_ = make(tmp_path / "b", [], entities=FULL)
    assert "maison_notifier" not in [s.name for s in src2.specs()]


# ── textes vus par les modèles : empreinte publiée dans docs/HOME-ASSISTANT.md §11 ──
def model_texts_sha256() -> str:
    import hashlib

    from nokker.home import agent as ag
    from nokker.home import source as s

    src = s.HomeAssistantSource(client=None, policy=HomePolicy.from_config({"entities": ["*"]}),
                                journal=s.HomeJournal(log=[]), notify_targets={"alice": "notify.x"})
    tools = [{"name": x.name, "effect": x.effect.value, "description": x.description, "parameters": x.parameters}
             for x in src.specs()]
    from nokker.home import triggers as tr

    bundle = {"tools": tools, "home_agent_prompt": ag.HOME_AGENT_PROMPT, "untrusted_notice": ag.UNTRUSTED_NOTICE,
              "triggers": tr.as_dict()}
    return hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_model_texts_hash():
    """Un texte vu par un modèle change : mettre à jour docs/HOME-ASSISTANT.md §11 (textes ET empreinte), puis la faire
    valider avant fusion."""
    from pathlib import Path

    digest = model_texts_sha256()
    doc = (Path(__file__).resolve().parents[3] / "docs" / "HOME-ASSISTANT.md").read_text(encoding="utf-8")
    assert digest in doc, digest


def test_path_sharing_is_written_in_the_maison_description():
    """Arbitrages relecture / Nokker dev (05.11.2026) : météo et agenda d'abord par weather_ch et calendar_events ; listes
    par tasks_* quand un compte est nommé ou qu'aucune maison n'est reliée ; réponse dans la langue de la demande."""
    from nokker.home.agent import HOME_AGENT_PROMPT
    from nokker.home.source import MAISON_DESC

    assert ("La météo et l'agenda : préférer weather_ch et calendar_events quand ils sont disponibles ; la lecture "
            "maison seulement pour une entité de la maison (station météo du jardin, agenda HA).") in MAISON_DESC
    assert ("tasks_* quand la personne nomme un compte (Todoist, Google, Microsoft) ou qu'aucune maison n'est reliée, "
            "sinon la liste de la maison (todo HA).") in MAISON_DESC
    assert "dans la langue de la demande" in HOME_AGENT_PROMPT and "en français" not in HOME_AGENT_PROMPT


@pytest.mark.parametrize("text, expected", [
    ("25 degrés demain ?", False),
    ("il joue au foot", False),
    ("mets ton manteau", False),
    ("la porte de Saint-Cloud", False),
    ("je fais une pause", False),
    ("quel temps fera-t-il ? météo", False),
    ("montre mon agenda", False),
    ("combien de degrés au salon ?", True),
    ("mets le chauffage à 21 degrés", True),
    ("joue de la musique", True),
    ("joue Bohemian Rhapsody sur l'enceinte du salon", True),
    ("mets la musique en pause", True),
    ("chanson suivante", True),
    ("ferme la porte d'entrée", True),
    ("fais une annonce au salon", True),
    ("éteins la lampe", True),
    ("quand la porte a-t-elle été ouverte ?", True),
])
def test_family_triggers_need_a_home_object_for_ambiguous_words(text, expected):
    """Relecture relecture (05.11.2026) : « degrés », « joue », « mets », « suivant », « pause », « porte », « annonce »
    ne chargent `maison` qu'avec un objet de la maison ; « météo », « agenda », « historique » jamais seuls."""
    from nokker.home.triggers import triggers

    assert triggers(text) is expected
