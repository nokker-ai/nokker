"""Clôture des résultats de read_file, search_files, des outils MCP, de browser_* et d'execute_code (r44, étape 2).

Même règle que `web_fence.py` : la transformation est DÉTERMINISTE à partir de l'ancien résultat seul (identifiant de
clôture dérivé du contenu, aucune donnée ajoutée), et ces fonctions sont celles que le noyau appelle (une seule source
de vérité) ; elles portent aussi les anciens résultats (traces, mélanges d'entraînement).

- `read_file` : « Fichier <chemin> : » hors clôture, métadonnées (lignes, taille, troncature, indice) en JSON hors
  clôture, contenu dans `<untrusted-data kind="file:content">` ;
- `search_files` : « Correspondances pour « <motif> » : <n>. » hors clôture, métadonnées en JSON hors clôture, puis par
  fichier « - <chemin> » hors clôture et ses lignes « <ligne>: <texte> » dans `<untrusted-data kind="file:matches">` ;
  une recherche de fichiers : « Fichiers : <n>. » et un chemin par ligne, hors clôture ;
- outil MCP : « Résultat de <serveur>/<outil> : » hors clôture, texte du serveur dans `<untrusted-data kind="mcp:result">` ;
- `browser_*` : « Navigateur, <outil> : » hors clôture, texte dans `<untrusted-data kind="web:browser">` ;
- `execute_code` : le bloc fixe `[execute_code] … [/execute_code]` reste ; dans chaque section (stdout, return, error), le
  texte hors des blocs déjà clôturés (résultats d'outils appelés par le script) est clôturé (`code:<section>`) ; un bloc
  déjà clôturé n'est jamais reclôturé.

Chemins, motifs et noms passent par `qa/untrusted.inline()` ; l'en-tête « Consignes suspectes » est commun. Les messages
du noyau sans contenu de tiers (refus, erreurs de l'outil sans contenu) restent tels quels.
"""
from __future__ import annotations

import json
import re
from typing import Any

from nokker.qa.untrusted import fence_checked, inline, suspect_header

_MASK = re.compile(r"\n(\[Masque\][^\n]*)\s*$")
_FENCED = re.compile(r'<untrusted-data kind="[^"]*" id="(\w+)"(?: suspect="consigne")?>\n.*?\n</untrusted-data id="\1">',
                     re.S)
_LABEL = re.compile(r"^\s*[^\[\{\n]{0,60}?:\s*(?=[\[\{])")

READ_META = ("path", "total_lines", "file_size", "truncated", "truncated_lines", "hint", "is_binary", "is_image",
             "mime_type", "dimensions", "not_found", "similar_files", "offset", "limit", "lines_read", "line_count",
             "error", "encoding", "start_line", "end_line", "range")
SEARCH_META = ("total_count", "total", "total_found", "truncated", "limit_reason", "warning", "counts", "error",
               "matches_format", "pattern", "query", "count", "total_count_is_lower_bound")
_PATH = ("path", "file", "filename", "file_path")
_LINE = ("line", "line_number", "lineno", "line_no")
_TEXT = ("content", "text", "match", "snippet", "line_content")


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.suspects = 0

    def fence(self, kind: str, text: str) -> None:
        block, suspect = fence_checked(kind, text)
        self.suspects += int(suspect)
        self.lines.append(block)

    def render(self, tail: str = "") -> str:
        return "\n".join(x for x in (suspect_header(self.suspects), *self.lines, tail) if x)


def _split_mask(old: str) -> tuple[str, str]:
    m = _MASK.search(old)
    return (old[:m.start()], m.group(1)) if m else (old, "")


def _json(text: str) -> Any:
    body = _LABEL.sub("", text, count=1) if _LABEL.match(text) else text
    try:
        v = json.loads(body.strip())
    except ValueError:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _dumps(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, Any]:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return k, d[k]
    return "", None


# ─────────────────────────────── read_file ───────────────────────────────


_IMAGE_KEYS = ("is_image", "base64_content", "mime_type", "dimensions", "file_size")


def image_line(v: dict[str, Any]) -> str:
    """« image <mime>, <n> octets, <l>×<h> » : ce que le modèle lit d'une image, jamais ses octets (r44)."""
    parts = [f"image {inline(v.get('mime_type') or 'inconnue', 60)}"]
    if isinstance(v.get("file_size"), int):
        parts.append(f"{v['file_size']} octets")
    dims = str(v.get("dimensions") or "")
    m = re.fullmatch(r"(\d{1,6})\s*[x×]\s*(\d{1,6})", dims)
    if m:
        parts.append(f"{m.group(1)}×{m.group(2)}")
    elif dims:
        parts.append(inline(dims, 40))
    return ", ".join(parts)


def _read(args: dict[str, Any], text: str, out: _Out) -> None:
    path = str(args.get("path") or "")
    v = _json(text)
    if isinstance(v, dict) and (v.get("is_image") or "base64_content" in v):
        # image : une ligne hors clôture, plus jamais de base64 en texte ; les octets partent en partie image du
        # message quand le palier lit les images (noyau, `ToolResult.images`)
        path = str(v.get("path") or path)
        out.lines.append(f"Fichier {inline(path)} :" if path else "Fichier :")
        out.lines.append(image_line(v))
        meta = {k: v[k] for k in READ_META if k in v and k not in ("path", *_IMAGE_KEYS)}
        if meta:
            out.lines.append(json.dumps(meta, ensure_ascii=False))
        rest = {k: x for k, x in v.items() if k not in READ_META and k not in _IMAGE_KEYS}
        if rest:
            out.fence("file:content", json.dumps(rest, ensure_ascii=False, indent=1))
        return
    if isinstance(v, dict):
        path = str(v.get("path") or path)
        meta = {k: v[k] for k in READ_META if k in v and k != "path"}
        body_keys = [k for k in v if k not in READ_META]
        out.lines.append(f"Fichier {inline(path)} :" if path else "Fichier :")
        if meta:
            out.lines.append(json.dumps(meta, ensure_ascii=False))
        main = next((k for k in ("content", "lines", "text") if k in body_keys and (
            isinstance(v[k], str) or (isinstance(v[k], list) and all(isinstance(x, str) for x in v[k])))), None)
        if main is not None:
            text_ = v[main] if isinstance(v[main], str) else "\n".join(v[main])
            extra = [f"{k} : {_dumps(v[k])}" for k in body_keys if k != main]
            out.fence("file:content", text_ + ("\n\n" + "\n".join(extra) if extra else ""))
        elif body_keys:
            # forme inconnue (fichier JSON lu, champs propres à une source) : le reste de l'objet tel quel
            out.fence("file:content", json.dumps({k: v[k] for k in body_keys}, ensure_ascii=False, indent=1))
        return
    out.lines.append(f"Fichier {inline(path)} :" if path else "Fichier :")
    out.fence("file:content", text)


# ─────────────────────────────── search_files ───────────────────────────────


def _group(matches: list[Any], out: _Out) -> None:
    current: str | None = None
    rows: list[str] = []

    def flush() -> None:
        if rows:
            out.fence("file:matches", "\n".join(rows))
            rows.clear()

    for m in matches:
        if not isinstance(m, dict):
            flush()
            current = None
            out.fence("file:matches", _dumps(m))
            continue
        pk, path = _first(m, _PATH)
        lk, line = _first(m, _LINE)
        tk, text = _first(m, _TEXT)
        extra = {k: v for k, v in m.items() if k not in (pk, lk, tk)}
        if path != current:
            flush()
            current = path
            out.lines.append(f"- {inline(path)}" if path else "-")
        row = (f"{line}: " if line not in (None, "") else "") + (_dumps(text) if text is not None else "")
        if extra:
            row += (" " if row else "") + json.dumps(extra, ensure_ascii=False)
        rows.append(row)
    flush()


def _search(args: dict[str, Any], text: str, out: _Out) -> None:
    pattern = str(args.get("pattern") or args.get("query") or "")
    v = _json(text)
    if isinstance(v, list):
        v = {"matches": v}
    if not isinstance(v, dict):
        out.lines.append(f"Correspondances pour « {inline(pattern, 300)} » :")
        out.fence("file:matches", text)
        return
    matches = v.get("matches") if isinstance(v.get("matches"), list) else None
    files = v.get("files") if isinstance(v.get("files"), list) else None
    total = next((v[k] for k in ("total_count", "total", "total_found", "count") if isinstance(v.get(k), int)),
                 len(matches) if matches is not None else len(files) if files is not None else None)
    head = f"Correspondances pour « {inline(pattern, 300)} »" + (f" : {total}." if total is not None else " :")
    out.lines.append(head)
    meta = {k: v[k] for k in SEARCH_META if k in v and k not in ("total_count", "total", "total_found", "count",
                                                                   "pattern", "query", "matches_format", "counts")}
    if meta:
        out.lines.append(json.dumps(meta, ensure_ascii=False))
    counts = v.get("counts")
    if isinstance(counts, dict) and counts:
        # nombre de correspondances par fichier : chemins hors clôture, passés par inline()
        out.lines.append("Par fichier :")
        out.lines.extend(f"- {inline(k)} : {_dumps(n)}" for k, n in counts.items())
    elif counts not in (None, {}):
        out.fence("file:matches", _dumps(counts))
    if matches is not None:
        _group(matches, out)
    if isinstance(v.get("matches_text"), str):
        out.fence("file:matches", v["matches_text"])
    if files is not None:
        out.lines.append(f"Fichiers : {len(files)}.")
        for f in files:
            if isinstance(f, str):
                out.lines.append(f"- {inline(f)}")
            else:
                out.fence("file:matches", _dumps(f))
    rest = {k: x for k, x in v.items() if k not in (*SEARCH_META, "matches", "files", "matches_text")}
    if rest:
        out.fence("file:matches", json.dumps(rest, ensure_ascii=False))


def fence_file_result(name: str, args: dict[str, Any] | None, old_result: str) -> str:
    """Nouvelle forme d'un résultat de `read_file` ou `search_files` à partir de l'ancien (voir le module)."""
    if name not in ("read_file", "search_files"):
        raise ValueError(f"outil non fichier : {name}")
    args = args if isinstance(args, dict) else {}
    body, tail = _split_mask(str(old_result or ""))
    v = _json(body)
    if isinstance(v, dict) and v.get("error") and not any(k in v for k in ("content", "matches", "files",
                                                                           "matches_text", "lines")):
        if isinstance(v.get("similar_files"), list):  # noms de fichiers voisins : hors clôture, passés par inline()
            v = {**v, "similar_files": [inline(f) if isinstance(f, str) else f for f in v["similar_files"]]}
            return json.dumps(v, ensure_ascii=False) + (f"\n{tail}" if tail else "")
        return str(old_result)  # erreur de l'outil (chemin absent, refus) : texte de l'outil, inchangé
    out = _Out()
    (_read if name == "read_file" else _search)(args, body, out)
    return out.render(tail)


# ─────────────────────────────── MCP, navigateur ───────────────────────────────


def fence_mcp_result(server: str, tool: str, old_result: str, note: str = "") -> str:
    """Texte rendu par un serveur MCP (résultat ou erreur du serveur). `note` : phrase du NOYAU, hors clôture, sur la
    ligne d'en-tête (tâche longue en échec…)."""
    body, tail = _split_mask(str(old_result or ""))
    out = _Out()
    out.lines.append(f"Résultat de {inline(server, 80)}/{inline(tool, 80)} :" + (f" {note}" if note else ""))
    out.fence("mcp:result", body)
    return out.render(tail)


def mcp_error_text(server: str, tool: str, exc: BaseException) -> str:
    """Échec d'un appel MCP. Erreur JSON-RPC rendue par le serveur (`McpError`) : son message est un texte de tiers,
    clôturé (`mcp:error`) ; toute autre exception (réseau, garde SSRF, délai) est du noyau : une ligne, par inline()."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    try:
        from mcp.shared import exceptions as _mcp_exc

        server_errors = tuple(c for c in (getattr(_mcp_exc, "McpError", None), getattr(_mcp_exc, "MCPError", None)) if c)
    except ImportError:  # pragma: no cover — SDK absent : tout est du noyau
        server_errors = ()
    if server_errors and isinstance(exc, server_errors):
        out = _Out()
        out.lines.append(f"erreur MCP {inline(server, 80)}/{inline(tool, 80)}, message du serveur :")
        err = getattr(exc, "error", None)
        out.fence("mcp:error", str(getattr(err, "message", None) or exc))
        return out.render()
    return f"erreur MCP {inline(server, 80)}/{inline(tool, 80)}: {inline(str(exc), 500)}"


_MCP_KERNEL_ERROR = re.compile(r"erreur MCP [^\n:]{1,200}?: (.*)", re.S)


def port_mcp_result(server: str, tool: str, old_result: str) -> str:
    """Portage d'un ANCIEN résultat MCP : les notes du noyau (tâche en cours, annulée, en attente) restent telles
    quelles, une tâche en échec garde sa phrase hors clôture et le message du serveur dedans, une erreur d'appel
    devient la ligne du noyau ; le reste est un texte de serveur, clôturé (`fence_mcp_result`)."""
    from nokker.tools import mcp_next

    old = str(old_result or "")
    pending = re.escape(mcp_next.TASK_PENDING).replace(re.escape("{status}"), r"[\w-]{1,40}")
    if re.fullmatch(pending, old) or old in (mcp_next.TASK_CANCELLED, mcp_next.TASK_INPUT):
        return old
    failed = mcp_next.TASK_FAILED.split("{message}")[0]
    if old.startswith(failed):
        return fence_mcp_result(server, tool, old[len(failed):], note=mcp_next.TASK_FAILED_NOTE)
    m = _MCP_KERNEL_ERROR.fullmatch(old)
    if m:
        return f"erreur MCP {inline(server, 80)}/{inline(tool, 80)}: {inline(m.group(1), 500)}"
    return fence_mcp_result(server, tool, old)


# Ligne d'instantané (arbre d'accessibilité) : `- rôle "nom" [attr] [ref=eN]: texte`. Le nom (entre guillemets,
# guillemets internes échappés) et le texte après les attributs viennent de la PAGE ; seuls les attributs sont écrits
# par le navigateur.
_SNAP_LINE = re.compile(r'^(\s*- )([^\s":\[]+)((?: "(?:[^"\\\n]|\\.)*")?)((?: \[[^\]\n]*\])*)(.*)$')
_ATTR = re.compile(r" \[[^\]\n]*\]")
_REF = re.compile(r"\[ref=([A-Za-z0-9_-]{1,32})\]")


_PAGE_REF = re.compile(r"\[ref=([^\]\n]*)\]")


def _no_ref(text: str) -> str:
    """« [ref=x] » écrit par une page → « (ref=x) » : lisible, mais plus une référence."""
    return _PAGE_REF.sub(r"(ref=\1)", text).replace("[ref=", "(ref=")


def browser_refs(text: str) -> tuple[str, list[str]]:
    """(texte où une page ne peut plus fabriquer de référence, références écrites par le navigateur). Une « [ref=… »
    venue de la page (nom d'élément, texte, titre, ligne hors arbre) devient « (ref=… » ; seules les références en
    position d'attribut d'une ligne d'instantané restent, et sont rendues pour le contrôle du noyau."""
    refs: list[str] = []
    lines = []
    for line in str(text or "").split("\n"):
        m = _SNAP_LINE.match(line)
        if m is None:
            lines.append(_no_ref(line))
            continue
        prefix, role, name, attrs, rest = m.groups()
        kept = []
        for a in _ATTR.findall(attrs):
            r = _REF.fullmatch(a[1:])
            if r is not None:
                refs.append(r.group(1))
                kept.append(a)
            else:
                kept.append(_no_ref(a))
        lines.append(prefix + role + _no_ref(name) + "".join(kept) + _no_ref(rest))
    return "\n".join(lines), list(dict.fromkeys(refs))


def fence_browser_result(name: str, args: dict[str, Any] | None, old_result: str) -> str:
    """Texte rendu par le navigateur (instantané, lecture, résultat d'un geste). Les références restent dans la
    clôture ; celles qu'une page tenterait d'écrire sont neutralisées (`browser_refs`)."""
    body, tail = _split_mask(str(old_result or ""))
    out = _Out()
    out.lines.append(f"Navigateur, {inline(name, 80)} :")
    out.fence("web:browser", browser_refs(body)[0])
    return out.render(tail)


# ─────────────────────────────── execute_code ───────────────────────────────

_SECTIONS = re.compile(r"^(stdout|return|error):\n", re.M)


def fence_code_text(section: str, text: str) -> tuple[str, int]:
    """Une section d'execute_code : les morceaux HORS des blocs déjà clôturés sont clôturés (`code:<section>`), les
    blocs déjà clôturés restent tels quels. (texte, nombre de blocs marqués suspects)."""
    if not text.strip():
        return text, 0
    parts, suspects, last = [], 0, 0
    for m in _FENCED.finditer(text):
        piece = text[last:m.start()].strip("\n")
        if piece.strip():
            block, s = fence_checked(f"code:{section}", piece)
            parts.append(block)
            suspects += int(s)
        parts.append(m.group(0))
        suspects += int('suspect="consigne"' in m.group(0).split("\n", 1)[0])
        last = m.end()
    piece = text[last:].strip("\n")
    if piece.strip():
        block, s = fence_checked(f"code:{section}", piece)
        parts.append(block)
        suspects += int(s)
    return "\n".join(parts), suspects


# Messages du NOYAU dans la section error (délai, annulation, bac à sable, trame) : sortis en note hors clôture.
# Seules des parties FIXES (et des nombres) passent dans la note : un texte libre (fin de la sortie d'erreur, message
# d'exception) reste dans la section, clôturé — une trame `done` forgée par le script ne peut donc écrire hors clôture
# qu'une de ces phrases.
_KERNEL_FIXED = {
    "annulé par l'utilisateur": "annulé par l'utilisateur.",
    "le bac à sable n'a pas accepté le script": "le bac à sable n'a pas accepté le script.",
    "trame trop longue : script arrêté": "trame trop longue, script arrêté.",
    "trame illisible : script arrêté": "trame illisible, script arrêté.",
    "trame inconnue : script arrêté": "trame inconnue, script arrêté.",
    "canal du script fermé": "canal du script fermé.",
    "argument manquant : code": "argument manquant : code.",
    "execute_code n'est appelable que pendant que tu réponds à la personne": "execute_code n'est appelable que pendant que tu réponds à la personne.",
    "execute_code n'est appelable que pendant un tour": "execute_code n'est appelable que pendant que tu réponds à la personne.",
    # ancienne formule d'un résultat déjà en historique : rendue avec la nouvelle
    "execute_code n'est appelable que dans un tour du noyau": "execute_code n'est appelable que pendant que tu réponds à la personne.",
    "pas de session de code sur ce fil : execute_code tourne dans son bac à sable":
        "pas de session de code sur ce fil : execute_code tourne dans son bac à sable.",
}
_KERNEL_TIMEOUT = re.compile(r"TimeoutError: délai dépassé \((\d{1,6}) s\), script arrêté")
_KERNEL_STOPPED = re.compile(r"le script s'est arrêté sans rendre de résultat \(code (-?\d{1,4})\)(?: : (.+))?", re.S)
_KERNEL_PREFIXED = (("bac à sable injoignable : ", "bac à sable injoignable"),
                    ("bac à sable indisponible : ", "bac à sable indisponible"),
                    ("REFUSÉ : ", "Refusé"))


def code_kernel_note(error: str, has_output: bool) -> tuple[str, str]:
    """(note du noyau, reste de la section error). Pas de note : ("", error) inchangé — l'exception du code."""
    text = error.strip("\n")
    if text in _KERNEL_FIXED:
        return _KERNEL_FIXED[text], ""
    m = _KERNEL_TIMEOUT.fullmatch(text)
    if m:
        return (f"délai dépassé ({m.group(1)} s) ; sortie partielle ci-dessous." if has_output
                else f"délai dépassé ({m.group(1)} s), script arrêté."), ""
    m = _KERNEL_STOPPED.fullmatch(text)
    if m:
        if m.group(2):
            return f"le script s'est arrêté sans rendre de résultat (code {m.group(1)}) ; fin de sa sortie d'erreur " \
                   f"ci-dessous.", m.group(2)
        return f"le script s'est arrêté sans rendre de résultat (code {m.group(1)}).", ""
    for prefix, note in _KERNEL_PREFIXED:
        if text.startswith(prefix) and "\n" not in text:
            return f"{note} ; détail ci-dessous.", text[len(prefix):]
    return "", error


def fence_code_result(old_result: str, note: str = "") -> str:
    """Bloc d'execute_code (`[execute_code]` … `[/execute_code]`) : sections clôturées, `(vide)` gardé tel quel ; un
    message du noyau dans la section error devient une note hors clôture, `[execute_code] <note>`, avant le bloc.
    `note` : refus du noyau AVANT tout script (argument, session, bac à sable), passé tel quel par le noyau — rien de
    tiers, il ne passe pas par la section error."""
    old = str(old_result or "")
    if not old.startswith("[execute_code]\n"):
        return old  # refus du noyau (pas de bloc) : inchangé
    inner = old[len("[execute_code]\n"):]
    close = inner.rfind("\n[/execute_code]")
    inner, after = (inner[:close], inner[close:]) if close >= 0 else (inner, "")
    pieces = _SECTIONS.split(inner)
    sections = [(pieces[i], pieces[i + 1].rstrip("\n")) for i in range(1, len(pieces) - 1, 2)]
    has_output = any(label in ("stdout", "return") and text.strip() != "(vide)" for label, text in sections)
    out, suspects = [], 0
    note = inline(note, 500)
    for label, text in sections:
        if label == "error" and text.strip() != "(vide)" and not note:
            note, text = code_kernel_note(text, has_output)
            text = text if text.strip() else "(vide)"
        if text.strip() == "(vide)":
            out.append(f"{label}:\n{text}")
            continue
        fenced, s = fence_code_text(label, text)
        suspects += s
        out.append(f"{label}:\n{fenced}")
    head = "\n".join(x for x in (suspect_header(suspects), f"[execute_code] {note}" if note else "") if x)
    return (head + "\n" if head else "") + "[execute_code]\n" + "\n".join(out) + after


__all__ = ["browser_refs", "code_kernel_note", "fence_browser_result", "fence_code_result", "fence_code_text",
           "fence_file_result", "fence_mcp_result", "image_line", "mcp_error_text", "port_mcp_result"]
