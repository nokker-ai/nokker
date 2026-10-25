"""Mémoire longue de Nokker Lokkal sur le VRAI CortHeXis (lot X1b, docs/LOKKAL.md §7.13) : un fait noté dans un fil
est rappelé au tour d'un autre fil, par le noyau construit comme en service (`build_kernel` en mode direct, mémoire de
projet installée, permissions locales), contre un vrai Postgres pgvector et un vrai serveur d'embeddings (llama.cpp
CPU, EmbeddingGemma-300M, comme le compose). Seul le modèle est scripté.

Marqué `integration` : SKIP sans `NOKKER_TEST_CORTHEXIS_DSN` / `NOKKER_TEST_CORTHEXIS_EMBED_URL` joignables (ou leurs
défauts de `conftest.py`), ou sans le paquet `corthexis`. Une base jetable par test, créée puis détruite ; jamais la
mémoire de prod ni sa base."""
from __future__ import annotations

import statistics
import time
import uuid

import pytest

pytestmark = pytest.mark.integration

SAID = "Retiens pour toujours : ma machine de sauvegarde s'appelle Zébulon, elle tourne la nuit à 2 h."
QUOTE = "ma machine de sauvegarde s'appelle Zébulon"
QUESTION = "Comment s'appelle la machine qui fait mes sauvegardes ?"
OFF_TOPIC = "Quelle est la capitale de l'Islande ?"
TESTER = "tn_aaaaaaaaaaaa"


class NoteThenAnswer:
    """Modèle scripté : à « Retiens… », appelle `memory_note` (portée user, citation de l'utilisateur) ; sinon répond.
    Garde chaque prompt reçu pour vérifier ce que le noyau a mis dans la fenêtre."""

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        from nokker.core.ports import Completion
        from nokker.core.turn import Message, ToolCall, Usage

        self.seen.append([dict(m) for m in messages])
        last_user = next(m for m in reversed(messages) if m["role"] == "user")["content"]
        if "Retiens pour toujours" in last_user and messages[-1]["role"] != "tool":
            args = {"kind": "fact", "title": "Machine de sauvegarde", "text": "La machine de sauvegarde de "
                    "l'utilisateur s'appelle Zébulon ; elle tourne la nuit à 2 h.", "derived_from": "user",
                    "scope": "user", "quote": QUOTE}
            import json

            msg = Message(role="assistant", content="", tool_calls=[
                ToolCall(id="c1", name="memory_note", arguments=args, raw_arguments=json.dumps(args))])
        else:
            msg = Message(role="assistant", content="Entendu.")
        return Completion(message=msg, usage=Usage(prompt=100, completion=10), model="scripted", latency_s=0.001)


def _timed(recall_for, thread_id: str, n: int = 10):
    times, hits = [], []
    for _ in range(n):
        t0 = time.monotonic()
        hits = recall_for(thread_id, QUESTION, 5)
        times.append(time.monotonic() - t0)
    return times, hits


def _prompt_text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


@pytest.fixture
def lokkal_kernel(request, tmp_path, corthexis_dsn, corthexis_embed_url, monkeypatch):
    mode = getattr(request, "param", "direct")  # `off` : témoin, le noyau d'avant X1b (mémoire longue nulle)
    pytest.importorskip("corthexis", reason="paquet corthexis absent")
    import psycopg
    from corthexis import embed as cx_embed
    from corthexis import service as cx_service

    from nokker.api.server import build_kernel
    from nokker.config import NokkerConfig
    from nokker.context.project_memory import ProjectMemoryTools, from_local_config, install
    from nokker.core.turn import Tier
    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates

    db = f"corthexis_x1b_{uuid.uuid4().hex[:8]}"
    admin = corthexis_dsn
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f'CREATE DATABASE "{db}"')
    dsn = corthexis_dsn.rsplit("/", 1)[0] + f"/{db}"
    for name, value in (("CORTHEXIS_DATABASE_URL", dsn), ("CORTHEXIS_EMBED_URLS", corthexis_embed_url),
                        ("CORTHEXIS_MEMORY_PROFILE", "leger")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CORTHEXIS_RERANK_URL", raising=False)
    monkeypatch.delenv("CORTHEXIS_RECALL_PROJECTS", raising=False)
    cx_service.reset()
    cx_embed.reset()

    # Comme `serve()` sur Lokkal (deploy/lokkal/core-config.yaml) : mémoire longue directe, mémoire de session sur
    # CortHeXis, mémoire de projet installée, permissions locales en accept_edits.
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", memory_mcp_url="", memory_mode=mode,
                       memory_direct={"projects": ["u-local"], "profile": "leger"}, mcp_tool_servers=[],
                       web_search_enabled=False, approvals_enabled=False, context_enabled=True,
                       context_corthexis={"enabled": True, "root_dir": str(tmp_path / "session-memory")})
    pm = from_local_config({"project_memory": True}, tmp_path / "state")
    kernel = build_kernel(cfg, extra_sources=[ProjectMemoryTools(pm)])
    install(kernel, pm)
    kernel.gates = PermissionGates(inner=kernel.gates, permissions=LocalPermissions.load(
        mode="accept_edits", fallback=Mode.ACCEPT_EDITS, user_settings=tmp_path / "settings.json"))
    provider = NoteThenAnswer()
    kernel.providers = {Tier.SHIP: provider, Tier.DEEP: provider}
    yield kernel, pm, provider
    pm.flush(60)
    cx_service.reset()
    cx_embed.reset()
    with psycopg.connect(admin, autocommit=True) as con:
        con.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')


def test_fact_noted_in_one_thread_is_recalled_in_another_turn(lokkal_kernel, tmp_path, capsys):
    from nokker.context.project_memory import USER_PROJECT
    from nokker.core.turn import TurnInput
    from nokker.local.api import LOCAL_OWNER
    from nokker.memory.corthexis import status_of
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    kernel, pm, provider = lokkal_kernel
    st = status_of(kernel.memory)
    assert st["mode"] == "direct" and st["ok"] is True, st  # vraie base, vrai embed

    # 1. Fil A (conversation, sans dossier) : l'utilisateur dit un fait durable, le modèle le note (portée user).
    with tenant_scope(INTERNAL_TENANT):
        kernel.run_turn(TurnInput(thread_id="th_a", user_text=SAID, sender=LOCAL_OWNER))
    notes = list((tmp_path / "state" / "project-memory" / USER_PROJECT).glob("*.md"))
    assert len(notes) == 1 and "Zébulon" in notes[0].read_text(encoding="utf-8")
    t0 = time.monotonic()
    assert pm.flush(180), "indexation CortHeXis non terminée"
    index_s = time.monotonic() - t0

    # 2. Fil B, un autre tour : la mémoire longue rappelle le fait dans `[Rappel mémoire]`, sans outil appelé.
    with tenant_scope(INTERNAL_TENANT):
        turn = kernel.run_turn(TurnInput(thread_id="th_b", user_text=QUESTION, sender=LOCAL_OWNER))
    prompt = _prompt_text(provider.seen[-1])
    assert "[Rappel mémoire]" in prompt and "Zébulon" in prompt, prompt[-1500:]
    assert any(v.rule == "memory:recall_used" for v in turn.verdicts)

    # 3. Temps de rappel mesuré, comme la boucle en tête de tour (`recall_for`) ; fil sans dossier : une recherche.
    with tenant_scope(INTERNAL_TENANT):
        times, hits = _timed(kernel.memory.recall_for, "th_b")
        noise = kernel.memory.recall_for("th_b", OFF_TOPIC, 5)
    assert hits and "Zébulon" in hits[0].text
    assert all(h.note != hits[0].note for h in noise), noise  # hors sujet : la note ne remonte pas

    # 4. Fil C, session de code dans un dépôt : la note `u-local` arrive par les deux rappels, n'entre qu'une fois.
    repo = tmp_path / "work" / "app"
    (repo / ".git").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# Tests\nlancer pytest -q avant de conclure\n", encoding="utf-8")
    pm.open_session("th_c", repo, tmp_path)
    assert pm.flush(180)
    with tenant_scope(INTERNAL_TENANT):
        kernel.run_turn(TurnInput(thread_id="th_c", user_text=QUESTION, sender=LOCAL_OWNER))
    block = _prompt_text(provider.seen[-1]).split("[Rappel mémoire]", 1)[1].split("[/Rappel]", 1)[0]
    assert block.count(f"[{hits[0].note}]") == 1, block
    with tenant_scope(INTERNAL_TENANT):  # session de code : mémoire longue + mémoire du projet, deux recherches
        times_c, _ = _timed(kernel.memory.recall_for, "th_c")

    # 5. Cloisonnement : un autre locataire n'a rien de la mémoire du propriétaire.
    with tenant_scope(TESTER):
        assert kernel.memory.recall(QUESTION) == []

    with capsys.disabled():
        print(f"\n[X1b] indexation d'une note : {index_s:.2f} s ; rappel fil sans dossier : médiane "
              f"{statistics.median(times) * 1000:.0f} ms, max {max(times) * 1000:.0f} ms ; session de code : médiane "
              f"{statistics.median(times_c) * 1000:.0f} ms, max {max(times_c) * 1000:.0f} ms (sur {len(times)}) ; "
              f"score {hits[0].score:.3f} ; hors sujet : {[(h.note, round(h.score, 3)) for h in noise]} ; "
              f"état : {st.get('store')} embed {st.get('embed')}")


@pytest.mark.parametrize("lokkal_kernel", ["off"], indirect=True)
def test_control_without_direct_memory_the_other_thread_gets_nothing(lokkal_kernel):
    """Témoin : le noyau d'avant X1b (mémoire longue nulle). La note est écrite, mais un fil sans dossier ne la revoit
    jamais — c'est le trou que ce lot ferme."""
    from nokker.core.turn import TurnInput
    from nokker.local.api import LOCAL_OWNER
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    kernel, pm, provider = lokkal_kernel
    with tenant_scope(INTERNAL_TENANT):
        kernel.run_turn(TurnInput(thread_id="th_a", user_text=SAID, sender=LOCAL_OWNER))
        assert pm.flush(180)
        kernel.run_turn(TurnInput(thread_id="th_b", user_text=QUESTION, sender=LOCAL_OWNER))
    assert "Zébulon" not in _prompt_text(provider.seen[-1])
