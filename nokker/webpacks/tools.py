"""Outils des packs pour le modèle d'un compte de nokker.ai (sources des testeurs, `TenantRegistry`).

`pack_list` (READ), `pack_install` et `pack_remove` (DESTRUCTIVE : carte, comme en local), `skill_list` et `skill_view`
(READ) sur les compétences des packs installés par CE compte — le contenu livré avec le noyau, masqué s'il a changé
depuis la garde (nokker/webpacks/service.py). Le compte est l'identité du tour (`ctx.sender`, `us_…`), le locataire
celui du tour ; un fil est celui du tour."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.webpacks.service import WebPackError, WebPacks

_SPECS: tuple[tuple[str, str, Effect, dict[str, Any]], ...] = (
    ("pack_list", "Liste les packs d'agents (compétences + modèles d'agents) : ce qui s'installe ici et ce qui "
     "demande une machine ; avec name, le détail d'un pack.", Effect.READ,
     {"type": "object", "properties": {"name": {"type": "string", "description": "pack à détailler (ex. mail)"}}}),
    ("pack_install", "Installe un pack sur ce compte : ses compétences (scannées) et la PROPOSITION de ses agents qui "
     "tournent ici (cartes agent_draft, rien n'est armé) ; dit ce qui demande une machine. Carte.", Effect.DESTRUCTIVE,
     {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string"},
         "bind": {"type": "object", "description": "facultatif : « secret:NOM » → autre NOM"}}}),
    ("pack_remove", "Retire un pack de ce compte en un appel : ses compétences et ses cartes non activées ; pour les "
     "agents déjà créés depuis le pack, une carte propose de les désarmer ou supprimer. Carte.", Effect.DESTRUCTIVE,
     {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}),
    ("skill_list", "Liste les compétences des packs installés (nom, famille, description).", Effect.READ,
     {"type": "object", "properties": {"category": {"type": "string", "description": "famille à lister"}}}),
    ("skill_view", "Lit le mode d'emploi d'une compétence, ou un de ses fichiers liés (references/, templates/…).",
     Effect.READ, {"type": "object", "required": ["name"], "properties": {
         "name": {"type": "string", "description": "nom de la compétence (ou famille/nom)"},
         "file": {"type": "string", "description": "fichier lié à lire, relatif au dossier de la compétence"}}}),
)
WEB_PACK_TOOLS = tuple(n for n, *_ in _SPECS)


@dataclass
class WebPackTools:
    service: WebPacks
    core: bool = False

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source="builtin:packs")
                for n, d, e, p in _SPECS]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        a = arguments or {}
        account = str(ctx.sender or "")
        if not account.startswith("us_"):
            return out("refusé : les packs se règlent pour un compte nokker.ai", True)
        svc = self.service
        try:
            if name == "pack_list":
                return out(svc.detail(account, str(a["name"])) if a.get("name") else svc.catalog(account))
            if name == "pack_install":
                return out(svc.install(account, str(a.get("name") or ""), a.get("bind"), thread_id=ctx.thread_id))
            if name == "pack_remove":
                return out(svc.remove(account, str(a.get("name") or ""), thread_id=ctx.thread_id))
            if name == "skill_list":
                return out(svc.list_skills(account, a.get("category")))
            if name == "skill_view":
                payload, err = svc.view_skill(account, str(a.get("name") or ""), a.get("file"))
                return out(payload, err)
        except WebPackError as e:
            extra = f"\n{json.dumps(e.payload, ensure_ascii=False)}" if e.payload else ""
            return out(f"refusé : {e.message}{extra}", True)
        return out(f"outil inconnu : {name}", True)


__all__ = ["WEB_PACK_TOOLS", "WebPackTools"]
