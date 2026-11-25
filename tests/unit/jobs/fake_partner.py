"""Faux service emploi, au contrat EXACT de l'API partenaire (docs/partner-api.md du service, f4e3f058) :
formes JSON, codes d'erreur, opérations 202, URL signées, JWT ES256. En mémoire, AUCUNE requête réseau.
"""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

AUDIENCE = "service-emploi-test"
ISSUER = "nokker"
MAX_TTL_S = 300
PDF = b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF"
ZERO = {"input_tokens": 0, "output_tokens": 0, "calls": []}
USAGE = {"input_tokens": 1000, "output_tokens": 500,
         "calls": [{"tier": "premium", "model": "m", "input_tokens": 1000, "output_tokens": 500}]}


def _keypair() -> tuple[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return pem, key.public_key()


PRIVATE_PEM, PUBLIC_KEY = _keypair()


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verify_jwt(token: str, seen: set[str], now: float) -> dict[str, Any] | None:
    """Comme le service : ES256 seulement, `aud`, `iss`, `exp - iat` ≤ 300 s, tolérance 30 s, `jti` 8-128 à usage
    unique."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        h, c, sig = token.split(".")
        header, claims, raw = json.loads(_unb64(h)), json.loads(_unb64(c)), _unb64(sig)
        if header.get("alg") != "ES256" or len(raw) != 64:
            return None
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        PUBLIC_KEY.verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature):
        return None
    iat, exp, jti = claims.get("iat"), claims.get("exp"), claims.get("jti")
    if claims.get("aud") != AUDIENCE or claims.get("iss") != ISSUER or not isinstance(jti, str) \
            or not 8 <= len(jti) <= 128:
        return None
    if not isinstance(iat, int) or not isinstance(exp, int) or exp - iat > MAX_TTL_S or exp < now - 30 \
            or iat > now + 30 or jti in seen:
        return None
    seen.add(jti)
    return claims


def parse_multipart(content_type: str, body: bytes) -> tuple[str, str, bytes]:
    """(champ, nom de fichier, octets) du seul fichier d'un corps multipart/form-data."""
    boundary = content_type.split("boundary=", 1)[1].encode()
    part = body.split(b"--" + boundary)[1]
    head, data = part.split(b"\r\n\r\n", 1)
    disp = head.decode()
    name = re.search(r'name="([^"]+)"', disp).group(1)
    filename = re.search(r'filename="([^"]*)"', disp).group(1)
    return name, filename, data[: -len(b"\r\n")]


OUTCOME = {"applied": "sent", "tracking": "sent", "waitlist": "sent", "job_delisted": "sent",
           "interview": "interview", "interview_scheduled": "interview", "rejected": "rejected", "offer": "offer",
           "hired": "hired"}


@dataclass
class Call:
    method: str
    path: str
    headers: dict[str, str]
    body: Any


@dataclass
class FakePartner:
    base: str = "https://emploi.invalid"
    name_on_cv: str | None = "Camille Martin"
    language: str | None = "fr"
    pending_polls: int = 1  # relevés `running` avant `done`
    offer_description: str = "Nous cherchons une ingénieure Python à Genève. Kubernetes apprécié."
    import_status: str = "imported"
    calls: list[Call] = field(default_factory=list)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    cvs: dict[str, dict[str, Any]] = field(default_factory=dict)
    ops: dict[str, dict[str, Any]] = field(default_factory=dict)
    applications: dict[int, dict[str, Any]] = field(default_factory=dict)
    preps: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail: dict[str, tuple[int, str]] = field(default_factory=dict)  # préfixe de chemin → (statut, code)
    jtis: set[str] = field(default_factory=set)
    uploads: list[tuple[str, str, bytes]] = field(default_factory=list)
    _n: int = 100

    def document(self, name: str) -> dict[str, Any]:
        return {"schema": "cv_document.v1",
                "header": {"name": name, "title": "Ingénieure logiciel",
                           "contact": {"email": "", "phone": "", "location": "Genève", "website": "", "linkedin": ""},
                           "photo": None},
                "summary": "Développeuse Python, 6 ans.",
                "experience": [{"company": "Acme SA", "title": "Développeuse", "start": "2019", "end": "2025",
                                "location": "Genève", "bullets": ["API en Python", "Tests automatisés"],
                                "stack": ["Python", "PostgreSQL"]}],
                "education": [{"degree": "Bachelor", "institution": "HEPIA", "location": "Genève", "start": "2015",
                               "end": "2018", "details": ""}],
                "skill_groups": [], "languages": [{"language": "français", "level": "C2"}], "certifications": [],
                "achievements": [],
                "layout": {"template_id": "zurich", "accent": "", "section_order": [], "hidden_sections": [],
                           "show_photo": True}}

    def _op(self, kind: str, result: dict[str, Any], extra: dict[str, Any] | None = None
            ) -> tuple[int, dict[str, str], bytes]:
        jid = str(uuid.uuid4())
        self.ops[jid] = {"kind": kind, "result": result, "polls": self.pending_polls}
        return self._json(202, {"job_id": jid, "kind": kind, "status": "queued", **(extra or {})})

    def _signed(self, path: str) -> str:
        return f"/api/v1/partner{path}?c=nokker&a=x&exp=9999999999&sig=abc"

    def _view(self, aid: int) -> dict[str, Any]:
        a = self.applications[aid]
        o = self._offer(a["offer_id"])
        return {"application_id": aid, "offer_id": a["offer_id"], "company": o["company"], "position": o["title"],
                "location": o["location"], "offer_url": o["url"], "status": a["status"],
                "outcome": "no_answer" if a.get("no_answer") else OUTCOME.get(a["status"]),
                "cv_profile_id": None, "cv_id": a.get("cv_id"), "apply_method": a.get("apply_method"),
                "applied_at": a.get("applied_at"), "has_letter": bool(a.get("letter")), "letter": a.get("letter")}

    def _offer(self, oid: int) -> dict[str, Any]:
        title, company, loc = {42: ("Ingénieure Python", "Acme SA", "Genève"),
                               43: ("Data Engineer", "Beta Sàrl", "Lausanne")}.get(oid, ("Poste", "Gamma SA", "Berne"))
        return {"id": oid, "title": title, "company": company, "location": loc, "url": f"https://acme.example/{oid}",
                "source": "acme"}

    # transport(method, url, headers, body) -> (status, headers, bytes)
    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None
                 ) -> tuple[int, dict[str, str], bytes]:
        u = urlsplit(url)
        assert u.path.startswith("/api/v1/partner/"), u.path
        path = u.path[len("/api/v1/partner"):]
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ctype = headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            self.uploads.append(parse_multipart(ctype, body or b""))
            data = None
        else:
            data = json.loads(body) if body else None
        self.calls.append(Call(method, path, dict(headers), data))
        if any(h.lower() in ("cf-ray", "cf-connecting-ip", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
               for h in headers):
            return 404, {"content-type": "application/json"}, b'{"detail": "Not Found"}'
        if "sig" in q:  # URL signée : sans jeton
            m = re.fullmatch(r"/prep/([\w-]+)/files/(\d\d)", path)
            if m:
                return 200, {"content-type": "text/html", "x-partner-usage": json.dumps(ZERO)}, \
                    f"<html><body>Fichier {m.group(2)}</body></html>".encode()
            return 200, {"content-type": "application/pdf", "x-partner-usage": json.dumps(ZERO)}, PDF
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or verify_jwt(auth[7:], self.jtis, time.time()) is None:
            return self._json(401, {"error": {"code": "unauthorized", "message": "Invalid partner credentials"}})
        for prefix, (st, code) in self.fail.items():
            if path.startswith(prefix):
                return self._json(st, {"error": {"code": code, "message": "x"}})
        if path == "/accounts" and method == "POST":
            if data.get("locale") not in ("fr", "de", "it", "en"):
                return self._json(422, {"error": {"code": "invalid", "message": "locale: invalid"}})
            pid = uuid.uuid4().hex
            self.accounts[pid] = dict(data)
            return self._json(201, {"partner_account_id": pid})
        m = re.fullmatch(r"/accounts/(\w+)", path)
        if m and method == "DELETE":
            if m.group(1) not in self.accounts:
                return self._json(404, {"error": {"code": "account_not_found", "message": "Unknown account"}})
            del self.accounts[m.group(1)]
            return self._json(200, {"erased": True, "erased_at": "2026-11-05T12:00:00+00:00",
                                    "partner_account_id": m.group(1), "files_deleted": 1, "files_errors": 0})
        acct = headers.get("X-Partner-Account")
        if not acct:
            return self._json(400, {"error": {"code": "account_required", "message": "X-Partner-Account"}})
        if acct not in self.accounts:
            return self._json(404, {"error": {"code": "account_not_found", "message": "Unknown account"}})
        m = re.fullmatch(r"/jobs/([\w-]+)", path)
        if m and method == "GET":
            op = self.ops.get(m.group(1))
            if op is None:
                return self._json(404, {"error": {"code": "not_found", "message": "Unknown operation"}})
            if op["polls"] > 0:
                op["polls"] -= 1
                return self._json(200, {"job_id": m.group(1), "kind": op["kind"], "status": "running", "result": None,
                                        "error": None, "created_at": "…", "finished_at": None})
            return self._json(200, {"job_id": m.group(1), "kind": op["kind"], "status": "done",
                                    "result": op["result"], "error": None, "created_at": "…",
                                    "finished_at": "…", "usage": USAGE})
        if path == "/cv" and method == "POST":
            if not self.uploads or self.uploads[-1][0] != "file":
                return self._json(422, {"error": {"code": "invalid", "message": "file: required"}})
            cid = str(uuid.uuid4())
            self.cvs[cid] = {"account": acct, "document": self.document(self.name_on_cv or "")}
            self._n += 1
            return self._op("cv_import", {"cv_id": cid, "cv_profile_id": self._n, "reparsed": True,
                                          "name_on_cv": self.name_on_cv, "language": self.language, "reason": None},
                            {"cv_profile_id": self._n})
        m = re.fullmatch(r"/cv/([\w-]+)", path)
        if m and method == "DELETE":
            if m.group(1) not in self.cvs:
                return self._json(404, {"error": {"code": "not_found", "message": "Unknown cv"}})
            del self.cvs[m.group(1)]
            for a in self.applications.values():
                if a.get("cv_id") == m.group(1):
                    a["cv_id"] = None  # les candidatures restent (relevé ORP)
            return self._json(200, {"erased": True, "erased_at": "…", "cv_ids": [m.group(1)], "cv_profile_ids": [],
                                    "prep_ids": [], "files_deleted": 0})
        if m and method == "GET":
            cv = self.cvs.get(m.group(1))
            if cv is None or cv["account"] != acct:
                return self._json(404, {"error": {"code": "not_found", "message": "Unknown cv"}})
            return self._json(200, {"cv_id": m.group(1), "document": cv["document"],
                                    "pdf_url": self._signed(f"/cv/{m.group(1)}/pdf"), "pdf_url_expires_in": 900,
                                    "template": {"id": "zurich", "version": "0123456789abcdef",
                                                 "bundle_url": "/api/v1/partner/templates/zurich/bundle?v=0123"}})
        m = re.fullmatch(r"/cv/([\w-]+)/adapt", path)
        if m and method == "POST":
            if not isinstance(data.get("offer_id"), int):
                return self._json(422, {"error": {"code": "invalid", "message": "offer_id: int"}})
            cid = str(uuid.uuid4())
            doc = self.document(self.name_on_cv or "")
            doc["summary"] = "Développeuse Python, 6 ans, orientée plateformes."
            self.cvs[cid] = {"account": acct, "document": doc}
            return self._op("cv_adapt", {"cv_id": cid, "source_cv_id": m.group(1), "offer_id": data["offer_id"]})
        if path == "/cv/render" and method == "POST":
            return 200, {"content-type": "application/pdf", "x-partner-usage": json.dumps(USAGE)}, PDF
        if path == "/matches/refresh" and method == "POST":
            return self._op("matches_refresh", {"refreshed": True, "summary": {"new": 2}})
        if path == "/matches" and method == "GET":
            items = [{"application_id": 9, "offer_id": 42, "cv_profile_id": None, "cv_id": None, "match_score": 0.81,
                      "status": "matched", "outcome": None, "applied_at": None, "apply_method": None,
                      "score_breakdown": {"skills": 0.9}, "offer": self._offer(42)},
                     {"application_id": 10, "offer_id": 43, "cv_profile_id": None, "cv_id": None, "match_score": 0.64,
                      "status": "applied", "outcome": "sent", "applied_at": "2026-11-02T10:00:00+01:00",
                      "apply_method": "nokker_lokkal", "score_breakdown": {}, "offer": self._offer(43)}]
            if q.get("min_score") is not None:
                ms = float(q["min_score"])
                if not 0 <= ms <= 1:
                    return self._json(422, {"error": {"code": "invalid", "message": "min_score: [0, 1]"}})
                items = [i for i in items if (i["match_score"] or 0) >= ms]
            return self._json(200, self._page(items, q, 50))
        if path == "/offers/search" and method == "GET":
            items = [{"offer_id": 42, "title": "Ingénieure Python", "company": "Acme SA", "company_display": "Acme",
                      "is_placement_agency": False, "location": "Genève", "source": "acme",
                      "scraped_at": "2026-11-03T08:00:00+00:00", "match_score": 0.7, "apply_channel": "workday"}]
            return self._json(200, {"items": items, "total": 1, "has_more": False,
                                    "limit": int(q.get("limit", 30)), "offset": 0})
        if path == "/offers/import" and method == "POST":
            if not str(data.get("url", "")).startswith(("http://", "https://")):
                return self._json(422, {"error": {"code": "invalid", "message": "url: http(s)"}})
            return self._json(200, {"offer_id": 900 if self.import_status == "imported" else None,
                                    "status": self.import_status, "usage": USAGE})
        m = re.fullmatch(r"/offers/(\d+)", path)
        if m and method == "GET":
            oid = int(m.group(1))
            app = next((aid for aid, a in self.applications.items() if a["offer_id"] == oid), None)
            match = {"application_id": app, "status": self.applications[app]["status"], "match_score": 0.81,
                     "explanation": "Python 6 ans ; Kubernetes absent du CV.", "score_breakdown": {"skills": 0.9}} \
                if app else None
            return self._json(200, {
                "offer": {**self._offer(oid), "description": self.offer_description, "salary_min": None,
                          "salary_max": None, "workload_min": 80, "workload_max": 100,
                          "scraped_at": "2026-11-03T08:00:00+00:00"},
                "channel": {"channel": "workday", "display_name": "Workday", "reason_slug": None,
                            "fallback_available": False, "ats_external_id": "R1"},
                "match": match})
        if path == "/applications" and method == "GET":
            items = [self._view(aid) for aid in sorted(self.applications, reverse=True)]
            if q.get("status"):
                items = [i for i in items if i["status"] == q["status"]]
            return self._json(200, self._page(items, q, 100))
        if path == "/applications" and method == "POST":
            oid = data.get("offer_id")
            if not isinstance(oid, int):
                return self._json(422, {"error": {"code": "invalid", "message": "offer_id: int"}})
            found = next((aid for aid, a in self.applications.items() if a["offer_id"] == oid), None)
            if found is not None:
                return self._json(200, {**self._view(found), "created": False})
            self._n += 1
            self.applications[self._n] = {"offer_id": oid, "status": "matched", "cv_id": data.get("cv_id")}
            return self._json(201, {**self._view(self._n), "created": True})
        m = re.fullmatch(r"/applications/(\d+)", path)
        if m and method == "PATCH":
            aid = int(m.group(1))
            a = self.applications.get(aid)
            if a is None:
                return self._json(404, {"error": {"code": "not_found", "message": "Unknown application"}})
            st = data.get("status")
            if st == "sent":
                return self._json(422, {"error": {"code": "invalid", "message": "status: use /deposit"}})
            if st not in ("interview", "rejected", "offer", "hired", "no_answer", "interview_scheduled", "tracking",
                          "waitlist", "withdrew"):
                return self._json(422, {"error": {"code": "invalid", "message": "status: invalid"}})
            if st == "no_answer":
                if a.get("apply_method") != "nokker_lokkal":
                    return self._json(409, {"error": {"code": "not_deposited", "message": "No deposit recorded"}})
                a.update(status="tracking", no_answer=True)
            else:
                a.update(status=st, no_answer=False, motif=data.get("motif"))
            return self._json(200, self._view(aid))
        m = re.fullmatch(r"/applications/(\d+)/deposit", path)
        if m and method == "POST":
            aid = int(m.group(1))
            a = self.applications.get(aid)
            if a is None:
                return self._json(404, {"error": {"code": "not_found", "message": "Unknown application"}})
            kind = (data.get("proof") or {}).get("kind")
            sha_ok = re.fullmatch(r"[0-9a-f]{64}", str(data.get("cv_sha256"))) or (
                kind == "declared" and data.get("cv_sha256") is None)
            ok = data.get("channel") in ("portal", "email", "manual") and sha_ok
            if not ok or kind not in ("confirmation_page", "ack_mail", "mail_sent", "declared"):
                return self._json(422, {"error": {"code": "invalid", "message": "deposit: invalid"}})
            a.update(status="applied", apply_method="nokker_lokkal", applied_at=data["applied_at"],
                     proof=data["proof"]["kind"])
            return self._json(200, {**self._view(aid), "proof": {"transport": "nokker_lokkal", "success": True,
                                                                 "deposit": data}})
        if path == "/letters" and method == "POST":
            aid = data.get("application_id")
            if aid not in self.applications:
                return self._json(404, {"error": {"code": "not_found", "message": "Unknown application"}})
            text = ("Madame, Monsieur,\nVotre offre pour une ingénieure Python chez Acme SA m'intéresse. "
                    "Six ans de Python chez Acme SA. Certifiée AWS.\n[à compléter]\nCordialement")
            self.applications[aid]["letter"] = text
            return self._op("letter", {"application_id": aid, "letter": text, "locale": data.get("locale") or "fr",
                                       "pdf_url": self._signed(f"/applications/{aid}/letter.pdf")})
        if path == "/prep" and method == "POST" and data.get("depth") == "questions":
            if not isinstance(data.get("offer_id"), int):
                return self._json(422, {"error": {"code": "invalid", "message": "offer_id: int"}})
            found = next((aid for aid, a in self.applications.items() if a["offer_id"] == data["offer_id"]), None)
            if found is None:
                self._n += 1
                found = self._n
                self.applications[found] = {"offer_id": data["offer_id"], "status": "matched"}
            return self._op("prep_questions", {
                "depth": "questions", "application_id": found, "offer_id": data["offer_id"], "cached": False,
                "questions": [{"q": "Parlez-moi d'une API conçue.", "category": "technical",
                               "answer_outline": ["contexte Acme SA", "choix techniques", "résultat"]}],
                "story": {"title": "Migration de l'API", "situation": "Acme SA, 2023", "action": "refonte",
                          "result": "API stable"}})
        if path == "/prep" and method == "POST":
            if data.get("depth", "full") != "full":
                return self._json(422, {"error": {"code": "invalid", "message": "depth: full|questions"}})
            if not isinstance(data.get("offer_id"), int) or data.get("language") not in ("fr", "de", "it", "en"):
                return self._json(422, {"error": {"code": "invalid", "message": "offer_id, language"}})
            pid = str(uuid.uuid4())
            files = {s: self._signed(f"/prep/{pid}/files/{s}") for s in ("01", "02", "03", "04")}
            self.preps[pid] = {"offer_id": data["offer_id"]}
            return self._op("prep", {"prep_id": pid, "status": "done", "language": data["language"],
                                     "role_title": "Ingénieure Python", "company": "Acme SA", "interview_date": None,
                                     "crash_topics": [{"title": "API REST", "why": "cœur du poste"}],
                                     "differentiating_story": "Migration de l'API d'Acme SA en 2023.",
                                     "pack_plan": None, "files": files})
        m = re.fullmatch(r"/prep/([\w-]+)", path)
        if m and method == "GET":
            pid = m.group(1)
            if pid not in self.preps:
                return self._json(404, {"error": {"code": "not_found", "message": "Unknown prep"}})
            files = {s: self._signed(f"/prep/{pid}/files/{s}") for s in ("01", "02", "03", "04")}
            return self._json(200, {"prep_id": pid, "status": "done", "offer_id": self.preps[pid]["offer_id"],
                                    "language": "fr", "role_title": "", "company": "", "interview_date": None,
                                    "crash_topics": [], "differentiating_story": None, "files": files,
                                    "files_expires_in": 900})
        m = re.fullmatch(r"/orp/(\d{4}-\d{2})", path)
        if m and method == "GET":
            return self._json(200, {
                "schema": "rav_report.v1", "person": {"last_name": "Martin", "first_name": "Camille", "address": ""},
                "period": {"month": m.group(1)},
                "efforts": [{"application_id": 10, "date": f"{m.group(1)}-04", "methods": ["electronic"],
                             "method": "electronic", "company": "Acme SA", "street": None, "number": None,
                             "po_box": None, "country": "Suisse", "npa": "1204", "city": "Genève",
                             "contact_person": None, "contact_email": None, "contact_phone": None,
                             "position": "Ingénieure Python", "url": "https://acme.example/42", "ref": None,
                             "orp_assignment": False, "workload": "full_time", "interview": False,
                             "result": "pending", "rejection_reason": None, "proof": "confirmation_page"}],
                "excluded": [{"application_id": 11, "date": f"{m.group(1)}-05", "company": "Beta Sàrl",
                              "position": "Data Engineer", "reason": "no_proof"}],
                "sources_addresses": ""})
        return self._json(404, {"error": {"code": "not_found", "message": path}})

    @staticmethod
    def _page(items: list[dict[str, Any]], q: dict[str, str], default: int) -> dict[str, Any]:
        lim, off = min(int(q.get("limit", default)), 500), int(q.get("offset", 0))
        nxt = off + lim if off + lim < len(items) else None
        return {"total": len(items), "limit": lim, "offset": off, "next_offset": nxt, "has_more": nxt is not None,
                "items": items[off:off + lim]}

    @staticmethod
    def _json(status: int, obj: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        if "usage" not in obj:
            obj = {**obj, "usage": ZERO}
        return status, {"content-type": "application/json"}, json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def paths(self) -> list[str]:
        return [f"{c.method} {c.path}" for c in self.calls]
