"""Drapeaux de config allumés, posés sur chaque ligne de trace (`meta.flags`, condition de l'équipe format) : la liste
TRIÉE des clés de `NokkerConfig` (ou `bloc.sous-clé`) qui sont vraies pour le noyau qui a produit l'appel. Calculée une
fois au démarrage depuis la config ; n'entre dans aucun texte vu par le modèle."""
from __future__ import annotations

from typing import Any

# Drapeaux booléens de premier niveau (nom = clé de `NokkerConfig`).
BOOL_FLAGS = (
    "steer_enabled", "text_attachments_enabled", "judge_enabled", "judge_review", "judge_advice", "outcomes_enabled",
    "goals_enabled", "delegate_fork", "context_enabled", "execute_code_enabled", "memory_search_api", "mcp_modern",
    "mcp_apps", "team", "household", "household_minors", "household_paper", "family_calendar", "payments",
    "house_room", "show_me", "who_does_what", "sso", "packs_web", "learning", "learning_nudges", "memory_pending",
    "tenant_long_memory", "self_improve", "memory_consolidation", "plan", "operate", "traces_record", "billing_enabled",
)
# Blocs de config dont la clé `enabled` allume le drapeau.
ENABLED_BLOCKS = ("provenance", "tts", "speak_lexicon", "knock", "auto_review", "approval_suggestions", "advise",
                  "home_assistant", "home_watch", "slack", "whatsapp")
# Sous-blocs `bloc.sous-clé.enabled`.
NESTED = (("ambient", "room_audio"), ("ambient", "eink"), ("ambient", "glance"), ("code", "egress_secrets"))


def _on(v: Any) -> bool:
    return bool(v.get("enabled")) if isinstance(v, dict) else bool(v)


def enabled_flags(config: Any) -> list[str]:
    """Liste triée des drapeaux allumés de `config`. Éteint ou absent : rien."""
    out = {n for n in BOOL_FLAGS if getattr(config, n, False) is True}
    out |= {n for n in ENABLED_BLOCKS if _on(getattr(config, n, None) or {})}
    for block, key in NESTED:
        raw = getattr(config, block, None)
        if isinstance(raw, dict) and _on(raw.get(key)):
            out.add(f"{block}.{key}")
    ssrf = getattr(config, "ssrf", None)
    if isinstance(ssrf, dict) and ssrf.get("strict"):
        out.add("ssrf.strict")
    code = getattr(config, "code", None)
    if isinstance(code, dict) and code.get("enabled"):
        out.add("code")
        if code.get("forge_triggers"):
            out.add("code.forge_triggers")
    if isinstance(getattr(config, "cloud_link", None), dict):
        out.add("cloud_link")
    if getattr(config, "local", None) is not None:
        out.add("local")
    return sorted(out)


__all__ = ["BOOL_FLAGS", "ENABLED_BLOCKS", "NESTED", "enabled_flags"]
