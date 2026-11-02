"""Lexique de prononciation du foyer et normalisation fr-CH avant le TTS (docs/VOICE.md §6). Pur : aucun réseau,
aucun modèle. Prouve la normalisation (cas limites compris), le lexique, l'étanchéité entre foyers, les routes, et que
drapeau éteint = texte au TTS identique octet pour octet."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from nokker.ambient.bridge import FakeBridge
from nokker.ambient.room_audio import RoomAudio
from nokker.voice import speak_lexicon as sl
from nokker.voice.speak_lexicon import (
    MemoryLexiconStore,
    SpeakLexicon,
    SpeakOptions,
    SpeakScope,
    number_to_words,
    speak_prepare,
)
from nokker.voice.tts import TTSError, VoiceReplies
from tests.unit.ambient.helpers import FakeHandler, FakeTTS, presence

HU = SpeakOptions(eighty="huitante")


def fr(text, entries=None, opts=None):
    return speak_prepare(text, "fr", entries, opts)


# ── nombres ──
@pytest.mark.parametrize("n,w", [
    (0, "zéro"), (1, "un"), (16, "seize"), (21, "vingt et un"), (22, "vingt-deux"), (70, "septante"),
    (71, "septante et un"), (75, "septante-cinq"), (80, "quatre-vingts"), (81, "quatre-vingt-un"),
    (90, "nonante"), (91, "nonante et un"), (99, "nonante-neuf"), (100, "cent"), (101, "cent un"),
    (200, "deux cents"), (201, "deux cent un"), (280, "deux cent quatre-vingts"), (1000, "mille"),
    (1001, "mille un"), (2000, "deux mille"), (80_000, "quatre-vingt mille"), (200_000, "deux cent mille"),
    (1_000_000, "un million"), (2_000_000, "deux millions"), (1_250_000, "un million deux cent cinquante mille"),
    (-5, "moins cinq"),
])
def test_number_to_words(n, w):
    assert number_to_words(n) == w


def test_huitante_variant():
    assert number_to_words(80, "huitante") == "huitante"
    assert number_to_words(81, "huitante") == "huitante et un"
    assert number_to_words(88, "huitante") == "huitante-huit"
    assert number_to_words(180, "huitante") == "cent huitante"


def test_huge_number_left_as_digits():
    assert fr("1234567890123") == "1234567890123"


# ── nombres dans le texte ──
def test_thousands_apostrophe():
    assert fr("1'250 personnes") == "mille deux cent cinquante personnes"
    assert fr("1’250 personnes") == "mille deux cent cinquante personnes"
    assert fr("1'000'000") == "un million"


def test_negative_and_decimal_and_percent():
    assert fr("il fait -5 degrés") == "il fait moins cinq degrés"
    assert fr("3.05") == "trois virgule zéro cinq"
    assert fr("12,5%") == "douze virgule cinq pour cent"
    assert fr("0.5") == "zéro virgule cinq"
    assert fr("50 %") == "cinquante pour cent"


def test_huitante_in_text():
    assert fr("80 et 90 et 70", opts=HU) == "huitante et nonante et septante"
    assert fr("80") == "quatre-vingts"


def test_digits_glued_to_words_untouched():
    assert fr("mp3 A4 4K 5kg") == "mp3 A4 4K 5kg"


def test_ordinals():
    assert fr("le 1er rang, la 2e, la 5e, la 21e, la 80e") == \
        "le premier rang, la deuxième, la cinquième, la vingt et unième, la quatre-vingtième"


# ── montants ──
@pytest.mark.parametrize("src,out", [
    ("CHF 12.50", "douze francs cinquante"),
    ("CHF 12.5", "douze francs cinquante"),
    ("CHF 1'250.-", "mille deux cent cinquante francs"),
    ("CHF 1.–", "un franc"),
    ("CHF 1.01", "un franc un"),
    ("CHF 0.05", "cinq centimes"),
    ("CHF 0.01", "un centime"),
    ("CHF 0.50", "cinquante centimes"),
    ("CHF 1'000'000.-", "un million de francs"),
    ("Fr. 25.-", "vingt-cinq francs"),
    ("12.50 CHF", "douze francs cinquante"),
    ("12 €", "douze euros"),
    ("-CHF 5.00", "moins cinq francs"),
    ("CHF 80.-", "quatre-vingts francs"),
])
def test_amounts(src, out):
    assert fr(src) == out


def test_amount_in_sentence_and_end_of_sentence():
    assert fr("Total : CHF 12.50.") == "Total : douze francs cinquante."
    assert fr("Il reste CHF 70.- à payer") == "Il reste septante francs à payer"
    assert fr("CHF 80.-", opts=HU) == "huitante francs"


# ── dates et heures ──
def test_dates():
    assert fr("02.11.2026") == "deux novembre deux mille vingt-six"
    assert fr("01.11.2026") == "premier novembre deux mille vingt-six"
    assert fr("02.11.") == "deux novembre"
    assert fr("le 01.11 à midi") == "le premier novembre à midi"
    assert fr("du 03.11 au 07.11") == "du trois novembre au sept novembre"
    assert fr("29.02.2024") == "vingt-neuf février deux mille vingt-quatre"
    assert fr("02.11.26") == "deux novembre deux mille vingt-six"


@pytest.mark.parametrize("bad", ["31.02.2026", "29.02.2025", "45.13.2026", "00.11.2026", "32.01.2026"])
def test_invalid_dates_untouched(bad):
    assert fr(bad) == bad


def test_decimal_not_mistaken_for_date():
    assert fr("pèse 12.50 kilos") == "pèse douze virgule cinquante kilos"
    assert fr("il y a 3.5.") == "il y a trois virgule cinq."  # un seul chiffre des deux côtés : décimale, pas date
    assert fr("31.04.") == "trente et un virgule zéro quatre."  # date impossible : lue comme décimale, pas inventée


def test_times():
    assert fr("14h30") == "quatorze heures trente"
    assert fr("9 h 15") == "neuf heures quinze"
    assert fr("14:30") == "quatorze heures trente"
    assert fr("à 8h") == "à huit heures"
    assert fr("1h05") == "une heure cinq"
    assert fr("00h00") == "minuit"
    assert fr("12:00") == "douze heures"
    assert fr("24h30") != "vingt-quatre heures trente"  # heure invalide : pas de lecture d'heure


def test_time_not_matched_in_words():
    assert fr("2 hectares") == "deux hectares"
    assert fr("12:00:00") != "douze heures"


# ── téléphones, IBAN ──
def test_swiss_phone_numbers():
    assert fr("+41 79 123 45 67") == "plus quarante et un, sept neuf, un deux trois, quatre cinq, six sept"
    assert fr("+41 (0)79 123 45 67") == fr("+41 79 123 45 67")
    assert fr("0041791234567") == fr("+41 79 123 45 67")
    assert fr("079 123 45 67") == "zéro sept neuf, un deux trois, quatre cinq, six sept"
    assert fr("022.123.45.67") == "zéro deux deux, un deux trois, quatre cinq, six sept"


def test_iban_spelled_in_groups():
    assert fr("CH93 0076 2011 6238 5295 7") == \
        "C H neuf trois, zéro zéro sept six, deux zéro un un, six deux trois huit, cinq deux neuf cinq, sept"
    assert fr("CH9300762011623852957") == fr("CH93 0076 2011 6238 5295 7")


def test_qr_reference_spelled():
    out = fr("Réf. 21 00000 00000 03139 47143 00901")
    assert "21" not in out and "deux un" in out and out.count(",") == 6


# ── abréviations et sigles ──
def test_abbreviations():
    assert fr("env. 5 minutes") == "environ cinq minutes"
    assert fr("Voir p. ex. la note") == "Voir par exemple la note"
    assert fr("à St-Gall et Ste-Croix") == "à Saint-Gall et Sainte-Croix"
    assert fr("Av. de la Gare 5") == "Avenue de la Gare cinq"
    assert fr("ch. de la Gare") == "chemin de la Gare"
    assert fr("Mme Dupont, Dr Martin, M. Roux") == "Madame Dupont, Docteur Martin, Monsieur Roux"
    assert fr("pommes, poires, etc.") == "pommes, poires, et cetera"
    assert fr("n° 12") == "numéro douze"


def test_abbreviation_needs_context():
    assert fr("le ch. suivant") == "le ch. suivant"  # « ch. » seul n'est pas « chemin »
    assert fr("St-gall") == "St-gall"


def test_acronyms_spelled_and_overridable():
    assert fr("Le billet CFF et l'AVS, TVA incluse") == "Le billet C F F et l'A V S, T V A incluse"
    assert fr("CFFA") == "CFFA"  # mot entier seulement
    assert fr("CFF", opts=SpeakOptions.from_raw({"acronyms": {"CFF": ""}})) == "CFF"
    assert fr("SIG", opts=SpeakOptions.from_raw({"acronyms": {"SIG": "sig"}})) == "sig"


# ── texte sans rien à normaliser ──
@pytest.mark.parametrize("text", [
    "", " ", "Bonjour, tout va bien.", "  espaces   multiples  ", "Ça marche très bien !", "ligne\nsuivante",
    "Hello, nothing to do here", "version 1.2.3", "ok.", "a-b", "…",
])
def test_nothing_to_normalize_is_unchanged(text):
    assert fr(text) == text


def test_english_text_only_gets_lexicon():
    assert speak_prepare("CHF 12.50 at 14h30", "en") == "CHF 12.50 at 14h30"
    assert speak_prepare("Meet Léa", "en", {"léa": "Lay-ah"}) == "Meet Lay-ah"


# ── lexique ──
def test_lexicon_words_and_phrases():
    e = {"léa": "Lé-a", "chemin des mélèzes": "chemin des mélèze", "rex": "Rekss"}
    assert fr("Léa promène Rex au Chemin   des Mélèzes.", e) == "Lé-a promène Rekss au chemin des mélèze."


def test_lexicon_whole_words_only_and_case_insensitive():
    assert fr("Rexona et REX", {"rex": "Rekss"}) == "Rexona et Rekss"


def test_lexicon_protected_from_normalization():
    # la prononciation n'est pas re-normalisée (ni sigle ni abréviation) et le mot n'est pas abîmé avant lecture
    assert fr("Avenue St-Jean CFF", {"st-jean": "Saint-Jan", "cff": "cé èf èf"}) == "Avenue Saint-Jan cé èf èf"
    assert fr("Chez Dr. X 3", {"dr. x": "Docteur Iks"}) == "Chez Docteur Iks trois"


def test_lexicon_beats_acronym_and_numbers():
    assert fr("Le 4x4 de Zoé", {"4x4": "quatre fois quatre"}) == "Le quatre fois quatre de Zoé"


def test_text_cannot_forge_a_token():
    out = fr(" Léa", {"léa": "Lé-a"})
    assert out == " Lé-a"


def test_check_entry_limits():
    for w, s in (("", "x"), ("x", ""), ("a" * 81, "x"), ("x", "a" * 121), (3, "x"), ("x", "y"), ("x\x00", "y")):
        with pytest.raises(sl.LexiconError):
            sl.check_entry(w, s)
    assert sl.check_entry("  Léa ", " Lé-a ") == ("Léa", "Lé-a")


# ── stockage et étanchéité entre foyers ──
A, B = SpeakScope("tn_a", "hh_a"), SpeakScope("tn_b", "hh_b")


def test_households_do_not_leak():
    store = MemoryLexiconStore()
    svc = SpeakLexicon(store=store)
    store.put(A, "Léa", "Lé-a", "us_alice")
    store.put(B, "Léa", "Lia", "us_bob")
    store.put(A, "Secret de A", "chut", "us_alice")
    assert svc.prepare("Léa", "fr", A) == "Lé-a"
    assert svc.prepare("Léa", "fr", B) == "Lia"
    assert svc.prepare("Secret de A", "fr", B) == "Secret de A"
    assert svc.prepare("Léa", "fr", SpeakScope("tn_a", "hh_autre")) == "Léa"  # même locataire, autre foyer
    assert svc.prepare("Léa", "fr", SpeakScope("tn_x", "hh_a")) == "Léa"  # même foyer id, autre locataire
    assert svc.prepare("Léa", "fr", None) == "Léa"  # pas de foyer : pas de lexique
    assert [r["word"] for r in store.listing(B)] == ["Léa"]
    assert store.delete(B, "léa") is True and store.delete(B, "léa") is False
    assert svc.prepare("Léa", "fr", A) == "Lé-a"


def test_config_entries_read_only():
    store = MemoryLexiconStore({"Léa": "Lé-a"})
    assert store.entries(A) == {"léa": "Lé-a"}
    with pytest.raises(sl.LexiconError) as e:
        store.put(A, "x", "y", "u")
    assert e.value.status == 409


def test_prepare_never_raises_when_store_fails():
    class Boom:
        def entries(self, scope):
            raise OSError("base")

    assert SpeakLexicon(store=Boom()).prepare("CHF 12.50 Léa", "fr", A) == "douze francs cinquante Léa"


def test_build_flag_off_by_default():
    assert sl.build(None) is None
    assert sl.build({}) is None
    assert sl.build({"enabled": False, "entries": {"a": "b"}}) is None
    assert sl.setup({}) is None and sl.service() is None
    s = sl.setup({"enabled": True, "eighty": "huitante", "entries": {"Léa": "Lé-a"}})
    try:
        assert s is not None and s.options.eighty == "huitante" and s.default_scope is not None
        assert s.prepare("Léa a 80 ans", "fr", s.default_scope) == "Lé-a a huitante ans"
    finally:
        sl.configure(None)


# ── drapeau éteint : octet pour octet ──
class Rec:
    name = "rec"

    def __init__(self):
        self.calls = []

    def synthesize(self, text, lang):
        self.calls.append((text, lang))
        return b"OggS" + b"\0" * 8


REPLY = "Total CHF 12.50, rdv le 02.11. à 14h30 chez Léa (+41 79 123 45 67). **OK**"


def test_voice_replies_flag_off_is_byte_identical():
    base, off = Rec(), Rec()
    VoiceReplies(provider=base, scrub=lambda s: s, enabled=True).render(REPLY, "fr")
    VoiceReplies(provider=off, scrub=lambda s: s, enabled=True, prepare=None).render(REPLY, "fr", scope=A)
    assert base.calls == off.calls and base.calls[0][0].encode() == off.calls[0][0].encode()
    assert "CHF 12.50" in base.calls[0][0]


def test_voice_replies_flag_on_normalizes_after_cleaning():
    rec = Rec()
    store = MemoryLexiconStore()
    store.put(A, "Léa", "Lé-a", "us_alice")
    svc = SpeakLexicon(store=store)
    v = VoiceReplies(provider=rec, scrub=lambda s: s, enabled=True, prepare=svc.prepare)
    assert v.render(REPLY, "fr", scope=A)
    said = rec.calls[0][0]
    assert said.startswith("Total douze francs cinquante, rdv le deux novembre à quatorze heures trente chez Lé-a")
    assert "**" not in said and "plus quarante et un" in said
    v.render("Léa", "fr", scope=B)
    assert rec.calls[1][0] == "Léa"


def test_voice_replies_length_cap_unchanged_by_expansion():
    rec = Rec()
    v = VoiceReplies(provider=rec, scrub=lambda s: s, enabled=True, max_chars=20,
                     prepare=SpeakLexicon(store=MemoryLexiconStore()).prepare)
    assert v.render("CHF 1'250.- et 80", "fr")  # 17 caractères avant, bien plus après : la voix part quand même
    assert len(rec.calls[0][0]) > 20


def test_voice_replies_provider_error_still_falls_back():
    class Down(Rec):
        def synthesize(self, text, lang):
            raise TTSError("panne")

    v = VoiceReplies(provider=Down(), scrub=lambda s: s, enabled=True,
                     prepare=SpeakLexicon(store=MemoryLexiconStore()).prepare)
    assert v.render("CHF 12.50", "fr") is None


def make_room(**kw):
    bridge = FakeBridge(presence_value=presence({"salon": ({"us_alice"}, False)}))
    tts = FakeTTS()
    ra = RoomAudio(bridge=bridge, tts=tts, scrub=lambda s: s, rooms=frozenset({"salon"}), enabled=True,
                   now=lambda: dt.datetime(2026, 11, 2, 12, 0), **kw)
    return ra, tts


def test_room_audio_flag_off_is_byte_identical():
    ra, tts = make_room()
    ra.say("auto", "Rendez-vous le 02.11. à 14h30, CHF 12.50", recipient="us_alice")
    assert tts.said == ["Rendez-vous le 02.11. à 14h30, CHF 12.50"]


def test_room_audio_flag_on_uses_household_lexicon():
    store = MemoryLexiconStore()
    store.put(A, "Léa", "Lé-a", "u")
    svc = SpeakLexicon(store=store, default_scope=A)
    ra, tts = make_room(prepare=svc.prepare, speak_scope=A)
    r = ra.say("auto", "Léa : rdv à 14h30, CHF 12.50", recipient="us_alice")
    assert tts.said == ["Lé-a : rdv à quatorze heures trente, douze francs cinquante"]
    assert r.spoken == "Léa : rdv à 14h30, CHF 12.50"  # le résultat garde le texte d'origine
    ra2, tts2 = make_room(prepare=svc.prepare, speak_scope=B)
    ra2.say("auto", "Léa", recipient="us_alice")
    assert tts2.said == ["Léa"]


def test_room_audio_neutral_announce_not_normalized():
    ra, tts = make_room(prepare=lambda *a: "X")
    ra.say("auto", "contenu", recipient="us_alice", privacy="private")
    assert tts.said == ["Un message vous attend sur votre téléphone."]


# ── routes ──
ADULT = SimpleNamespace(account_id="us_alice", household_id="hh_a", household_tenant="tn_a", profile="adult")
TEEN = SimpleNamespace(account_id="us_teen", household_id="hh_a", household_tenant="tn_a", profile="teen")
KID = SimpleNamespace(account_id="us_kid", household_id="hh_a", household_tenant="tn_a", profile="child")
BOB = SimpleNamespace(account_id="us_bob", household_id="hh_b", household_tenant="tn_b", profile="adult")


def call(method, who, body=None, store=None):
    from nokker.ambient import http as ah

    h = FakeHandler(body=body)
    ok = sl.handle_member(h, method, sl.PATH, who, ah._body, ah._json)
    return ok, h.status, h.json()


@pytest.fixture
def routes():
    sl.configure(SpeakLexicon(store=MemoryLexiconStore()))
    yield
    sl.configure(None)


def test_routes_absent_when_flag_off():
    sl.configure(None)
    assert sl.is_path(sl.PATH) is False
    assert sl.handle_member(FakeHandler(), "GET", sl.PATH, ADULT, lambda h: {}, lambda *a: None) is False


def test_routes_crud_and_isolation(routes):
    assert sl.is_path(sl.PATH) and not sl.is_path("/v1/speak/other")
    assert call("PUT", ADULT, {"word": "Léa", "say": "Lé-a"})[1] == 200
    assert call("PUT", ADULT, {"word": "léa", "say": "Lé-ah"})[1] == 200  # même entrée, remplacée
    _, st, out = call("GET", ADULT)
    assert st == 200 and out["entries"] == [{"word": "léa", "say": "Lé-ah", "source": "foyer"}]
    assert call("GET", TEEN)[1] == 200
    assert call("GET", KID)[1] == 403
    assert call("GET", BOB)[2]["entries"] == []  # autre foyer : rien
    assert call("DELETE", BOB, {"word": "Léa"})[1] == 404  # ni lecture ni suppression chez le voisin
    assert call("DELETE", TEEN, {"word": "Léa"})[1] == 403 and call("PUT", TEEN, {"word": "a", "say": "b"})[1] == 403
    assert call("DELETE", ADULT, {"word": "LÉA"})[1] == 200
    assert call("GET", ADULT)[2]["entries"] == []


def test_routes_validation(routes):
    assert call("PUT", ADULT, {"word": "", "say": "x"})[1] == 400
    assert call("PUT", ADULT, {"word": "x" * 200, "say": "x"})[1] == 400
    assert call("PUT", ADULT, [1])[1] == 400
    assert call("DELETE", ADULT, {})[1] == 400
    assert call("POST", ADULT, {})[1] == 405
    assert call("GET", None)[1] == 403


def test_routes_entry_cap(routes):
    for i in range(sl.MAX_ENTRIES):
        assert call("PUT", ADULT, {"word": f"m{i}", "say": "x"})[1] == 200
    assert call("PUT", ADULT, {"word": "de-trop", "say": "x"})[1] == 409
    assert call("PUT", ADULT, {"word": "m0", "say": "y"})[1] == 200  # remplacer reste permis


def test_routes_read_only_with_config_entries():
    sl.configure(SpeakLexicon(store=MemoryLexiconStore({"Léa": "Lé-a"})))
    try:
        assert call("PUT", ADULT, {"word": "x", "say": "y"})[1] == 409
        assert call("GET", ADULT)[2]["entries"][0]["source"] == "config"
    finally:
        sl.configure(None)


# ── Telegram : le foyer du compte arrive au lexique ──
def test_telegram_passes_household_scope_and_flag_off_keeps_old_call():
    from nokker.core.turn import Turn
    from nokker.telegram.adapter import TelegramAdapter
    from nokker.telegram.pairing import MemoryPairing
    from nokker.tenancy import MemoryTenancy
    from tests.unit.telegram.test_voice import FakeBot

    def run(speak_scope, voice):
        tenancy, bot = MemoryTenancy(), FakeBot()
        acc = tenancy.create_account("a@x.ch")
        tenancy.bind_identity(acc.account_id, "telegram", "111")
        ad = TelegramAdapter(tenancy=tenancy, pairing=MemoryPairing(), bot=bot, voice=voice, speak_scope=speak_scope,
                             run_turn=lambda inp: Turn(thread_id=inp.thread_id, index=0, input=None,
                                                       reply="Léa : CHF 12.50"))
        ad._process({"chat": {"id": 111, "type": "private"}, "from": {"id": 111, "language_code": "fr"},
                     "text": "salut"})
        return bot

    class OldVoice:  # un faux sans paramètre `scope` : l'ancien appel doit rester celui d'avant
        enabled = True

        def __init__(self):
            self.calls = []

        def wanted(self, chat, voice_in):
            return True

        def render(self, reply, lang):
            self.calls.append((reply, lang))
            return b"OggS"

    old = OldVoice()
    assert run(None, old).voices and old.calls == [("Léa : CHF 12.50", "fr")]
    store = MemoryLexiconStore()
    store.put(A, "Léa", "Lé-a", "u")
    svc = SpeakLexicon(store=store)
    rec = Rec()
    v = VoiceReplies(provider=rec, scrub=lambda s: s, enabled=True, reply_mode="always", prepare=svc.prepare)
    run(lambda account: A, v)
    assert rec.calls == [("Lé-a : douze francs cinquante", "fr")]
    rec2 = Rec()
    run(lambda account: B, VoiceReplies(provider=rec2, scrub=lambda s: s, enabled=True, reply_mode="always",
                                         prepare=svc.prepare))
    assert rec2.calls == [("Léa : douze francs cinquante", "fr")]
