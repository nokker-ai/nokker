"""Socle des importeurs : élément de rapport, rapport (report.json + summary.md), réconciliation des sources."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

# planned : sera repris par `apply` ; imported : repris ; native : Nokker le lit déjà tel quel (rien à faire) ;
# skipped : volontairement laissé (raison donnée) ; unsupported : pas d'équivalent dans Nokker (raison donnée) ;
# conflict : la cible existe et diffère ; error : lecture ou écriture en échec.
STATUSES = ("planned", "imported", "native", "skipped", "unsupported", "conflict", "error")
UNACCOUNTED = "unaccounted-source"

# Code STABLE d'une raison (le web traduit ; le texte français reste en repli). Premier fragment trouvé dans la
# raison, dans cet ordre (les plus précis d'abord). Une raison sans fragment connu → "other".
REASON_CODES: tuple[tuple[str, str], ...] = (
    ("importé comme skill", "subagent_as_skill"), ("bloc .nokker/settings.json du dépôt", "project_hooks"),
    ("inactif tant que", "hooks_untrusted"), ("à déclarer de confiance", "needs_trust"),
    ("hooks de l'utilisateur", "user_hooks_manual"), ("l'événement", "hook_event_unsupported"),
    ("seuls les hooks de type commande", "hook_type_unsupported"), ("outil sans équivalent", "tool_unsupported"),
    ("élargit les droits", "allow_manual"), ("valeur jamais copiée", "secret_name_only"),
    ("réglage propre", "setting_unsupported"), ("réglages d'agent propres", "setting_unsupported"),
    ("ajout après confirmation", "mcp_confirm"), ("à ajouter dans une session", "mcp_in_session"),
    ("non confirmé", "mcp_not_confirmed"), ("serveur stdio", "mcp_stdio"), ("refusé par Nokker", "mcp_refused"),
    ("un serveur MCP s'appelle déjà", "mcp_name_taken"), ("serveurs ajoutés au plus", "mcp_limit"),
    ("note de mémoire de portée utilisateur", "user_note"), ("mémoire de projet désactivée", "memory_off"),
    ("note refusée", "note_refused"), ("déjà lu par Nokker", "native_project_memory"),
    ("réunies dans NOKKER.md", "rules_to_nokker_md"), ("règles à fusionner", "nokker_md_exists"),
    ("est apparu entre le scan", "appeared_since_scan"), ("règle vide", "empty_rule"),
    ("déjà repris d'un autre dossier", "skill_duplicate"), ("un skill du même nom", "skill_exists"),
    ("sans description", "skill_no_description"), ("lien symbolique", "symlink_refused"),
    ("sous-agent sans prompt", "subagent_empty"), ("données de fonctionnement", "runtime_data"),
    ("historique des sessions", "runtime_data"), ("lu clé par clé", "read_by_key"), ("lu par sections", "read_by_key"),
    ("lu ligne par ligne", "read_by_key"), ("lu tâche par tâche", "read_by_key"), ("fichier vide", "empty_file"),
    ("illisible", "unreadable"), ("agent créé en pause", "agent_created_paused"), ("agent en brouillon", "agent_draft"),
    ("noyau local non joint", "agent_no_kernel"), ("refusé par le noyau", "agent_refused"),
    ("tâche sans prompt", "cron_no_prompt"), ("seule une expression cron", "cron_schedule_unsupported"),
    ("format des tâches d'OpenClaw", "cron_format_unknown"), ("canal à relier", "channel_relink"),
    ("Nokker choisit son modèle", "model_manual"), ("document propre à OpenClaw", "openclaw_doc"),
    ("jamais lu", "never_read"), ("configuration de secours", "config_fallback"),
    ("index des skills propre", "skill_index"), ("en échec", "write_failed"), ("pas d'écriture pour", "no_writer"),
    ("Aucune étape de cet importeur", "unread_path"),
)


def reason_code(reason: str) -> str:
    if not reason:
        return ""
    return next((code for frag, code in REASON_CODES if frag in reason), "other")
SKILL_DESC_MAX = 60  # nokker/tools/packs.py MAX_SKILL_DESC : l'index des skills reste compact


@dataclass
class ImportItem:
    kind: str
    source: str
    target: str | None
    status: str
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"statut inconnu : {self.status}")


@dataclass
class Report:
    origin: str
    items: list[ImportItem] = field(default_factory=list)

    def add(self, kind: str, source: Path | str, target: Path | str | None, status: str, reason: str = "", **detail) -> ImportItem:
        it = ImportItem(kind, str(source), None if target is None else str(target), status, reason, detail)
        self.items.append(it)
        return it

    def counts(self) -> dict[str, int]:
        c = Counter(i.status for i in self.items)
        return {s: c.get(s, 0) for s in STATUSES}

    def sources(self) -> set[str]:
        return {i.source for i in self.items}

    def to_json(self) -> dict:
        # `code` calculé à l'écriture : une raison changée par `apply` garde un code juste.
        return {"origin": self.origin, "counts": self.counts(),
                "items": [{**asdict(i), "code": reason_code(i.reason)} for i in self.items]}

    def summary_md(self) -> str:
        c = self.counts()
        lines = [f"# Import depuis {self.origin}", "", " · ".join(f"{k} : {v}" for k, v in c.items() if v), ""]
        for title, keep in (("Repris ou à reprendre", ("planned", "imported", "native")),
                            ("Pas repris (raison)", ("skipped", "unsupported", "conflict", "error"))):
            rows = [i for i in self.items if i.status in keep]
            if not rows:
                continue
            lines += [f"## {title}", ""]
            lines += [f"- **{i.kind}** `{i.source}`" + (f" → `{i.target}`" if i.target else "") + f" — {i.status}" + (f" : {i.reason}" if i.reason else "") for i in rows]
            lines.append("")
        return "\n".join(lines)

    def write(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "summary.md").write_text(self.summary_md(), encoding="utf-8")


def _covered(path: Path, recorded: set[Path]) -> bool:
    return any(path == r or r in path.parents for r in recorded)


def reconcile(report: Report, roots: list[Path]) -> None:
    """Tout fichier sous `roots` que ni un élément ni un dossier déjà rapporté ne couvre reçoit UNE ligne
    `unsupported` par chemin de premier niveau sous sa racine. Rien n'est lu, copié ni effacé : la passe rend
    seulement visible ce qu'aucune étape n'a pris en compte."""
    recorded = {Path(s) for s in report.sources()}
    grouped: dict[Path, int] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_symlink() or not p.is_file() or _covered(p, recorded):
                continue
            top = root / p.relative_to(root).parts[0]
            grouped[top] = grouped.get(top, 0) + 1
    for top, n in grouped.items():
        report.add(UNACCOUNTED, top, None, "unsupported", "Aucune étape de cet importeur ne lit ce chemin.", files=n)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Frontmatter YAML simple (`clé: valeur` sur une ligne) entre deux `---`, et le corps. Les valeurs sur plusieurs
    lignes ne sont pas reprises : le corps, lui, est toujours gardé entier."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, text[end + 4:].lstrip("\n")


def short_description(desc: str, limit: int = SKILL_DESC_MAX) -> tuple[str, str]:
    """Description d'index tenant dans `limit` caractères (coupée au mot) et le reste, à garder dans le corps."""
    desc = " ".join(desc.split())
    if len(desc) <= limit:
        return desc, ""
    cut = desc[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…", desc
