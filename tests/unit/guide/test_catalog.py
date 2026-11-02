"""Catalogue du Guide : anti-dérive contre les drapeaux de la config, cohérence des codes, textes propres."""
from __future__ import annotations

import dataclasses
import typing
from pathlib import Path

from nokker.config import NokkerConfig
from nokker.guide import catalog as C
from nokker.guide.texts import ALL_MODEL_TEXTS

ROOT = Path(__file__).resolve().parents[3]


def _flag_fields() -> dict[str, str]:
    """Champs de la config qui sont des drapeaux : booléens, et dicts de fonction (pas les réglages `*_settings`…)."""
    hints = typing.get_type_hints(NokkerConfig)
    out = {}
    for f in dataclasses.fields(NokkerConfig):
        tp = str(hints[f.name])
        if f.name.endswith(C.SETTINGS_SUFFIXES):
            continue
        if hints[f.name] is bool or tp.startswith(("dict", "typing.Dict")) or "dict[" in tp:
            out[f.name] = tp
    return out


def test_every_config_flag_has_an_entry_or_an_explicit_exclusion():
    referenced = {fl.partition(".")[0] for f in C.CATALOG for fl in f.flags}
    missing = sorted(n for n in _flag_fields() if n not in referenced and n not in C.EXCLUDED)
    assert missing == [], f"drapeaux sans entrée du catalogue ni exclusion (catalog.EXCLUDED) : {missing}"


def test_catalog_and_exclusions_only_name_existing_fields():
    names = {f.name for f in dataclasses.fields(NokkerConfig)}
    unknown = sorted({fl.partition(".")[0] for f in C.CATALOG for fl in f.flags} - names)
    assert unknown == [], f"drapeaux inconnus de la config : {unknown}"
    assert sorted(set(C.EXCLUDED) - names) == [], "exclusion d'un champ qui n'existe plus"
    both = sorted({fl.partition(".")[0] for f in C.CATALOG for fl in f.flags} & set(C.EXCLUDED) - {"local"})
    assert both == [], f"à la fois exclu et au catalogue : {both}"


def test_entries_are_coherent():
    ids = [f.id for f in C.CATALOG]
    assert len(ids) == len(set(ids))
    for f in C.CATALOG:
        assert f.id.replace("_", "").isalnum() and f.id == f.id.lower(), f.id
        assert f.screen is None or f.screen in C.SCREENS, f.id
        assert f.ui is None or f.ui in C.UI_TARGETS, f.id
        assert set(f.targets) <= {C.CLOUD, C.LOKKAL} and f.targets, f.id
        assert set(f.profiles) <= set(C.PROFILES) and f.profiles, f.id
        assert set(f.needs) <= {"household", "telegram"}, f.id
        for txt in (f.name_fr, f.name_en, f.purpose_fr, f.example_fr, f.how_fr):
            assert txt.strip() and "\n" not in txt and len(txt) <= 160, (f.id, txt)


def test_flag_on_reads_fields_and_dotted_paths():
    cfg = NokkerConfig(slack={"enabled": True}, local={"lsp": {"enabled": True}}, show_me=True)
    assert C.flag_on(cfg, "show_me") and C.flag_on(cfg, "slack.enabled") and C.flag_on(cfg, "local.lsp.enabled")
    assert not C.flag_on(cfg, "whatsapp.enabled") and not C.flag_on(cfg, "local.cast.enabled")
    assert not C.flag_on(NokkerConfig(), "cloud_link") and C.flag_on(NokkerConfig(cloud_link={"x": 1}), "cloud_link")


def test_no_guard_word_in_catalog_or_model_texts():
    words = [w.strip().lower() for w in (ROOT / "tests/guard-words.txt").read_text(encoding="utf-8").splitlines()
             if w.strip() and not w.startswith("#")]
    corpus = " ".join([*ALL_MODEL_TEXTS, *(f"{f.name_fr} {f.name_en} {f.purpose_fr} {f.example_fr} {f.how_fr}"
                                          for f in C.CATALOG)]).lower()
    import re

    hits = [w for w in words if re.search(rf"\b{re.escape(w)}\b", corpus)]
    assert hits == []
