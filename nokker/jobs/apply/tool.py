"""`jobs_apply` (Lokkal seulement) : dépôt d'UNE candidature sur le portail de l'employeur (docs/JOBS-PACK.md §5.6, §6,
§7).

Au registre seulement sur une machine qui a les sites connectés ET le pack Emploi (profil emploi actif) ; ailleurs
l'outil n'existe pas (§10). Chemin d'un appel :
1. appel du modèle (`offer`, `cv`, `letter`, `answers`) : vérifications du §7.2 (offre, canal, doublon, quota, lettre
   complète), puis relevé du formulaire SANS rien envoyer (`JobsDesk.scan`) → `questions` (groupées, clôturées) ou
   `pending_approval` : l'outil pose LUI-MÊME la carte, dont les arguments portent l'id et l'empreinte du plan ;
2. approbation de la personne : l'exécuteur des cartes rappelle l'outil avec ces arguments et `approval_id` ; le plan
   est rejoué exactement (`JobsDesk.submit`), la preuve capturée, le dépôt enregistré chez le service emploi
   (`POST /applications/{id}/deposit`). Sans preuve : `unconfirmed`, jamais renvoyé.

La porte `JobsApplyGates` laisse passer l'appel du MODÈLE (un relevé n'envoie rien ; seuls les refus du noyau —
mode plan, règle `deny` — l'arrêtent) : l'envoi n'a lieu que par la carte que l'outil pose, jamais par une règle
`allow`, un mode `auto` ou un relecteur automatique.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nokker.core.ports import APPROVED_LIVE, ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, Gate, ToolCall, ToolResult, Verdict
from nokker.jobs.apply.desk import DeskError, JobsDesk, Offer, Piece, ScanResult
from nokker.jobs.apply.model import FilePart, Plan
from nokker.jobs.apply.portals import BEYOND
from nokker.jobs.localtime import TZ, day, local
from nokker.jobs.texts_pending import PENDING
from nokker.jobs.texts_v2 import APPLY, CARD, ERRORS, TOOLS, count, de, err
from nokker.qa.untrusted import inline

logger = logging.getLogger("nokker.jobs.apply")

TOOL = "jobs_apply"
_EMAIL_CHANNELS = {"hr_email", "hr_email_predicted", "email"}
_MANUAL_CHANNELS = {"manual", "external", "external_link", "none", "jobup", "jobcloud", "linkedin"}
_SECRET_ARGS = ("plan", "plan_sha256")
INCOMPLETE = "[à compléter]"


class _Fail(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def mask_email(addr: str) -> str:
    """« n…@domaine » : l'identifiant du compte de portail tel que la carte et le modèle le voient."""
    local, _, dom = str(addr or "").partition("@")
    return f"{local[:1]}…@{dom}" if dom else "…"


def _when(ts: float) -> str:
    return local(ts).strftime("%H:%M")


@dataclass
class _Ctx:
    person: Any
    account: str  # compte partenaire (service emploi)
    sender: str
    offer: Offer
    pieces: dict[str, Piece]
    username: str
    warnings: list[str]


class JobsApplySource:
    """Source `jobs_apply` (locality `local`). `jobs` : la source du pack (`JobsSource`, service emploi) ; `desk` :
    `jobsd` (sites connectés)."""

    def __init__(self, jobs: Any, desk: JobsDesk, *, scan_wait_s: float = 240.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.jobs, self.desk = jobs, desk
        self.scan_wait_s, self.clock = scan_wait_s, clock
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jobsd-scan")
        self._scans: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._register_lock = threading.RLock()  # un seul enregistrement à la fois (lecture, dépôt, tâche de fond)
        # lecture de la source du pack (jobs_applications, jobs_report) : retenter les enregistrements en attente
        if isinstance(getattr(jobs, "before_read", None), list):
            jobs.before_read.append(self._on_read)
        if isinstance(getattr(jobs, "pending_deposits", None), list):
            jobs.pending_deposits.append(self._pending_items)
        if isinstance(getattr(jobs, "on_registered", None), list):
            jobs.on_registered.append(self._on_registered)
        if isinstance(getattr(jobs, "stored_deposit", None), list):
            jobs.stored_deposit.append(self._register_stored)

    # ── registre ──
    def available_for(self, tenant_id: str) -> bool:
        return bool(self.jobs.available_for(tenant_id))

    def specs(self) -> list[ToolSpec]:
        from nokker.tenancy.context import current_tenant_or_none

        tid = current_tenant_or_none()
        if tid is None or not self.jobs.available_for(tid):
            return []
        desc, params = TOOLS[TOOL]
        return [ToolSpec(name=TOOL, description=desc, parameters=params, effect=Effect.SEND_EXTERNAL,
                         source="builtin", locality="local")]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name != TOOL:
            return ToolResult(call_id="", name=name, content=f"argument_invalide : outil inconnu : {name}",
                              is_error=True)
        a = arguments if isinstance(arguments, dict) else {}
        try:
            if ctx.approval_id:
                text = self._approved(a, ctx)
            else:
                if any(k in a for k in _SECRET_ARGS):
                    raise _Fail(APPLY["plan_arg"])
                text = self._prepare(a, ctx)
        except _Fail as e:
            return ToolResult(call_id="", name=name, content=e.text, is_error=True)
        except DeskError as e:
            return ToolResult(call_id="", name=name, content=self._desk_text(e, a), is_error=True)
        except Exception as e:  # noqa: BLE001 — jamais une trace vers le modèle
            from nokker.jobs.source import JobsFail

            if isinstance(e, JobsFail):
                return ToolResult(call_id="", name=name, content=e.text, is_error=True)
            logger.warning("jobs_apply en échec", exc_info=True)
            return ToolResult(call_id="", name=name, content=err("service_indisponible"), is_error=True)
        return ToolResult(call_id="", name=name, content=text)

    # ── vérifications du §7.2, pièces ──
    def _context(self, a: dict[str, Any], ctx: ToolContext) -> _Ctx:
        from nokker.jobs.source import _safe_file

        jobs = self.jobs
        people = jobs._people()
        offer_ref = str(a.get("offer") or "").strip()
        p, ident = jobs._ref(offer_ref, "offer", people)
        if a.get("person"):
            who = jobs._resolve(a.get("person"), people)
            if who is not p:
                raise _Fail(err("person_forbidden", person=who.label))
        if p.minor:
            raise _Fail(err("mineur"))
        oid = jobs._num(ident)
        account = jobs._account(p)
        sender = ctx.sender or ""
        jobs._check_quota(sender, p)
        data = jobs._call(sender, lambda: jobs.client.request("GET", f"/offers/{oid}", account=account)).data or {}
        o, ch = data.get("offer") or {}, data.get("channel") or {}
        channel = str(ch.get("channel") or "")
        if channel in _EMAIL_CHANNELS:
            raise _Fail(APPLY["email_channel"])
        portal_name = inline(ch.get("display_name") or channel or "le site de l'employeur", 60)
        if channel in _MANUAL_CHANNELS:
            raise _Fail(err("canal_manuel", portal=portal_name))
        offer = Offer(ref=f"{p.profile.id}|offer:{oid}", id=oid, url=str(o.get("url") or ""),
                      company=str(o.get("company") or ""), title=str(o.get("title") or ""),
                      location=str(o.get("location") or ""), ats=channel)
        if self.desk.recipe_for(offer) is None:
            raise _Fail(err("canal_manuel", portal=portal_name))
        warnings = self._duplicates(p, account, sender, offer)
        pieces: dict[str, Piece] = {}
        cv = str(a.get("cv") or "").strip()
        if not cv:
            raise _Fail(err("cv_absent", person=p.label))
        who = re.sub(r"\s+", "_", p.label)
        if cv.startswith("cvl_"):
            owner, d = jobs._local_doc(cv, "cv", people)
            if owner is not p:
                raise _Fail(err("ref_unknown"))
            pdf, facts = d.pdf, list(d.data.get("new_facts") or [])
        else:
            owner, cv_id = jobs._ref(cv, "cv", people)
            if owner is not p:
                raise _Fail(err("ref_unknown"))
            meta = jobs._service_cv(sender, account, cv_id)
            pdf = jobs._call(sender, lambda: jobs.client.fetch_signed(str(meta.get("pdf_url") or ""))).content
            facts = []
        pieces["cv"] = Piece(FilePart("cv", cv, _safe_file(who, "CV", offer.company), hashlib.sha256(pdf).hexdigest(),
                                      len(pdf), _pages(pdf), facts), pdf)
        letter = str(a.get("letter") or "").strip()
        if letter:
            if not letter.startswith("lt_"):
                raise _Fail(APPLY["letter_ref"].format(offer=offer.ref))
            owner, d = jobs._local_doc(letter, "letter", people)
            if owner is not p:
                raise _Fail(err("ref_unknown"))
            text = str(d.data.get("text") or "")
            if INCOMPLETE in text:
                line = next((ln for ln in text.splitlines() if INCOMPLETE in ln), "")
                field = inline(line.replace(INCOMPLETE, "").strip(" :—-") or "champ", 40)
                raise _Fail(err("letter_incomplete", field=field))
            pieces["letter"] = Piece(FilePart("letter", letter, _safe_file(who, "Lettre", offer.company),
                                              hashlib.sha256(d.pdf).hexdigest(), len(d.pdf), _pages(d.pdf),
                                              list(d.data.get("new_facts") or [])), d.pdf)
        answers = a.get("answers") if isinstance(a.get("answers"), dict) else {}
        sheet = self.desk.sheets.get(p.account_id, p.profile.id)
        username = str(answers.get("email") or sheet["fields"].get("email") or "").strip()
        return _Ctx(p, account, sender, offer, pieces, username, warnings)

    def _duplicates(self, p: Any, account: str, sender: str, offer: Offer) -> list[str]:
        """Doublon (même offre, ou même entreprise + titre proche dans les 60 jours) → `already_applied` ; 3
        candidatures ou plus dans l'année chez la même entreprise → avertissement sur la carte (§7.2)."""
        mine = self.desk.sent_for(p.account_id, offer.ref)
        if mine is not None:
            raise _Fail(err("already_applied", person=p.label, date=local(mine["at"]).strftime(
                "%d.%m"), proof="page capturée" if mine.get("status") == "sent" else "sans preuve"))
        now = self.clock()
        same_company = 0
        for item in self.jobs._pages(sender, account, "/applications", {}):
            applied = item.get("applied_at") or ""
            if not (applied or item.get("apply_method")):
                continue
            when = _date(applied) or now
            proof = "dépôt enregistré" if item.get("apply_method") == "nokker_lokkal" else "déclarée"
            if item.get("offer_id") == offer.id or (
                    _fold(item.get("company")) == _fold(offer.company) and _close(item.get("position"), offer.title)
                    and now - when <= timedelta(days=60)):
                raise _Fail(err("already_applied", person=p.label, date=when.astimezone(TZ).strftime("%d.%m"),
                                proof=proof))
            if _fold(item.get("company")) == _fold(offer.company) and now - when <= timedelta(days=365):
                same_company += 1
        return [CARD["warning"].format(n=same_company, company=inline(offer.company, 80))] if same_company >= 3 else []

    # ── 1. préparation : relevé à blanc puis carte ──
    def _prepare(self, a: dict[str, Any], ctx: ToolContext) -> str:
        c = self._context(a, ctx)
        self._retry_registrations(c)
        answers = {str(k): str(v) for k, v in (a.get("answers") or {}).items()} \
            if isinstance(a.get("answers"), dict) else {}
        key = hashlib.sha256(repr(sorted({k: str(v) for k, v in a.items()}.items())).encode()).hexdigest()
        with self._lock:
            fut = self._scans.get(key)
            if fut is None or (fut.done() and fut.exception() is not None):
                fut = self._pool.submit(self.desk.scan, account=c.person.account_id, profile=c.person.profile.id,
                                        offer=c.offer, pieces=c.pieces, answers=answers, username=c.username)
                self._scans[key] = fut
        try:
            res = fut.result(timeout=self.scan_wait_s)
        except TimeoutError:
            return APPLY["preparing"].format(company=inline(c.offer.company, 80),
                                             portal=inline(self.desk.recipe_for(c.offer).name, 60))
        finally:
            if fut.done():
                with self._lock:
                    self._scans.pop(key, None)
        return self._after_scan(res, c, a, ctx)

    def _after_scan(self, res: ScanResult, c: _Ctx, a: dict[str, Any], ctx: ToolContext, prefix: str = "") -> str:
        from nokker.jobs.source import _Out

        plan = res.plan
        plan.warnings = list(c.warnings)
        if res.status == "questions":
            out = _Out()
            out.line(prefix + APPLY["questions"].format(portal=inline(plan.portal, 60),
                                                         de_company=de(inline(plan.company, 80)),
                                                         reponses=count("reponses", len(res.questions)),
                                                         person=c.person.label))
            for q in res.questions:
                out.line(APPLY["question"].format(
                    id=q.id, kind=q.kind, required=APPLY["required"] if q.required else "",
                    options=APPLY["options"].format(options=" | ".join(inline(o, 60) for o in q.options))
                    if q.options else "", sensitive=APPLY["sensitive"] if q.sensitive else ""))
                out.data("jobs:portal:question", q.label)
            return out.render()
        self._card(ctx, plan, a)
        if res.status == "account":
            return prefix + APPLY["pending_account"].format(person=c.person.label, portal=inline(plan.portal, 60),
                                                            company=inline(plan.company, 80),
                                                            username_masque=inline(mask_email(plan.username), 120))
        text = prefix + APPLY["pending_approval"].format(
            person=c.person.label, company=inline(plan.company, 80), title=inline(plan.title, 120),
            portal=inline(plan.portal, 60), account=self._account_text(plan),
            champs=count("champs", sum(1 for f in plan.fields if f.kind in ("text", "choice") and f.value)),
            pieces=count("pieces", len(plan.files)),
            consentements=count("consentements", sum(1 for f in plan.fields
                                                     if f.kind == "check" and f.value == "true")))
        if res.answered:
            text += " " + APPLY["to_sheet"].format(person=c.person.label, ids=", ".join(sorted(res.answered)))
        return text

    @staticmethod
    def _account_text(plan: Plan) -> str:
        if plan.account == "guest":
            return APPLY["account_guest"]
        return APPLY["account_create" if plan.account == "create" else "account_login"].format(
            username=inline(mask_email(plan.username), 120))

    def _card(self, ctx: ToolContext, plan: Plan, a: dict[str, Any]) -> None:
        kernel = ctx.kernel
        approvals = getattr(kernel, "approvals", None)
        if approvals is None or ctx.turn is None:
            raise _Fail(APPLY["card_gone"])
        args = {k: a[k] for k in ("offer", "cv", "letter", "answers", "person") if a.get(k) not in (None, "", {})}
        args.update(plan=plan.id, plan_sha256=plan.sha256())
        call = ToolCall(id=f"call_{plan.id}", name=TOOL, arguments=args)
        strong = bool(plan.strong)
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=f"jobs:apply:{plan.ats}",
                          reason=card_text(plan, self.jobs_person_label(plan)), subject=call.id,
                          strength="strong" if strong else "standard")
        approvals.request(ctx.turn, call, verdict, None)
        pending = getattr(ctx.turn, "pending_approvals", None)
        if isinstance(pending, list):
            pending.append(call)

    def jobs_person_label(self, plan: Plan) -> str:
        for p in self.jobs._people():
            if p.profile is not None and p.profile.id == plan.profile:
                return p.label
        return ""

    # ── 2. approbation : rejeu exact, preuve, enregistrement ──
    def _approved(self, a: dict[str, Any], ctx: ToolContext) -> str:
        from nokker.approvals.crypto import args_sha256

        approvals = getattr(ctx.kernel, "approvals", None)
        appr = approvals.get(ctx.approval_id) if approvals is not None else None
        if appr is None or appr.status not in APPROVED_LIVE or appr.tool_name != TOOL \
                or appr.args_sha256 != args_sha256(a) or not a.get("plan"):
            raise _Fail(APPLY["card_gone"])
        c = self._context({k: v for k, v in a.items() if k not in _SECRET_ARGS}, ctx)
        plan = self.desk.plans.get(c.person.account_id, str(a["plan"]))
        if plan is None or plan.sha256() != a.get("plan_sha256") or plan.offer_ref != c.offer.ref:
            raise _Fail(APPLY["card_gone"])
        answers = {str(k): str(v) for k, v in (a.get("answers") or {}).items()} \
            if isinstance(a.get("answers"), dict) else {}
        if plan.kind == "account":
            res = self.desk.create_account(plan, c.offer, c.pieces, answers)
            made = APPLY["account_made"].format(portal=inline(plan.portal, 60), company=inline(plan.company, 80),
                                                username=inline(mask_email(plan.username), 120))
            return self._after_scan(res, c, {k: v for k, v in a.items() if k not in _SECRET_ARGS}, ctx, prefix=made)
        out = self.desk.submit(plan, c.pieces)
        if out.status != "sent":
            raise _Fail(err("unconfirmed", company=inline(plan.company, 80)))
        aid = self._register(c, plan, out)
        recipe = self.desk.recipes[plan.ats]
        proof = APPLY["proof_page"].format(heading=inline(recipe.labels["done"], 80))
        if out.proof.get("ack_message_id"):
            proof += APPLY["proof_ack"].format(time=_when(time.time()))
        ref = f"{plan.profile}|app:{aid}" if aid else out.deposit
        text = APPLY["done" if aid else "done_pending"].format(
            company=inline(plan.company, 80), title=inline(plan.title, 120), portal=inline(plan.portal, 60),
            account=self._account_text(plan), proof=proof, ref=ref)
        return text if aid else text + " " + PENDING["registered_later"]

    def _register(self, c: _Ctx, plan: Plan, out: Any) -> int | None:
        """Dépôt enregistré chez le service emploi (§13.7) : type de preuve, heure, empreintes ; jamais la capture. En
        échec, le dépôt reste « en attente d'enregistrement » et est retenté (`retry_pending`)."""
        from nokker.tenancy.context import current_tenant_or_none

        # tout ce qu'il faut pour retenter sans la carte ni le contexte de l'appel (tâche de fond)
        self.desk.journal.append(plan.owner_account, {
            "deposit": out.deposit, "event": "register_payload", "offer_id": plan.offer_id, "ats": plan.ats,
            "files": {f.role: f.sha256 for f in plan.files}, "partner_account": c.account,
            "tenant": current_tenant_or_none() or ""})
        with self._register_lock:
            try:
                aid = self._post_deposit(plan.owner_account, out.deposit, c.account, c.sender,
                                         {"offer_id": plan.offer_id, "ats": plan.ats, "proof": out.proof,
                                          "files": {f.role: f.sha256 for f in plan.files}})
            except Exception:  # noqa: BLE001 — le dépôt est fait ; l'enregistrement est retenté (lecture, tâche de fond)
                logger.warning("jobs_apply : enregistrement du dépôt %s en attente", out.deposit, exc_info=True)
                self.desk.mark_registered(plan.owner_account, out.deposit, None)
                return None
            self.desk.mark_registered(plan.owner_account, out.deposit, aid)
        return aid

    def _post_deposit(self, account: str, deposit: str, partner_account: str, sender: str,
                      d: dict[str, Any]) -> int:
        """`POST /applications` (idempotent par offre) puis `POST /applications/{id}/deposit`. L'id de candidature
        obtenu est journalisé avant le second appel : une relance ne recrée rien. Sans preuve, rien n'est enregistré."""
        proof = d.get("proof") or {}
        if not proof.get("kind") or not proof.get("captured_at"):
            raise ValueError("preuve absente")
        if not partner_account:
            raise ValueError("compte partenaire inconnu")

        def call(fn: Callable[[], Any]) -> Any:
            return self.jobs._call(sender, fn) if sender else fn()

        aid = d.get("application_id")
        if not isinstance(aid, int):
            created = call(lambda: self.jobs.client.request("POST", "/applications", account=partner_account,
                                                             body={"offer_id": d["offer_id"]})).data or {}
            aid = created.get("application_id")
            if not isinstance(aid, int):
                raise ValueError("application_id")
            self.desk.journal.append(account, {"deposit": deposit, "event": "application_linked",
                                               "application_id": aid})
        at = local(proof["captured_at"]).isoformat()  # heure suisse avec décalage : le jour du relevé ORP en dépend
        files = d.get("files") or {}
        body = {"channel": "portal", "ats": d.get("ats"), "applied_at": at,
                "proof": {"kind": proof["kind"], "captured_at": at,
                          "sha256": proof.get("sha256"), "ack_message_id": proof.get("ack_message_id")},
                "cv_sha256": files.get("cv"), "letter_sha256": files.get("letter")}
        call(lambda: self.jobs.client.request("POST", f"/applications/{aid}/deposit", account=partner_account,
                                              body=body))
        return aid

    # ── enregistrements en attente : retentés à chaque lecture du pack et par une tâche de fond ──
    RETRY_EVERY_S = 1800.0  # tâche de fond : toutes les 30 min
    RETRY_FOR_S = 24 * 3600.0  # 24 h au plus après le dépôt ; ensuite événement « register_expired » au journal

    def pending(self, account: str) -> list[dict[str, Any]]:
        """Dépôts faits (preuve capturée) mais pas encore enregistrés chez le service emploi."""
        return [d for d in self.desk.journal.deposits(account).values()
                if d.get("status") == "sent" and d.get("proof") and d.get("registered") is False
                and d.get("offer_id")]

    def retry_pending(self, account: str, *, partner_account: str = "", sender: str = "") -> int:
        """Retente l'enregistrement des dépôts en attente (sans quoi ils manqueraient au relevé ORP), 24 h au plus
        après le dépôt ; passé ce délai, l'échec est inscrit au journal (`register_expired`) et plus rien n'est
        tenté. Sérialisé : la tâche de fond et une lecture ne peuvent pas enregistrer deux fois le même dépôt. Rend le
        nombre de dépôts enregistrés."""
        done = 0
        with self._register_lock:
            now = self.desk.clock()
            for d in self.pending(account):
                if d.get("register_expired"):
                    continue
                if now - float(d.get("sent_at") or d.get("at") or now) > self.RETRY_FOR_S:
                    logger.warning("jobs_apply : dépôt %s non enregistré après 24 h", d.get("deposit"))
                    self.desk.journal.append(account, {"deposit": d["deposit"], "event": "register_expired",
                                                       "register_expired": True})
                    continue
                try:
                    aid = self._post_deposit(account, d["deposit"],
                                             partner_account or str(d.get("partner_account") or ""), sender, d)
                except Exception:  # noqa: BLE001
                    logger.info("jobs_apply : enregistrement toujours en attente (%s)", d.get("deposit"))
                    continue
                self.desk.mark_registered(account, d["deposit"], aid)
                done += 1
        return done

    def tick(self) -> int:
        """Un passage de la tâche de fond : chaque compte du journal, dans le locataire de ses dépôts."""
        from nokker.tenancy.context import tenant_scope

        done = 0
        for account in self.desk.journal.accounts():
            pending = self.pending(account)
            if not pending:
                continue
            tenant = next((str(d["tenant"]) for d in pending if d.get("tenant")), "")
            try:
                with tenant_scope(tenant) if tenant else nullcontext():
                    done += self.retry_pending(account)
            except Exception:  # noqa: BLE001 — un compte en panne ne bloque pas les autres
                logger.warning("jobs_apply : passage de fond en échec", exc_info=True)
        return done

    def start(self) -> threading.Thread:
        """Tâche de fond : un passage toutes les `RETRY_EVERY_S` jusqu'à `stop()`."""
        def loop() -> None:
            while not self._stop.wait(self.RETRY_EVERY_S):
                self.tick()

        t = threading.Thread(target=loop, name="jobsd-register", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _on_read(self, person: Any, partner_account: str, sender: str) -> None:
        """Crochet de `jobs_applications` et `jobs_report` : les dépôts en attente sont retentés avant la lecture."""
        self.retry_pending(person.account_id, partner_account=partner_account, sender=sender)

    def _pending_items(self, person: Any) -> list[dict[str, Any]]:
        """Dépôts en attente d'enregistrement, pour `jobs_applications` (ligne `PENDING["line"]`)."""
        return [{"date": day(d.get("sent_at") or d.get("at") or 0),
                 "offer_id": d["offer_id"], "company": d.get("company", ""), "title": d.get("title", ""),
                 "expired": bool(d.get("register_expired"))}
                for d in self.pending(person.account_id) if isinstance(d.get("offer_id"), int)]

    def _register_stored(self, person: Any, offer_id: int, partner_account: str,
                         sender: str) -> dict[str, Any] | None:
        """Crochet de `jobs_update result=sent` : si l'offre a un dépôt en attente (ou expiré après 24 h), il est
        enregistré avec SA preuve (canal portail, page capturée, heure du dépôt, empreintes du CV et de la lettre
        déposés), pas comme un dépôt déclaré. Rend {application_id, kind, date (jour du dépôt)} ; None sans dépôt."""
        with self._register_lock:
            for d in self.pending(person.account_id):
                if d.get("offer_id") != offer_id:
                    continue
                aid = self._post_deposit(person.account_id, d["deposit"],
                                         partner_account or str(d.get("partner_account") or ""), sender, d)
                self.desk.mark_registered(person.account_id, d["deposit"], aid)
                return {"application_id": aid, "kind": d["proof"]["kind"],
                        "date": day(d["proof"]["captured_at"])}
        return None

    def _on_registered(self, person: Any, offer_id: int, application_id: int) -> None:
        """Crochet de `jobs_update result=sent` : le dépôt de cette offre est enregistré par la personne ; il n'est plus
        en attente (ni relancé, ni listé deux fois)."""
        with self._register_lock:
            for d in self.pending(person.account_id):
                if d.get("offer_id") == offer_id:
                    self.desk.mark_registered(person.account_id, d["deposit"], application_id)

    def _retry_registrations(self, c: _Ctx) -> None:
        self.retry_pending(c.person.account_id, partner_account=c.account, sender=c.sender)

    # ── erreurs de jobsd → textes figés ──
    def _desk_text(self, e: DeskError, a: dict[str, Any]) -> str:
        x = e.extra
        if e.code in ERRORS and e.code not in ("recipe_broken",):
            if e.code == "canal_manuel":
                return err("canal_manuel", portal="le site de l'employeur")
            if e.code == "stale":
                return err("stale", change=inline(x.get("change", ""), 120))
            if e.code == "needs_letter":
                return err("needs_letter", offer=x.get("offer", ""))
            if e.code in ("offer_closed", "sites_absent"):
                return err(e.code)
            return ERRORS[e.code]
        if e.code == "recipe_broken":
            return err("recipe_broken", portal=x.get("portal", ""), step=inline(x.get("step", ""), 80))
        if e.code == "needs_letter":
            return err("needs_letter", offer=x.get("offer", ""))
        if e.code == "argument_invalide" and x.get("case") == "username":
            person = ""
            try:
                person = self.jobs._ref(str(a.get("offer") or ""), "offer", self.jobs._people())[0].label
            except Exception:  # noqa: BLE001
                person = ""
            return APPLY["username"].format(de_person=de(person) if person else "")
        from nokker.sites.tools import error_text

        # libellé du connecteur du portail = nom de l'ATS + entreprise (donnée de tiers) : une ligne, par inline()
        return error_text(e.code, **{**x, **({"label": inline(x["label"], 80)} if "label" in x else {})})


def card_text(plan: Plan, person: str) -> str:
    """Ce que la carte montre (§7.3) : pour qui, où, pièces, TOUS les champs, réponses, consentements. Les libellés du
    portail sont des textes de tiers : une ligne chacun (`inline`), jamais d'en-tête du noyau fabriqué."""
    lines = [CARD["head"].format(person=person, company=inline(plan.company, 80), title=inline(plan.title, 120),
                                 location=inline(plan.location, 60)),
             CARD["where"].format(portal=inline(plan.portal, 60), account=JobsApplySource._account_text(plan),
                                  url=inline(plan.url, 200))]
    if plan.kind == "account":
        lines.append(CARD["account"].format(portal=inline(plan.portal, 60), company=inline(plan.company, 80),
                                            username=inline(plan.username, 120)))
    for p in plan.files:
        facts = CARD["facts"].format(facts=", ".join(inline(f, 60) for f in p.new_facts)) if p.new_facts else ""
        lines.append(CARD["piece"].format(role="CV" if p.role == "cv" else "lettre", name=p.name,
                                          pages=count("pages", p.pages, CARD),
                                          sha=p.sha256[:12], facts=facts))
    filled = [f for f in plan.fields if f.kind in ("text", "choice") and f.source != "réponse"]
    answered = [f for f in plan.fields if f.kind in ("text", "choice") and f.source == "réponse"]
    if filled:
        lines.append(CARD["fields"])
        for f in filled:
            lines.append(CARD["field"].format(label=inline(f.label, 120), value=inline(f.value, 200),
                                              source=f" ({f.source})" if f.source in ("défaut",) else "")
                         if f.value else CARD["empty"].format(label=inline(f.label, 120)))
    if answered:
        lines.append(CARD["answers"])
        lines.extend(CARD["field"].format(label=inline(f.label, 120), value=inline(f.value, 200), source="")
                     for f in answered)
    consents = [f for f in plan.fields if f.kind == "check"]
    if consents:
        lines.append(CARD["consents"])
        for f in consents:
            if f.value == "true":
                lines.append(CARD["consent_on"].format(label=inline(f.label, 200),
                                                       beyond=CARD["beyond"].get(f.consent, "")
                                                       if f.consent in BEYOND else ""))
            else:
                lines.append(CARD["consent_off"].format(label=inline(f.label, 200)))
    lines.extend(plan.warnings)
    if plan.strong:
        why = []
        for s in plan.strong:
            key = "consent" if s.startswith("consent:") else s
            text = CARD["strong_why"].get(key, "")
            if text and text not in why:
                why.append(text)
        lines.append(CARD["strong"].format(why=" ; ".join(why)))
    lines.append(CARD["expires"])
    return "\n".join(lines)


class JobsApplyGates:
    """Porte de `jobs_apply` (outermost) : l'appel du MODÈLE est un relevé sans envoi — seuls les refus du noyau
    l'arrêtent ; l'envoi passe toujours par la carte que l'outil pose."""

    SCAN_RULE = "jobs:apply:scan"

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Any) -> list[Verdict]:
        verdicts = self.inner.action(call, spec, turn)
        if call.name != TOOL:
            return verdicts
        denied = [v for v in verdicts if v.decision is Decision.DENY]
        if denied:
            return denied
        return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=self.SCAN_RULE,
                        reason="relevé du formulaire sans envoi ; le dépôt passe par la carte de l'outil",
                        subject=call.id)]

    def switch(self, reply: str, turn: Any, tier: Any) -> list[Verdict]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf or b"")) or 1


def _words(s: Any) -> set[str]:
    import unicodedata

    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().casefold()
    return {w for w in re.sub(r"[^a-z0-9]+", " ", t).split() if w not in ("sa", "sarl", "ag", "gmbh", "ltd", "inc")}


def _fold(s: Any) -> str:
    return " ".join(sorted(_words(s)))


def _close(a: Any, b: Any) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / max(len(wa | wb), 1) >= 0.6


def _date(text: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


__all__ = ["JobsApplyGates", "JobsApplySource", "TOOL", "card_text", "mask_email"]
