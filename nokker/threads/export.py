"""Export complet d'un fil (docs/CLIENT-API.md « Export et partage d'un fil ») : `json`, `md`, `html`.

Un export contient les messages humains, les réponses ET les appels d'outils du tour (nom, arguments, résultat), lus
dans `Turn.messages` (ce que `GET /v1/threads/{id}` n'expose pas). Trois règles, quel que soit le format :

1. **Résultat d'un outil secret = scellé.** Un outil d'effet `SECRET` (spec du registre, sinon table par défaut
   `nokker/tools/effects.py`), ou un résultat déjà scellé par le noyau (`qa/invariants.py::_seal_secret_result`), est
   rendu par un texte fixe : jamais son contenu, même caviardé, même d'un tour antérieur au scellement.
2. **Tout texte passe par `scrub`** (`Invariants.scrub_output` : coffre de valeurs connues + motifs) : message,
   réponse, arguments, résultat, titre.
3. **Taille bornée** : tours paginés (`before`/`limit`, comme `GET /v1/threads/{id}`), chaque résultat et chaque
   argument tronqués, et un plafond d'octets pour l'export entier (`truncated`, `next_before`).

`public=True` (instantané d'un lien partagé) retire ce qui identifie le compte : id du fil, propriétaire, membres,
expéditeur de chaque message.

Rendu `html` : page autonome, CSS dans la page, AUCUN script, tout texte échappé (`html.escape`, guillemets compris),
aucun lien rendu cliquable. Rendu `md` : clôtures de code plus longues que toute suite d'accents graves du contenu."""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from nokker.core.turn import Effect, Turn

FORMAT = "nokker-thread-export/1"
FORMATS = ("json", "md", "html")
DEFAULT_LIMIT = 100  # tours par page
MAX_LIMIT = 500
TOOL_RESULT_CHARS = 8000
TOOL_ARGS_CHARS = 4000
TEXT_CHARS = 40000  # un message ou une réponse
MAX_BYTES = 4 * 1024 * 1024  # export entier (JSON), au-delà : tours plus anciens laissés à la page suivante
SEALED_TEXT = "[résultat d'outil secret scellé : la valeur reste dans le coffre et n'est jamais exportée]"
# Marque du résultat déjà scellé par le noyau (`_seal_secret_result`) : scellé ici aussi, même si l'outil a disparu
# du registre ou changé d'effet depuis.
_KERNEL_SEAL_MARK = "Résultat de l'outil secret"

Scrub = Callable[[str], str]
EffectOf = Callable[[str], "Effect | None"]


class ExportError(ValueError):
    """Paramètre invalide (400)."""


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts is not None else None


def _cut(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


def default_effect_of(tools: Any) -> EffectOf:
    """Effet d'un outil : spec du registre du noyau, sinon table par défaut ; inconnu → None."""
    from nokker.tools.effects import DEFAULT_EFFECTS

    def effect_of(name: str) -> Effect | None:
        spec = None
        if tools is not None and hasattr(tools, "spec"):
            try:
                spec = tools.spec(name)
            except Exception:  # noqa: BLE001 — registre indisponible : la table par défaut reste
                spec = None
        if spec is not None and getattr(spec, "effect", None) is not None:
            return spec.effect
        return DEFAULT_EFFECTS.get(name)

    return effect_of


def is_sealed(name: str | None, content: str, effect_of: EffectOf) -> bool:
    if _KERNEL_SEAL_MARK in (content or ""):
        return True
    try:
        return bool(name) and effect_of(str(name)) is Effect.SECRET
    except Exception:  # noqa: BLE001 — dans le doute, on scelle
        return True


def _turn_messages(t: Turn, *, include_tools: bool, public: bool, scrub: Scrub,
                   effect_of: EffectOf) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    text, cut = _cut(scrub(t.input.text or ""), TEXT_CHARS)
    user: dict[str, Any] = {"index": t.index, "role": "user", "content": text, "channel": t.channel}
    if cut:
        user["truncated"] = True
    if not public:
        user["sender"] = t.sender
        if t.images:
            user["images"] = [i.to_dict() for i in t.images]
    elif t.images:
        user["images_count"] = len(t.images)
    out.append(user)
    if include_tools:
        names: dict[str, str] = {}
        for m in t.messages:
            if m.role == "assistant" and m.tool_calls:
                calls = []
                for tc in m.tool_calls:
                    names[tc.id] = tc.name
                    raw = json.dumps(tc.arguments, ensure_ascii=False, default=str) if tc.arguments \
                        else (tc.raw_arguments or "{}")
                    args, acut = _cut(scrub(raw), TOOL_ARGS_CHARS)
                    call = {"id": tc.id, "name": tc.name, "arguments": args}
                    if acut:
                        call["truncated"] = True
                    calls.append(call)
                step: dict[str, Any] = {"index": t.index, "role": "assistant", "tool_calls": calls}
                if (m.content or "").strip():
                    step["content"] = _cut(scrub(m.content), TEXT_CHARS)[0]
                out.append(step)
            elif m.role == "tool":
                name = m.name or names.get(m.tool_call_id or "", "")
                sealed = is_sealed(name, m.content or "", effect_of)
                row: dict[str, Any] = {"index": t.index, "role": "tool", "tool_call_id": m.tool_call_id,
                                       "name": name, "sealed": sealed}
                if sealed:
                    row["content"] = SEALED_TEXT
                else:
                    row["content"], rcut = _cut(scrub(m.content or ""), TOOL_RESULT_CHARS)
                    if rcut:
                        row["truncated"] = True
                out.append(row)
            # Les autres messages du tour (notes internes du noyau, brouillons écartés) ne sont pas la conversation.
    reply, rcut = _cut(scrub(t.reply or ""), TEXT_CHARS)
    final: dict[str, Any] = {"index": t.index, "role": "assistant", "content": reply}
    if rcut:
        final["truncated"] = True
    if t.error and not public:
        final["error"] = True
    out.append(final)
    return out


def build_export(thread: dict[str, Any], turns: Iterable[Turn], *, scrub: Scrub, effect_of: EffectOf,
                 include_tools: bool = True, before: int | None = None, limit: int = DEFAULT_LIMIT,
                 public: bool = False, max_bytes: int = MAX_BYTES, now: float | None = None) -> dict[str, Any]:
    """Document d'export d'un fil. `thread` = `thread_to_json` ; `turns` = historique complet du fil (ordre croissant).
    Les tours rendus sont les DERNIERS avant `before` (exclu), au plus `limit`, et au plus `max_bytes` d'export :
    `page.next_before` (index de tour) donne la page précédente, `null` quand tout l'historique est rendu."""
    if limit < 1 or limit > MAX_LIMIT:
        raise ExportError(f"limit entre 1 et {MAX_LIMIT}")
    eligible = [t for t in turns if before is None or t.index < before]
    window = eligible[-limit:]
    kept: list[list[dict[str, Any]]] = []
    size = 0
    truncated = False
    for t in reversed(window):  # du plus récent au plus ancien : le plafond d'octets garde la fin du fil
        msgs = _turn_messages(t, include_tools=include_tools, public=public, scrub=scrub, effect_of=effect_of)
        n = len(json.dumps(msgs, ensure_ascii=False).encode("utf-8"))
        if kept and size + n > max_bytes:
            truncated = True
            break
        kept.append(msgs)
        size += n
    kept.reverse()
    messages = [m for msgs in kept for m in msgs]
    first = messages[0]["index"] if messages else None
    has_older = bool(eligible) and first is not None and eligible[0].index < first
    head = {"title": scrub(str(thread.get("title") or "")), "created_at": thread.get("created_at"),
            "updated_at": thread.get("updated_at")}
    if not public:
        head = {"id": thread.get("id"), **head, "owner": thread.get("owner"), "members": thread.get("members") or []}
    return {
        "format": FORMAT,
        "exported_at": _iso(now if now is not None else datetime.now(UTC).timestamp()),
        "include_tools": include_tools,
        "thread": head,
        "turn_count": len(kept),
        "messages": messages,
        "page": {"before": before, "limit": limit, "next_before": first if has_older else None,
                 "truncated": truncated},
    }


# ─────────────────────────────── rendus ───────────────────────────────

_ROLE_LABEL = {"user": "Utilisateur", "assistant": "Assistant", "tool": "Outil"}


def _fence(text: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    bar = "`" * max(3, longest + 1)
    return f"{bar}{lang}\n{text}\n{bar}"


def render_md(doc: dict[str, Any]) -> str:
    th = doc.get("thread") or {}
    title = (th.get("title") or "Conversation").replace("\n", " ")
    lines = [f"# {title}", "", f"_Exporté le {doc.get('exported_at')} — {doc.get('turn_count', 0)} tour(s)_", ""]
    page = doc.get("page") or {}
    if page.get("next_before") is not None:
        lines += [f"_Tours plus anciens non inclus (page suivante : before={page['next_before']})._", ""]
    current = None
    for m in doc.get("messages") or []:
        if m["index"] != current:
            current = m["index"]
            lines += ["---", "", f"## Tour {current}", ""]
        role = m["role"]
        if role == "user":
            who = f" ({m['sender']})" if m.get("sender") else ""
            lines += [f"**Utilisateur{who}**", "", m.get("content") or "", ""]
        elif role == "assistant" and m.get("tool_calls"):
            if m.get("content"):
                lines += ["**Assistant**", "", m["content"], ""]
            for c in m["tool_calls"]:
                lines += [f"**Appel d'outil** `{c['name']}`", "", _fence(c.get("arguments") or "", "json"), ""]
        elif role == "tool":
            label = " (scellé)" if m.get("sealed") else (" (tronqué)" if m.get("truncated") else "")
            lines += [f"**Résultat** `{m.get('name') or ''}`{label}", "", _fence(m.get("content") or ""), ""]
        else:
            lines += ["**Assistant**", "", m.get("content") or "", ""]
    return "\n".join(lines).rstrip() + "\n"


_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--card:#ffffff;--line:#e4e1d8;--user:#eef3fb;--tool:#f5f3ee;--seal:#fdf1e7}
@media (prefers-color-scheme: dark){:root{--bg:#151513;--fg:#ecebe6;--muted:#a3a29b;--card:#1f1f1c;--line:#34332e;
--user:#1c2533;--tool:#24231f;--seal:#33261b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.5rem;margin:0 0 4px;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:.85rem;margin-bottom:24px}
.turn{border-top:1px solid var(--line);padding-top:16px;margin-top:16px}
.msg{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:8px 0}
.msg.user{background:var(--user)}
.msg.tool{background:var(--tool);font-size:.9rem}
.msg.sealed{background:var(--seal)}
.who{font-weight:600;font-size:.8rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
.text{white-space:pre-wrap;overflow-wrap:anywhere}
pre{margin:6px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
""".strip()


def _e(text: Any) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def render_html(doc: dict[str, Any]) -> str:
    th = doc.get("thread") or {}
    title = th.get("title") or "Conversation"
    parts = ["<!doctype html>", '<html lang="fr"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta name="robots" content="noindex, nofollow">', '<meta name="referrer" content="no-referrer">',
             f"<title>{_e(title)}</title>", f"<style>{_CSS}</style>", "</head><body><main>",
             f"<h1>{_e(title)}</h1>",
             f'<div class="meta">Exporté le {_e(doc.get("exported_at"))} · {_e(doc.get("turn_count", 0))} tour(s)'
             + (" · tours plus anciens non inclus" if (doc.get("page") or {}).get("next_before") is not None else "")
             + "</div>"]
    current = None
    for m in doc.get("messages") or []:
        if m["index"] != current:
            if current is not None:
                parts.append("</section>")
            current = m["index"]
            parts.append(f'<section class="turn" id="t{_e(current)}">')
        role = m["role"]
        if role == "user":
            who = "Utilisateur" + (f" · {_e(m['sender'])}" if m.get("sender") else "")
            parts.append(f'<div class="msg user"><div class="who">{who}</div>'
                         f'<div class="text">{_e(m.get("content"))}</div></div>')
        elif role == "assistant" and m.get("tool_calls"):
            if m.get("content"):
                parts.append(f'<div class="msg"><div class="who">Assistant</div>'
                             f'<div class="text">{_e(m["content"])}</div></div>')
            for c in m["tool_calls"]:
                parts.append(f'<div class="msg tool"><div class="who">Appel d\'outil · <code>{_e(c["name"])}</code>'
                             f'</div><pre>{_e(c.get("arguments"))}</pre></div>')
        elif role == "tool":
            cls = "msg tool sealed" if m.get("sealed") else "msg tool"
            label = " · scellé" if m.get("sealed") else (" · tronqué" if m.get("truncated") else "")
            parts.append(f'<div class="{cls}"><div class="who">Résultat · <code>{_e(m.get("name"))}</code>{label}'
                         f'</div><pre>{_e(m.get("content"))}</pre></div>')
        else:
            parts.append(f'<div class="msg"><div class="who">Assistant</div>'
                         f'<div class="text">{_e(m.get("content"))}</div></div>')
    if current is not None:
        parts.append("</section>")
    parts.append("</main></body></html>")
    return "\n".join(parts) + "\n"


# En-têtes des rendus servis : aucun script ne peut s'exécuter, rien n'est chargé d'ailleurs, jamais en cache.
HTML_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'; sandbox")
CONTENT_TYPES = {"json": "application/json; charset=utf-8", "md": "text/markdown; charset=utf-8",
                 "html": "text/html; charset=utf-8"}


def render(doc: dict[str, Any], fmt: str) -> bytes:
    if fmt == "json":
        return json.dumps(doc, ensure_ascii=False).encode("utf-8")
    if fmt == "md":
        return render_md(doc).encode("utf-8")
    if fmt == "html":
        return render_html(doc).encode("utf-8")
    raise ExportError("format : json | md | html")


__all__ = [
    "CONTENT_TYPES",
    "DEFAULT_LIMIT",
    "FORMAT",
    "FORMATS",
    "HTML_CSP",
    "MAX_LIMIT",
    "SEALED_TEXT",
    "ExportError",
    "build_export",
    "default_effect_of",
    "is_sealed",
    "render",
    "render_html",
    "render_md",
]
