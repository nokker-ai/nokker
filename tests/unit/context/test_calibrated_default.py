"""(d) `calibrate_tokens` allumé par défaut : la fenêtre suit le ratio réel/estimé observé (banc : estimé 16 201, prompt_tokens 18 977)."""
from __future__ import annotations

from nokker.context.tokens import CALIBRATOR
from nokker.context.types import ContextBudgets

from .harness import make_session

# ── (d) ──
def test_calibrate_tokens_is_on_by_default_and_scales_the_window(tmp_path):
    assert ContextBudgets().calibrate_tokens is True
    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    s.say("bonjour " * 50)
    raw = s.manager.window_tokens(s.thread)
    CALIBRATOR.observe("27b", 16201, 18977)  # banc 22.11 : estimé 16 201, prompt_tokens 18 977 (≈ ×1,17)
    scaled = s.manager.window_tokens(s.thread)
    assert 1.15 <= scaled / raw <= 1.19
    (tmp_path / "o").mkdir()
    off = make_session(ContextBudgets(calibrate_tokens=False), tmp_path=tmp_path / "o")
    off.say("bonjour " * 50)
    assert off.manager.window_tokens(off.thread) == raw
