"""Export d'opérateur des approbations du relecteur automatique (`python -m nokker auto-review export`), pour la revue
humaine des premiers jours (docs/beta-features-rc7.md).

Stockage lu (jamais écrit) : la décision vit sur la carte, colonne `approvals.auto_review` (jsonb : decision, reason,
model = modèle RÉEL qui a répondu, at = epoch, by_rule, revoked_by/revoked_at) et dans `approval_events`
(`event_type = 'auto_review'`, même objet) ; la ligne `approvals` porte aussi outil, règle, arguments, `thread_id`,
`turn_index`, `call_id`. La TRAJECTOIRE n'est pas stockée avec la décision : elle est reconstruite des messages du tour
(`turns.turn`, JSONB) en prenant les noms d'outils des appels qui précèdent `call_id` (au plus `trajectory_calls`) ; sans
tour retrouvé, `trajectory` vaut null. L'EFFET n'est pas stocké non plus : déduit de la règle (`effect:<x>`), null sinon.

Lecture seule, sous le rôle et le DSN du noyau (`NOKKER_PG_DSN`, rôle soumis à la RLS) : un locataire après l'autre, sans
valeur joker, transaction `READ ONLY`.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

SUMMARY_CHARS = 200
TRAJECTORY_CALLS = 8

_CARDS_SQL = ("SELECT id, thread_id, turn_index, tool_name, rule, arguments, call_id, auto_review FROM approvals "
              "WHERE auto_review->>'decision' = 'approve' "
              "AND (auto_review->>'at')::double precision >= %s AND (auto_review->>'at')::double precision < %s "
              "ORDER BY (auto_review->>'at')::double precision, id")
_TURN_SQL = "SELECT turn FROM turns WHERE thread_id = %s AND turn_index = %s"


def parse_instant(text: str) -> float:
    """Date ISO 8601 -> epoch ; sans fuseau, UTC."""
    d = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def summarize(value: Any, n: int = SUMMARY_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "…"


def effect_of_rule(rule: str) -> str | None:
    parts = (rule or "").split(":")
    return parts[1] if len(parts) > 1 and parts[0] == "effect" else None


def trajectory_names(turn: dict[str, Any] | None, call_id: str | None, limit: int = TRAJECTORY_CALLS) -> list[str] | None:
    """Noms des outils des derniers appels du tour AVANT `call_id` (même sélection que `auto_review.trajectory_of`),
    sans arguments ni résultats. None : tour introuvable."""
    if not isinstance(turn, dict):
        return None
    calls = [tc.get("name", "") for m in turn.get("messages") or [] for tc in m.get("tool_calls") or []
             if tc.get("id") != call_id]
    return calls[-limit:]


def build_row(tenant: str, card: tuple, turn: dict[str, Any] | None) -> dict[str, Any]:
    aid, _thread, _idx, tool, rule, arguments, call_id, review = card
    review = dict(review or {})
    return {
        "date": datetime.fromtimestamp(float(review["at"]), timezone.utc).isoformat(timespec="seconds"),
        "locataire": tenant, "carte": aid, "outil": tool, "regle": rule, "effet": effect_of_rule(rule),
        "arguments": summarize(arguments), "raison": review.get("reason", ""), "modele": review.get("model", ""),
        "trajectoire": trajectory_names(turn, call_id),
        "revoquee_par": review.get("revoked_by"),
    }


def export_rows(dsn: str, since: float, until: float) -> Iterator[dict[str, Any]]:
    """Toutes les décisions APPROUVER du relecteur dans [since, until[, tous locataires, lecture seule."""
    from nokker.store.conn import connect_tenant, known_tenants

    for tenant in known_tenants(dsn):
        conn = connect_tenant(dsn, tenant)
        try:
            conn.execute("SET TRANSACTION READ ONLY")
            if conn.execute("SELECT to_regclass('approvals')").fetchone()[0] is None:
                continue
            for card in conn.execute(_CARDS_SQL, (since, until)).fetchall():
                row = conn.execute(_TURN_SQL, (card[1], card[2])).fetchone()
                yield build_row(tenant, card, row[0] if row else None)
        finally:
            conn.rollback()
            conn.close()


def main(dsn: str, since: str, until: str | None, out: Any) -> int:
    t1 = parse_instant(until) if until else datetime.now(timezone.utc).timestamp() + 1
    n = 0
    for row in export_rows(dsn, parse_instant(since), t1):
        out.write(json.dumps(row, ensure_ascii=False) + "\n")
        n += 1
    return n


__all__ = ["build_row", "effect_of_rule", "export_rows", "main", "parse_instant", "summarize", "trajectory_names"]
