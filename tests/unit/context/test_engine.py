"""`ContextEngineV0` — couches, hash de préfixe stable, réservoir v0 (troncature)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from nokker.context.engine import ZURICH, ContextEngineV0, estimate_tokens, format_now_fr
from nokker.core.ports import Recall, ToolSpec
from nokker.core.turn import Effect, InputCheck, Message, Tier, ToolCall, ToolResult, Turn


@dataclass
class FakeRegistry:
    core_specs: list[ToolSpec] = field(default_factory=list)
    index_entries: list[tuple[str, str]] = field(default_factory=list)

    def core(self):
        return self.core_specs

    def index(self):
        return self.index_entries


def make_turn(text="question ?", thread="t1", index=0) -> Turn:
    return Turn(thread_id=thread, index=index, input=InputCheck(text=text))


def test_format_now_fr_french_weekday_month_and_iso():
    # mardi 20 octobre 2026, 14:05
    dt = datetime(2026, 10, 20, 14, 5, tzinfo=ZURICH)
    out = format_now_fr(dt)
    assert "mardi 20 octobre 2026" in out
    assert "14:05" in out
    assert "2026-10-20T14:05:00" in out


def test_date_injected_in_turn_block_not_in_prefix(tmp_path):
    """Régression P1-S1 itération 3 (17.10.2026) : le modèle n'avait AUCUNE date, ne pouvait pas
    résoudre une date relative (« mardi prochain », « le 24 », « vendredi ») et appelait `clarify`
    pour demander — la date doit être dans le BLOC DU TOUR (couche 5), jamais dans le préfixe (A4 :
    hash stable entre tours/paliers, cf tests ci-dessus, non affectés par ce champ)."""
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    fixed_now = datetime(2026, 10, 20, 14, 5, tzinfo=ZURICH)
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix), now_fn=lambda: fixed_now)
    assembled = engine.assemble(make_turn(), [], [], [], Tier.SHIP)
    assert assembled.messages[0] == {"role": "system", "content": "Tu es Atlas."}  # préfixe intact
    assert assembled.messages[-1]["role"] == "user"
    assert "mardi 20 octobre 2026" in assembled.messages[-1]["content"]
    assert "2026-10-20T14:05:00" in assembled.messages[-1]["content"]


def test_turn_date_is_frozen_across_calls_of_the_same_turn(tmp_path):
    """Chantier cache (20.10.2026) : un horodatage recalculé à chaque appel du même tour cassait le préfixe
    réutilisable au bloc du tour. Figé par tour ; un nouveau tour prend la nouvelle heure."""
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    clock = iter([datetime(2026, 10, 20, 14, 5, s, tzinfo=ZURICH) for s in range(0, 50, 10)])
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix), now_fn=lambda: next(clock))
    t3 = make_turn(index=3)  # le MÊME tour d'un appel à l'autre (c'est le cas de la boucle) : en-tête figé sur le Turn
    first = engine.assemble(t3, [], [], [], Tier.SHIP)
    again = engine.assemble(t3, [], [], [], Tier.SHIP)
    assert first.messages[-1]["content"] == again.messages[-1]["content"]
    nxt = engine.assemble(make_turn(index=4), [], [], [], Tier.SHIP)
    assert "14:05:10" in nxt.messages[-1]["content"] and "14:05:00" in first.messages[-1]["content"]


def test_prefix_hash_unaffected_by_now_fn(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    e1 = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix),
                         now_fn=lambda: datetime(2026, 1, 1, tzinfo=ZURICH))
    e2 = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix),
                         now_fn=lambda: datetime(2027, 6, 15, 9, 30, tzinfo=ZURICH))
    a = e1.assemble(make_turn(), [], [], [], Tier.SHIP)
    b = e2.assemble(make_turn(), [], [], [], Tier.SHIP)
    assert a.prefix_sha256 == b.prefix_sha256
    assert a.messages[0] == b.messages[0] == {"role": "system", "content": "Tu es Atlas."}


def test_prefix_hash_identical_across_tiers(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    turn = make_turn()
    a = engine.assemble(turn, [], [], [], Tier.SHIP)
    b = engine.assemble(turn, [], [], [], Tier.DEEP)
    assert a.prefix_sha256 == b.prefix_sha256


def test_prefix_hash_identical_across_turns(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    a = engine.assemble(make_turn(index=0), [], [], [], Tier.SHIP)
    b = engine.assemble(make_turn(text="autre question", index=7), [], [], [], Tier.SHIP)
    assert a.prefix_sha256 == b.prefix_sha256


def test_prefix_hash_does_not_depend_on_recall_or_loaded_tools(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    loaded = [ToolSpec(name="x", description="d", parameters={}, effect=Effect.READ)]
    a = engine.assemble(make_turn(), [], [], [], Tier.SHIP)
    b = engine.assemble(make_turn(), [], [Recall(note="n", text="t", score=0.9)], loaded, Tier.SHIP)
    assert a.prefix_sha256 == b.prefix_sha256


def test_prefix_hash_changes_with_core_tools(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    core = [ToolSpec(name="clarify", description="d", parameters={}, effect=Effect.READ, core=True)]
    e1 = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    e2 = ContextEngineV0(tools=FakeRegistry(core_specs=core), prefix_path=str(prefix))
    a = e1.assemble(make_turn(), [], [], [], Tier.SHIP)
    b = e2.assemble(make_turn(), [], [], [], Tier.SHIP)
    assert a.prefix_sha256 != b.prefix_sha256


def test_layer1_system_message_is_prefix_file_content(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("INVARIANT: ne jamais recopier un secret.")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    assembled = engine.assemble(make_turn(), [], [], [], Tier.SHIP)
    assert assembled.messages[0] == {"role": "system", "content": "INVARIANT: ne jamais recopier un secret."}


def test_history_reconstructs_user_and_assistant_messages(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    past = make_turn(text="mon agenda ?", index=0)
    past.messages = [Message(role="assistant", content="2 rdv")]
    current = make_turn(text="et demain ?", index=1)
    assembled = engine.assemble(current, [past], [], [], Tier.SHIP)
    contents = [m["content"] for m in assembled.messages if m.get("role") == "user"]
    # "et demain ?" porte maintenant la date/heure en plus (couche 5) : vérifie par préfixe de ligne,
    # pas par égalité exacte.
    assert "mon agenda ?" in contents
    assert any(c.endswith("et demain ?") and c.startswith("[Date et heure actuelles]") for c in contents)
    assert {"role": "assistant", "content": "2 rdv"} in assembled.messages


def test_recall_rendered_in_turn_block(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    recall = [Recall(note="hote21-gpu-experiment-safety", text="GO explicite requis", score=0.81)]
    assembled = engine.assemble(make_turn(), [], recall, [], Tier.SHIP)
    # Rendu dans le DERNIER message, rôle `user` (pas `system`) — cf. docstring de `assemble()` :
    # un message `system` qui n'est pas `messages[0]` fait échouer l'appel en 502 sur le Router
    # SOKKAN réel (mesuré P1-S1, 17.10.2026).
    assert assembled.messages[-1]["role"] == "user"
    rendered = assembled.messages[-1]["content"]
    assert "hote21-gpu-experiment-safety" in rendered and "GO explicite requis" in rendered


def test_non_core_index_rendered_but_not_in_tools_payload(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    reg = FakeRegistry(index_entries=[("send_bulk_email", "mailing de masse")])
    engine = ContextEngineV0(tools=reg, prefix_path=str(prefix))
    assembled = engine.assemble(make_turn(), [], [], [], Tier.SHIP)
    # Option A (Alice, 20.10.2026) : l'index est dans le message système, stable d'un tour à l'autre.
    assert assembled.messages[0]["role"] == "system"
    assert "send_bulk_email" in assembled.messages[0]["content"]
    assert "send_bulk_email" not in assembled.messages[-1]["content"]
    assert assembled.tools == []  # pas de schéma complet pour un outil non chargé


def test_history_replays_each_turn_exactly_as_sent_so_the_next_prompt_extends_the_previous(tmp_path):
    """Option A (cache entre tours) : le prompt du tour N+1 commence par le prompt complet du tour N — en-tête
    (date, rappel) figé et rejoué tel quel par l'historique, index d'outils dans le système."""
    from nokker.core.loop import prompt_text
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    clock = iter([datetime(2026, 10, 20, 14, m, tzinfo=ZURICH) for m in range(10)])
    reg = FakeRegistry(index_entries=[("send_bulk_email", "mailing de masse")])
    engine = ContextEngineV0(tools=reg, prefix_path=str(prefix), now_fn=lambda: next(clock))
    t0 = make_turn("première ?", index=0)
    a0 = engine.assemble(t0, [], [Recall(note="n", text="fait rappelé", score=0.9)], [], Tier.SHIP)
    t0.messages.append(Message("assistant", "réponse"))
    t1 = make_turn("seconde ?", index=1)
    a1 = engine.assemble(t1, [t0], [], [], Tier.SHIP)
    assert prompt_text(a1).startswith(prompt_text(a0))
    assert "fait rappelé" in a1.messages[1]["content"] and "14:00" in a1.messages[1]["content"]


def test_current_turn_tool_calls_replayed_on_next_assemble(tmp_path):
    """Régression P1-S1 (17.10.2026) : `core/loop.py` rappelle `assemble()` à CHAQUE appel du MÊME
    tour (pas une fois par tour) — sans rejouer `turn.messages`, le modèle ne voit jamais ses propres
    tool_calls/résultats déjà produits dans ce tour, et republie le même appel en boucle jusqu'à
    `max_calls` (mesuré en réel contre le Router : `memory_search`/`infra_disk_usage` rappelés jusqu'à
    14 fois). Le Protocol `ContextEngine` passe le `Turn` entier précisément pour permettre ceci."""
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    turn = make_turn(text="alerte disque, que faire ?")
    turn.messages = [
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="infra_disk_usage",
                                                                    arguments={"host": "hote21"})]),
        Message(role="tool", content='{"used_pct": 87}', tool_call_id="c1", name="infra_disk_usage"),
    ]
    assembled = engine.assemble(turn, [], [], [], Tier.SHIP)
    assert any(m.get("tool_calls") for m in assembled.messages if m["role"] == "assistant")
    assert {"role": "tool", "content": '{"used_pct": 87}', "tool_call_id": "c1",
            "name": "infra_disk_usage"} in assembled.messages
    # aucun message `system` après le premier (contrainte Router, cf. test ci-dessous)
    assert [m["role"] for m in assembled.messages].count("system") == 1


def test_at_most_one_system_message_and_it_is_first(tmp_path):
    """Régression P1-S1 (17.10.2026) : le Router SOKKAN réel renvoie 502 pour toute requête qui
    porte un message `system` hors position 0, ou plus d'un message `system` — mesuré en isolant
    chaque combinaison de rôles (`sys,user`→200, `user,sys`→502, `sys,sys`→502, `sys,user,sys`→502).
    Avant le correctif, `assemble()` ajoutait le rappel CortHeXis et l'index d'outils comme des
    messages `system` SUPPLÉMENTAIRES après le tour humain — cassait tout appel qui a un rappel ou
    des outils non cœur, c'est-à-dire la quasi-totalité des tours réels."""
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    reg = FakeRegistry(index_entries=[("send_bulk_email", "mailing de masse")])
    engine = ContextEngineV0(tools=reg, prefix_path=str(prefix))
    recall = [Recall(note="n", text="t", score=0.9)]
    past = make_turn(text="avant", index=0)
    past.messages = [Message(role="assistant", content="ok")]
    assembled = engine.assemble(make_turn(text="maintenant", index=1), [past], recall, [], Tier.SHIP)
    roles = [m["role"] for m in assembled.messages]
    assert roles.count("system") == 1
    assert roles[0] == "system"
    assert roles[-1] == "user"


def test_loaded_tool_schema_included_in_tools_payload(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    loaded = [ToolSpec(name="calendar_list", description="agenda",
                       parameters={"type": "object", "properties": {}}, effect=Effect.READ)]
    assembled = engine.assemble(make_turn(), [], [], loaded, Tier.SHIP)
    assert any(t["function"]["name"] == "calendar_list" for t in assembled.tools)


def test_admit_result_passes_short_results_through(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix), reservoir_char_limit=4000)
    result = ToolResult(call_id="c1", name="x", content="court")
    assert engine.admit_result(result) is result


def test_admit_result_truncates_long_results_with_mention(tmp_path):
    prefix = tmp_path / "prefix.md"
    prefix.write_text("P")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix), reservoir_char_limit=10)
    result = ToolResult(call_id="c1", name="x", content="0123456789abcdefghij")
    out = engine.admit_result(result)
    assert out.content.startswith("0123456789")
    assert "A-1" in out.content
    assert len(out.content) > 10  # texte tronqué + mention, pas juste coupé net


def test_estimate_tokens_is_len_over_4():
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("") == 0


def test_missing_prefix_file_gives_empty_layer_not_crash(tmp_path):
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(tmp_path / "absent.md"))
    assembled = engine.assemble(make_turn(), [], [], [], Tier.SHIP)
    assert assembled.messages[0] == {"role": "system", "content": ""}
