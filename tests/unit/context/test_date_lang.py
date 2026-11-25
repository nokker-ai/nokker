"""En-tête de date du tour (`[Date et heure actuelles]`) dans la langue du tour : jours et mois fr/en/de/it écrits à la main,
format naturel par langue, libellé en table à quatre langues ; le français ne change pas d'un octet."""
from __future__ import annotations

from datetime import datetime

import pytest

from nokker.approvals.rule_labels import lang_scope
from nokker.context.engine import ZURICH, ContextEngineV0, date_block, format_now, format_now_fr
from nokker.core.turn import InputCheck, Tier, Turn
from tests.unit.context.test_engine import FakeRegistry

WED = datetime(2026, 11, 18, 14, 5, tzinfo=ZURICH)
ISO = "(Europe/Zurich) — ISO 2026-11-18T14:05:00+01:00"


@pytest.mark.parametrize("lang,expected", [
    ("fr", f"mercredi 18 novembre 2026, 14:05 {ISO}"),
    ("en", f"Wednesday, November 18, 2026, 14:05 {ISO}"),
    ("de", f"Mittwoch, 18. November 2026, 14:05 {ISO}"),
    ("it", f"mercoledì 18 novembre 2026, 14:05 {ISO}"),
])
def test_the_date_is_written_in_the_language(lang, expected):
    assert format_now(WED, lang) == expected


def test_french_is_the_frozen_legacy_rendering():
    assert format_now_fr(datetime(2026, 10, 20, 14, 5, tzinfo=ZURICH)) == (
        "mardi 20 octobre 2026, 14:05 (Europe/Zurich) — ISO 2026-10-20T14:05:00+02:00")
    assert format_now(WED) == format_now_fr(WED) == format_now(WED, "xx") == format_now(WED, None)


def test_every_weekday_and_month_exists_in_every_language():
    for lang in ("fr", "en", "de", "it"):
        days = {format_now(datetime(2026, 11, 16 + i, 9, 0, tzinfo=ZURICH), lang).split(",")[0].split()[0] for i in range(7)}
        months = {format_now(datetime(2026, m, 5, 9, 0, tzinfo=ZURICH), lang) for m in range(1, 13)}
        assert len(days) == 7 and len(months) == 12


@pytest.mark.parametrize("lang,label", [
    ("fr", "[Date et heure actuelles]"), ("en", "[Current date and time]"),
    ("de", "[Aktuelles Datum und Uhrzeit]"), ("it", "[Data e ora attuali]"),
])
def test_the_label_and_the_context_language(lang, label):
    assert date_block(WED, Turn("t", 0, InputCheck(text="x"), lang=lang)).startswith(f"{label}\n")
    with lang_scope(lang):  # sans langue posée sur le tour : celle du contexte
        assert date_block(WED).startswith(f"{label}\n")
    assert date_block(WED).startswith("[Date et heure actuelles]\n")


def _engine(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    return ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix), now_fn=lambda: WED)


@pytest.mark.parametrize("lang,start", [
    ("fr", "[Date et heure actuelles]\nmercredi 18 novembre 2026, 14:05"),
    ("en", "[Current date and time]\nWednesday, November 18, 2026, 14:05"),
    ("de", "[Aktuelles Datum und Uhrzeit]\nMittwoch, 18. November 2026, 14:05"),
    ("it", "[Data e ora attuali]\nmercoledì 18 novembre 2026, 14:05"),
])
def test_the_assembled_head_is_in_the_language_of_the_turn(tmp_path, lang, start):
    turn = Turn("t1", 0, InputCheck(text="question"), lang=lang)
    out = _engine(tmp_path).assemble(turn, [], [], [], Tier.SHIP)
    assert out.messages[-1]["content"].startswith(start)
    assert out.messages[-1]["content"].endswith("\n\nquestion")


def test_a_language_change_leaves_the_sent_head_untouched(tmp_path):
    """Append-only : l'en-tête déjà envoyé est rejoué tel quel ; seul le tour suivant prend sa langue."""
    engine = _engine(tmp_path)
    first = Turn("t1", 0, InputCheck(text="bonjour"), lang="fr")
    sent = engine.assemble(first, [], [], [], Tier.SHIP).messages[-1]["content"]
    second = Turn("t1", 1, InputCheck(text="hello"), lang="en")
    out = engine.assemble(second, [first], [], [], Tier.SHIP)
    assert out.messages[-2]["content"] == sent and sent.startswith("[Date et heure actuelles]\nmercredi")
    assert out.messages[-1]["content"].startswith("[Current date and time]\nWednesday, November 18, 2026")
    # sans changement de langue, l'historique reste identique d'un tour à l'autre
    again = engine.assemble(Turn("t1", 1, InputCheck(text="hello"), lang="en"), [first], [], [], Tier.SHIP)
    assert again.messages[:-1] == out.messages[:-1]
