"""Drapeau `reply_language` : rendu du préfixe produit, mot pour mot ; éteint = identique à l'octet près."""
from __future__ import annotations

from nokker.config import PRODUCT_PREFIX_PATH, NokkerConfig
from nokker.context import reply_language
from nokker.context.engine import ContextEngineV0
from nokker.tools.registry import Registry

LIGNE = ("- **Langue** : réponds dans la langue dans laquelle l'utilisateur t'écrit, même si ce préfixe, les blocs du "
         "noyau, les résultats d'outils ou les documents sont dans une autre langue. S'il change de langue, suis-le. "
         "Ne traduis pas les noms, chiffres et citations que tu relaies.")
ANCIENNE = "- Réponds dans la langue de l'utilisateur ; s'il change de langue, suis-le."
BRUT = PRODUCT_PREFIX_PATH.read_text(encoding="utf-8")


def _style(text: str) -> list[str]:
    body = text.split("## Style\n\n", 1)[1].split("\n## ", 1)[0]
    return [ln for ln in body.splitlines() if ln.strip()]


def test_texte_mot_pour_mot():
    assert reply_language.LANGUAGE_LINE == LIGNE + "\n"
    assert reply_language.OLD_LINE == ANCIENNE + "\n"


def test_rendu_allume():
    out = reply_language.apply(BRUT)
    assert out.count(LIGNE) == 1 and ANCIENNE not in out
    style = _style(out)
    assert style[0] == LIGNE
    assert style[1:] == [ln for ln in _style(BRUT) if ln != ANCIENNE]  # le reste de Style, dans l'ordre
    assert out.replace(LIGNE + "\n", "") == BRUT.replace(ANCIENNE + "\n", "")  # rien d'autre ne change


def test_rendu_eteint_identique():
    assert ANCIENNE in BRUT and LIGNE not in BRUT
    assert NokkerConfig.__dataclass_fields__["reply_language"].default is False
    assert ContextEngineV0(tools=Registry(), prefix_path=str(PRODUCT_PREFIX_PATH))._prefix_text == BRUT


def test_moteur_allume_et_empreinte():
    off = ContextEngineV0(tools=Registry(), prefix_path=str(PRODUCT_PREFIX_PATH))
    on = ContextEngineV0(tools=Registry(), prefix_path=str(PRODUCT_PREFIX_PATH), reply_language=True)
    assert on._prefix_text == reply_language.apply(BRUT)
    assert on._prefix_text.count(LIGNE) == 1
    assert on._prefix_sha256 != off._prefix_sha256  # empreinte calculée sur le préfixe rendu
    again = ContextEngineV0(tools=Registry(), prefix_path=str(PRODUCT_PREFIX_PATH), reply_language=True)
    assert again._prefix_sha256 == on._prefix_sha256  # stable


def test_prefixe_sans_la_ligne_inchange():
    for text in ("# Code\n\n## Style\n\n- Sois bref.\n", "# Sans style\n", BRUT.replace(ANCIENNE + "\n", "")):
        assert reply_language.apply(text) == text
    once = reply_language.apply(BRUT)
    assert reply_language.apply(once) == once  # idempotent : jamais deux lignes Langue


def test_session_vocale_garde_sa_ligne_langue():
    from pathlib import Path

    from nokker.knock import policy

    assert "Langue : celle de la personne qui parle (pas celle de ce bloc)." in Path(policy.__file__).read_text("utf-8")


# ── drapeau reply_language_tail : ligne en fin d'en-tête du tour ──

QUEUE = "Langue de réponse : celle du message de l'utilisateur ci-dessous."


def _user(**kw) -> str:
    from datetime import datetime

    from nokker.context.engine import ZURICH
    from nokker.core.turn import InputCheck, Tier, Turn

    eng = ContextEngineV0(tools=Registry(), prefix_path=str(PRODUCT_PREFIX_PATH),
                          now_fn=lambda: datetime(2026, 11, 3, 9, 0, tzinfo=ZURICH), **kw)
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="Fass den Bericht zusammen."), attached="[Pièce]\nx\n[/Pièce]")
    out = eng.assemble(turn, [], [], [], Tier.SHIP)
    return out.messages[-1]["content"], out.messages[0]["content"]


def test_queue_texte_mot_pour_mot():
    assert reply_language.TAIL_LINE == QUEUE
    assert NokkerConfig.__dataclass_fields__["reply_language_tail"].default is False


def test_queue_position_juste_avant_le_message():
    user, _ = _user(reply_language_tail=True)
    assert user.count(QUEUE) == 1
    assert user.endswith(f"[Pièce]\nx\n[/Pièce]\n\n{QUEUE}\n\nFass den Bericht zusammen.")  # après les pièces jointes
    assert user.startswith("[Date et heure actuelles]")


def test_queue_eteinte_identique():
    off, sys_off = _user()
    on, sys_on = _user(reply_language_tail=True)
    assert QUEUE not in off and on.replace(f"{QUEUE}\n\n", "") == off
    assert sys_on == sys_off  # le préfixe ne change pas


def test_queue_sans_interaction_avec_reply_language():
    user_a, sys_a = _user(reply_language=True)
    user_ab, sys_ab = _user(reply_language=True, reply_language_tail=True)
    off, sys_off = _user()
    assert sys_ab == sys_a and sys_a != sys_off  # préfixe : seul reply_language le change
    assert user_a == off and user_ab.replace(f"{QUEUE}\n\n", "") == off  # en-tête : seul reply_language_tail le change
