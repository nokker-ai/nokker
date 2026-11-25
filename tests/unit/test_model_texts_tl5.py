"""Lot turn-lang-5 : les textes que les outils « natifs » et les modules métier rendent au MODÈLE suivent la langue du
tour (en/de/it traduits), le français reste celui d'avant (octet pour octet, empreintes gelées dans
`model_texts_fingerprints.json`)."""
from __future__ import annotations

import os

import pytest

from nokker.approvals.rule_labels import lang_scope
from nokker.core.ports import ToolContext
from nokker.tools.code import CodeToolSource, Workspace
from tests.unit.tools.test_code_host_paths import Env, Ops

CTX = ToolContext(thread_id="th_1", turn_index=0, channel="api")
LANG_PARAMS = ["fr", "en", "de", "it"]


# ── fichiers et terminal (tools/code.py) ──────────────────────────────────────────────────────────────────────────────
def _code(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    os.symlink(tmp_path, work / "dirlien")
    src = CodeToolSource(root=str(work), backend="local")
    src._spaces["th_1"] = Workspace(env=Env(), ops=Ops(), todos=None, cwd=str(work))
    return src


CODE_CASES = [  # (outil, arguments, {langue: fragment attendu})
    ("nope", {}, {"fr": "outil inconnu : nope", "en": "unknown tool: nope", "de": "unbekanntes Werkzeug: nope",
                  "it": "strumento sconosciuto: nope"}),
    ("patch", {"mode": "replace", "path": "a"},
     {"fr": "mode replace : path, old_string et new_string sont requis", "en": "mode replace: path, old_string and new_string are required",
      "de": "Modus replace: path, old_string und new_string sind erforderlich",
      "it": "modalità replace: path, old_string e new_string sono richiesti"}),
    ("patch", {"mode": "zz"}, {"fr": "mode inconnu : 'zz' (replace | patch)", "en": "unknown mode: 'zz' (replace | patch)",
                               "de": "unbekannter Modus: 'zz' (replace | patch)", "it": "modalità sconosciuta: 'zz' (replace | patch)"}),
]


@pytest.mark.parametrize("lang", LANG_PARAMS)
@pytest.mark.parametrize("name,args,expect", CODE_CASES)
def test_code_tool_errors_follow_the_turn_language(tmp_path, lang, name, args, expect):
    src = _code(tmp_path)
    with lang_scope(lang):
        r = src.call(name, args, CTX)
    assert r.is_error and expect[lang] in r.content


@pytest.mark.parametrize("lang", LANG_PARAMS)
def test_code_texts_without_a_direct_path_follow_the_turn_language(lang):
    """Textes atteints seulement par un processus ou un bac à sable : la table, lue dans la langue du tour."""
    from nokker.core import model_texts as mt
    from nokker.core.model_texts import pick

    with lang_scope(lang):
        assert pick(mt.CODE_PORT_INVALID).format(port=7) == {
            "fr": "port invalide : 7", "en": "invalid port: 7", "de": "ungültiger Port: 7", "it": "porta non valida: 7"}[lang]
        assert pick(mt.CODE_PROCESS_HINT_PORT).format(port=7).startswith(" ; ")
        assert ("process_logs" in pick(mt.CODE_PROCESS_HINT)) and ("process_stop" in pick(mt.CODE_PROCESS_HINT))
        assert pick(mt.CODE_OUTSIDE_ROOT).format(path="'x'") == {
            "fr": "'x' sort du dossier de travail", "en": "'x' is outside the working folder",
            "de": "'x' liegt außerhalb des Arbeitsordners", "it": "'x' esce dalla cartella di lavoro"}[lang]
        assert pick(mt.CODE_EXEC_LOCAL).startswith("execute_code")


# ── toutes les nouvelles tables : la langue du tour choisit la colonne, le français est la colonne `fr` ────────────────
def _tl5_tables():
    from nokker.core import model_texts_tools as mtt

    return sorted((n, t) for n, t in vars(mtt).items() if n.isupper() and isinstance(t, dict))


@pytest.mark.parametrize("name,table", _tl5_tables())
def test_every_tl5_table_follows_the_language_of_the_turn(name, table):
    from nokker.core.model_texts import pick

    for lang in ("fr", "en", "de", "it"):
        with lang_scope(lang):
            assert pick(table) == table[lang], (name, lang)
    assert pick(table) == table["fr"]  # hors tour : français


def test_the_unknown_tool_text_is_read_back_in_every_language():
    """Les lecteurs de ce texte (traces d'amélioration, clôture web) reconnaissent chaque langue."""
    from nokker.core import model_texts as mt
    from nokker.improve.traces import UNKNOWN_PREFIX
    from nokker.tools.web_fence import KERNEL_TEXTS

    for lang in ("fr", "en", "de", "it"):
        for table in (mt.UNKNOWN_TOOL, mt.UNKNOWN_TOOL_BARE):
            text = table[lang].format(name="x")
            assert text.lower().startswith(UNKNOWN_PREFIX) and text.startswith(KERNEL_TEXTS), (lang, text)
        for table in (mt.WEB_EMPTY_QUERY, mt.WEB_NO_URLS):
            assert table[lang].startswith(KERNEL_TEXTS), (lang, table[lang])
        assert mt.WEB_PERSONAL[lang].format(kind="x").startswith(KERNEL_TEXTS), lang


# ── web (tools/web.py, tools/web_fence.py) ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lang,head,page", [
    ("fr", "Résultats web pour « x » (texte brut) :", "Page (4 caractères) :"),
    ("en", "Web results for \"x\" (plain text):", "Page (4 characters):"),
    ("de", "Webergebnisse für „x“ (Rohtext):", "Seite (4 Zeichen):"),
    ("it", "Risultati web per «x» (testo semplice):", "Pagina (4 caratteri):"),
])
def test_web_fence_headers_follow_the_turn_language(lang, head, page):
    from nokker.tools.web_fence import fence_web_result

    with lang_scope(lang):
        assert head in fence_web_result("web_search", {"query": "x"}, "texte brut")
        assert page in fence_web_result("web_fetch", {"urls": ["a", "b"]}, "abcd")


@pytest.mark.parametrize("lang,label", [("fr", "numéro AVS"), ("en", "AVS number"), ("de", "AHV-Nummer"),
                                        ("it", "numero AVS")])
def test_web_personal_data_kind_follows_the_turn_language(lang, label):
    from nokker.tools.web import pii_violation

    with lang_scope(lang):
        assert pii_violation("756.1234.5678.97") == label


# ── ssh (tools/ssh.py) ────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lang,frag", [("fr", "délai dépassé"), ("en", "timed out"), ("de", "Zeitüberschreitung"),
                                       ("it", "tempo scaduto")])
def test_ssh_hints_follow_the_turn_language(lang, frag):
    from nokker.tools.ssh import SshToolSource

    with lang_scope(lang):
        assert frag in SshToolSource._hint(124, "")
        assert "(code 255)" in SshToolSource._hint(255, "Permission denied") or "255" in SshToolSource._hint(255, "Permission denied")


# ── sous-agents (tools/delegate.py) ───────────────────────────────────────────────────────────────────────────────────
class _Brick:
    """Forme de la brique amont `tools.delegation_output_schema` (messages anglais d'origine), avec espions."""

    def __init__(self):
        self.calls = []

    def coerce_output_schema(self, raw):
        self.calls.append("coerce")
        if isinstance(raw, str):
            return None, "output_schema must be a JSON Schema object, got a non-JSON string."
        if not isinstance(raw, dict):
            return None, f"output_schema must be a JSON Schema object, got {type(raw).__name__}."
        if raw.get("type") == "bogus":
            return None, "output_schema is not a valid JSON Schema: 'bogus' is not valid under any of the given schemas"
        return raw, None

    def append_output_contract(self, context, schema):
        self.calls.append("contract")
        return "OUTPUT CONTRACT (machine-validated):\nYour FINAL response must be ONLY the JSON value"

    def validate_output(self, text, schema):
        self.calls.append("validate")
        if not (text or "").strip():
            return False, ["Response was empty — expected a JSON object matching the schema."]
        if text == "nope":
            return False, ["Response is not valid JSON: Expecting value: line 1 column 1 (char 0)"]
        return False, ["$.items[0].name: 'x' is not of type 'integer'", "$: 'id' is a required property"]

    def build_retry_message(self, errors):
        self.calls.append("retry")
        return "Your previous final response was rejected by the output contract validator."


@pytest.fixture(params=["brick", "absent"])
def brick(request, monkeypatch):
    import sys
    import types

    import nokker.tools.delegate as dg

    if request.param == "absent":
        monkeypatch.setattr(dg, "_schema_module", lambda: None)
        return None
    fake = _Brick()
    pkg = types.ModuleType("tools")
    pkg.__path__ = []
    pkg.delegation_output_schema = fake
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.delegation_output_schema", fake)
    return fake


@pytest.mark.parametrize("lang,note,contract,err", [
    ("fr", "[ISOLATION WORKTREE] Tu travailles", "[Contrat de sortie]", "output_schema n'est pas un JSON valide"),
    ("en", "[WORKTREE ISOLATION] You work", "[Output contract]", "output_schema is not valid JSON"),
    ("de", "[WORKTREE-ISOLATION] Du arbeitest", "[Ausgabevertrag]", "output_schema ist kein gültiges JSON"),
    ("it", "[ISOLAMENTO WORKTREE] Lavori", "[Contratto di uscita]", "output_schema non è un JSON valido"),
])
def test_delegate_child_prompts_follow_the_turn_language(brick, lang, note, contract, err):
    from nokker.tools.delegate import append_contract, coerce_schema, worktree_context_note

    with lang_scope(lang):
        assert note in worktree_context_note({"path": "/p", "branch": "b"})
        text = append_contract("", {"type": "object"})
        assert contract in text and "OUTPUT CONTRACT" not in text  # jamais le texte anglais de la brique
        assert coerce_schema("{")[1] == err
    if brick is not None:
        assert "coerce" in brick.calls and "contract" not in brick.calls  # validation de la brique appelée


@pytest.mark.parametrize("lang,invalid,obj,empty,field,retry", [
    ("fr", "output_schema n'est pas un JSON Schema valide (champ « $ »)", "output_schema doit être un objet JSON Schema",
     "réponse vide : un objet JSON conforme au schéma est attendu", "$.items[0].name : ne respecte pas le schéma",
     "refusée par le contrat de sortie"),
    ("en", "output_schema is not a valid JSON Schema (field \"$\")", "output_schema must be a JSON Schema object",
     "empty response: a JSON object matching the schema is expected", "$.items[0].name: does not match the schema",
     "refused by the output contract"),
    ("de", "output_schema ist kein gültiges JSON-Schema (Feld „$“)", "output_schema muss ein JSON-Schema-Objekt sein",
     "leere Antwort: Ein JSON-Objekt gemäß Schema wird erwartet", "$.items[0].name: entspricht nicht dem Schema",
     "vom Ausgabevertrag abgelehnt"),
    ("it", "output_schema non è un JSON Schema valido (campo «$»)", "output_schema deve essere un oggetto JSON Schema",
     "risposta vuota: è atteso un oggetto JSON conforme allo schema", "$.items[0].name: non rispetta lo schema",
     "rifiutata dal contratto di uscita"),
])
def test_delegate_validation_errors_are_ours_in_the_turn_language(brick, lang, invalid, obj, empty, field, retry):
    from nokker.core import model_texts as mt
    from nokker.core.model_texts import pick
    from nokker.tools.delegate import coerce_schema, retry_message, validate_reply

    with lang_scope(lang):
        assert coerce_schema(3)[1] == obj or brick is None  # sans brique : repli maison (même texte pour « pas un objet »)
        assert retry in retry_message(["e"])
        assert "rejected by the output contract" not in retry_message(["e"])
        if brick is None:
            assert coerce_schema({"type": "bogus"}) == ({"type": "bogus"}, None)
            return
        assert coerce_schema({"type": "bogus"})[1].replace("$.type", "$") == invalid  # chemin relu par jsonschema si installé
        assert validate_reply("", {"type": "object"})[1] == [empty]
        ok, errors = validate_reply("x", {"type": "object"})
        assert not ok and errors[0] == field and len(errors) == 2 and all("integer" not in e for e in errors)
        assert validate_reply("nope", {})[1] == [pick(mt.DELEGATE_REPLY_NOT_JSON)]
    if brick is not None:
        assert "validate" in brick.calls and "retry" not in brick.calls


# ── mémoire longue (memory/long_note.py) ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lang,size,written", [
    ("fr", "requis : une note par idée, courte", "rien n'a été noté"),
    ("en", "required: one note per idea, short", "nothing was noted"),
    ("de", "erforderlich: eine Notiz pro Idee, kurz", "nichts wurde notiert"),
    ("it", "richiesti: una nota per idea, breve", "non è stato annotato nulla"),
])
def test_long_note_refusals_follow_the_turn_language(lang, size, written):
    from tests.unit.memory.test_long_note import Env, args

    env = Env()
    with lang_scope(lang):
        text = env.call(args(title="")).content
    assert size in text
    assert written in text


# ── courrier et agenda ────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lang,frag", [
    ("fr", "date/heure invalide : 'zz' — attendu ISO 8601"), ("en", "invalid date/time: 'zz' — expected ISO 8601"),
    ("de", "ungültiges Datum/ungültige Uhrzeit: 'zz' — erwartet ISO 8601"),
    ("it", "data/ora non valida: 'zz' — atteso ISO 8601")])
def test_calendar_validation_errors_follow_the_turn_language(lang, frag):
    from zoneinfo import ZoneInfo

    from nokker.connectors.calendar import parse_input_time

    with lang_scope(lang), pytest.raises(ValueError) as exc:
        parse_input_time("zz", ZoneInfo("UTC"))
    assert frag in str(exc.value)
