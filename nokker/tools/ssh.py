"""nokker/tools/ssh.py — outils ops de Nokker Lokkal : commandes et lecture de fichiers sur les hôtes de l'inventaire
(lot X11, docs/LOKKAL.md §7.14 ; drapeau `local.ops`).

Outils :
  - `ssh_hosts`  : les hôtes de l'inventaire (nom, rôle, adresse, saut, clé d'hôte connue ou non) — READ ;
  - `ssh_run`    : une commande sur un hôte ; effet = celui de la commande, classée comme `terminal`
                   (`classify_command`) ; une commande « hardline » n'est jamais lancée ;
  - `ssh_copy`   : lit un fichier distant (READ), ou le copie dans le dossier de travail (`local_path` : WRITE local) ;
  - `ssh_trust`  : premier contact. Sans `fingerprint` : montre l'empreinte que l'hôte présente (READ). Avec : ajoute
                   CETTE clé à known_hosts, toujours sur carte (l'empreinte est dans les arguments de la carte).

Plancher par rôle (`floor`, branché devant les permissions par `PermissionGates.floors`) :
  - hôte absent de l'inventaire du fil : refus ;
  - clé d'hôte (ou de son saut) absente de known_hosts : refus, avec la marche à suivre (`ssh_trust`) — jamais de
    `StrictHostKeyChecking=no` ni d'`accept-new` vers le known_hosts de l'utilisateur ;
  - `ssh_run` sur un hôte `prod` dont la commande n'est pas une lecture : carte FORTE (`ops:prod`), quel que soit le
    mode, et aucune règle `allow` (session ou « always ») ni GO textuel ne la lève ; les `deny` et le mode plan
    refusent toujours. Une carte `ops:prod` ne se décide pas au jeton local : clé d'appareil (docs/APPROVALS.md) ;
  - `ssh_trust` avec empreinte : carte (`ops:trust_host`), quel que soit le mode et les règles.

La clé privée : lue au coffre (X4, référence `secret:NOM` de l'inventaire) au moment de l'appel, passée au bac à sable
par l'entrée standard (jamais dans la ligne de commande), écrite dans un fichier 0600 d'un dossier temporaire du bac à
sable, effacée à la fin de la commande. Elle n'entre jamais dans un argument d'outil, un résultat ou le contexte du
modèle ; sa valeur est de toute façon caviardée (coffre → `redact_result`). Le client ssh tourne DANS le bac à sable
du fil (comme `terminal`), avec un known_hosts réduit aux lignes des hôtes touchés.
"""
from __future__ import annotations

import base64
import json
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec, call_effect
from nokker.core.turn import Decision, Effect, Gate, ToolCall, ToolResult, Turn, Verdict
from nokker.tools.code import classify_command, hardline_reason
from nokker.tools.hosts import (
    Host,
    Inventory,
    append_known,
    fingerprint,
    known_entries,
    known_lines,
    load_inventory,
    parse_scanned,
)

SSH_TOOLS = ("ssh_hosts", "ssh_run", "ssh_copy", "ssh_trust")
REMOTE_TOOLS = ("ssh_run", "ssh_copy")  # touchent un hôte : toujours au journal d'audit, même en lecture
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600
READ_MAX_BYTES = 20_000  # ssh_copy sans local_path : ce qui entre dans la fenêtre ; au-delà, local_path
READ_HARD_MAX = 25_000
PROD_RULE = "ops:prod"
TRUST_RULE = "ops:trust_host"
SUBAGENT_MARK = "/sa-"

# (thread_id, commande, stdin, délai) -> (code de sortie ou None, sortie) : le bac à sable du fil
# (`SessionCodeTools.run_hook` : docker exec dans le container de la session, ou l'hôte en backend local).
Sandbox = Callable[[str, str, str, int], tuple[int | None, str]]

# Options communes du client : aucune config de l'utilisateur (-F /dev/null), pas d'agent, pas de redirection, pas de
# mot de passe, pas de multiplexage (la clé est effacée après CHAQUE commande), known_hosts fourni par nous seul.
_BASE_OPTS = ("-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
              "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
              "-o", "HashKnownHosts=no", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
              "-o", "ServerAliveCountMax=2", "-o", "ControlMaster=no", "-o", "ControlPath=none",
              "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
              "-o", "PermitLocalCommand=no", "-o", "PasswordAuthentication=no",
              "-o", "KbdInteractiveAuthentication=no", "-o", "GSSAPIAuthentication=no", "-o", "LogLevel=ERROR")
_KH_MARK = "@@nokker-known-hosts@@"


def _specs() -> list[tuple[str, str, Effect, dict[str, Any]]]:
    return [
        ("ssh_hosts", ("Liste les hôtes ssh de l'inventaire de l'utilisateur (nom, rôle prod/preprod/dev, adresse, "
                       "saut, clé d'hôte connue)."), Effect.READ, {"type": "object", "properties": {}}),
        ("ssh_run", ("Lance une commande sur un hôte de l'inventaire, par son nom (ssh). Sur un hôte prod, toute "
                     "commande qui n'est pas une lecture demande une approbation forte."), Effect.DESTRUCTIVE, {
            "type": "object", "required": ["host", "command"], "properties": {
                "host": {"type": "string", "description": "nom de l'hôte (ssh_hosts)"},
                "command": {"type": "string", "description": "commande shell distante"},
                "timeout": {"type": "integer", "description": "secondes (défaut 60, max 600)", "default": 60}}}),
        ("ssh_copy", ("Lit un fichier d'un hôte de l'inventaire ; avec local_path, le copie dans le dossier de "
                      "travail."), Effect.READ, {
            "type": "object", "required": ["host", "remote_path"], "properties": {
                "host": {"type": "string"},
                "remote_path": {"type": "string", "description": "chemin du fichier sur l'hôte"},
                "local_path": {"type": "string", "description": "facultatif : destination relative au dossier de "
                                                                "travail (le fichier n'entre pas dans la réponse)"},
                "max_bytes": {"type": "integer", "description": "lecture : octets rendus (défaut 20000, max 25000)",
                              "default": READ_MAX_BYTES}}}),
        ("ssh_trust", ("Premier contact avec un hôte : sans fingerprint, montre l'empreinte de sa clé ; avec "
                       "fingerprint, l'ajoute à known_hosts après approbation de l'utilisateur."), Effect.READ, {
            "type": "object", "required": ["host"], "properties": {
                "host": {"type": "string"},
                "fingerprint": {"type": "string", "description": "SHA256:… lue avec ssh_trust sans fingerprint"}}}),
    ]


def _effect_of(name: str) -> Callable[[dict[str, Any]], Effect] | None:
    if name == "ssh_run":
        return lambda a: classify_command(str(a.get("command", "")))
    if name == "ssh_copy":
        return lambda a: Effect.WRITE if str(a.get("local_path") or "").strip() else Effect.READ
    if name == "ssh_trust":
        return lambda a: Effect.WRITE if str(a.get("fingerprint") or "").strip() else Effect.READ
    return None


def _q(s: str) -> str:
    return shlex.quote(s)


@dataclass
class SshToolSource:
    """`ToolSource` des outils ops. `sandbox` exécute dans le bac à sable du fil ; `secret(nom)` lit la portée
    `user` du coffre (lève `SecretsError`, message sans valeur) ; `workspace_path(fil, chemin)` donne le chemin d'une
    destination dans le bac à sable (borné au dossier de travail)."""

    sandbox: Sandbox
    secret: Callable[[str], str]
    hosts_path: Path
    known_hosts_path: Path
    workspace_path: Callable[[str, str], str] | None = None
    core: bool = False
    _inventories: dict[str, Inventory] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ── session ──
    def open_session(self, thread_id: str) -> dict[str, Any]:
        """Lit l'inventaire pour CE fil (une fois, à l'ouverture). Rend le bloc `ops` de la réponse d'ouverture."""
        inv = load_inventory(self.hosts_path)
        with self._lock:
            self._inventories[thread_id] = inv
        return {"enabled": True, "hosts": len(inv.hosts), "roles": {r: sum(h.role == r for h in inv.hosts.values())
                                                                      for r in ("prod", "preprod", "dev")},
                "notices": inv.notices}

    def inventory(self, thread_id: str) -> Inventory | None:
        """L'inventaire du fil ; un sous-agent (`<fil>/sa-<id>`) a celui de son fil parent."""
        with self._lock:
            return self._inventories.get(thread_id.split(SUBAGENT_MARK, 1)[0])

    def known(self, host: Host) -> bool:
        return bool(known_entries(self.known_hosts_path, host.known_key))

    def specs(self) -> list[ToolSpec]:
        # `fresh` : une lecture distante change sans qu'aucun outil d'ici n'écrive (état d'un service, journal) :
        # jamais resservie par le cache des lectures du tour.
        return [ToolSpec(name=n, description=d, parameters=p, effect=e, core=self.core, source="builtin:ssh",
                         effect_of=_effect_of(n), fresh=n != "ssh_hosts") for n, d, e, p in _specs()]

    # ── plancher (avant les permissions) ──
    def floor(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> Verdict | None:
        """Verdict imposé quel que soit le mode et les règles (DENY ou HITL), ou None : les permissions décident."""
        if call.name not in SSH_TOOLS or call.name == "ssh_hosts":
            return None

        def verdict(decision: Decision, rule: str, reason: str, strength: str | None = None) -> Verdict:
            return Verdict(gate=Gate.ACTION, decision=decision, layer=1, rule=rule, reason=reason, subject=call.id,
                           strength=strength)

        inv = self.inventory(getattr(turn, "thread_id", "") or "")
        if inv is None:
            return verdict(Decision.DENY, "ops:no_session", "pas d'inventaire d'hôtes sur ce fil (session de code)")
        name = str(call.arguments.get("host") or "").strip()
        host = inv.get(name)
        if host is None:
            known = ", ".join(inv.names()) or "aucun"
            return verdict(Decision.DENY, "ops:unknown_host", f"hôte inconnu de l'inventaire : {name!r} (hôtes : {known})")
        if call.name == "ssh_trust":
            fp = str(call.arguments.get("fingerprint") or "").strip()
            if not fp:
                return None
            return verdict(Decision.HITL, TRUST_RULE,
                           f"premier contact : faire confiance à la clé {fp} de {host.name} ({host.address}:"
                           f"{host.port}, {host.role}) ? Vérifie l'empreinte par un autre moyen.", "standard")
        missing = [h.name for h in inv.chain(host) if not self.known(h)]
        if missing:
            return verdict(Decision.DENY, "ops:first_contact",
                           f"clé d'hôte de {', '.join(missing)} absente de known_hosts : appelle ssh_trust "
                           f"{{host: {missing[0]!r}}} pour voir son empreinte, l'utilisateur l'approuve, puis reprends")
        if call.name == "ssh_run":
            command = str(call.arguments.get("command") or "")
            why = hardline_reason(command)
            if why:
                return verdict(Decision.DENY, "ops:hardline", why)
            effect = call_effect(spec, call.arguments)
            if host.role == "prod" and effect is not Effect.READ:
                return verdict(Decision.HITL, PROD_RULE, f"hôte de prod {host.name} : commande à effet "
                                                          f"{effect.value} ({command[:200]})", "strong")
        return None

    def describe(self, name: str, arguments: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
        """Champs du journal d'audit pour un appel ssh (cible, rôle, adresse)."""
        if name not in SSH_TOOLS:
            return None
        inv = self.inventory(thread_id)
        host = inv.get(str(arguments.get("host") or "")) if inv is not None else None
        if host is None:
            return {"target": f"ssh:{arguments.get('host')}"}
        return {"target": f"ssh:{host.name}", "role": host.role, "address": f"{host.address}:{host.port}"}

    # ── appels ──
    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(payload: Any, error: bool = False) -> ToolResult:
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        if name not in SSH_TOOLS:
            return out(f"outil inconnu : {name}", True)
        inv = self.inventory(ctx.thread_id)
        if inv is None:
            return out("pas d'inventaire d'hôtes sur ce fil (POST /v1/local/sessions)", True)
        if name == "ssh_hosts":
            return out({"hosts": [{**h.view(), "host_key_known": self.known(h)} for h in inv.hosts.values()],
                        "notices": inv.notices})
        host = inv.get(str(arguments.get("host") or ""))
        if host is None:  # défense en profondeur : le plancher a déjà refusé
            return out(f"REFUSÉ : hôte inconnu de l'inventaire ({', '.join(inv.names()) or 'aucun hôte'})", True)
        try:
            if name == "ssh_trust":
                return self._trust(ctx.thread_id, inv, host, str(arguments.get("fingerprint") or "").strip(), out)
            missing = [h.name for h in inv.chain(host) if not self.known(h)]
            if missing:
                return out(f"REFUSÉ : clé d'hôte de {', '.join(missing)} absente de known_hosts (ssh_trust)", True)
            if name == "ssh_run":
                return self._run(ctx.thread_id, inv, host, arguments, out)
            return self._copy(ctx.thread_id, inv, host, arguments, out)
        except PermissionError as e:
            return out(f"REFUSÉ : {e}", True)
        except Exception as e:  # noqa: BLE001 — l'erreur revient au modèle (sans valeur de secret : SecretsError)
            return out(f"{type(e).__name__}: {e}", True)

    # ── construction du script du bac à sable ──
    def _target_opts(self, host: Host, key_file: str, kh_file: str, strict: str) -> list[str]:
        """Options d'une cible, en mots déjà prêts pour bash (les chemins `$d/…` restent entre guillemets doubles)."""
        return [*map(_q, _BASE_OPTS), f'"-oIdentityFile={key_file}"', f'"-oUserKnownHostsFile={kh_file}"',
                f"-oStrictHostKeyChecking={strict}", "-p", str(host.port), "-l", _q(host.user)]

    def _proxy(self, jump: Host) -> str:
        """`ProxyCommand` du saut : même client, clé du saut, known_hosts réduit (le saut doit y être)."""
        inner = ["ssh", *_BASE_OPTS, "-oIdentityFile=$d/j", "-oUserKnownHostsFile=$d/h",
                 "-oStrictHostKeyChecking=yes", "-p", str(jump.port), "-l", jump.user, "-W", "%h:%p", jump.address]
        return f'"-oProxyCommand={" ".join(inner)}"'

    def _script(self, inv: Inventory, host: Host, *, remote: str | None, timeout: int, tail: str = "",
                scan: bool = False) -> tuple[str, str, list[tuple[str, str]]]:
        """(script bash, entrée standard, clés utilisées). L'entrée porte les clés privées, rien d'autre ; le script
        les écrit dans un dossier temporaire 0700 (fichiers 0600), lance ssh, puis efface le dossier."""
        chain = inv.chain(host)
        jump = chain[0] if len(chain) == 2 else None
        payload, lines, used = "", [], []
        keys = [("k", host)] + ([("j", jump)] if jump is not None else [])
        if scan:  # premier contact : pas de clé pour l'hôte lui-même (aucune authentification n'est tentée)
            keys = [("j", jump)] if jump is not None else []
        for fname, h in keys:
            value = self.secret(h.key)
            used.append((h.key, value))
            value = value if value.endswith("\n") else value + "\n"
            data = value.encode()
            payload += value
            lines.append(f'dd bs=1 count={len(data)} of="$d/{fname}" status=none 2>/dev/null')
        kh = known_lines(self.known_hosts_path, [h.known_key for h in chain if not (scan and h is host)])
        lines.append(f"printf '%s' {_q(base64.b64encode(kh.encode()).decode())} | base64 -d > \"$d/h\"")
        if scan:
            opts = [*map(_q, _BASE_OPTS), '"-oUserKnownHostsFile=$d/s"', "-oStrictHostKeyChecking=accept-new",
                    "-oPubkeyAuthentication=no", "-oPreferredAuthentications=none", "-p", str(host.port),
                    "-l", _q(host.user)]
        else:
            opts = self._target_opts(host, "$d/k", "$d/h", "yes")
        if jump is not None:
            opts.append(self._proxy(jump))
        ssh = " ".join(["$T", "ssh", *opts, "-n", "-T", _q(host.address), *([_q(remote)] if remote else [])])
        body = "\n".join([
            "umask 077",
            ('d=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/.nokker-ssh.XXXXXXXX") || '
             '{ echo "nokker: dossier temporaire impossible dans le bac à sable" >&2; exit 97; }'),
            "trap 'rm -rf \"$d\"' EXIT HUP INT TERM",
            *lines,
            f'T=""; command -v timeout >/dev/null 2>&1 && T="timeout -k 5 {int(timeout)}"',
            ssh + (" " + tail if tail else ""),
            "rc=$?",
            *([f'echo {_KH_MARK}; cat "$d/s" 2>/dev/null'] if scan else []),
            'rm -rf "$d"',
            "exit $rc",
        ])
        return f"(\n{body}\n)", payload, used

    def _exec(self, thread_id: str, prepared: tuple[str, str, list[tuple[str, str]]],
              timeout: int) -> tuple[int | None, str]:
        """Lance le script ; la sortie est rendue sans les clés utilisées (un hôte hostile qui renverrait la clé :
        valeur entière et chacune de ses lignes remplacées par `[secret:NOM]`, avant même le caviardage du noyau,
        qui ne la reconnaîtrait plus une fois le résultat encodé en JSON, retours à la ligne compris)."""
        script, stdin, used = prepared
        rc, output = self.sandbox(thread_id.split(SUBAGENT_MARK, 1)[0], script, stdin, timeout + 20)
        for name, value in used:
            for piece in sorted({value.strip(), *(ln.strip() for ln in value.splitlines() if len(ln.strip()) >= 16)},
                                key=len, reverse=True):
                if piece:
                    output = output.replace(piece, f"[secret:{name}]")
        return rc, output

    @staticmethod
    def _hint(rc: int | None, output: str) -> str | None:
        if "Host key verification failed" in output or "REMOTE HOST IDENTIFICATION HAS CHANGED" in output:
            return ("la clé présentée ne correspond pas à known_hosts : possible interception. Ne contourne pas ; "
                    "l'utilisateur vérifie et corrige known_hosts lui-même (ssh_trust ne remplace jamais une clé)")
        if rc == 255 and ("Permission denied" in output or "Connection refused" in output
                          or "timed out" in output or "Could not resolve" in output):
            return "connexion ou authentification ssh en échec (code 255) : hôte, port, utilisateur ou clé au coffre"
        if rc == 124:
            return "délai dépassé : la commande a été arrêtée"
        if "No user exists for uid" in output:
            return ("le bac à sable ne connaît pas l'uid de l'utilisateur et le client ssh refuse de tourner : limite "
                    "connue de l'image du bac à sable (uid 1000 seulement), à signaler à l'utilisateur")
        return None

    def _run(self, thread_id: str, inv: Inventory, host: Host, a: dict[str, Any], out: Any) -> ToolResult:
        command = str(a.get("command") or "").strip()
        if not command:
            return out("commande vide", True)
        why = hardline_reason(command)
        if why:
            return out(f"REFUSÉ, jamais exécuté même approuvé : {why}", True)
        timeout = max(1, min(int(a.get("timeout") or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
        rc, output = self._exec(thread_id, self._script(inv, host, remote=command, timeout=timeout, tail="2>&1"),
                                timeout)
        result: dict[str, Any] = {"host": host.name, "role": host.role, "returncode": rc, "output": output}
        if (hint := self._hint(rc, output)) is not None:
            result["hint"] = hint
        return out(result)

    def _copy(self, thread_id: str, inv: Inventory, host: Host, a: dict[str, Any], out: Any) -> ToolResult:
        remote = str(a.get("remote_path") or "").strip()
        if not remote:
            return out("remote_path est requis", True)
        local = str(a.get("local_path") or "").strip()
        if local:
            if self.workspace_path is None:
                return out("copie vers le dossier de travail indisponible sur ce fil", True)
            dest = self.workspace_path(thread_id, local)
            part = dest + ".nokker-part"
            tail = (f"> {_q(part)} 2>\"$d/e\"; rc=$?; if [ $rc -eq 0 ]; then mv -f -- {_q(part)} {_q(dest)} && "
                    f"wc -c < {_q(dest)}; else rm -f -- {_q(part)}; cat \"$d/e\"; fi; (exit $rc)")
            rc, output = self._exec(thread_id, self._script(inv, host, remote=f"cat -- {_q(remote)}",
                                                            timeout=MAX_TIMEOUT_S, tail=tail), MAX_TIMEOUT_S)
            if rc != 0:
                return out({"host": host.name, "remote_path": remote, "returncode": rc, "error": output.strip(),
                            **({"hint": h} if (h := self._hint(rc, output)) else {})}, True)
            size = output.strip().splitlines()[-1] if output.strip() else "?"
            return out({"host": host.name, "remote_path": remote, "local_path": local, "bytes": size})
        limit = max(1, min(int(a.get("max_bytes") or READ_MAX_BYTES), READ_HARD_MAX))
        tail = '2>"$d/e"; rc=$?; [ $rc -eq 0 ] || cat "$d/e"; (exit $rc)'
        rc, output = self._exec(thread_id, self._script(inv, host, remote=f"head -c {limit + 1} -- {_q(remote)}",
                                                        timeout=120, tail=tail), 120)
        if rc != 0:
            return out({"host": host.name, "remote_path": remote, "returncode": rc, "error": output.strip(),
                        **({"hint": h} if (h := self._hint(rc, output)) else {})}, True)
        if "\x00" in output:
            return out({"host": host.name, "remote_path": remote,
                        "error": "fichier binaire : copie-le dans le dossier de travail (local_path)"}, True)
        truncated = len(output.encode()) > limit
        content = output.encode()[:limit].decode(errors="replace") if truncated else output
        return out({"host": host.name, "remote_path": remote, "truncated": truncated, "content": content})

    def _scan(self, thread_id: str, inv: Inventory, host: Host) -> list[tuple[str, str, str]]:
        _, output = self._exec(thread_id, self._script(inv, host, remote=None, timeout=20, tail="2>&1", scan=True), 20)
        scanned = output.split(_KH_MARK, 1)[1] if _KH_MARK in output else ""
        return [(k, t, b) for k, t, b in parse_scanned(scanned) if k == host.known_key]

    def _trust(self, thread_id: str, inv: Inventory, host: Host, fp: str, out: Any) -> ToolResult:
        jump = inv.chain(host)[0] if host.jump else None
        if jump is not None and not self.known(jump):
            return out(f"le saut {jump.name} n'est pas encore dans known_hosts : ssh_trust {{host: {jump.name!r}}} "
                       "d'abord", True)
        existing = known_entries(self.known_hosts_path, host.known_key)
        scanned = self._scan(thread_id, inv, host)
        if not fp:
            if not scanned:
                return out({"host": host.name, "error": "aucune clé d'hôte reçue (hôte injoignable ?)"}, True)
            return out({"host": host.name, "address": f"{host.address}:{host.port}", "role": host.role,
                        "known": bool(existing),
                        "keys": [{"type": t, "fingerprint": fingerprint(b),
                                  "known": any(b == eb for _, eb in existing)} for _, t, b in scanned],
                        "next": "montre l'empreinte à l'utilisateur, puis ssh_trust {host, fingerprint} : il "
                                "l'approuve sur la carte"})
        if existing:
            return out(f"REFUSÉ : {host.name} a déjà une clé dans known_hosts ; un changement de clé se règle à la "
                       "main par l'utilisateur (peut-être une interception)", True)
        match = [(t, b) for _, t, b in scanned if fingerprint(b) == fp]
        if not match:
            seen = ", ".join(fingerprint(b) for _, _, b in scanned) or "aucune"
            return out(f"REFUSÉ : l'hôte ne présente plus l'empreinte approuvée {fp} (présentée : {seen})", True)
        key_type, key_b64 = match[0]
        append_known(self.known_hosts_path, host.known_key, key_type, key_b64)
        return out({"host": host.name, "trusted": True, "fingerprint": fp, "type": key_type})


def ssh_subject(call_name: str, arguments: dict[str, Any]) -> str | None:
    """Sujet d'une règle de permission pour un outil ssh : `hôte:commande` ou `hôte:chemin` (ex. `ssh_run(web1:git
    status*)`)."""
    host = str(arguments.get("host") or "").strip()
    if call_name == "ssh_run":
        return f"{host}:{' '.join(str(arguments.get('command') or '').split())}"
    if call_name == "ssh_copy":
        return f"{host}:{str(arguments.get('remote_path') or '').strip()}"
    if call_name == "ssh_trust":
        return host
    return None


def is_prod_card(rule: str | None) -> bool:
    """Une carte du plancher prod : décidée par une clé d'appareil seulement (jamais au jeton local)."""
    return bool(rule) and (rule == PROD_RULE or rule.startswith(PROD_RULE + ":"))


__all__ = ["PROD_RULE", "REMOTE_TOOLS", "SSH_TOOLS", "TRUST_RULE", "SshToolSource", "is_prod_card", "ssh_subject"]
