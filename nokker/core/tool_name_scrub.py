"""Nettoyage de fin de tour : le NOM TECHNIQUE d'un outil (`screen_draft`, `calendar_create`, `srv__memory_write`) ne doit
jamais apparaître dans le texte lu par la personne. Un nom d'outil du catalogue du tour trouvé dans la réponse finale est
remplacé par une tournure neutre (`person_texts.TOOL_NAME_NEUTRAL`).

Prudence (mieux vaut laisser un nom que casser une réponse technique) :
  - seuls les noms qui contiennent « _ » (snake_case, MCP `serveur__outil`) : « terminal », « patch », « clarify » sont des
    mots ordinaires ;
  - jamais dans une session de code (un outil de code est dans le catalogue du tour : on y parle d'outils) ;
  - jamais un nom que la personne a elle-même écrit dans son message du tour ;
  - jamais dans un bloc de code, une citation (`…`, « … », "…") ni un chemin ou une adresse (`a/nom`, `nom.py`)."""
from __future__ import annotations

import re

from nokker.core import person_texts as ptx

CODE_TOOLS = frozenset({"read_file", "write_file", "patch", "search_files", "terminal", "process_start"})
_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_QUOTED = re.compile(r"`[^`\n]*`|«[^»\n]*»|\"[^\"\n]*\"|“[^”\n]*”")


def scrub(reply: str, tools: tuple[set[str], bool], user_text: str, lang: str | None) -> str:
    names, code_session = tools
    if not reply or code_session:
        return reply
    names = sorted((n for n in names if "_" in n and n not in (user_text or "")), key=len, reverse=True)
    if not names:
        return reply
    pattern = re.compile(r"(?<![\w`/.\-])(?:" + "|".join(re.escape(n) for n in names) + r")(?![\w`/\-]|\.\w)")
    neutral = ptx.pick(ptx.TOOL_NAME_NEUTRAL, lang)
    protected = [m.span() for rx in (_FENCE, _QUOTED) for m in rx.finditer(reply)]

    def sub(m: re.Match) -> str:
        return m.group(0) if any(a <= m.start() < b for a, b in protected) else neutral

    return pattern.sub(sub, reply)
