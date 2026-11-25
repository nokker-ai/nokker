"""Règles de masquage de l'appareil (`GET /v1/redact/rules`, docs/LOKKAL-PHONE.md §4.3.2, lot P9b).

L'app du téléphone applique ces règles à tout texte produit sur l'appareil AVANT l'envoi et remplace chaque valeur par
un marqueur `⟦CODE-n⟧` (`nokker/redact/markers.py`). Ce module ne masque rien côté noyau : il PUBLIE des motifs.

Forme (calquée sur le SDK de l'app, nokker-apps `packages/sdk/src/redact.ts`, type `RedactRules`) :

    {"version": str, "rules": [RedactRule…], "vectors": [RedactVector…]}
    RedactRule   = {"id", "kind", "pattern", "flags"?: "i", "group": 0|1|2, "validate", "context"?}
    RedactVector = {"text", "expect": [{"kind", "value"}…]}

`kind` ∈ phone, email, iban, avs, card, birthdate, address, secret (code du marqueur : `KIND_CODE`, avs → AHV) ;
`validate` ∈ none, iban_mod97, luhn, avs_ean13 ; `context` : mot-clé exigé juste avant la valeur (appliqué par
l'appareil comme `(?:context)[^0-9\\n]{0,12}$`, insensible à la casse, sur les 40 caractères qui précèdent).
`version` : sha256[:16] du JSON canonique (clés triées, sans espace, UTF-8) de `{rules, vectors}` ; c'est aussi l'ETag.

Source unique : les motifs sont ceux des caviardages du noyau, repris par leur `.pattern` et TRANSPOSÉS mécaniquement
(`transpose`) dans le dialecte commun Python `re` / JS (`u`) / NSRegularExpression — ni lookbehind, ni groupe nommé, ni
`\\p{…}`, ni quantificateur possessif, ni `\\b` : une borne de tête (`\\b`, `(?<![…])`) devient `(?:^|[^…])`, une borne de
fin `(?![…])`, le groupe capturé (la valeur) termine le motif ; `\\d` → `[0-9]`, `\\w` → lettres latines, chiffres, `_`.
  - secrets (`nokker/qa/secrets.py` : `_PEM_RE`, `_PATTERNS` dans leur ordre, `_PASSWORD_KEYWORD_RE`) ;
  - EMAIL, PHONE (suisse, E.164), AVS (`nokker/tools/web.py`) ; IBAN contigu et par groupes (`qa/secrets.py`) ;
  - CARD (`nokker/payments/cardmask.py::_RUN`) ; ADDRESS (`nokker/traces/pii.py` : voie FR, …strasse 12, 12 Main St).
  BIRTHDATE n'a pas de caviardage dans le noyau (`traces/pii.py::NOT_COVERED`) : sa règle (date + contexte) est ici.
Pas de repli par entropie côté appareil (trop bruyant sur un texte d'OCR ou de transcription).

`find_sensitive` est le moteur de l'app (`findSensitive`) en Python : ordre des règles = priorité, pas de
chevauchement, marqueurs déjà posés jamais repris. Les vecteurs sont rejoués avec lui par les tests du noyau.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from nokker.payments import cardmask
from nokker.qa import secrets
from nokker.redact.markers import MARKER_RE
from nokker.tools import web
from nokker.traces import pii

KINDS = ("phone", "email", "iban", "avs", "card", "birthdate", "address", "secret")
KIND_CODE = {"phone": "PHONE", "email": "EMAIL", "iban": "IBAN", "avs": "AHV", "card": "CARD",
             "birthdate": "BIRTHDATE", "address": "ADDRESS", "secret": "SECRET"}
CONTEXT_WINDOW = 40

# ─────────────────────────────── validateurs (mêmes règles que le SDK de l'app) ───────────────────────────────


def iban_mod97(value: str) -> bool:
    s = re.sub(r"[^0-9A-Za-z]", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", s):
        return False
    return secrets._iban_valid(s)


def luhn(value: str) -> bool:
    s = re.sub(r"[^0-9]", "", value)
    if not 13 <= len(s) <= 19 or len(set(s)) == 1:
        return False
    return cardmask._luhn(s)


def avs_ean13(value: str) -> bool:
    """Numéro AVS (AHV) : 13 chiffres commençant par 756, clé de contrôle EAN-13 (poids 1 et 3 depuis la gauche)."""
    s = re.sub(r"[^0-9]", "", value)
    if not re.fullmatch(r"756[0-9]{10}", s):
        return False
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(s[:12]))
    return (10 - total % 10) % 10 == int(s[12])


VALIDATORS: dict[str, Callable[[str], bool]] = {"none": lambda _v: True, "iban_mod97": iban_mod97, "luhn": luhn,
                                                 "avs_ean13": avs_ean13}

# ─────────────────────────────── transposition dans le dialecte commun ───────────────────────────────

_W = "0-9A-Za-z_À-ÖØ-öø-ÿ"  # `\w` transposé : chiffres, lettres latines (accents compris), `_`
_DIALECT_BAD = re.compile(r"\(\?<[=!]|\(\?P?<[A-Za-z]|\\[pP]\{|\\b")


class NotPortable(ValueError):
    pass


def dialect_problem(pattern: str) -> str | None:
    """Même refus que `dialectProblem` du SDK (lookbehind, groupe nommé, \\p{…}, possessif, \\b), plus les drapeaux en
    ligne et `\\A`/`\\Z` que JS ne lit pas."""
    if re.search(r"\(\?<[=!]", pattern):
        return "lookbehind"
    if re.search(r"\(\?P?<[A-Za-z]", pattern):
        return "named_group"
    if re.search(r"\\[pP]\{", pattern):
        return "unicode_property"
    if re.search(r"[*+?}]\+", re.sub(r"\\.", "", pattern)):
        return "possessive"
    if "\\b" in pattern:
        return "word_boundary"
    if re.search(r"\(\?[aiLmsux-]+[:)]", pattern) or re.search(r"\\[AZzG]", pattern):
        return "inline_flag_or_anchor"
    return None


def _split_top(pattern: str) -> list[str]:
    """Alternatives de premier niveau (hors groupes et classes)."""
    out, cur, depth, cls, i = [], [], 0, False, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            cur.append(pattern[i:i + 2])
            i += 2
            continue
        if cls:
            cls = c != "]"
        elif c == "[":
            cls = True
            if pattern[i + 1:i + 2] == "]":  # `[]…]` : le crochet fermant littéral en tête de classe
                cur.append("[]")
                i += 2
                continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _class_end(s: str, i: int) -> int:
    """Index juste après la classe qui commence en `s[i] == "["`."""
    j = i + 1
    if s[j:j + 1] == "^":
        j += 1
    if s[j:j + 1] == "]":
        j += 1
    while j < len(s):
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == "]":
            return j + 1
        j += 1
    raise NotPortable("classe non fermée")


def _body(s: str, *, dotall: bool, keep_group: int | None) -> str:
    """Corps transposé : `\\d`/`\\w`, `.` (dotall), `\\-` hors classe, groupes capturants → non capturants (sauf
    `keep_group`, compté dans le motif d'origine), `\\b` de fin de groupe/de motif → lookahead. Autre `\\b` ou lookbehind
    au milieu : `NotPortable`."""
    out: list[str] = []
    group_no, i = 0, 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            e = s[i:i + 2]
            if e == "\\d":
                out.append("[0-9]")
            elif e == "\\w":
                out.append(f"[{_W}]")
            elif e == "\\-":
                out.append("-")
            elif e == "\\b":
                nxt = s[i + 2:i + 3]
                if nxt not in ("", ")"):
                    raise NotPortable("\\b au milieu du motif")
                out.append(f"(?![{_W}])")
            elif out and out[-1][-1:] in ("*", "+", "?", "}") and s[i + 2:i + 3] in ("+", "*", "?", "{"):
                # `,?\s+` : l'app retire les échappements avant de chercher un possessif (`?+`) ; en classe, plus de
                # voisinage trompeur, même sens.
                out.append(f"[{e}]")
            else:
                out.append(e)
            i += 2
            continue
        if c == "[":
            j = _class_end(s, i)
            inner = s[i:j].replace("\\d", "0-9").replace("\\w", _W)
            out.append(inner)
            i = j
            continue
        if c == "(":
            if s.startswith("(?<", i) and s[i + 3:i + 4] in ("=", "!"):
                raise NotPortable("lookbehind au milieu du motif")
            if s[i + 1:i + 2] != "?":
                group_no += 1
                out.append("(" if group_no == keep_group else "(?:")
                i += 1
                continue
        if c == "." and dotall:
            out.append("[\\s\\S]")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_LEAD_LB = re.compile(r"^\(\?<!(\[[^\]]*\]|\\d)\)")
_TAIL_LA = re.compile(r"\(\?!(\[[^\]]*\]|\\d|\\w)\)$")


def _bounds(alt: str) -> tuple[str, str, str]:
    """(borne de tête transposée, corps, borne de fin transposée) d'une alternative."""
    lead, tail = "", ""
    if alt.startswith("\\b"):
        lead, alt = f"(?:^|[^{_W}])", alt[2:]
    else:
        m = _LEAD_LB.match(alt)
        if m:
            cls = m.group(1)
            cls = "[0-9]" if cls == "\\d" else cls.replace("\\d", "0-9")
            lead, alt = "(?:^|[^" + cls[1:], alt[m.end():]
            lead = lead + ")"
    if alt.endswith("\\b") and not alt.endswith("\\\\b"):
        tail, alt = f"(?![{_W}])", alt[:-2]
    else:
        m = _TAIL_LA.search(alt)
        if m:
            cls = m.group(1)
            cls = {"\\d": "[0-9]", "\\w": f"[{_W}]"}.get(cls, cls.replace("\\d", "0-9").replace("\\w", _W))
            tail, alt = f"(?!{cls})", alt[:m.start()]
    return lead, alt, tail


def transpose(pattern: str, *, dotall: bool = False, value_group: int | None = None) -> list[tuple[str, str]]:
    """Motif du noyau → [(motif du dialecte commun, drapeaux)]. Une alternative de premier niveau dont les bornes
    diffèrent des autres devient une règle à part ; les autres restent ensemble. `value_group` : le groupe du motif
    d'origine qui porte la valeur (il doit terminer le motif) ; sinon tout le corps est la valeur."""
    flags = ""
    if pattern.startswith("(?i)"):
        flags, pattern = "i", pattern[4:]
    alts = _split_top(pattern)
    parts = [_bounds(a) for a in alts]
    groups: list[list[tuple[str, str, str]]] = []
    for p in parts:
        for g in groups:
            if (g[0][0], g[0][2]) == (p[0], p[2]):
                g.append(p)
                break
        else:
            groups.append([p])
    out: list[tuple[str, str]] = []
    for g in groups:
        lead, tail = g[0][0], g[0][2]
        if value_group is not None:
            if len(alts) > 1:
                raise NotPortable("groupe de valeur dans un motif à alternatives")
            body = _body(g[0][1], dotall=dotall, keep_group=value_group)
            if not body.endswith(")"):
                raise NotPortable("le groupe de valeur ne termine pas le motif")
            pat = lead + body + tail
        else:
            bodies = [_body(b, dotall=dotall, keep_group=None) for _l, b, _t in g]
            pat = lead + "(" + "|".join(bodies) + ")" + tail
        problem = dialect_problem(pat)
        if problem:
            raise NotPortable(problem)
        out.append((pat, flags))
    return out


# ─────────────────────────────── règles ───────────────────────────────

# BIRTHDATE : une date jj.mm.aaaa ou jj.mm.aa (/ ou -), TOUJOURS précédée d'un mot de la naissance (`context`).
_BIRTHDATE = r"(?:^|[^0-9])([0-3]?[0-9][./-][01]?[0-9][./-](?:19|20)?[0-9]{2})(?![0-9])"
_BIRTHDATE_CONTEXT = r"né|née|né\(e\)|date de naissance|naissance|geb[.]|geboren|geburtsdatum|dob|date of birth|born"


def _rules_from(rid: str | list[str], kind: str, pattern: str, *, validate: str = "none", dotall: bool = False,
                value_group: int | None = None) -> list[dict[str, Any]]:
    out = []
    for n, (pat, flags) in enumerate(transpose(pattern, dotall=dotall, value_group=value_group)):
        ident = rid[n] if isinstance(rid, list) else (rid if n == 0 else f"{rid}-{n + 1}")
        rule: dict[str, Any] = {"id": ident, "kind": kind, "pattern": pat, "group": 1, "validate": validate}
        if flags:
            rule["flags"] = flags
        out.append(rule)
    return out


def build_rules() -> list[dict[str, Any]]:
    """Les règles, dans l'ordre d'application (= priorité : une valeur prise n'est plus relue)."""
    rules = _rules_from("pem", "secret", secrets._PEM_RE.pattern, dotall=True)
    seen: dict[str, int] = {}
    for kind, pattern in secrets._PATTERNS:
        seen[kind] = seen.get(kind, 0) + 1
        rules += _rules_from(kind if seen[kind] == 1 else f"{kind}-{seen[kind]}", "secret", pattern.pattern)
    rules += _rules_from("password-keyword", "secret", secrets._PASSWORD_KEYWORD_RE.pattern, value_group=3)
    rules += _rules_from("avs", "avs", web._AVS_RE.pattern, validate="avs_ean13")
    rules += _rules_from("iban", "iban", secrets._IBAN_RE.pattern, validate="iban_mod97")
    rules += _rules_from("iban-spaced", "iban", secrets._IBAN_SPACED_RE.pattern, validate="iban_mod97")
    rules += _rules_from("card", "card", cardmask._RUN.pattern, validate="luhn")
    rules += _rules_from("email", "email", web._EMAIL_RE.pattern)
    rules += _rules_from(["phone-ch", "phone-ch-national"], "phone", web._PHONE_CH_RE.pattern)
    rules += _rules_from("phone-e164", "phone", web._PHONE_E164_RE.pattern)
    rules.append({"id": "birthdate", "kind": "birthdate", "pattern": _BIRTHDATE, "group": 1, "validate": "none",
                  "context": _BIRTHDATE_CONTEXT})
    for rid, street in (("address-fr", pii._STREET_FR_RE), ("address-de", pii._STREET_DE_RE),
                        ("address-en", pii._STREET_EN_RE)):
        rules += [{**r, "pattern": _with_postal(r["pattern"])} for r in _rules_from(rid, "address", street.pattern)]
    return rules


def _with_postal(pattern: str) -> str:
    """Voie transposée `<tête>(<voie>)<fin>` → `<tête>(<voie><fin>(?:<séparateur><code postal localité>)?)` : la
    partie postale du noyau (`traces/pii.py::_POSTAL_SEP`, `_POSTAL_BODY`) accolée, facultative, dans la valeur."""
    [(postal, _f)] = transpose(f"(?:{pii._POSTAL_SEP}){pii._POSTAL_BODY}")
    tail = _TAIL_LA.search(pattern)
    head, tail_text = (pattern[:tail.start()], tail.group(0)) if tail else (pattern, "")
    if not head.endswith(")"):
        raise NotPortable("voie sans groupe de valeur final")
    return head[:-1] + tail_text + "(?:" + postal[1:-1] + ")?)"


# ─────────────────────────────── vecteurs ───────────────────────────────
# Valeurs FACTICES assemblées (préfixe + suite de lettres et chiffres), jamais écrites d'un bloc dans le source : les
# scanners de secrets d'un miroir public ne doivent pas les prendre pour de vraies clés. IBAN, AVS et cartes : exemples
# publics (IBAN de démonstration, AVS d'exemple de la Confédération, cartes de test des prestataires).
_ALNUM = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" * 2


def _fake(prefix: str, n: int) -> str:
    return prefix + _ALNUM[:n]


def _pem_mark(word: str) -> str:
    """Ligne d'en-tête ou de fin d'un bloc PEM FACTICE, assemblée à l'exécution (ni le source ni le .pyc ne portent un
    bloc entier : gitleaks `private-key` reste à 0)."""
    return f"-----{word} {'PRIVATE'} KEY-----"


def _v(text: str, *expect: tuple[str, str]) -> dict[str, Any]:
    return {"text": text, "expect": [{"kind": k, "value": v} for k, v in expect]}


def build_vectors() -> list[dict[str, Any]]:
    pem = "\n".join((_pem_mark("BEGIN"), "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", _pem_mark("END")))
    keys = [_fake("sk-" + "ant-", 24), _fake("sk-" + "proj-", 24), _fake("sk_" + "live_", 24), _fake("wh" + "sec_", 24),
            _fake("gh" + "p_", 36), _fake("github" + "_pat_", 24), _fake("gl" + "pat-", 24),
            _fake("xo" + "xb-1234567890-", 12), _fake("xa" + "pp-1-", 12),
            "https://hooks.slack" + ".com/services/" + _fake("T0/B0/", 24), _fake("AI" + "za", 35),
            _fake("hf" + "_", 32), _fake("np" + "m_", 36), "si" + "k_" + "0123456789abcdef" * 2,
            "123456789:" + _ALNUM[:35], "AK" + "IA" + "ABCDEFGHIJKLMNOP", _fake("abc1Q" + "~", 32),
            "ey" + "JhbGciOi.eyJzdWIiOi.SflKxwRJSM"]
    return [
        _v("Appelle-moi au 079 123 45 67 ou au +41 22 345 67 89.", ("phone", "079 123 45 67"),
           ("phone", "+41 22 345 67 89")),
        _v("Bureau 0041.22.345.67.89, Paris +33 1 42 68 53 00", ("phone", "0041.22.345.67.89"),
           ("phone", "+33 1 42 68 53 00")),
        _v("le 12 345, réf. 1234567, +41 79 12", ),
        _v("Écris à Jean.Dupont@example.ch, merci", ("email", "Jean.Dupont@example.ch")),
        _v("jean.dupont at example.ch, user@localhost"),
        _v("IBAN : CH93 0076 2011 6238 5295 7 pour le loyer", ("iban", "CH93 0076 2011 6238 5295 7")),
        _v("virement DE89370400440532013000 reçu", ("iban", "DE89370400440532013000")),
        _v("IBAN faux : CH93 0076 2011 6238 5295 8 et CH9300762011623852958"),
        _v("Mon AVS 756.9217.0769.85 et un faux 756.9217.0769.84", ("avs", "756.9217.0769.85")),
        _v("AVS 7569217076985 ; 123.4567.8901.23", ("avs", "7569217076985")),
        _v("Carte 4111 1111 1111 1111, commande 1234 5678 9012 3456", ("card", "4111 1111 1111 1111")),
        _v("carte 5555-5555-5555-4444 ou 378282246310005 ; faux 4242 4242 4242 4241",
           ("card", "5555-5555-5555-4444"), ("card", "378282246310005")),
        _v("Date de naissance : 14.03.1987 ; rendez-vous le 14.03.2027", ("birthdate", "14.03.1987")),
        _v("née le 3/7/2001, geboren am 01-12-1990", ("birthdate", "3/7/2001"), ("birthdate", "01-12-1990")),
        _v("né le 03.07.85, livré le 03.07.85", ("birthdate", "03.07.85")),
        _v("Livraison : rue du Rhône 42, 1204 Genève", ("address", "rue du Rhône 42, 1204 Genève")),
        _v("Chemin des Crêts 4\n1218 Grand-Saconnex", ("address", "Chemin des Crêts 4\n1218 Grand-Saconnex")),
        _v("j'habite 12 rue de la Paix ; AVENUE du Mail 10", ("address", "12 rue de la Paix"),
           ("address", "AVENUE du Mail 10")),
        _v("Bahnhofstrasse 1, 8001 Zürich", ("address", "Bahnhofstrasse 1, 8001 Zürich")),
        _v("lives at 221 Baker Street", ("address", "221 Baker Street")),
        _v("la rue est calme, die Bahnhofstrasse ist lang, 221 baker street"),
        _v("mot de passe : Hunter2!x", ("secret", "Hunter2!x")),
        _v("le password du wifi = Xk9qpzT42", ("secret", "Xk9qpzT42")),
        _v(pem, ("secret", pem)),
        _v("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"),
        *[_v(f"clé {k} ici", ("secret", k)) for k in keys],
        _v("clés courtes sk-court ghp_court xoxb-x AKIAcourt eyJ.a.b"),
        _v("Total 89.90 CHF, facture 2026-114, tél. ⟦PHONE-1⟧, ⟦IBAN-2⟧, ⟦AHV-1⟧ ⟦CARD-9999⟧ ⟦SECRET-3⟧"),
        _v("mot de passe : ⟦SECRET-1⟧ ; née le ⟦BIRTHDATE-1⟧"),
    ]


# ─────────────────────────────── moteur (findSensitive du SDK, en Python) ───────────────────────────────


def find_sensitive(text: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Valeurs sensibles du texte, sans chevauchement (ordre des règles = priorité), hors des marqueurs déjà posés :
    `[{start, end, kind, value, rule}]` triés par position. Même algorithme que `findSensitive` de l'app."""
    taken = [(m.start(), m.end()) for m in MARKER_RE.finditer(text)]
    out: list[dict[str, Any]] = []
    for rule in rules:
        flags = re.IGNORECASE if rule.get("flags") == "i" else 0
        ctx = re.compile(f"(?:{rule['context']})[^0-9\\n]{{0,12}}\\Z", re.IGNORECASE) if rule.get("context") else None
        check = VALIDATORS[rule["validate"]]
        for m in re.finditer(rule["pattern"], text, flags):
            value = m.group(rule["group"])
            if not value:
                continue
            end = m.end()
            start = end - len(value)
            if any(start < y and x < end for x, y in taken) or not check(value):
                continue
            if ctx and not ctx.search(text[max(0, start - CONTEXT_WINDOW):start]):
                continue
            taken.append((start, end))
            out.append({"start": start, "end": end, "kind": rule["kind"], "value": value, "rule": rule["id"]})
    return sorted(out, key=lambda s: s["start"])


def check_vectors(body: dict[str, Any]) -> list[str]:
    """Écarts entre les vecteurs publiés et le moteur (vide = jeu accepté), comme `checkVectors` de l'app."""
    bad = []
    for v in body["vectors"]:
        got = [f"{s['kind']}:{s['value']}" for s in find_sensitive(v["text"], body["rules"])]
        want = [f"{e['kind']}:{e['value']}" for e in v["expect"]]
        if got != want:
            bad.append(f"{v['text']!r} → {got} (attendu {want})")
    return bad


# ─────────────────────────────── corps publié ───────────────────────────────


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def device_rules() -> dict[str, Any]:
    """Corps de `GET /v1/redact/rules` : `{version, rules, vectors}`."""
    body = {"rules": build_rules(), "vectors": build_vectors()}
    return {"version": hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()[:16], **body}


def rules_json() -> tuple[bytes, str]:
    """(octets exacts de la réponse, ETag). Les mêmes que `python -m nokker.redact.export`."""
    body = device_rules()
    return json.dumps(body, ensure_ascii=False).encode("utf-8"), f'"{body["version"]}"'


__all__ = ["KINDS", "KIND_CODE", "VALIDATORS", "NotPortable", "avs_ean13", "build_rules", "build_vectors",
           "check_vectors", "device_rules", "dialect_problem", "find_sensitive", "iban_mod97", "luhn", "rules_json",
           "transpose"]
