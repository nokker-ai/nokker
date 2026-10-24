"""Skill « sessions Lokkal » de nokker.ai (docs/LOKKAL.md §6.4, §7.5) : une session web confie une tâche à la machine du
client, sans jamais la modifier elle-même.

Outil `lokkal_session {machine?, cwd, task}` — effet DESTRUCTIVE côté web (il agit sur la machine du client) : carte
d'approbation forte la première fois (porte du noyau web). Il :
  1. choisit la machine liée au compte (par nom, ou la seule) ;
  2. signe un jeton de DÉLÉGATION court (5 min, portée `lokkal:session`, machine nommée) ;
  3. par la passerelle (`https://<nom>.nokker.app`) : `POST /v1/local/sessions {cwd}` puis `POST /v1/chat/completions`
     (`X-Session-Id` = le fil Lokkal) avec la tâche ;
  4. rend la réponse finale et le fil Lokkal. Les cartes de la session Lokkal suivent SES permissions locales et
     arrivent à l'utilisateur ; ce noyau n'en décide aucune.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

TOOL = "lokkal_session"
_SPEC = {"type": "object", "required": ["cwd", "task"], "properties": {
    "machine": {"type": "string", "description": "nom de la machine liée (facultatif s'il n'y en a qu'une)"},
    "cwd": {"type": "string", "description": "dossier de travail sur la machine (sous une racine déclarée)"},
    "task": {"type": "string", "description": "la tâche à faire, en entier"}}}


@dataclass
class LokkalSessionTools:
    links: Any  # LinkService
    signer: Any  # Signer
    timeout_s: float = 900.0
    scheme: str = "https"
    client: Any = None  # httpx.Client injectable (tests)

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=TOOL, description="Confie une tâche à ta machine liée (Nokker Lokkal), dans un dossier ; "
                                                "rend sa réponse.", parameters=_SPEC, effect=Effect.DESTRUCTIVE)]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(text: str, error: bool = False) -> ToolResult:
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        account = ctx.sender or ""
        machines = self.links.machines_of(account) if account.startswith("us_") else []
        if not machines:
            return out("aucune machine liée à ce compte : installe Nokker Lokkal et relie-le (app.nokker.ai/link)", True)
        wanted = str(arguments.get("machine") or "").strip().lower()
        pick = [m for m in machines if not wanted or wanted in (m.name.lower(), m.host.split(".")[0])]
        if len(pick) != 1:
            names = ", ".join(m.name for m in machines)
            return out(f"précise la machine : {names}", True)
        m = pick[0]
        token = self.signer.delegation_token(account, m.tenant, machine=m.id, web_session=ctx.thread_id)
        base = f"{self.scheme}://{m.host}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        http = self.client or httpx.Client(timeout=self.timeout_s)
        try:
            r = http.post(base + "/v1/local/sessions", headers=headers, json={"cwd": str(arguments.get("cwd") or "")})
            if r.status_code != 201:
                return out(f"la machine {m.name} refuse la session ({r.status_code}) : "
                           f"{_msg(r)}", True)
            thread = r.json()["thread_id"]
            r = http.post(base + "/v1/chat/completions", headers={**headers, "X-Session-Id": thread},
                          json={"model": "nokker", "messages": [{"role": "user", "content": str(arguments["task"])}]})
            if r.status_code != 200:
                return out(f"la session Lokkal a échoué ({r.status_code}) : {_msg(r)}", True)
            reply = r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            return out(f"machine {m.name} injoignable ({type(e).__name__}) : est-elle allumée et reliée ?", True)
        return out(json.dumps({"machine": m.name, "thread": thread, "reply": reply}, ensure_ascii=False))


def _msg(r: Any) -> str:
    try:
        return str(r.json().get("error", {}).get("message") or r.text[:200])
    except (ValueError, AttributeError):
        return r.text[:200]


__all__ = ["TOOL", "LokkalSessionTools"]
