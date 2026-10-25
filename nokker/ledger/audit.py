"""Journal d'audit chaîné de Nokker Lokkal (lot X11, docs/LOKKAL.md §7.14) : chaque appel d'outil à effet exécuté
(local ou ssh) laisse une entrée, et chaque entrée porte l'empreinte de la précédente.

Ce que fait déjà le ledger (`nokker/ledger/costs.py`) : les appels MODÈLE (`ledger_entries`) et les VERDICTS des portes
(`ledger_verdicts` : décision, règle, `subject` = id de l'appel), dans Postgres sous RLS par locataire, sans chaîne. Il
ne voit pas l'exécution d'un outil (qui l'a lancé, sur quoi, avec quel résultat). Le journal d'audit ne double donc
rien : il porte l'EXÉCUTION, avec les clés du ledger (`thread_id`, `turn_index`, `tool`) pour retrouver le verdict qui
l'a permise, et il vit à côté du ledger dans le même paquet. Fichier plutôt que table : il appartient à la machine
(LOKKAL §6.7, rien ne part au miroir), il se vérifie sans base, et `audit export|verify` marche dans le conteneur `core`
comme sur l'hôte.

Fichier `<state_dir>/audit/audit.jsonl` (0600, dossier 0700), une ligne JSON par entrée, en ajout seulement, et
`audit.head.json` : `{seq, hash}` de la dernière entrée, réécrit après chaque ajout. Entrée :

    {"v": 1, "seq": 12, "at": "2026-10-25T10:00:00.123456+00:00", "thread_id": "th_…", "turn_index": 3,
     "channel": "api", "actor": "local-owner", "tool": "ssh_run", "effect": "write", "target": "ssh:web1",
     "role": "dev", "address": "10.0.0.5:22", "args": {…caviardés, chaînes coupées…}, "args_sha256": "…",
     "is_error": false, "result_sha256": "…", "preview": "…", "ms": 412, "prev": "<hash de l'entrée 11>",
     "hash": "<sha256 du JSON canonique de l'entrée sans « hash »>"}

`verify` : chaque ligne relue, `hash` recalculé (entrée modifiée), `seq` contigu depuis 1 et `prev` = `hash` de la
précédente (entrée retirée au début ou au milieu), dernière entrée = tête (`audit.head.json` : fin du journal
tronquée), et en option une ancre gardée ailleurs (`--anchor seq:hash`). Limite dite : quelqu'un qui peut écrire dans
le dossier d'état peut réécrire TOUTE la chaîne et la tête de façon cohérente ; contre ça, garder une ancre hors de la
machine (la dernière ligne d'un export, `audit verify` en affiche une).
"""
from __future__ import annotations

import csv
import dataclasses
import fcntl
import hashlib
import io
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext, call_effect
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.ledger.audit")

GENESIS = "0" * 64
VERSION = 1
ARG_CHARS = 2000  # une chaîne d'argument au-delà est coupée (son empreinte complète est dans args_sha256)
PREVIEW_CHARS = 200
CSV_FIELDS = ("seq", "at", "thread_id", "turn_index", "channel", "actor", "tool", "effect", "target", "role",
              "address", "is_error", "ms", "args", "args_sha256", "result_sha256", "preview", "prev", "hash")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(canonical({k: v for k, v in entry.items() if k != "hash"}).encode()).hexdigest()


def _clip_args(obj: Any) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= ARG_CHARS else obj[:ARG_CHARS] + f"[… {len(obj) - ARG_CHARS} car. coupés]"
    if isinstance(obj, dict):
        return {str(k): _clip_args(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clip_args(v) for v in obj]
    return obj


def default_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / "audit" / "audit.jsonl"


@dataclass
class AuditLog:
    """Le journal d'une machine. Ajouts sérialisés (verrou du processus + `flock` du fichier, pour qu'un second
    processus — un autre noyau sur le même dossier — n'entrelace pas la chaîne)."""

    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser()

    @property
    def head_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".head.json")

    def head(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and "seq" in data and "hash" in data else None

    def _last_line(self) -> dict[str, Any] | None:
        """Dernière entrée lisible (tête absente : premier ajout après une perte de `audit.head.json`)."""
        last = None
        for e in self.entries():
            if isinstance(e, dict):
                last = e
        return last

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        """Ajoute une entrée (champs de l'appel) ; rend l'entrée complète (seq, at, prev, hash)."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock:
            lock_fd = os.open(self.path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                head = self.head() or self._last_line()
                seq = int(head["seq"]) + 1 if head else 1
                prev = str(head["hash"]) if head else GENESIS
                entry = {"v": VERSION, "seq": seq, "at": datetime.now(UTC).isoformat(), **record,
                         "prev": prev}
                entry["hash"] = entry_hash(entry)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(canonical(entry) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                tmp = self.head_path.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(canonical({"seq": seq, "hash": entry["hash"]}))
                os.replace(tmp, self.head_path)
                return entry
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def entries(self) -> Iterator[dict[str, Any] | str]:
        """Les entrées dans l'ordre du fichier ; une ligne illisible est rendue telle quelle (chaîne)."""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        yield line
                        continue
                    yield obj if isinstance(obj, dict) else line
        except FileNotFoundError:
            return


# ─────────────────────────────── vérification, export ───────────────────────────────


@dataclass
class Verification:
    ok: bool
    count: int
    last_seq: int
    last_hash: str
    problems: list[str]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def verify(entries: Iterable[dict[str, Any] | str], head: dict[str, Any] | None = None,
           anchor: tuple[int, str] | None = None) -> Verification:
    problems: list[str] = []
    prev_hash, prev_seq, count = GENESIS, 0, 0
    anchored = anchor is None
    for i, e in enumerate(entries, start=1):
        if not isinstance(e, dict):
            problems.append(f"ligne {i} : illisible")
            continue
        count += 1
        seq = e.get("seq")
        if not isinstance(seq, int):
            problems.append(f"ligne {i} : seq absent")
            continue
        if e.get("hash") != entry_hash(e):
            problems.append(f"entrée {seq} : modifiée (empreinte recalculée différente)")
        if seq != prev_seq + 1:
            problems.append(f"entrée {seq} : suit l'entrée {prev_seq} (entrée(s) retirée(s) ou déplacée(s))")
        if e.get("prev") != prev_hash:
            problems.append(f"entrée {seq} : « prev » ne correspond pas à l'entrée précédente (chaîne rompue)")
        if anchor is not None and seq == anchor[0]:
            anchored = True
            if e.get("hash") != anchor[1]:
                problems.append(f"entrée {seq} : différente de l'ancre gardée ({anchor[1][:12]}…)")
        prev_hash, prev_seq = str(e.get("hash")), seq
    if head is not None:
        if int(head.get("seq", 0)) > prev_seq:
            problems.append(f"journal tronqué : la tête annonce l'entrée {head.get('seq')}, la dernière lue est "
                            f"{prev_seq}")
        elif int(head.get("seq", 0)) != prev_seq or head.get("hash") != prev_hash:
            problems.append(f"tête ({head.get('seq')}) différente de la dernière entrée ({prev_seq})")
    if not anchored:
        problems.append(f"ancre {anchor[0]} introuvable (journal tronqué avant elle ?)")
    return Verification(ok=not problems, count=count, last_seq=prev_seq, last_hash=prev_hash, problems=problems)


def _since(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.strip())  # « Z » accepté par fromisoformat (Python ≥ 3.11)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def export(entries: Iterable[dict[str, Any] | str], fmt: str, out: io.TextIOBase, since: str | None = None) -> int:
    """Écrit les entrées (depuis `since`, ISO 8601 ; date seule = minuit UTC) en JSONL (tel quel, donc revérifiable)
    ou en CSV (`CSV_FIELDS`, `args` en JSON). Rend le nombre d'entrées écrites."""
    start = _since(since)
    writer = None
    if fmt == "csv":
        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
    elif fmt != "jsonl":
        raise ValueError(f"format inconnu : {fmt!r} (jsonl | csv)")
    n = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        if start is not None:
            try:
                if datetime.fromisoformat(str(e.get("at"))) < start:
                    continue
            except ValueError:
                continue
        if writer is None:
            out.write(canonical(e) + "\n")
        else:
            writer.writerow({**{k: e.get(k) for k in CSV_FIELDS}, "args": canonical(e.get("args"))})
        n += 1
    return n


# ─────────────────────────────── branchement sur le registre ───────────────────────────────


@dataclass
class AuditedTools:
    """Le registre d'outils du noyau, entouré du journal : tout `call` (tour du modèle, appel direct du propriétaire,
    carte approuvée, sous-agent) dont l'effet n'est pas une lecture — ou qui touche un hôte distant (`remote`) — laisse
    une entrée APRÈS l'exécution. Arguments et aperçu caviardés (`scrub`). Un journal en panne ne casse jamais un
    appel (erreur journalisée)."""

    inner: Any
    log: AuditLog
    scrub: Callable[[str], str] = lambda s: s
    describe: list[Callable[[str, dict[str, Any], str], dict[str, Any] | None]] = field(default_factory=list)
    remote: frozenset[str] = frozenset()

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spec_of = getattr(self.inner, "spec", None)
        spec = spec_of(name) if callable(spec_of) else None
        effect = call_effect(spec, arguments)
        t0 = time.monotonic()
        result = self.inner.call(name, arguments, ctx)
        if effect is not Effect.READ or name in self.remote:
            try:
                self.log.append(self.record(name, arguments, ctx, effect, result, int((time.monotonic() - t0) * 1000)))
            except Exception:
                logger.exception("journal d'audit : ajout en échec (%s)", name)
        return result

    def record(self, name: str, arguments: dict[str, Any], ctx: ToolContext, effect: Effect, result: ToolResult,
               ms: int) -> dict[str, Any]:
        from nokker.approvals.crypto import args_sha256

        extra: dict[str, Any] = {"target": "local"}
        for d in self.describe:
            got = d(name, arguments, ctx.thread_id)
            if got:
                extra = {**extra, **got}
                break
        try:
            args = json.loads(self.scrub(canonical(arguments)))
        except ValueError:
            args = {"_caviardé": self.scrub(canonical(arguments))}
        content = self.scrub(result.content or "")
        return {"thread_id": ctx.thread_id, "turn_index": ctx.turn_index, "channel": ctx.channel,
                "actor": ctx.sender, "tool": name, "effect": effect.value, **extra, "args": _clip_args(args),
                "args_sha256": args_sha256(arguments), "is_error": bool(result.is_error),
                "result_sha256": hashlib.sha256((result.content or "").encode()).hexdigest(),
                "preview": content[:PREVIEW_CHARS], "ms": ms}

    def wrap(self, registry: Any) -> AuditedTools:
        """Le même journal autour d'un autre registre (celui, filtré, d'un sous-agent)."""
        return dataclasses.replace(self, inner=registry)

    def __getattr__(self, name: str) -> Any:  # spec, find, core, index, internal, refresh… : du registre
        if name in ("inner", "log", "scrub", "describe", "remote"):
            raise AttributeError(name)
        return getattr(self.inner, name)


# ─────────────────────────────── commande ───────────────────────────────


def add_audit_subparser(subparsers: Any) -> Any:
    p = subparsers.add_parser("audit", help="journal d'audit chaîné des appels à effet (lot X11) : export, vérification")
    sub = p.add_subparsers(dest="audit_command", required=True)
    exp = sub.add_parser("export", help="exporte le journal (JSONL revérifiable, ou CSV)")
    exp.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    exp.add_argument("--since", default=None, help="à partir de (ISO 8601 : 2026-10-25 ou 2026-10-25T10:00:00Z)")
    ver = sub.add_parser("verify", help="vérifie la chaîne : entrée modifiée, retirée, journal tronqué")
    ver.add_argument("--anchor", default=None, help="ancre gardée ailleurs, « seq:hash » (sortie d'un verify passé)")
    for sp in (exp, ver):
        sp.add_argument("--file", default=None, help="journal (défaut : local.audit_path, sinon "
                                                     "<local.state_dir>/audit/audit.jsonl)")
        sp.set_defaults(func=audit_command, unscoped=True)
    return p


def _path_from_config(args: Any) -> Path:
    if getattr(args, "file", None):
        return Path(args.file).expanduser()
    from nokker.config import load_config

    local = load_config(getattr(args, "config", None)).local or {}
    if local.get("audit_path"):
        return Path(local["audit_path"]).expanduser()
    return default_path(local.get("state_dir") or os.environ.get("NOKKER_HOME") or "~/.config/nokker")


def audit_command(args: Any) -> int:
    import sys

    log = AuditLog(_path_from_config(args))
    if args.audit_command == "export":
        n = export(log.entries(), args.format, sys.stdout, args.since)
        print(f"[{n} entrée(s) exportée(s) de {log.path}]", file=sys.stderr)
        return 0
    anchor = None
    if args.anchor:
        seq, _, h = args.anchor.partition(":")
        anchor = (int(seq), h.strip())
    if not log.path.exists():
        print(f"[journal absent : {log.path}]", file=sys.stderr)
        return 1
    v = verify(log.entries(), log.head(), anchor)
    if v.ok:
        print(f"journal intact : {v.count} entrée(s), ancre {v.last_seq}:{v.last_hash}")
        return 0
    for p in v.problems:
        print(f"[ROMPU] {p}")
    print(f"{len(v.problems)} problème(s) sur {v.count} entrée(s) lue(s)", file=sys.stderr)
    return 1


__all__ = [
    "CSV_FIELDS",
    "GENESIS",
    "AuditLog",
    "AuditedTools",
    "Verification",
    "add_audit_subparser",
    "audit_command",
    "canonical",
    "default_path",
    "entry_hash",
    "export",
    "verify",
]
