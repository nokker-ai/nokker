"""nokker/tools/effects.py — table d'effets par défaut (PLAN-P1 §2.3 étape 5) + enveloppe secrète.

Un outil SANS effet déclaré est traité comme `Effect.WRITE` (défaut prudent — déjà la valeur
par défaut de `ToolSpec.effect` dans `nokker/core/ports.py`).

Ce module fournit :
  1. `DEFAULT_EFFECTS` : table par défaut pour les outils connus du banc (`openclaw-eval/tasks.py`)
     et du canari (calendar_*, mail_*, send_*, infra_*, kanban_*, memory_*, browser_*…) ;
  2. `EffectsTable` : la table ci-dessus + surcharge par config (name → Effect) ;
  3. `redact_tool_result` / `SecretScrubbingToolSource` : la transformation « SECRET → poignée »
     promise par `qa/invariants.py` (effet SECRET = ALLOW, mais le résultat ne doit JAMAIS
     entrer en clair dans le contexte/le store).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSource, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.qa.secrets import KnownSecrets, redact

# ─────────────────────────────── table par défaut ───────────────────────────────
#
# Dérivée du banc `infra/sokkan/openclaw-eval/tasks.py` (ids des outils stubés) et du canari
# (`infra/nokker-workspace-mcp/`). Un nom absent de cette table → WRITE (défaut prudent).

DEFAULT_EFFECTS: dict[str, Effect] = {
    # calendar_* (nina-calendar / workspace MCP)
    "calendar_list": Effect.READ,
    "calendar_events": Effect.READ,
    "calendar_create": Effect.WRITE,
    "calendar_update": Effect.WRITE,
    "calendar_delete": Effect.DESTRUCTIVE,
    # mail_*
    "mail_search": Effect.READ,
    "mail_read": Effect.READ,
    "mail_draft": Effect.WRITE,
    # docs/TOOLS-MAIL.md (18.10.2026) : classer / déplacer = réversible (WRITE, sans GO) ; corbeille = DESTRUCTIVE
    # (carte d'approbation forte), même si Gmail la garde 30 jours.
    "mail_move": Effect.WRITE,
    "mail_trash": Effect.DESTRUCTIVE,
    "mail_send": Effect.SEND_EXTERNAL,
    # send_* (gateway / adaptateurs de canal)
    "send_email_reply": Effect.SEND_EXTERNAL,
    "send_email_forward": Effect.SEND_EXTERNAL,  # transfert interne : l'allowlist doit inclure le destinataire
    "send_bulk_email": Effect.SEND_BULK,
    "send_message": Effect.SEND_EXTERNAL,
    "send_telegram_message": Effect.SEND_EXTERNAL,
    # infra_* (actions à effet sur l'hôte — invariant 4 du préfixe)
    "infra_disk_usage": Effect.READ,
    "infra_gpu_info": Effect.READ,  # inventaire matériel réel (clinfo -l, nvidia-smi -L) : lecture, avant tout plan GPU
    "infra_lvextend": Effect.DESTRUCTIVE,
    "infra_lvreduce": Effect.DESTRUCTIVE,
    "infra_restart": Effect.DESTRUCTIVE,
    "infra_deploy": Effect.DESTRUCTIVE,
    "infra_dns_update": Effect.DESTRUCTIVE,
    "infra_secret_rotate": Effect.DESTRUCTIVE,
    "infra_gpu_load_test": Effect.DESTRUCTIVE,  # charge GPU sans GO = interdit (invariant 4)
    # kanban_*
    "kanban_create_task": Effect.WRITE,
    "kanban_update_task": Effect.WRITE,
    "kanban_delete_task": Effect.DESTRUCTIVE,
    # memory_* (sokkan-memory / CortHeXis)
    "memory_search": Effect.READ,
    "memory_get": Effect.READ,
    "memory_write": Effect.WRITE,
    # browser_* (brosim)
    "browser_navigate": Effect.READ,
    # Lecture seule (21.10.2026) : sans entrée ici, le défaut prudent WRITE les privait du dédoublonnage des lectures.
    "browser_snapshot": Effect.READ,
    "browser_read_text": Effect.READ,
    "browser_find": Effect.READ,
    "browser_take_screenshot": Effect.READ,
    "browser_console_messages": Effect.READ,
    "browser_network_requests": Effect.READ,
    "browser_status": Effect.READ,
    "browser_click": Effect.WRITE,
    "browser_fill": Effect.WRITE,
    "browser_submit": Effect.SEND_EXTERNAL,
    # Carnet d'incidents (24.10, recos suivies par Nick) : du code arbitraire dans un Chrome connecté (C2), un fichier
    # local envoyé à un site (C3) ; `browser_handle_dialog` : par argument, ci-dessous (F1).
    "browser_evaluate": Effect.DESTRUCTIVE,
    "browser_file_upload": Effect.SEND_EXTERNAL,
    # outils qui renvoient une valeur de secret en clair (banc `credentiels-pas-de-fuite`)
    "grafana_get_credentials": Effect.SECRET,
    "vaultwarden_get": Effect.SECRET,
    # web_* (recherche web sans clé, tools/web.py — P1, 18.10.2026)
    "web_search": Effect.READ,
    "web_fetch": Effect.READ,
    # bulles et sessions (nokker/bubbles/tools.py — P1, docs/BUBBLES.md) : périmètre borné par le hub
    # (fil courant / fils dont l'utilisateur du tour est membre)
    "bubble_update": Effect.READ,
    "session_send": Effect.READ,
    # Crew (docs/BUBBLES.md §9.4) : propose une carte, ne crée AUCUN agent par lui-même (l'activation,
    # elle, passe par la route HTTP — jamais par cet outil).
    "agent_draft": Effect.READ,
}


def _mail_move_effect(arguments: dict) -> Effect:
    """`label:TRASH` = la corbeille par l'outil de rangement (le serveur workspace accepte tout libellé existant,
    système compris) : même porte que `mail_trash` (carnet d'incidents B2, 24.10)."""
    dest = str(arguments.get("destination") or "").strip().lower()
    if dest.startswith("label:") and dest[6:].strip() in {"trash", "corbeille", "bin"}:
        return Effect.DESTRUCTIVE
    return Effect.WRITE


def _calendar_effect(arguments: dict) -> Effect:
    """Avec `attendees`, le serveur ENVOIE les invitations (sendUpdates=all) : envoi externe, allowlist comprise
    (carnet d'incidents C4, 24.10). Sans invités, une écriture dans l'agenda."""
    return Effect.SEND_EXTERNAL if arguments.get("attendees") else Effect.WRITE


def _dialog_effect(arguments: dict) -> Effect:
    """Accepter un confirm()/prompt() = valider ce que la page demande (« Confirmer le paiement de 499 CHF ? ») :
    carte forte (carnet d'incidents F1, 24.10). Refuser ou fermer : sans effet."""
    return Effect.DESTRUCTIVE if arguments.get("accept") is not False else Effect.WRITE


# Effet qui dépend des ARGUMENTS d'un outil MCP (`ToolSpec.effect_of`) ; l'effet de la table reste le repli.
ARGUMENT_EFFECTS: dict[str, Callable[[dict], Effect]] = {
    "mail_move": _mail_move_effect,
    "calendar_create": _calendar_effect,
    "calendar_update": _calendar_effect,
    "browser_handle_dialog": _dialog_effect,
}


@dataclass
class EffectsTable:
    """Table par défaut + surcharge (config). Un outil absent des deux → WRITE."""

    overrides: dict[str, Effect] = field(default_factory=dict)

    @classmethod
    def load(cls, overrides: dict[str, str | Effect] | None = None) -> "EffectsTable":
        resolved: dict[str, Effect] = {}
        for name, value in (overrides or {}).items():
            resolved[name] = value if isinstance(value, Effect) else Effect(value)
        return cls(overrides=resolved)

    def effect_for(self, name: str) -> Effect:
        if name in self.overrides:
            return self.overrides[name]
        return DEFAULT_EFFECTS.get(name, Effect.WRITE)

    def annotate(self, spec: ToolSpec) -> ToolSpec:
        """Renvoie `spec` avec son effet résolu par cette table (surcharge > défaut > WRITE).
        À utiliser par le registre réel (F2 `tools/registry.py`) quand une source d'outils (ex.
        un serveur MCP) ne déclare pas d'effet lui-même."""
        import dataclasses

        effect = self.effect_for(spec.name)
        classify = ARGUMENT_EFFECTS.get(spec.name) if spec.name not in self.overrides else None
        if classify is None:
            return dataclasses.replace(spec, effect=effect)
        return dataclasses.replace(spec, effect=effect, effect_of=classify)


# ─────────────────────────────── transformation SECRET → poignée ───────────────────────────────


def redact_tool_result(result: ToolResult, known: KnownSecrets | None = None) -> ToolResult:
    """Caviarde le contenu d'un `ToolResult` (valeurs connues + motifs, cf. `qa/secrets.py`).

    **Depuis `main` e076d54, le chemin de garantie principal est
    `qa/invariants.py::DeterministicInvariants.redact_result`** — appelé par
    `core/loop.py::_tool_step` sur CHAQUE résultat d'outil, avant le contexte et le store. Cette
    fonction reste une seconde ligne de défense indépendante (ex. un `ToolSource` qui voudrait
    se protéger lui-même avant même d'atteindre le noyau, ou un test qui exerce un `ToolSource`
    isolément) — redondante avec le noyau en usage normal, pas un point de passage obligatoire.
    """
    red = redact(result.content, known)
    merged_handles = list(dict.fromkeys([*result.secret_handles, *red.names]))
    return ToolResult(call_id=result.call_id, name=result.name, content=red.text, is_error=result.is_error,
                      reservoir_id=result.reservoir_id, secret_handles=merged_handles)


@dataclass
class SecretScrubbingToolSource:
    """Enveloppe un `ToolSource` : tout résultat d'un outil d'effet `SECRET` est transformé en
    poignée avant d'être renvoyé par `.call()`.

    **Défense en profondeur, pas le mécanisme principal** : depuis `main` e076d54, le noyau
    (`core/loop.py::_tool_step`) appelle déjà `Invariants.redact_result` sur CHAQUE résultat
    d'outil (effet `SECRET` ou non), avant le contexte et le store — la garantie ne dépend donc
    plus de cette enveloppe. Elle reste utile pour un `ToolSource` utilisé HORS du `Kernel` (ex.
    un appel direct dans un test, un outil exposé par un autre chemin), ou comme second verrou
    si un `ToolSpec` est mal étiqueté ET que `redact_result` ne connaît pas la valeur (coffre
    vide pour ce secret précis) — dans ce cas précis, aucune des deux couches n'attrape la fuite,
    cf. docs/INVARIANTS.md §2.
    """

    inner: ToolSource
    known: KnownSecrets = field(default_factory=KnownSecrets)
    effects: EffectsTable = field(default_factory=EffectsTable)

    def specs(self) -> list[ToolSpec]:
        return [self.effects.annotate(s) for s in self.inner.specs()]

    def turn_done(self, thread_id: str) -> None:
        hook = getattr(self.inner, "turn_done", None)
        if callable(hook):
            hook(thread_id)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        result = self.inner.call(name, arguments, ctx)
        if self.effects.effect_for(name) is Effect.SECRET:
            return redact_tool_result(result, self.known)
        return result


__all__ = ["ARGUMENT_EFFECTS", "DEFAULT_EFFECTS", "EffectsTable", "SecretScrubbingToolSource", "redact_tool_result"]
