"""Aides communes aux tests du foyer : faux modèle scripté, registre d'outils minimal, porte intérieure permissive."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nokker.core.ports import Completion
from nokker.core.turn import Decision, Gate, Usage, Verdict

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("Europe/Zurich")).timestamp()


class _Inner:
    def action(self, call, spec, turn):
        return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=f"effect:{spec.effect.value}")]

    def switch(self, reply, turn, tier):
        flags = turn.input.domain_flags
        if flags:
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=1,
                            rule=f"domaine:{'+'.join(sorted(flags))}", reason="drapeau")]
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]


class Scripted:
    def __init__(self, script):
        self.script, self.model = list(script), "scripted"

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        return Completion(message=self.script.pop(0), usage=Usage(prompt=10), model="scripted", latency_s=0.0)


class Registry:
    def __init__(self, src):
        self.src = src
        self.calls: list[str] = []

    def core(self):
        return []

    def spec(self, n):
        return next((s for s in self.src.specs() if s.name == n), None)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        self.calls.append(n)
        return self.src.call(n, a, ctx)
