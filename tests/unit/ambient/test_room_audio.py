"""Voix de la pièce : choix de la pièce par la présence, mode invités, contenu privé jamais lu, heures calmes, panne.
Faux noyau de passerelle (FakeBridge), fausse voix (FakeTTS) : aucun réseau, aucun modèle."""
from __future__ import annotations

import datetime as dt

from nokker.ambient.bridge import FakeBridge
from nokker.ambient.room_audio import NEUTRAL, RoomAudio, from_config, in_quiet, parse_quiet
from tests.unit.ambient.helpers import OGG, FakeTTS, presence

PRIVATE = "Résultat de ta prise de sang : cholestérol élevé"


def make(p=None, *, rooms=("salon", "chambre_a"), **kw):
    bridge = FakeBridge(presence_value=p or presence({"salon": ({"us_alice"}, False), "chambre_a": (set(), False)}))
    tts = FakeTTS()
    ra = RoomAudio(bridge=bridge, tts=tts, scrub=lambda s: s.replace("hunter2", "[secret:mdp]"),
                   rooms=frozenset(rooms), enabled=True, now=lambda: dt.datetime(2026, 11, 2, 12, 0), **kw)
    return ra, bridge, tts


def played(bridge):
    return [c for c in bridge.calls if c[0] == "audio.play"]


def test_personal_read_when_recipient_alone_in_auto_room():
    ra, bridge, tts = make()
    r = ra.say("auto", "Le colis est arrivé.", recipient="us_alice")
    assert (r.status, r.room, r.reason) == ("spoken", "salon", "ok")
    assert tts.said == ["Le colis est arrivé."]
    assert played(bridge)[0][1]["room"] == "salon"


def test_private_is_never_read_even_alone():
    ra, _bridge, tts = make()
    r = ra.say("salon", PRIVATE, recipient="us_alice", privacy="private")
    assert (r.status, r.reason) == ("neutral", "private")
    assert tts.said == [NEUTRAL["fr"]] and all("cholestérol" not in t for t in tts.said)


def test_guests_switch_from_home_gives_neutral_announce():
    ra, _bridge, tts = make(presence({"salon": ({"us_alice"}, False)}, guests=True), rooms=("salon",))
    r = ra.say("salon", "Ton rendez-vous chez le dentiste est demain.", recipient="us_alice")
    assert (r.status, r.reason) == ("neutral", "guests")
    assert tts.said == [NEUTRAL["fr"]]


def test_guests_mode_set_in_kernel_and_expiry():
    clock = [1000.0]
    ra, _bridge, _tts = make(clock=lambda: clock[0])
    ra.set_guests(True, hours=1)
    assert ra.say("salon", "Message perso", recipient="us_alice").reason == "guests"
    clock[0] += 3601
    assert ra.say("salon", "Message perso", recipient="us_alice").status == "spoken"


def test_common_is_read_with_guests():
    ra, _bridge, tts = make(presence({"salon": (set(), True)}, guests=True), rooms=("salon",))
    r = ra.say("salon", "Le dîner est prêt !", privacy="common")
    assert r.status == "spoken" and tts.said == ["Le dîner est prêt !"]


def test_unknown_occupant_or_other_member_means_neutral():
    ra, _, tts = make(presence({"salon": ({"us_alice"}, True)}), rooms=("salon",))
    assert ra.say("salon", "perso", recipient="us_alice").reason == "not_alone"
    ra, _, tts = make(presence({"salon": ({"us_alice", "us_bob"}, False)}), rooms=("salon",))
    assert ra.say("salon", "perso", recipient="us_alice").reason == "not_alone"
    assert tts.said == [NEUTRAL["fr"]]


def test_unreadable_presence_is_prudent():
    ra, bridge, tts = make()
    bridge.fail = True  # presence() rend UNKNOWN ; play échoue aussi
    r = ra.say("salon", "perso", recipient="us_alice")
    assert r.status == "failed" and r.reason == "bridge"
    assert tts.said == [NEUTRAL["fr"]]  # même en panne, jamais le contenu personnel


def test_unknown_privacy_level_is_strictest():
    ra, _, _tts = make()
    assert ra.say("salon", PRIVATE, recipient="us_alice", privacy="??").reason == "private"


def test_audio_bytes_private_not_played_as_is():
    ra, bridge, _tts = make()
    r = ra.say("salon", OGG + b"PRIVE", recipient="us_alice", privacy="private")
    assert r.status == "neutral"
    assert all(b"PRIVE" not in c[1]["audio"] for c in played(bridge))
    r = ra.say("salon", OGG + b"public", privacy="common")
    assert r.status == "spoken" and played(bridge)[-1][1]["audio"] == OGG + b"public"


def test_text_is_scrubbed_and_cleaned_before_voice():
    ra, _, tts = make()
    ra.say("salon", "**Mot de passe** : hunter2\n```\nrm -rf /\n```", recipient="us_alice")
    assert tts.said and "hunter2" not in tts.said[0] and "rm -rf" not in tts.said[0]
    assert "secret masqué" in tts.said[0]


def test_too_long_becomes_neutral():
    ra, _, _tts = make(max_chars=20)
    assert ra.say("salon", "x " * 50, recipient="us_alice").reason == "too_long"


def test_room_resolution_and_quiet_hours():
    ra, _bridge, _ = make()
    assert ra.say("grenier", "x", privacy="common").reason == "no_room"
    assert ra.say("auto", "x", recipient="us_bob").reason == "no_room"  # bob n'est localisé nulle part
    ra2, bridge2, _ = make(quiet=parse_quiet("22:00-07:00"))
    ra2.now = lambda: dt.datetime(2026, 11, 2, 23, 30)
    assert ra2.say("salon", "x", privacy="common").reason == "quiet"
    assert not played(bridge2)
    assert in_quiet(dt.datetime(2026, 11, 2, 6, 59), parse_quiet("22:00-07:00"))
    assert not in_quiet(dt.datetime(2026, 11, 2, 7, 0), parse_quiet("22:00-07:00"))
    assert parse_quiet("n'importe quoi") is None


def test_flag_off_does_nothing():
    bridge = FakeBridge()
    ra = from_config({}, bridge=bridge, tts=FakeTTS(), scrub=lambda s: s)
    assert ra.say("salon", "x", privacy="common").reason == "off" and bridge.calls == []
    ra = from_config({"enabled": True}, bridge=None, tts=FakeTTS(), scrub=lambda s: s)
    assert ra.say("salon", "x", privacy="common").reason == "off"
