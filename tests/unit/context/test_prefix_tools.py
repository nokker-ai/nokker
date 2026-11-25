"""Le préfixe suit le catalogue du fil (`context/prefix_tools.py`) : sans outil retiré, le texte rendu est le préfixe d'avant les marques, à l'octet
près ; un outil retiré (`deny_tools`, `surfaces_gate`) n'est plus nommé dans le préfixe de CE fil ; empreinte stable par jeu d'outils ; par le vrai serveur."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from nokker.context import prefix_tools
from nokker.context.engine import ZURICH, ContextEngineV0
from nokker.context.project_memory import MEMORY_PREFIX_PATH
from nokker.core.ports import Completion
from nokker.core.turn import InputCheck, Message, Tier, Turn, Usage
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.resources import TenantRegistry
from nokker.tools.registry import Registry
from nokker.tools.scope import thread_scope
from tests.unit.context.test_engine import FakeRegistry

PROMPTS = Path(__file__).resolve().parents[3] / "nokker" / "prompts"
CODE = PROMPTS / "prefix-lokkal-code.md"
# sha256 des fichiers de préfixe AVANT l'ajout des marques de ligne (le texte vu du modèle ne change pas sans outil retiré)
# (code : empreintes recalculées quand la ligne « budget » est devenue « quand on te demande de terminer », puis quand l'invariant 2
# est devenu « une carte d'approbation est présentée », puis le 17.11.2026 : vocabulaire produit Lokkal / cloud / machine liée,
# préfixes code, dossier et produit ; puis le 18.11.2026 : ligne « Réponds dans la langue de la personne » dans les préfixes code et dossier ; puis le 18.11.2026 au soir : dans
# les préfixes code et dossier, « y compris pour demander d'accepter une carte » rattaché à la langue, p2/integ-lang-approval-wait)
CODE_BEFORE = "42ac0761e558ba8ac7b5c443d31cda2682cac83b575f48afb50c594af7776cfe"
MEMORY_BEFORE = "079579d4234a21bccdd07fce9801050dcba9eb4c425bec951e5a8d093f7cc052"
CODE_MEMORY_BEFORE = "6feed44d61e51fe44b553702575bff3602e56dea3f61b9406c4c22142fbf06ab"
UNMARKED_BEFORE = {"prefix-lokkal-folder.md": "ec87bcab0f5485da9ec7c8aeca236cfd688f06b4bfc79a22b985e879c298b9d4",
                   "prefix-nokker.md": "df0b988bd73b509fb723c2c801d23c57227316ea8bc21fc0f29ca1716b7213d8",
                   "prefix-young.md": "974dfcdd8b8c4606edc31759c59cedee3ec8ea4918090c9279c32e82b3668e05",
                   # invariants seuls (session `prefix: "host"`) : lignes 11 à 14 du préfixe de code, mot pour mot ; texte à geler
                   "prefix-invariants.md": "25ad5e9b472f67141a06da3e9e55046accfad43eedd76091bc7cde3aee3a8bb7"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _code() -> str:
    return CODE.read_text(encoding="utf-8")


def _with_memory() -> str:
    return _code().rstrip("\n") + "\n\n" + MEMORY_PREFIX_PATH.read_text(encoding="utf-8")


def _names(text: str, tool: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.search(rf"(?<![A-Za-z0-9_]){re.escape(tool)}(?![A-Za-z0-9_])", ln)]


def test_without_deny_the_rendered_prefix_is_the_old_one_byte_for_byte():
    assert _sha(prefix_tools.render(_code())) == CODE_BEFORE
    assert _sha(prefix_tools.render(MEMORY_PREFIX_PATH.read_text(encoding="utf-8"))) == MEMORY_BEFORE
    assert _sha(prefix_tools.render(_with_memory())) == CODE_MEMORY_BEFORE
    assert prefix_tools.render(_code(), ["inconnu", "autre"]) == prefix_tools.render(_code())  # nom absent du préfixe : rien ne change
    for name, sha in UNMARKED_BEFORE.items():  # les autres préfixes n'ont pas de marque : fichier lu tel quel
        assert _sha((PROMPTS / name).read_text(encoding="utf-8")) == sha, name


def test_no_mark_reaches_the_model_and_only_the_head_of_each_group_is_rendered():
    for hidden in ((), ("terminal",), ("patch", "write_file", "todo", "read_file", "process_stop", "memory_note")):
        out = prefix_tools.render(_with_memory(), hidden)
        assert "<!--" not in out and "-->" not in out
    assert prefix_tools.named(_with_memory()) == {"read_file", "search_files", "todo", "process_start", "process_logs", "process_stop",
                                                  "terminal", "patch", "write_file", "memory_note"}


def test_a_denied_name_is_never_named_in_the_rendered_prefix():
    for tool in sorted(prefix_tools.named(_with_memory())):
        out = prefix_tools.render(_with_memory(), [tool])
        assert not _names(out, tool), tool
        assert out != prefix_tools.render(_with_memory()), tool


def test_terminal_denied_always_says_do_not_launch_long_commands_never_the_process_variant():
    full = prefix_tools.render(_code())
    for hidden in (["terminal"], ["terminal", "process_start", "process_logs", "process_stop"], ["terminal", "process_logs"]):
        out = prefix_tools.render(_code(), hidden)
        for name in ("terminal", "process_start", "process_logs", "process_stop"):
            assert not _names(out, name), (hidden, name)
        assert "Une commande qui ne rend pas la main (serveur de dev, `watch`, `tail -f`) : ne la lance pas." in out
    assert "`process_start`" in full  # sans deny : la ligne d'aujourd'hui


def test_process_tools_denied_drop_their_line_for_a_neutral_one_and_the_rest_is_intact():
    full, out = prefix_tools.render(_code()), prefix_tools.render(_code(), ["process_start", "process_logs"])
    for name in ("process_start", "process_logs", "process_stop"):
        assert not _names(out, name)
    assert "ne la lance pas." in out and "`terminal`" not in out.split("ne la lance pas.")[0].splitlines()[-1]
    keep = [ln for ln in full.splitlines() if "process_start" not in ln]
    assert [ln for ln in out.splitlines() if "ne la lance pas." not in ln] == keep


# verbes qui supposent un outil : absents du rendu quand plus rien ne permet de les accomplir
RUN_VERBS = ("lance les tests", "lances les commandes", "relance les tests", "lance les tests EXISTANTS", "jusqu'au vert", "script d'essai",
             "commande de test", "ÉCHOUE", "lance les tests concernés", "reproduis-le d'abord")
READ_VERBS = ("lis les fichiers", "Tu lis le code", "Relis ce que tu as changé", "relis le fichier")
WRITE_VERBS = ("modifies les fichiers", "Modifie au plus juste", "réécris")


def _absent(out, verbs):
    return [v for v in verbs if v in out]


def test_run_verbs_follow_terminal_only():
    assert prefix_tools.render(_code()) .count("lance les tests") >= 3
    out = prefix_tools.render(_code(), ["terminal"])
    assert _absent(out, RUN_VERBS) == [] and not _names(out, "terminal")
    assert "Tu lis le code, cherches, modifies les fichiers, et tu rends" in out and "lis les fichiers concernés et les tests (`read_file`, `search_files`)." in out
    assert "Relis ce que tu as changé. Une erreur" in out and "range et conclus." in out
    assert "Courte : ce qui était cassé, ce que tu as changé (fichiers)." in out and "commande de test" not in out
    only_other = prefix_tools.render(_code(), ["execute_code", "process_start"])  # `execute_code` ne donne pas de commande : rien à retirer
    assert "lance les tests" in only_other and "`terminal`" not in only_other.split("ne la lance pas.")[0].splitlines()[-1]


def test_read_capability_combinations():
    for hidden, reads in ((["read_file"], True), (["search_files"], True), (["read_file", "search_files"], True),
                          (["read_file", "terminal"], True), (["search_files", "terminal"], True),
                          (["read_file", "search_files", "terminal"], False)):
        out = prefix_tools.render(_code(), hidden)
        assert bool(re.search(r"\blis les fichiers|Tu lis le code|Relis ce que", out)) is reads, hidden
        for name in hidden:
            assert not _names(out, name), (hidden, name)
        if not reads:
            assert _absent(out, READ_VERBS) == [] and _absent(out, RUN_VERBS) == []
    out = prefix_tools.render(_code(), ["read_file", "search_files", "terminal"])
    assert "Comprendre avant d'écrire" not in out and "Relis ce que" not in out and "Tu es Nokker, un agent de développement" in out


def test_write_capability_combinations():
    for hidden, writes in ((["patch"], True), (["write_file"], True), (["patch", "write_file"], True),
                           (["patch", "write_file", "terminal"], False)):
        out = prefix_tools.render(_code(), hidden)
        assert bool(re.search(r"Modifie au plus juste|modifies les fichiers", out)) is writes, hidden
        for name in hidden:
            assert not _names(out, name), (hidden, name)
    out = prefix_tools.render(_code(), ["patch", "write_file", "terminal"])
    assert _absent(out, WRITE_VERBS) == [] and _absent(out, RUN_VERBS) == []


def test_every_tool_alone_and_every_critical_pair_leaves_no_removed_name_and_no_orphan_verb():
    names = sorted(prefix_tools.named(_with_memory()))
    sets = [[n] for n in names] + [["terminal", "process_start"], ["terminal", "process_start", "process_logs", "process_stop"],
                                   ["read_file", "terminal"], ["read_file", "search_files"], ["patch", "write_file"],
                                   ["read_file", "search_files", "terminal"], ["patch", "write_file", "terminal"],
                                   ["todo", "memory_note"], names]
    base = prefix_tools.render(_with_memory())
    for hidden in sets:
        out = prefix_tools.render(_with_memory(), hidden)
        assert out != base and "<!--" not in out and "\n\n\n" not in out
        for name in hidden:
            assert not _names(out, name), (hidden, name)
        if "terminal" in hidden:
            assert _absent(out, RUN_VERBS) == [], hidden
        if "terminal" in hidden and {"read_file", "search_files"} <= set(hidden):
            assert _absent(out, READ_VERBS) == [], hidden
        if "terminal" in hidden and {"patch", "write_file"} <= set(hidden):
            assert _absent(out, WRITE_VERBS) == [], hidden
        assert "Tu es Nokker" in out and "Réponse finale" in out and "tutoie la personne" in out  # le socle reste
    assert prefix_tools.render(_with_memory(), ["rien", "ne_correspond"]) == base


def test_a_line_without_variant_disappears_without_a_blank_line_or_a_broken_list():
    full, out = prefix_tools.render(_code()), prefix_tools.render(_code(), ["todo"])
    assert not _names(out, "todo") and "\n\n\n" not in out
    assert out == full.replace("- Pour une tâche de plus de deux étapes, tiens un plan avec `todo` et mets-le à jour.\n", "")
    mem = MEMORY_PREFIX_PATH.read_text(encoding="utf-8")
    out_mem = prefix_tools.render(mem, ["memory_note"])
    assert not _names(out_mem, "memory_note") and out_mem.startswith("## Mémoire du projet\n\n- `[Mémoire du projet]`")
    assert "tutoie la personne" in out_mem and "\n\n\n" not in out_mem


def test_patch_and_write_file_variants():
    only_patch = prefix_tools.render(_code(), ["patch"])
    only_write = prefix_tools.render(_code(), ["write_file"])
    both = prefix_tools.render(_code(), ["patch", "write_file"])
    assert not _names(only_patch, "patch") and "`write_file` pour un fichier neuf" in only_patch
    assert not _names(only_write, "write_file") and "`patch` (mode `replace`" in only_write
    assert not _names(both, "patch") and not _names(both, "write_file") and "Modifie au plus juste" in both


def test_numbered_list_is_renumbered_and_empty_section_dropped():
    text = ("# T\n\n## Liste\n\n1. un\n2. deux <!-- needs: x -->\n3. trois\n\n## Vide\n\n- seul <!-- needs: x -->\n\n## Fin\n\nfin\n")
    assert prefix_tools.render(text) == text.replace(" <!-- needs: x -->", "")
    assert prefix_tools.render(text, ["x"]) == "# T\n\n## Liste\n\n1. un\n2. trois\n\n## Fin\n\nfin\n"


def test_crlf_and_missing_final_newline_are_kept():
    assert prefix_tools.render("a\r\nb <!-- needs: x -->\r\nc", []) == "a\r\nb\r\nc"
    assert prefix_tools.render("a\nb <!-- needs: x -->", ["x"]) == "a\n"
    assert prefix_tools.render("a\nb <!-- needs: x -->", []) == "a\nb"


# ── moteur : préfixe PAR FIL, empreinte, cache ──


def _engine(tmp_path, reg=None, text=None):
    prefix = tmp_path / "prefix.md"
    prefix.write_text(text if text is not None else _with_memory(), encoding="utf-8")
    with tenant_scope(INTERNAL_TENANT):  # le noyau fixe le locataire à la frontière
        return ContextEngineV0(tools=reg if reg is not None else TenantRegistry(internal=Registry(sources=[]), tester_sources=[]),
                               prefix_path=str(prefix), now_fn=lambda: datetime(2026, 11, 15, tzinfo=ZURICH))


def _system(eng, thread):
    with tenant_scope(INTERNAL_TENANT), thread_scope(thread):
        return eng.assemble(Turn(thread_id=thread, index=0, input=InputCheck(text="go")), [], [], [], Tier.SHIP).messages[0]["content"]


def test_engine_renders_per_thread_and_the_same_instance_keeps_the_full_prefix_elsewhere():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        reg = TenantRegistry(internal=Registry(sources=[]), tester_sources=[])
        eng = _engine(Path(d), reg)
        reg.hide_for_thread("th_a", ["terminal"])
        reg.hide_for_thread("th_b", ["process_start", "process_logs"])
        a, b, free = _system(eng, "th_a"), _system(eng, "th_b"), _system(eng, "th_free")
        assert not _names(a, "terminal") and not _names(a, "process_start") and "ne la lance pas." in a  # `terminal` retiré : jamais la variante process
        assert not _names(b, "process_start") and "ne la lance pas." in b
        assert _sha(free) == CODE_MEMORY_BEFORE and _names(free, "terminal")  # même instance, fil sans deny_tools : préfixe d'aujourd'hui
        assert _system(eng, "th_a/sa-1") == a  # un sous-agent a le préfixe de son parent
        with tenant_scope(INTERNAL_TENANT), thread_scope("th_a"):
            assert eng.stable_prefix() == a  # c'est ce que le fournisseur cache en tête du message système
        assert _system(eng, "th_a") == a  # stable sur la vie du fil


def test_prefix_sha256_is_stable_per_tool_set_and_ignores_names_the_prefix_does_not_use():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        reg = TenantRegistry(internal=Registry(sources=[]), tester_sources=[])
        eng = _engine(Path(d), reg)

        def sha(thread):
            with tenant_scope(INTERNAL_TENANT), thread_scope(thread):
                return eng._prefix_sha256

        base = sha("th_free")
        reg.hide_for_thread("th_a", ["terminal"])
        reg.hide_for_thread("th_a2", ["terminal"])
        reg.hide_for_thread("th_b", ["process_start"])
        reg.hide_for_thread("th_c", ["terminal", "bubble_show", "n_importe_quoi"])
        reg.hide_for_thread("th_d", ["bubble_show", "bubble_update"])
        assert sha("th_a") == sha("th_a") == sha("th_a2") and sha("th_a") != base
        assert sha("th_b") not in {base, sha("th_a")}
        assert sha("th_c") == sha("th_a")  # un nom que le préfixe n'emploie pas ne change pas l'empreinte
        assert sha("th_d") == base and sha("th_free") == base  # outils de bulles : aucune ligne ne les nomme


def test_without_a_registry_by_thread_the_engine_is_unchanged(tmp_path):
    eng = _engine(tmp_path, FakeRegistry())
    assert _sha(eng._prefix_text) == CODE_MEMORY_BEFORE and "<!--" not in eng._prefix_text
    with thread_scope("th_x"):
        assert eng.stable_prefix() == eng._prefix_text


# ── par le vrai serveur ──


def test_session_opened_with_deny_tools_sends_a_prefix_without_the_removed_name(tmp_path):
    from tests.unit.test_fixes_e2e_contracts import _req
    from tests.unit.test_local_tool_call import _server

    httpd, port, api, kernel_, _, _, l3, _, _thread = _server(tmp_path)
    sent: list[list[dict]] = []

    class Model:
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            sent.append(messages)
            return Completion(message=Message("assistant", content="ok"), usage=Usage(prompt=1), model="t", latency_s=0.0)

    try:
        reg = TenantRegistry(internal=Registry(sources=[l3]), tester_sources=[])
        kernel_.tools = reg
        api.on_session_deny = lambda thread, names: reg.hide_for_thread(thread, names)
        kernel_.context = _engine(tmp_path, reg)
        kernel_.providers = {Tier.SHIP: Model(), Tier.DEEP: Model()}
        cwd = str(tmp_path / "projets" / "app")
        plain = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd})[1]["thread_id"]
        status, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd, "deny_tools": ["terminal", "todo"]})
        assert status in (200, 201)
        denied = body["thread_id"]

        def chat(thread):
            assert _req(port, "/v1/chat/completions", token=api.token, headers_extra={"X-Session-Id": thread},
                        body={"messages": [{"role": "user", "content": "salut"}]})[0] == 200
            return sent[-1]

        full = chat(plain)
        old = prefix_tools.render(_with_memory())
        assert full[0]["role"] == "system" and _sha(old) == CODE_MEMORY_BEFORE and full[0]["content"].startswith(old)  # tête = préfixe d'avant
        cut = chat(denied)
        flat = json.dumps(cut)
        assert not _names(cut[0]["content"], "terminal") and not _names(cut[0]["content"], "todo") and "<!--" not in flat
        assert not _names(cut[0]["content"], "process_start") and "`patch`" in cut[0]["content"]
        assert chat(plain)[0]["content"] == full[0]["content"]  # le fil sans deny_tools garde `terminal`
        assert chat(denied)[0]["content"] == cut[0]["content"]  # et celui-ci reste constant d'un tour à l'autre
    finally:
        httpd.shutdown()


def test_surfaces_gate_removed_bubble_tools_reach_the_same_hook(tmp_path):
    """Les outils de bulles retirés par `surfaces_gate` passent par le même crochet : un préfixe qui les nommerait suivrait."""
    from nokker.local.api import BUBBLE_TOOLS
    text = "base\n- ligne <!-- needs: bubble_show,bubble_update -->\nfin\n"
    assert prefix_tools.render(text, BUBBLE_TOOLS) == "base\nfin\n"
    assert prefix_tools.render(text, ["bubble_show"]) == "base\nfin\n" and prefix_tools.render(text, []) == "base\n- ligne\nfin\n"


def test_understand_line_for_the_eight_read_x_terminal_combinations():
    """Ligne « Comprendre avant d'écrire » : une seule variante par combinaison, et `read_file`/`search_files` ne sont
    nommés que s'ils sont tous deux présents, avec ou sans `terminal`."""
    both = "(`read_file`, `search_files`)"
    run = "lance les tests une première fois"
    for rf in (True, False):
        for sf in (True, False):
            for term in (True, False):
                hidden = [n for n, on in (("read_file", rf), ("search_files", sf), ("terminal", term)) if not on]
                lines = [ln for ln in prefix_tools.render(_code(), hidden).splitlines() if ln.startswith("- Comprendre avant d'écrire")]
                assert len(lines) == (1 if rf or sf or term else 0), hidden  # ni lecture ni commande : la ligne disparaît
                if not lines:
                    continue
                line = lines[0]
                assert ("`read_file`" in line) == (rf and sf) == (both in line), hidden
                assert "`search_files`" not in line or (rf and sf), hidden
                assert (run in line) == term and "<!--" not in line, hidden
