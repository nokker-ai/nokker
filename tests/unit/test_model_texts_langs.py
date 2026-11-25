"""Textes vus du MODÈLE (`nokker/core/model_texts.py`) : quatre langues pour chacun, mêmes trous, empreintes figées par
langue, et balayage des modules du noyau pour qu'aucun résultat d'outil ou note système ne reparte en français « en dur ».

Un texte qui n'existe qu'en français fait répondre le modèle en français à une personne qui écrit en anglais : la suite
échoue tant qu'un texte manque dans une langue, ou qu'une phrase française multi-mots est posée hors des tables dans un
module surveillé (liste d'exemptions explicite, avec la raison)."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from nokker.approvals.rule_labels import LANGS, lang_scope
from nokker.core import model_texts as mt

ROOT = Path(__file__).resolve().parents[2]
FINGERPRINTS = Path(__file__).with_name("model_texts_fingerprints.json")


def H(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _holes(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)", text))


# Mots identiques au français dans une langue, justifiés (nom propre, mot international) : (clé, langue).
SAME_AS_FRENCH = {
    ("tool.family.Web", "en"), ("tool.family.Web", "de"), ("tool.family.Web", "it"),
    ("tool.family.Mail", "en"), ("tool.family.Mail", "de"), ("tool.family.Agenda", "it"),
    ("tool.family.Sessions", "en"), ("tool.family.Agents", "en"), ("tool.family.Skills", "en"),
    ("tool.family.Skills", "de"), ("tool.family.Skills", "it"), ("tool.family.Packs", "en"),
    ("tool.family.Packs", "de"),
    # séparateurs sans mot : `{field}: {description}`
    ("tool.deferred_field_desc", "en"), ("tool.deferred_field_desc", "de"), ("tool.deferred_field_desc", "it"),
}


@pytest.mark.parametrize("key", sorted(mt.ALL_TABLES))
def test_every_text_has_four_languages_and_the_same_holes(key):
    table = mt.ALL_TABLES[key]
    assert set(table) == set(LANGS) and all(isinstance(v, str) and v.strip() for v in table.values()), key
    for lang in ("en", "de", "it"):
        if (key, lang) not in SAME_AS_FRENCH:
            assert table[lang] != table["fr"], f"{key} : {lang} est le français"
    assert _holes(table["en"]) == _holes(table["de"]) == _holes(table["it"]) == _holes(table["fr"]), key


def test_texts_keep_the_second_person_and_name_no_internal_word():
    """Pas de nom interne (« noyau ») ni de flèche dans les langues étrangères ; pas de vouvoiement allemand."""
    for key, table in mt.ALL_TABLES.items():
        for lang in ("en", "de", "it"):
            low = table[lang].lower()
            assert "noyau" not in low and "kernel" not in low, (key, lang)
            assert "Sie " not in table[lang].replace("Sie die", "").replace("sie ", "") or key.startswith("tool.family"), (key, lang)


def test_fingerprints_are_frozen_per_language():
    """Toute retouche d'un texte change son empreinte : à relire avant de la refiger (`PYTHONPATH=. python tests/unit/test_model_texts_langs.py`)."""
    current = {k: {lang: H(v[lang]) for lang in LANGS} for k, v in sorted(mt.ALL_TABLES.items())}
    frozen = json.loads(FINGERPRINTS.read_text())
    assert set(current) == set(frozen), f"textes ajoutés ou retirés : {sorted(set(current) ^ set(frozen))}"
    changed = sorted(f"{k}/{lang}" for k in current for lang in LANGS if current[k][lang] != frozen[k][lang])
    assert not changed, changed


def test_markers_readers_recognise_every_language():
    for lang in LANGS:
        assert mt.PENDING_BARE[lang].startswith(mt.marks(mt.PENDING_BARE, head=" (")[LANGS.index(lang)])
        assert mt.PENDING[lang].startswith(mt.PENDING_BARE[lang].split(" (")[0])
        assert mt.PENDING_SKIPPED[lang].startswith(mt.PENDING_SKIPPED_MARK[lang])
        assert mt.ESCALATED[lang].startswith(mt.marks(mt.ESCALATED, head=" (")[LANGS.index(lang)])
        assert mt.SECRET_NOTICE[lang].startswith(mt.SECRET_NOTICE_MARKS[LANGS.index(lang)])
        assert mt.NOTE_NOT_WRITTEN[lang].startswith(mt.NOTE_NOT_WRITTEN_MARK[lang])
        assert mt.REFUSED_WHY[lang].startswith(mt.REFUSED_UPPER[lang])
        with lang_scope(lang):
            from nokker.core.refusal import is_refusal, refused_text
            assert is_refusal(refused_text("motif")) and is_refusal(mt.REFUSED_WHY[lang].format(why="x"))
            from nokker.core.tool_outcomes import _PENDING
            assert mt.PENDING[lang].startswith(tuple(_PENDING))
            assert mt.CALL_CANCELLED[lang] in _PENDING


# ── balayage : aucune phrase française « en dur » dans un résultat d'outil ou une note système ──
SWEPT = (
    "nokker/core/loop.py", "nokker/core/approval_repeat.py", "nokker/core/deferred_args.py", "nokker/approvals/executor.py",
    "nokker/qa/gates.py", "nokker/qa/invariants.py", "nokker/qa/permissions.py", "nokker/tools/registry.py",
    "nokker/tools/select.py", "nokker/tools/hooks.py", "nokker/tools/processes.py", "nokker/tools/checkpoints.py",
    "nokker/tools/readfile_hint.py", "nokker/tools/skills.py", "nokker/tools/ssh.py", "nokker/tools/web.py",
    "nokker/tools/delegate.py", "nokker/tools/code.py", "nokker/tools/browser_pool.py", "nokker/browserlive/live.py",
    "nokker/context/project_memory.py", "nokker/redact/guard.py",
)
# Appels dont les arguments textuels partent au modèle : résultat d'outil, message système, retour d'outil.
_SINKS = {"ToolResult", "out", "err", "_result"}
_FRENCH = re.compile(r"[éèàêçùâîôû]|\b(le|la|les|des|une|pas|est|tu|ton|ta|te|ne|qui|que|avec|dans|sans|pour|ce|cette|du|"
                     r"sur|et|ou|il|elle)\b", re.IGNORECASE)

# (module, début du littéral) : textes français légitimes ou restant à convertir, chacun avec sa raison. Une entrée qui ne
# correspond plus à rien fait échouer le test (liste à jour).
EXEMPT: dict[tuple[str, str], str] = {}


def _literals(call: ast.Call):
    """Chaînes littérales (et morceaux de f-strings) passées en argument d'un appel « sortie »."""
    args = [*call.args, *(k.value for k in call.keywords if k.arg in ("content", "note"))]
    for a in args:
        for n in ast.walk(a):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                yield n


def _french_hits() -> list[tuple[str, int, str]]:
    hits = []
    for rel in SWEPT:
        tree = ast.parse((ROOT / rel).read_text())
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            fn = call.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in _SINKS:
                continue
            for lit in _literals(call):
                s = lit.value
                if len(s.split()) >= 3 and len(_FRENCH.findall(s)) >= 2:
                    hits.append((rel, lit.lineno, s))
    return hits


def test_no_french_sentence_is_hardcoded_in_a_tool_result():
    unmatched = set(EXEMPT)
    bad = []
    for rel, line, s in _french_hits():
        key = next(((m, p) for (m, p) in EXEMPT if m == rel and s.startswith(p)), None)
        if key is not None:
            unmatched.discard(key)
            continue
        bad.append(f"{rel}:{line} {s[:70]!r}")
    assert not bad, "phrase française hors des tables de model_texts.py :\n" + "\n".join(bad)
    assert not unmatched, f"exemptions sans objet : {sorted(unmatched)}"


if __name__ == "__main__":  # refige les empreintes après relecture : PYTHONPATH=. python tests/unit/test_model_texts_langs.py
    FINGERPRINTS.write_text(json.dumps(
        {k: {lang: H(v[lang]) for lang in LANGS} for k, v in sorted(mt.ALL_TABLES.items())}, indent=0, sort_keys=True))
