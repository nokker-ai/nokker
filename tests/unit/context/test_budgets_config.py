"""`context_budgets` de la configuration → `ContextBudgets.from_config` : clés inconnues ignorées, maxima, booléens stricts."""
from __future__ import annotations

from nokker.context.types import ContextBudgets


def test_defaults_of_the_compact_cache_lot():
    b = ContextBudgets()
    assert (b.compaction_step, b.soft_ratio, b.admit_turn_budget, b.summary_key_chars) == (8000, 0.70, 100_000, 300)
    assert all(getattr(b, k) is True for k in ("memory_as_message", "bootstrap_compact", "summary_key_items",
                                               "direct_recall_hint", "revision_markers", "recall_recent_first",
                                               "never_clear_fixes_enabled"))
    assert all(getattr(b, k) is False for k in ("shed_heads", "retroactive_shed", "calibrate_tokens"))


def test_from_config_clamps_and_ignores_unknown_keys():
    b = ContextBudgets.from_config({"compaction_step": 10 ** 9, "admit_turn_budget": -5, "summary_key_chars": 5000,
                                    "nope": 1, "current_budget": 9000, "retroactive_shed": "true",
                                    "memory_as_message": "false", "shed_heads": 3})
    assert b.compaction_step == ContextBudgets.MAXIMA["compaction_step"]
    assert b.admit_turn_budget == 0 and b.summary_key_chars == 600 and b.current_budget == 9000
    assert b.retroactive_shed is True and b.memory_as_message is False and b.shed_heads is False  # illisible : défaut


def test_from_config_unreadable_numbers_fall_back_to_the_default():
    assert ContextBudgets.from_config({"compaction_step": "beaucoup"}).compaction_step == 8000
    assert ContextBudgets.from_config(None) == ContextBudgets()
