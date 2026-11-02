"""Traces d'entraînement (docs/TRACES.md) : une ligne par appel modèle, drapeau éteint = rien, training_ok selon le type
de compte et le consentement, secrets jamais exportés, PII des testeurs caviardées, lot signé (SHA256SUMS)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

import pytest

from nokker.config import NokkerConfig
from nokker.core.loop import Kernel
from nokker.core.ports import Assembled, Completion, RouteDecision, ToolSpec
from nokker.core.turn import (
    Decision,
    Effect,
    Gate,
    Message,
    Tier,
    ToolCall,
    ToolResult,
    TurnInput,
    Usage,
    Verdict,
)
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from nokker.store.pg import MemoryStore, migration_files, turn_to_dict
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.traces import pii
from nokker.traces.export import ExportOptions, MemoryTraceSource, export, user_signal, verify_lot
from nokker.traces.recorder import MemoryTraceSink, TraceRecorder, build_recorder

TESTER = "tn_aaaabbbbcccc"
OTHER_TESTER = "tn_ddddeeeeffff"
PREFIX = "Tu es Atlas."
SCHEMAS = [{"type": "function", "function": {"name": "calendar_list", "description": "agenda", "parameters": {}}}]
SECRET = "Zq9pLm2x" + "T7wK4vRb8"  # valeur du coffre du noyau (factice, construite à l'exécution : gitleaks)
EXPORT_ONLY_SECRET = "Hj3kLp9Q" + "wErTy7Ui"  # connue seulement du coffre de l'export (factice, gitleaks)


class Provider:
    def __init__(self, script):
        self.script, self.seen = list(script), []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(json.loads(json.dumps(messages)))
        return Completion(message=self.script.pop(0), usage=Usage(prompt=100, completion=7), model="m-ship",
                          latency_s=0.25)


class Ctx:
    def assemble(self, turn, history, recall, loaded, tier):
        msgs = [{"role": "system", "content": PREFIX}]
        for past in history:
            msgs += [{"role": "user", "content": past.input.text}] + [m.to_openai() for m in past.messages]
        msgs += [{"role": "user", "content": turn.input.text}] + [m.to_openai() for m in turn.messages]
        return Assembled(messages=msgs, tools=list(SCHEMAS), prefix_sha256=hashlib.sha256(PREFIX.encode()).hexdigest())

    def admit_result(self, r):
        return r


class Mem:
    def recall(self, q, k=5):
        return []

    def propose(self, t, f):
        pass


class Gates:
    def __init__(self, inv):
        self.inv = inv

    def action(self, call, spec, turn):
        return [self.inv.check_action(call, spec, turn)]

    def switch(self, reply, turn, tier):
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]


class Esc:
    def escalate(self, turn, reason):
        return "Je transmets."


class Pol:
    def choose(self, state):
        return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="défaut")


class Tools:
    specs: ClassVar[dict[str, ToolSpec]] = {"calendar_list": ToolSpec("calendar_list", "agenda", {}, Effect.READ),
             "vault_get": ToolSpec("vault_get", "coffre", {}, Effect.SECRET),
             "infra_wipe": ToolSpec("infra_wipe", "efface", {}, Effect.DESTRUCTIVE)}

    def core(self):
        return []

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        if n == "vault_get":
            return ToolResult(call_id="", name=n, content="mot de passe Xk9qpzT42LmWv8")
        return ToolResult(call_id="", name=n, content=f"2 rendez-vous ({a.get('day', '?')})")


class Ledger:
    def record(self, t, i, c):
        pass

    def record_verdict(self, t, i, v):
        pass


def make_kernel(script, recorder=None, store=None):
    known = KnownSecrets()
    known.add("ROUTER", SECRET)
    inv = DeterministicInvariants(known=known)
    prov = Provider(script)
    k = Kernel(store=store or MemoryStore(), context=Ctx(), memory=Mem(), invariants=inv, gates=Gates(inv),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: prov}, tools=Tools(), ledger=Ledger(),
               traces=recorder)
    return k, prov


def recorder(sink=None):
    return TraceRecorder(sink=sink or MemoryTraceSink(), seal=None, kernel_sha="abc1234", target="canary")


def tc(name, cid, **args):
    return ToolCall(id=cid, name=name, arguments=args)


THREE_CALLS = [Message("assistant", "je regarde", tool_calls=[tc("calendar_list", "c1", day="lundi")]),
               Message("assistant", tool_calls=[tc("calendar_list", "c2", day="mardi")]),
               Message("assistant", "2 rdv lundi, 2 mardi")]


def run(script, text="mon agenda ?", tenant=INTERNAL_TENANT, thread="fil", sender=None, rec=None, store=None):
    rec = rec if rec is not None else recorder()
    k, prov = make_kernel(script, rec, store)
    with tenant_scope(tenant):
        if rec.seal is None:
            rec.seal = k.invariants.redact_context
        turn = k.run_turn(TurnInput(thread, text, sender=sender))
    return turn, prov, rec


def source_from(sinks_rows, store_turns=(), consents=None, emails=None, approvals=None):
    turns = [(t, at, d) for t, at, d in store_turns]
    return MemoryTraceSource(rows=list(sinks_rows), turn_rows=turns, consents=consents or {}, emails=emails or {},
                             approval_rows=approvals or {})


def turns_of(store, tenant):
    at = datetime.now(timezone.utc)
    return [(tenant, at, turn_to_dict(t)) for ts in store._by_thread.values() for t in ts]


def window():
    now = datetime.now(timezone.utc)
    return now - timedelta(days=1), now + timedelta(days=1)


def opts(**kw):
    since, until = window()
    return ExportOptions(since=since, until=until, **kw)


def read_lines(lot: Path):
    return [json.loads(x) for x in (lot / "calls.jsonl").read_text(encoding="utf-8").splitlines()]


# ───────────────────────────── une ligne par appel modèle ─────────────────────────────


def test_three_model_calls_give_three_lines_with_the_exact_prompt(tmp_path):
    turn, prov, rec = run(list(THREE_CALLS))
    rows = rec.sink.rows
    assert [r["call_index"] for r in rows] == [0, 1, 2] and len(turn.calls) == 3
    for r, sent in zip(rows, prov.seen):
        assert r["prompt"] == sent  # EXACTEMENT ce que le fournisseur a reçu
        assert r["tools"] == SCHEMAS
    # Appel 0 : sa réponse (outil) puis le résultat de l'outil, jusqu'au prochain appel modèle.
    c0 = rows[0]["completion"]
    assert [m["role"] for m in c0] == ["assistant", "tool"] and c0[0]["tool_calls"][0]["id"] == "c1"
    assert "lundi" in c0[1]["content"]
    assert [m["role"] for m in rows[1]["completion"]] == ["assistant", "tool"]
    assert rows[2]["completion"] == [{"role": "assistant", "content": "2 rdv lundi, 2 mardi"}]
    # Le prompt de l'appel k+1 = prompt de k + sa suite (aucun message perdu entre deux lignes).
    assert rows[1]["prompt"] == rows[0]["prompt"] + rows[0]["completion"]

    lot = export(source_from(rows), tmp_path, opts(), exporter_sha="abc1234").path
    lines = read_lines(lot)
    assert [ln["meta"]["call"] for ln in lines] == [0, 1, 2]
    assert set(lines[0]) == {"prompt", "tools", "completion", "meta"}
    m = lines[0]["meta"]
    for key in ("kernel_sha", "target", "tenant_type", "thread", "turn", "call", "tier", "model", "latency_s", "tokens",
                "gates_and_invariants", "approvals", "escalation", "secret_seals", "judge_verdict_v3", "user_signal",
                "training_ok", "reconstructed"):
        assert key in m
    assert m["kernel_sha"] == "abc1234" and m["target"] == "canary" and m["tenant_type"] == "internal"
    assert m["tokens"] == {"prompt": 100, "cached": None, "completion": 7} and m["model"] == "m-ship"
    assert m["training_ok"] is True and m["reconstructed"] is False and m["thread"] == "fil"


def test_recording_has_no_effect_on_what_the_model_sees():
    _, with_rec, _ = run(list(THREE_CALLS))
    k, without = make_kernel(list(THREE_CALLS), None)
    with tenant_scope(INTERNAL_TENANT):
        k.run_turn(TurnInput("fil", "mon agenda ?"))
    assert with_rec.seen == without.seen


def test_recorder_failure_never_breaks_the_turn():
    class Broken:
        def write(self, rows):
            raise RuntimeError("base en panne")

        def purge(self, days):
            return 0

    turn, _, _ = run(list(THREE_CALLS), rec=recorder(Broken()))
    assert turn.reply == "2 rdv lundi, 2 mardi" and turn.error is None


def test_flag_off_records_nothing(monkeypatch):
    monkeypatch.setenv("NOKKER_PG_DSN", "postgresql://example.invalid/x")
    assert build_recorder(NokkerConfig()) is None  # traces_record: false par défaut
    assert NokkerConfig().traces_record is False
    k, _ = make_kernel(list(THREE_CALLS), None)
    with tenant_scope(INTERNAL_TENANT):
        k.run_turn(TurnInput("fil", "mon agenda ?"))
    assert k.traces is None
    on = build_recorder(NokkerConfig(traces_record=True, traces_target="beta"))
    assert on is not None and on.target == "beta" and on.retention_days == 30
    monkeypatch.delenv("NOKKER_PG_DSN")
    assert build_recorder(NokkerConfig(traces_record=True)) is None  # pas de base : rien


def test_migration_045_is_the_trace_table():
    names = [p.name for p in migration_files() if p.parent.parent.name == "store"]
    assert "045_trace_calls.sql" in names
    store = [n for n in names if n[:3].isdigit()]
    assert len({n[:3] for n in store}) == len(store)  # aucun numéro en double


# ───────────────────────────── training_ok et consentement ─────────────────────────────


def _three_accounts():
    sink, store_turns = MemoryTraceSink(), []
    for tenant, thread, sender in ((INTERNAL_TENANT, "fil-int", None),
                                   (TESTER, f"{TESTER}:fil", "us_aaaaaaaaaaaa"),
                                   (OTHER_TESTER, f"{OTHER_TESTER}:fil", "us_bbbbbbbbbbbb")):
        store = MemoryStore()
        run([Message("assistant", "bonjour")], text="salut", tenant=tenant, thread=thread, sender=sender,
            rec=recorder(sink), store=store)
        store_turns += turns_of(store, tenant)
    consents = {(TESTER, "us_aaaaaaaaaaaa"): True, (OTHER_TESTER, "us_bbbbbbbbbbbb"): False}
    return sink, store_turns, consents


def test_training_ok_internal_by_default_tester_only_with_consent(tmp_path):
    sink, turns, consents = _three_accounts()
    src = source_from(sink.rows, turns, consents)
    res = export(src, tmp_path, opts())
    lines = read_lines(res.path)
    assert {(ln["meta"]["tenant_type"], ln["meta"]["training_ok"]) for ln in lines} == {("internal", True),
                                                                                       ("tester", True)}
    assert len(lines) == 2 and res.manifest["counts"]["excluded_no_training_consent"] == 1
    # --include-eval : la ligne sans consentement part, marquée training_ok=false.
    res = export(src, tmp_path, opts(include_eval=True))
    lines = read_lines(res.path)
    assert sorted(ln["meta"]["training_ok"] for ln in lines) == [False, True, True]
    assert res.path.name.endswith("-2")
    # Fil d'un testeur pseudonymisé, jamais son identifiant.
    assert all(TESTER not in ln["meta"]["thread"] for ln in lines)


def test_consent_withdrawn_applies_to_past_calls(tmp_path):
    sink, turns, consents = _three_accounts()
    consents[(TESTER, "us_aaaaaaaaaaaa")] = False
    lines = read_lines(export(source_from(sink.rows, turns, consents), tmp_path, opts()).path)
    assert [ln["meta"]["tenant_type"] for ln in lines] == ["internal"]


def test_tester_without_sender_is_never_training_ok(tmp_path):
    sink = MemoryTraceSink()
    run([Message("assistant", "ok")], tenant=TESTER, thread=f"{TESTER}:x", sender=None, rec=recorder(sink))
    lines = read_lines(export(source_from(sink.rows), tmp_path, opts(include_eval=True)).path)
    assert lines[0]["meta"]["training_ok"] is False


# ───────────────────────────── secrets ─────────────────────────────


def test_no_known_secret_value_ever_reaches_the_export(tmp_path):
    script = [Message("assistant", f"la clé est {SECRET}",
                      tool_calls=[tc("vault_get", "v1", item="acme/router"),
                                  tc("calendar_list", "c1", note=f"token {SECRET}")]),
              Message("assistant", f"voici : {SECRET} et Xk9qpzT42LmWv8")]
    sink, store = MemoryTraceSink(), MemoryStore()
    turn, _, _ = run(script, text=f"mon mot de passe {SECRET} ?", rec=recorder(sink), store=store)
    raw = json.dumps(sink.rows, ensure_ascii=False)
    assert SECRET not in raw and "Xk9qpzT42LmWv8" not in raw  # scellé dès l'enregistrement
    assert sink.rows[0]["meta"]["secret_seals"]["count"] >= 1
    # Un tour d'avant l'enregistreur, avec une valeur que seul le coffre de l'export connaît.
    old = turn_to_dict(turn)
    old["index"], old["thread_id"] = 0, "ancien"
    old["messages"][-1]["content"] = f"ancien {EXPORT_ONLY_SECRET}"
    known = KnownSecrets()
    known.add("ROUTER", SECRET)
    known.add("OTHER", EXPORT_ONLY_SECRET)
    res = export(source_from(sink.rows, [(INTERNAL_TENANT, datetime.now(timezone.utc), old)]), tmp_path,
                 opts(include_eval=True), known=known)
    blob = b"".join(p.read_bytes() for p in res.path.iterdir())
    for value in (SECRET, EXPORT_ONLY_SECRET, "Xk9qpzT42LmWv8"):
        assert value.encode() not in blob
    assert res.manifest["counts"]["reconstructed"] >= 1 and res.manifest["counts"]["recorded"] == 2


def test_secret_guard_drops_a_line_that_would_leak(tmp_path):
    row = {"tenant_id": INTERNAL_TENANT, "thread_id": "f", "turn_index": 0, "call_index": 0, "account_id": None,
           "kernel_sha": "", "target": "canary", "recorded_at": datetime.now(timezone.utc).isoformat(),
           "prompt": [{"role": "user", "content": "x"}], "tools": [{"name": "outil", "description": SECRET + "-suffixe"}],
           "completion": [{"role": "assistant", "content": "ok"}], "meta": {}}
    known = KnownSecrets()
    known.add("ROUTER", SECRET)
    res = export(source_from([row]), tmp_path, opts(), known=known)
    assert res.manifest["counts"].get("dropped_secret_guard") == 1
    assert SECRET.encode() not in (res.path / "calls.jsonl").read_bytes()


# ───────────────────────────── PII des testeurs ─────────────────────────────


def test_tester_pii_is_redacted_in_eval_mode(tmp_path):
    text = ("Bonjour, je m'appelle Jeanne Exemplaire, écris à jeanne.exemplaire@example.org ou au +41 79 123 45 67. "
            "J'habite rue du Lac 12, 1200 Genève. IBAN CH93 0076 2011 6238 5295 7.")
    sink = MemoryTraceSink()
    run([Message("assistant", "Merci Jeanne, c'est noté pour Mme Exemplaire.\n\nCordialement,\nPaul Témoin")],
        text=text, tenant=TESTER, thread=f"{TESTER}:jeanne.exemplaire@example.org", sender="us_aaaaaaaaaaaa",
        rec=recorder(sink))
    src = source_from(sink.rows, emails={"us_aaaaaaaaaaaa": "jeanne.exemplaire@example.org"})
    res = export(src, tmp_path, opts(include_eval=True))
    blob = (res.path / "calls.jsonl").read_text(encoding="utf-8")
    for leaked in ("Exemplaire", "exemplaire", "Jeanne", "example.org", "79 123 45 67", "rue du Lac", "Genève",
                   "CH93", "Paul Témoin"):
        assert leaked not in blob, leaked
    assert "[email]" in blob and "[téléphone]" in blob and "[adresse]" in blob and "[nom]" in blob
    assert res.manifest["pii"]["replacements"]["email"] >= 1 and res.manifest["pii"]["not_covered"]


def test_internal_lines_are_not_pii_scrubbed_unless_asked(tmp_path):
    sink = MemoryTraceSink()
    run([Message("assistant", "ok")], text="écris à client@example.org", rec=recorder(sink))
    assert "client@example.org" in (export(source_from(sink.rows), tmp_path, opts()).path / "calls.jsonl").read_text()
    assert "client@example.org" not in (export(source_from(sink.rows), tmp_path, opts(pii_internal=True)).path
                                        / "calls.jsonl").read_text()


def test_pii_pass_keeps_ordinary_text():
    plain = "Le palier ship a répondu en 2.5 s via Slack ; 1200 Mo libres, version 2026.11, appel 3 sur 8."
    assert pii.scrub_pii(plain) == plain
    assert pii.scrub_pii("M. Durand-Marchal viendra") == "M. [nom] viendra"
    assert pii.scrub_pii("Bahnhofstrasse 12, 8001 Zürich") == "[adresse], [adresse]"
    assert pii.name_terms("prenom.nom+tag@example.org") == ["prenom", "nom"]
    assert pii.name_terms("info@example.org") == []


# ───────────────────────────── lot, manifeste, empreintes ─────────────────────────────


def test_lot_layout_manifest_and_sha256sums(tmp_path):
    _, _, rec = run(list(THREE_CALLS))
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    res = export(source_from(rec.sink.rows), tmp_path, opts(max_lines=2), exporter_sha="abc1234", now=now)
    assert res.path.name == "traces-20261101-1"
    names = sorted(p.name for p in res.path.iterdir())
    assert names == ["SHA256SUMS", "calls-00001.jsonl", "calls-00002.jsonl", "manifest.json"]
    sums = dict(reversed(line.split("  ")) for line in (res.path / "SHA256SUMS").read_text().splitlines())
    for name, digest in sums.items():
        assert hashlib.sha256((res.path / name).read_bytes()).hexdigest() == digest
    assert set(sums) == {"calls-00001.jsonl", "calls-00002.jsonl", "manifest.json"}
    assert verify_lot(res.path) == []
    man = json.loads((res.path / "manifest.json").read_text())
    assert man["kernel_sha"] == "abc1234" and man["counts"]["lines"] == 3
    assert man["filters"]["include_eval"] is False and man["date_range"]["first_call_at"]
    assert [f["lines"] for f in man["files"]] == [2, 1]
    (res.path / "calls-00002.jsonl").write_text("{}\n")
    assert verify_lot(res.path) == ["calls-00002.jsonl"]
    assert export(source_from(rec.sink.rows), tmp_path, opts(), now=now).path.name == "traces-20261101-2"


# ───────────────────────────── reconstruction, signal, approbations ─────────────────────────────


def test_history_is_reconstructed_once_and_marked(tmp_path):
    store = MemoryStore()
    _, _, rec = run(list(THREE_CALLS), store=store)
    turns = turns_of(store, INTERNAL_TENANT)
    # Enregistré : pas de doublon reconstruit.
    res = export(source_from(rec.sink.rows, turns), tmp_path, opts())
    assert res.manifest["counts"]["lines"] == 3 and "reconstructed" not in res.manifest["counts"]
    # Sans enregistrement (drapeau éteint à l'époque) : reconstruit, marqué, prompt partiel, outils inconnus.
    lines = read_lines(export(source_from([], turns), tmp_path, opts()).path)
    assert len(lines) == 3 and all(ln["meta"]["reconstructed"] for ln in lines)
    assert lines[0]["tools"] is None and lines[0]["prompt"][0] == {"role": "user", "content": "mon agenda ?"}
    assert lines[2]["completion"][0]["content"] == "2 rdv lundi, 2 mardi"


def test_user_signal_from_next_turn(tmp_path):
    store, sink = MemoryStore(), MemoryTraceSink()
    run([Message("assistant", "Il fait 12 degrés.")], text="météo à Lausanne ?", rec=recorder(sink), store=store)
    run([Message("assistant", "Pardon, 14.")], text="non, c'est faux", rec=recorder(sink), store=store)
    lines = read_lines(export(source_from(sink.rows, turns_of(store, INTERNAL_TENANT)), tmp_path, opts()).path)
    assert [ln["meta"]["user_signal"] for ln in lines] == [{"kind": "no", "next_turn": True}, None]
    assert "c'est faux" not in json.dumps(lines[0])  # le texte du tour suivant ne part pas avec l'appel
    assert user_signal("météo à Lausanne ?", "en fait je voulais Genève")["kind"] == "correction"
    assert user_signal("météo à Lausanne ?", "météo à Lausanne ??")["kind"] == "relance"
    assert user_signal("x", "alors ?")["kind"] == "relance"
    assert user_signal("x", "merci, et demain ?")["kind"] == "none"


def test_approvals_created_then_decided_by_the_human(tmp_path):
    sink = MemoryTraceSink()
    turn, _, _ = run([Message("assistant", tool_calls=[tc("infra_wipe", "w1", target="/data")]),
                      Message("assistant", "j'attends ta validation")], text="vide /data", rec=recorder(sink))
    assert [p.name for p in turn.pending_approvals] == ["infra_wipe"]
    row0 = sink.rows[0]
    assert row0["meta"]["approvals"] == [{"call_id": "w1", "tool": "infra_wipe", "state": "created"}]
    assert any(v["decision"] == "hitl" for v in row0["meta"]["gates_and_invariants"])
    src = source_from(sink.rows, approvals={(INTERNAL_TENANT, "fil", 0): [
        {"call_id": "w1", "tool_name": "infra_wipe", "status": "rejected"}]})
    lines = read_lines(export(src, tmp_path, opts()).path)
    assert lines[0]["meta"]["approvals"][0]["state"] == "refused"


def test_retention_purges_old_rows_of_the_current_tenant_only():
    sink = MemoryTraceSink()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    sink.rows += [{"tenant_id": INTERNAL_TENANT, "recorded_at": old}, {"tenant_id": TESTER, "recorded_at": old},
                  {"tenant_id": INTERNAL_TENANT, "recorded_at": datetime.now(timezone.utc).isoformat()}]
    with tenant_scope(INTERNAL_TENANT):
        assert sink.purge(30) == 1
    assert [r["tenant_id"] for r in sink.rows] == [TESTER, INTERNAL_TENANT]
    with pytest.raises(ValueError):
        TraceRecorder(sink=sink, retention_days=0)
    with pytest.raises(ValueError):
        TraceRecorder(sink=sink, target="prod")


# ── Marque de banc (docs/SELF-IMPROVE.md §5) : aucune ligne d'un banc n'atteint un export ──

def test_bench_mark_on_lines_and_never_exported(tmp_path, monkeypatch):
    plain = recorder()
    run(THREE_CALLS, rec=plain)
    assert all("bench" not in r["meta"] for r in plain.sink.rows)  # sans NOKKER_BENCH : lignes inchangées
    monkeypatch.setenv("NOKKER_BENCH", "conv")
    marked = recorder()
    run(THREE_CALLS, thread="fil-banc", rec=marked)
    assert {r["meta"]["bench"] for r in marked.sink.rows} == {"conv"}
    src = source_from([*plain.sink.rows, *marked.sink.rows])
    for include_eval in (False, True):
        res = export(src, tmp_path, opts(include_eval=include_eval))
        lines = read_lines(res.path)
        assert lines and {ln["meta"]["thread"] for ln in lines} == {"fil"}
        assert res.manifest["counts"]["excluded_bench"] == len(marked.sink.rows)


def test_bench_thread_prefix_never_exported(tmp_path):
    rec = recorder()
    run(THREE_CALLS, thread="bench-x9", rec=rec)
    run(THREE_CALLS, thread="improve-nuit", rec=rec)
    res = export(source_from(rec.sink.rows), tmp_path, opts(include_eval=True))
    assert read_lines(res.path) == [] and res.manifest["counts"]["excluded_bench"] == len(rec.sink.rows)


# ───────────────────── réponses écrites par le noyau (relecture du format, 02.11) ─────────────────────

def test_export_marks_kernel_replies_grader_and_goal_cmd(tmp_path):
    """Réponses du noyau (« [Correcteur] … » d'un `goal_check`, réponse d'une commande `/goal`) : une ligne à part
    `meta.synthetic = grader | goal_cmd`, et leurs positions dans le prompt des appels suivants
    (`meta.synthetic_in_prompt`) ; une ligne d'appel modèle porte `synthetic: null`."""
    from nokker.goals.service import GoalService
    from nokker.goals.store import MemoryGoals
    from nokker.qa.outcomes import OutcomeGrader

    class G:
        def post(self, body):
            return {"choices": [{"message": {"content": "C1: 1 | pas encore"}}], "model": "g"}

    store = MemoryStore()
    rec = recorder()
    k, _prov = make_kernel([Message("assistant", "je m'y mets"), Message("assistant", "suite")], rec, store)
    rec.seal = k.invariants.redact_context
    svc = GoalService(store=MemoryGoals(), grader=OutcomeGrader(transport=G()), outcomes_enabled=True,
                      goals_enabled=True, submit=lambda fn, *a: None)
    svc.kernel, k.goals = k, svc
    with tenant_scope(INTERNAL_TENANT):
        cmd = k.run_turn(TurnInput("fil", "/goal corrige le parseur\nles tests passent", sender="alice"))
        svc.current("fil").driver = "client"
        k.run_turn(TurnInput("fil", "vas-y", sender="alice"))
        chk = k.run_turn(TurnInput("fil", "[Vérification du but]", control="goal_check", sender="alice"))
        k.run_turn(TurnInput("fil", "continue", sender="alice"))
    assert chk.reply.startswith("[Correcteur]") and not chk.calls and not cmd.calls
    lot = export(source_from(rec.sink.rows, turns_of(store, INTERNAL_TENANT)), tmp_path, opts(),
                 exporter_sha="abc1234").path
    lines = read_lines(lot)
    synth = {ln["meta"]["synthetic"]: ln for ln in lines if ln["meta"]["synthetic"]}
    assert set(synth) == {"grader", "goal_cmd"}
    assert synth["grader"]["completion"] == [{"role": "assistant", "content": chk.reply}]
    assert synth["goal_cmd"]["completion"][0]["content"] == cmd.reply
    assert synth["grader"]["meta"]["reconstructed"] is True and synth["grader"]["meta"]["model"] is None
    model_lines = [ln for ln in lines if ln["meta"]["synthetic"] is None]
    assert len(model_lines) == 2 and all(ln["meta"]["model"] == "m-ship" for ln in model_lines)
    last = max(model_lines, key=lambda ln: ln["meta"]["turn"])
    marked = [last["prompt"][i]["content"] for i in last["meta"]["synthetic_in_prompt"]]
    assert marked == [cmd.reply, chk.reply]  # repérées par le verdict de leur tour, pas par leur texte
    first = min(model_lines, key=lambda ln: ln["meta"]["turn"])
    assert [first["prompt"][i]["content"] for i in first["meta"]["synthetic_in_prompt"]] == [cmd.reply]


# ───────────────────────────── meta.flags : drapeaux allumés (condition de l'équipe format) ─────────────────────────────


def test_enabled_flags_is_the_sorted_list_of_lit_flags():
    from nokker.traces.flags import enabled_flags

    assert enabled_flags(NokkerConfig()) == []  # tout éteint par défaut : rien
    cfg = NokkerConfig(traces_record=True, show_me=True, learning=True, payments=True, family_calendar=True,
                       ssrf={"strict": True}, provenance={"enabled": True}, auto_review={"enabled": False},
                       ambient={"glance": {"enabled": True}, "eink": {"enabled": False}},
                       code={"enabled": True, "forge_triggers": False, "egress_secrets": {"enabled": False}},
                       speak_lexicon={"enabled": True, "eighty": "huitante"})
    got = enabled_flags(cfg)
    assert got == sorted(got)
    assert got == ["ambient.glance", "code", "family_calendar", "learning", "payments", "provenance", "show_me",
                   "speak_lexicon", "ssrf.strict", "traces_record"]


def test_every_recorded_line_and_every_exported_line_carries_meta_flags(tmp_path):
    flags = ("family_calendar", "show_me", "traces_record")
    rec = TraceRecorder(sink=MemoryTraceSink(), seal=None, kernel_sha="abc1234", target="canary", flags=flags)
    run(list(THREE_CALLS), rec=rec)
    assert [r["meta"]["flags"] for r in rec.sink.rows] == [list(flags)] * 3
    lines = read_lines(export(source_from(rec.sink.rows), tmp_path, opts(), exporter_sha="abc1234").path)
    assert [ln["meta"]["flags"] for ln in lines] == [list(flags)] * 3
    # Lignes d'avant le champ : `null` à l'export, rien d'autre ne change.
    old = [{**r, "meta": {k: v for k, v in r["meta"].items() if k != "flags"}} for r in rec.sink.rows]
    lines = read_lines(export(source_from(old), tmp_path / "old", opts(), exporter_sha="abc1234").path)
    assert [ln["meta"]["flags"] for ln in lines] == [None] * 3


def test_build_recorder_records_the_flags_of_the_config(monkeypatch):
    monkeypatch.setenv("NOKKER_PG_DSN", "postgresql://example.invalid/x")
    on = build_recorder(NokkerConfig(traces_record=True, traces_target="beta", show_me=True))
    assert on is not None and on.flags == ("show_me", "traces_record")


def test_flags_do_not_change_what_the_model_sees():
    off, on = recorder(), TraceRecorder(sink=MemoryTraceSink(), seal=None, kernel_sha="abc1234", target="canary",
                                        flags=("show_me",))
    _, p0, _ = run(list(THREE_CALLS), rec=off)
    _, p1, _ = run(list(THREE_CALLS), rec=on)
    assert p0.seen == p1.seen
    strip = lambda rows: [{**r, "meta": {k: v for k, v in r["meta"].items() if k not in ("flags", "latency_s")},
                           "recorded_at": ""} for r in rows]
    assert strip(off.sink.rows) == strip(on.sink.rows)
