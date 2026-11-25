"""(v) Extraction déterministe : la capture appelle le palier à température 0 avec une graine fixe ; le fournisseur dédié est
construit pour elle (le palier de conversation n'est pas modifié)."""
from __future__ import annotations

from types import SimpleNamespace

from nokker.api import server
from nokker.core.ports import Completion
from nokker.core.turn import Message, Usage


class _Provider:
    def __init__(self) -> None:
        self.sampling_body = {"top_p": 0.9}

    def complete(self, *a, **k):
        return Completion(message=Message(role="assistant", content="{}"), usage=Usage(), model="m", latency_s=0.0)


def test_capture_provider_gets_temperature_zero_and_a_fixed_seed(monkeypatch):
    prov = _Provider()
    import nokker.memory.consolidation.wiring as wiring

    monkeypatch.setattr(wiring, "provider_for", lambda config, tier: (prov, "m"))
    hooks = {}
    mgr = SimpleNamespace()
    kernel = SimpleNamespace(context=SimpleNamespace(manager=mgr), ledger=None)
    config = SimpleNamespace(memory_capture=True, memory_capture_settings={"seed": 11})
    server.configure_memory_capture(config, kernel, SimpleNamespace(account_for_tenant=None))
    assert prov.sampling_body == {"top_p": 0.9, "temperature": 0, "seed": 11}
    assert callable(mgr.on_capture) and callable(mgr.superseded) and hooks == {}
