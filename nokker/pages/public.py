"""Publier une page (docs/LOKKAL.md §7.26, docs/BUBBLES.md §10.4) : une sortie vers le PUBLIC, donc une carte forte.

- Le choix est celui de l'utilisateur, page par page : l'action « publier » de la bulle appelle
  `POST /v1/pages/{id}/publish {index?}` ; jamais un outil du modèle (aucun `ToolSpec` ne porte ce geste).
- La carte (`page_publish`, règle `pages:publish`, force `strong`) porte la page, son chemin, la version et son
  empreinte : ce que l'utilisateur signe est EXACTEMENT ce qui devient public. Une version plus récente ne l'est pas
  tant qu'on ne republie pas.
- Elle ne s'approuve qu'à la clé d'appareil (passkey sur app.nokker.ai, relayée par la boîte aux lettres X10, ou
  preuve signée locale), jamais au jeton local seul, ni par la passerelle avec un jeton d'accès : la route locale et
  `decide_local` refusent `approve` sur cette règle ; refuser reste possible partout.
- Une page épinglée `local` est refusée à la demande ET à l'exécution.
- C'est un geste du noyau (`ApprovalExecutor.handlers`), pas un appel d'outil : pas de tour de suite.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from nokker.core.ports import Approval
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.pages.store import PageError, PageMeta, PageStore

logger = logging.getLogger("nokker.pages.public")

PUBLISH_TOOL = "page_publish"
PUBLISH_RULE = "pages:publish"
PUBLISH_CHANNEL = "pages"


def is_publish_card(rule: str | None) -> bool:
    return rule == PUBLISH_RULE


def public_url(relay_host: str | None, meta: PageMeta) -> str | None:
    """`https://<nom>--pub.nokker.app/<public_id>` : une autre origine que l'interface (`<nom>.nokker.app`) et que
    les pages privées (`<nom>--pages.nokker.app`), un autre id que le `page_id`."""
    if not relay_host or not meta.public or not meta.public_id:
        return None
    name, dot, domain = relay_host.partition(".")
    return f"https://{name}--pub.{domain}/{meta.public_id}" if dot else None


def request_publish(approvals: Any, store: PageStore, meta: PageMeta, *, index: bool, by: str | None) -> Approval:
    """Pose la carte forte de publication de la DERNIÈRE version de la page. PageError si la page est `local`."""
    if meta.locality == "local":
        raise PageError("page épinglée `local` : elle ne quitte pas la machine")
    found = store.read(meta.page_id)
    if found is None:
        raise PageError("page introuvable")
    meta, content = found
    import hashlib

    args = {"page_id": meta.page_id, "path": meta.path, "title": meta.title, "version": meta.version,
            "sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "index": bool(index)}
    call = ToolCall(id=f"publish-{meta.page_id}-v{meta.version}", name=PUBLISH_TOOL, arguments=args)
    seen = "indexable par les moteurs de recherche" if index else "non indexée (noindex)"
    verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=PUBLISH_RULE,
                      reason=(f"rendre PUBLIQUE la page « {meta.title} » ({meta.path}, version {meta.version}, "
                              f"{len(content)} octets) : lisible par n'importe qui qui a le lien, {seen} ; "
                              "dépublier la retire tout de suite"),
                      subject=call.id, strength="strong")
    turn = Turn(thread_id=meta.thread_id, index=0, input=InputCheck(text=""), channel=PUBLISH_CHANNEL, sender=by)
    return approvals.request(turn, call, verdict, None)


def publish_handler(store: PageStore, after: Callable[[PageMeta], None] | None = None
                    ) -> Callable[[Approval], str]:
    """Décision d'une carte `page_publish` : approuvée → la version de la carte devient publique (si elle a toujours
    son empreinte et que la page n'est pas `local`) ; refusée → rien. `after(meta)` : bulle et miroir prévenus."""

    def handle(approval: Approval) -> str:
        if approval.status != "approved":
            return ""
        a = approval.arguments or {}
        try:
            meta = store.publish(str(a.get("page_id") or ""), int(a.get("version") or 0), str(a.get("sha256") or ""),
                                 bool(a.get("index")))
        except (PageError, ValueError, TypeError) as e:
            raise PageError(f"publication refusée : {e}") from None
        if after is not None:
            try:
                after(meta)
            except Exception:  # noqa: BLE001 — la page est publique ; une bulle non rafraîchie n'y change rien
                logger.warning("suite de la publication de %s en échec", meta.page_id, exc_info=True)
        return f"page {meta.page_id} publique (version {meta.public_version})"

    return handle


__all__ = ["PUBLISH_CHANNEL", "PUBLISH_RULE", "PUBLISH_TOOL", "is_publish_card", "public_url", "publish_handler",
           "request_publish"]
