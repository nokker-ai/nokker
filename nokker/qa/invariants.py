"""nokker/qa/invariants.py — couche 1 (déterministe) du port `Invariants` (PLAN-P1 §2.3, §2.5).

Toujours AVANT le juge (couche 2, `qa/questions.py`/`qa/gates.py`, chantier B-2/B-3). Aucun
appel modèle ici : tout est regex/table, donc rapide et testable sans fake LLM.

Ordre du tour concerné (PLAN §2.3) :
  - étape 1 : `check_input`  — caviardage AVANT tout enregistrement + drapeaux de domaine ;
  - étape 5 : `check_action` — avant chaque appel d'outil ;
  - étape 8 : `scrub_output` — sur tout ce qui part (réponse, et par construction tout message
    sortant qui repasse par ce même scrubber).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolSpec, call_effect
from nokker.core.turn import RELAY_CHANNEL, Decision, Effect, Gate, InputCheck, ToolCall, ToolResult, Turn, Verdict
from nokker.qa.secrets import KnownSecrets, redact, redact_messages
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none

# ─────────────────────────────── drapeaux de domaine ───────────────────────────────
#
# FR/EN/DE au minimum (consigne F3). Précis : on ne flague QUE ces domaines, jamais sur
# « mot de passe » seul (ça relève de qa/secrets.py, pas d'un drapeau de domaine) ni sur un mot
# isolé trop générique (« montant » seul n'est pas dans la liste : trop de faux positifs — le
# signal est le PAIEMENT/la FACTURE, pas le chiffre).

_FISCAL = re.compile(
    r"(?i)\b(tva|imp[oô]ts?|fiscal\w*|taxe\w*|d[ée]ductions?|"
    r"vat|taxes?|tax(ed|ation)?|deduction\w*|"
    r"steuer\w*|mwst|abzug\w*)\b"
)
_JURIDIQUE = re.compile(
    r"(?i)\b(contrats?|litiges?|avocats?|r[ée]siliations?|clauses?|"
    r"contracts?|disputes?|lawyers?|terminations?|"
    r"vertr(ag|äge)\w*|anwalt\w*|k[üu]ndigung\w*|klausel\w*)\b"
)
_MONTANT = re.compile(
    r"(?i)\b(paiements?|virements?|factures?|iban|"
    r"payments?|invoices?|wire\s*transfers?|"
    r"zahlung\w*|[uü]berweisung\w*|rechnung\w*)\b"
)

DOMAIN_PATTERNS: dict[str, re.Pattern[str]] = {"fiscal": _FISCAL, "juridique": _JURIDIQUE, "montant": _MONTANT}

# ─────────────── engagement et simple mention (rc.7, docs/INVARIANTS.md §1.2) ───────────────
#
# Le drapeau dit seulement que le domaine est NOMMÉ. L'escalade forcée (qa/gates.py::domain_verdict) vise la demande de
# DÉCIDER ou d'ENGAGER dans ce domaine. Ces motifs servent à une restriction précise : trois familles de « simple
# mention » (arnaque, lecture d'un document reçu, rappel ou relance) lèvent l'escalade, SAUF si le message porte un
# marqueur d'engagement. Tout ce qui n'est dans aucune famille escalade comme avant ; dans le doute, on escalade.

# Ordre en tête de proposition (« Vire 3 000 CHF », « puis paie-la », « Please pay … »). Ancré pour ne pas prendre
# « ma fiche de paie » ou « la règle fiscale » pour un ordre.
_CLAUSE = (r"(?:^|[.!?;:\n,(«\"]\s*|\b(?:stp|svp|allez|ok|bon|maintenant|ensuite|puis|et|please|pls|now|then|and|"
           r"just|go ahead and|can you|could you|would you|bitte|dann|und|jetzt)\s+)")
_MONEY_VERBS = (r"(?:paie|paye|payez|vire|virez|verse|versez|transf[èe]re|transf[ée]rez|"
                r"pay(?!\s+attention)|wire|transfer|send (?:the )?(?:money|payment|funds)|"
                r"[üu]berweise|[üu]berweis|bezahle|bezahl|zahle|begleiche)\b")
_MONEY_ORDER = re.compile(r"(?i)" + _CLAUSE + _MONEY_VERBS)
# Même ordre suivi d'un montant (« Vire 3 000 CHF », « Wire CHF 3,000 ») : seul cas où il POSE le drapeau `montant`
# (« transfer the files », « vire ce fichier » ne sont pas de l'argent).
_AMOUNT = (r"(?:\d[\d'’  .,]*\s*(?:chf|eur|€|usd|\$|gbp|£|fr\.?|francs?|euros?|dollars?)(?![a-z])|"
           r"(?:chf|eur|€|usd|\$|gbp|£)\s*\d)")
_MONEY_ORDER_AMOUNT = re.compile(r"(?i)" + _CLAUSE + _MONEY_VERBS + r"[^.?!\n]{0,40}?" + _AMOUNT)
_COMMIT_ORDER = re.compile(
    r"(?i)" + _CLAUSE + r"(?:r[èe]gle|r[ée]glez|signe|signez|r[ée]silie|r[ée]siliez|d[ée]clare|d[ée]clarez|d[ée]duis|"
    r"d[ée]duisez|conteste|contestez|accepte|acceptez|ordonne|"
    r"sign|terminate|cancel|file|declare|deduct|settle|dispute|"
    r"k[üu]ndige|k[üu]ndig|unterschreibe|unterschreib|unterzeichne|deklariere|widersprich)\b")
_MODAL_ORDER = re.compile(
    r"(?i)\b(?:peux-tu|pourrais-tu|tu peux|tu pourrais|merci de|il faut|il faudrait|je veux|je voudrais|j'aimerais|"
    r"on doit|veuillez|kannst du|k[öo]nntest du|w[üu]rdest du|bitte)\b[^.?!\n]{0,60}?\b"
    r"(?:payer|r[ée]gler|virer|verser|signer|r[ée]silier|d[ée]clarer|d[ée]duire|contester|accepter|transf[ée]rer|"
    r"[üu]berweisen|bezahlen|zahlen|k[üu]ndigen|unterschreiben|ausf[üu]hren|deklarieren|absetzen|begleichen)\b")
_PAYMENT_ORDER = re.compile(
    r"(?i)\b(?:fais|faites|effectue|effectuez|lance|lancez|relance|relancez|ex[ée]cute|ex[ée]cutez|programme|programmez|"
    r"valide|validez|approuve|approuvez|faire|effectuer|lancer|relancer|ex[ée]cuter|programmer|valider|approuver|"
    r"make|send|execute|approve|schedule|retry)\s+(?:le|la|un|une|ce|cette|les|mon|ma|mes|the|a|this|my)\s+"
    r"(?:virement|paiement|versement|transfert|r[èe]glement|ordre|payment|transfer|wire)")
_FIX_AMOUNT = re.compile(
    r"(?i)\b(?:fixe[rz]?|d[ée]termine[rz]?|set|decide)\s+(?:le|la|un|une|ce|the|a)\s+(?:montant|prix|tarif|taux|acompte|"
    r"amount|price|rate|fee)\b|\b(?:lege|setze)\s+den\s+(?:betrag|preis|satz)\b")
# Avis qui engage : « dois-je », « puis-je », « quel taux », « should I », « muss ich »… (dont le banc `escalade-fiscal`).
_ADVICE = re.compile(
    r"(?i)\b(?:dois[- ]je|doit[- ]on|je dois|faut[- ]il|puis[- ]je|peux[- ]je|est[- ]ce que (?:je|j'|on) (?:dois|peux|doit)|"
    r"est[- ]ce (?:l[ée]gal|obligatoire|d[ée]ductible|valable)|est[- ]il (?:l[ée]gal|obligatoire|d[ée]ductible|valable)|"
    r"ai[- ]je (?:le droit|int[ée]r[êe]t)|a[- ]t[- ]on le droit|suis[- ]je (?:oblig[ée]e?|tenue?)|vaut[- ]il mieux|"
    r"que (?:dois|faut)[- ](?:je|il)|quel(?:le)? (?:est le )?taux|combien (?:d'imp[ôo]ts?|dois[- ]je|vais[- ]je|je dois|"
    r"je vais|facturer|payer|demander)|quel (?:montant|prix) (?:facturer|payer|demander)|"
    r"can i|could i|should i|must i|do i (?:have|need)|am i (?:required|allowed|liable)|"
    r"is (?:it|this|that) (?:legal|deductible|allowed|valid|binding|enforceable)|how much (?:tax|vat|do i|should i|will i)|"
    r"what (?:tax |vat )?rate|which (?:tax |vat )?rate|"
    r"muss ich|darf ich|soll ich|sollte ich|kann ich|wie (?:hoch|viel)|welche[rsn]? (?:steuer)?satz|"
    r"ist (?:das|es|sie|er) (?:legal|erlaubt|absetzbar|g[üu]ltig|zul[äa]ssig|rechtens)|absetzbar)")
# Juger un montant ou un calcul : « la TVA est-elle juste ? », « vérifie la TVA », « is the amount correct? ».
_JUDGE_AMOUNT = re.compile(
    r"(?i)\b(?:est|sont|para[îi]t|semble)(?:[- ](?:elle|il|ils|elles))?\s+(?:vraiment\s+)?(?:juste|correct|exact|faux|"
    r"fausse|erron[ée]|bon|bonne)\w*|"
    r"\b(?:v[ée]rifie\w*|contr[ôo]le\w*|recalcule\w*|check\w*|verify|double-check|pr[üu]f\w*|[üu]berpr[üu]f\w*|"
    r"nachrechn\w*)\s+(?:(?:si|le|la|les|l'|mon|ma|mes|the|my|this|if|die|den|das|ob)\s*)?(?:tva|taxe\w*|imp[ôo]t\w*|"
    r"montant\w*|calcul\w*|total|vat|tax\w*|amount\w*|steuer\w*|mwst|betrag\w*|summe\w*)\b|"
    r"\bis (?:it|this|that|the \w+) (?:correct|right|accurate|wrong)\b|\b(?:stimmt|richtig|korrekt|falsch)\w*")
_ENGAGEMENT_PATTERNS = (_MONEY_ORDER, _COMMIT_ORDER, _MODAL_ORDER, _PAYMENT_ORDER, _FIX_AMOUNT, _ADVICE, _JUDGE_AMOUNT)

# Familles de simple mention, chacune avec les drapeaux qu'elle peut lever. Une lecture ou une relance ne lève jamais
# `juridique` (un contrat lu, une clause citée : on escalade).
_SCAM = re.compile(
    r"(?i)\b(?:arnaque\w*|escroqu\w*|hame[çc]onnage|phishing|frauduleu\w*|fraudes?|fausses? factures?|faux (?:mail|"
    r"courriel|message|sms|avocat|huissier|fournisseur)|suspect\w*|"
    r"scams?|scammer\w*|fraud\w*|fake (?:invoice|bill|email|e-mail|lawyer)s?|suspicious|spoof\w*|"
    r"betrug\w*|betr[üu]g\w*|gef[äa]lscht\w*|fake-rechnung\w*|verd[äa]chtig\w*)")
_REMIND_ME = re.compile(
    r"(?i)\b(?:rappelle[sz]?[- ]moi (?:de |d['’])|(?:mets|cr[ée]e|ajoute|programme|note)[sz]?(?:[- ]moi)? un rappel|"
    r"remind me to|(?:set|add|create) (?:me )?an? reminder|erinnere mich)")
_FOLLOW_UP = re.compile(
    r"(?i)\b(?:relance\w*|rappel de paiement|lettre de rappel|payment reminder|follow[- ]up|chase (?:the |a |my )?"
    r"(?:client|customer|invoice|payment)|dunning|zahlungserinnerung\w*|mahnung\w*|nachfass\w*)")
_READ = re.compile(
    r"(?i)\b(?:explique[sz]?(?:[- ]moi)?|que (?:veut dire|signifie)|qu'est-ce que (?:veut dire|signifie)|"
    r"[àa] quoi correspond|r[ée]sume[sz]?(?:[- ]moi)?|lis(?:[- ]moi)?|relis|d[ée]chiffre|traduis|"
    r"explain|what does\b.{1,60}\bmean|summari[sz]e|read (?:me|this|the)|walk me through|translate|"
    r"erkl[äa]r\w*|was (?:bedeutet|hei[ßs]t)|fasse?\b.{0,40}\bzusammen|lies|[üu]bersetze?)\b")
_RECEIVED_DOC = re.compile(
    r"(?i)\b(?:factures?|d[ée]comptes?|relev[ée]s?|bulletins?|quittances?|re[çc]us?|"
    r"invoices?|bills?|statements?|receipts?|"
    r"rechnung\w*|abrechnung\w*|beleg\w*|quittung\w*|kontoauszug\w*)\b")
_ALL_FLAGS = frozenset({"fiscal", "juridique", "montant"})
_MONEY_FLAGS = frozenset({"fiscal", "montant"})


def domain_engagement(text: str) -> bool:
    """Le message demande-t-il au système de DÉCIDER ou d'ENGAGER (payer, virer, signer, résilier, déclarer, déduire,
    fixer un montant, donner un avis qui engage, juger un montant) ? Vrai dès qu'un motif répond."""
    return any(p.search(text or "") for p in _ENGAGEMENT_PATTERNS)


def domain_mention(text: str, flags: set[str] | frozenset[str]) -> str | None:
    """Famille de simple mention qui lève TOUS les `flags` (« arnaque », « rappel », « lecture », « relance »), ou None.
    None dès qu'un marqueur d'engagement est présent, qu'aucune famille ne s'applique, ou qu'un drapeau reste (ex.
    `juridique` dans une lecture) : l'escalade forcée s'applique alors comme avant."""
    text = text or ""
    if not flags or domain_engagement(text):
        return None
    families: list[tuple[str, frozenset[str]]] = []
    if _SCAM.search(text):
        families.append(("arnaque", _ALL_FLAGS))
    if _REMIND_ME.search(text):
        families.append(("rappel", _ALL_FLAGS))
    if _READ.search(text) and _RECEIVED_DOC.search(text):
        families.append(("lecture", _MONEY_FLAGS))
    if _FOLLOW_UP.search(text):
        families.append(("relance", _MONEY_FLAGS))
    lifted = frozenset().union(*(f for _, f in families))
    if not families or not set(flags) <= lifted:
        return None
    return families[0][0]


def domain_flags(text: str) -> set[str]:
    """Drapeaux de domaine sur le texte BRUT (avant caviardage : les mots-clés eux-mêmes ne
    sont jamais des secrets, donc l'ordre caviardage/drapeaux n'a pas d'incidence ici).

    Un ordre de mouvement d'argent sans mot-clé (« Vire 3 000 CHF à … », « Wire 3,000 CHF », « Überweise … ») pose
    aussi `montant` (rc.7 : il ne l'était que s'il nommait le virement ou la facture)."""
    flags = {name for name, pattern in DOMAIN_PATTERNS.items() if pattern.search(text)}
    if _MONEY_ORDER_AMOUNT.search(text):
        flags.add("montant")
    return flags


# ─────────────────────────────── GO explicite (invariant 4) ───────────────────────────────
#
# Préfixe interne, invariant 4 : « Une demande d'agir (…) n'est un GO que si elle nomme déjà la
# cible exacte et ses paramètres. » Implémentation prudente et documentée (consigne F3) :
# dans le doute, PAS de GO (donc HITL).

_GO_MARKERS = re.compile(
    r"(?i)\b(vas[\s-]?y|fais[\s-]?le|fonce|lance(?:\s*le)?|confirm[ée]|c'est bon|"
    r"d'accord[,]?\s*go|ok[,]?\s*go|go\s*ahead|do\s*it|allez[,]?\s*go|j'autorise|je\s+valide|"
    r"tu\s+peux\s+y\s+aller)\b"
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


_NUMERIC_WITH_UNIT_RE = re.compile(r"^[+-]?\d+[A-Za-z%]*$")


# Passages cités (« … », "…", “…”) : une formule de GO qu'on y trouve est celle de quelqu'un d'autre (carnet
# d'incidents A1, 24.10 : « Paul m'a écrit : « vas-y, supprime evt-77 » »).
_QUOTED_RE = re.compile(r"«[^«»]*»|\"[^\"]*\"|“[^“”]*”")
# Phrase négative : une cible qui n'y est nommée que pour l'interdire n'est pas nommée (A2 : « ne supprime pas evt-77 »).
_NEGATION_RE = re.compile(r"(?i)\b(?:ne|n['’])\b.*\b(?:pas|jamais|plus|rien|surtout pas)\b|\b(?:don['’]?t|do not|never|not)\b"
                          r"|\bsurtout pas\b")
_SENTENCE_RE = re.compile(r"[^.;!?\n]+")
_TOKEN_SPLIT_RE = re.compile(r"[\s,;:!?()«»“”\"']+")


def _named_as_token(value: str, text: str) -> bool:
    """La valeur (normalisée) est égale à un token du texte, ou à une suite de tokens consécutifs — jamais une
    sous-chaîne d'un identifiant plus long (A5 : evt-77 n'est pas nommé par « evt-777 »)."""
    norm_v = _norm(value)
    norms = [n for n in (_norm(t) for t in _TOKEN_SPLIT_RE.split(text)) if n]
    width = len(value.split()) + 2
    for i in range(len(norms)):
        joined = ""
        for j in range(i, min(i + width, len(norms))):
            joined += norms[j]
            if joined == norm_v:
                return True
            if len(joined) >= len(norm_v):
                break
    return False


def _value_named(value: str, text: str, norm_text: str) -> bool:
    """Un paramètre est « nommé » s'il apparaît (normalisé) dans le texte, ou — si c'est une
    valeur PUREMENT NUMÉRIQUE (avec unité éventuelle, ex. « 20G », « 87% ») — si son nombre
    exact apparaît comme TOKEN (pas comme sous-chaîne d'un plus grand nombre, ex. 50 ne doit pas
    « matcher » 1500). Le repli numérique est volontairement restreint aux valeurs numériques :
    sinon un identifiant comme « hote1 » matcherait à tort sur le « 1 » de « hote21 »."""
    value = value.strip()
    if not value:
        return True  # paramètre vide : rien à vérifier, ne bloque pas la détection
    norm_v = _norm(value)
    if norm_v and norm_v in norm_text:
        sentences = [x for x in _SENTENCE_RE.findall(text) if norm_v in _norm(x)]
        if any(not _NEGATION_RE.search(x) and _named_as_token(value, x) for x in sentences):
            return True
    if _NUMERIC_WITH_UNIT_RE.match(value):
        for d in re.findall(r"\d+", value):
            if re.search(rf"(?<!\d){re.escape(d)}(?!\d)", text):
                return True
    return False


def has_explicit_go(text: str, call: ToolCall, spec: ToolSpec | None = None) -> bool:
    """GO = message humain du tour courant qui nomme l'outil/la cible ET ses paramètres.

    Limites connues (documentées dans docs/INVARIANTS.md) :
      - ne regarde que le texte du tour COURANT (`turn.input.text`), pas tout l'historique —
        un GO donné dans un tour précédent, ou qui arrive dans un message séparé, n'est pas vu ;
      - un appel d'outil SANS paramètre ne peut jamais avoir de GO confirmé (on ne peut pas
        vérifier qu'« ses paramètres » sont nommés s'il n'y en a pas) — c'est volontaire, pas un
        oubli : par défaut, HITL ;
      - l'identité de l'auteur n'entre PAS dans CETTE fonction (elle ne regarde que le texte) :
        c'est `check_action`, ci-dessous, qui combine `has_explicit_go` avec
        `GoAuthorizedSenders.authorized(turn.sender)` pour décider le verdict final.
    """
    if not _GO_MARKERS.search(_QUOTED_RE.sub(" ", text)):
        return False
    values = [str(v) for v in call.arguments.values()]
    if not values:
        return False
    norm_text = _norm(text)
    return all(_value_named(v, text, norm_text) for v in values)


@dataclass(frozen=True)
class GoAuthorizedSenders:
    """Qui peut donner un GO sur une action `DESTRUCTIVE` : une liste de config (`extra_go_senders` et
    `telegram_ids`, `NokkerConfig.go_senders()`), pas une heuristique. **Vide par défaut** : personne ; chaque
    instance pose les siens (l'instance interne ses opérateurs, Lokkal `local-owner`).

    `Turn.sender` existe (`core/turn.py`) et `core/loop.py::run_turn` le remplit depuis `TurnInput.sender` avant
    d'appeler `check_action`. `check_action` (ci-dessous) exige `go_senders.authorized(turn.sender)` EN PLUS de
    `has_explicit_go` pour accorder un GO sur une action `DESTRUCTIVE` : un texte qui a toutes les apparences d'un GO
    mais vient d'un expéditeur non autorisé (ou `sender=None`, ex. canal qui ne porte pas d'identité) reste `HITL`.

    La forme exacte de `sender` (adresse mail, identifiant Telegram, nom court…) dépend du canal amont : la config
    liste toutes les formes utiles, en minuscules.
    """

    senders: frozenset[str] = frozenset()

    def authorized(self, sender: str | None) -> bool:
        return sender is not None and sender.strip().lower() in self.senders


# ─────────────────────────────── allowlist SEND_EXTERNAL ───────────────────────────────

_RECIPIENT_KEYS = ("to", "recipient", "recipients", "email", "address", "destinataire", "destinataires", "attendees")


def _extract_recipients(arguments: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in _RECIPIENT_KEYS:
        v = arguments.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, list):
            out.extend(str(x).strip() for x in v if str(x).strip())
    return out


@dataclass
class Allowlist:
    """Destinataires externes autorisés pour un outil SEND_EXTERNAL — domaines ou adresses
    exactes. Préfixe interne invariant 2 : un expéditeur/destinataire hors allowlist → brouillon
    + escalade, jamais d'envoi direct."""

    domains: set[str] = field(default_factory=set)
    addresses: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, domains: list[str] | None = None, addresses: list[str] | None = None) -> "Allowlist":
        return cls(domains={d.lower().lstrip("@") for d in (domains or [])},
                   addresses={a.lower() for a in (addresses or [])})

    def allows(self, recipient: str) -> bool:
        r = recipient.strip().lower()
        if r in self.addresses:
            return True
        domain = r.rsplit("@", 1)[-1] if "@" in r else r
        return domain in self.domains


# ─────────────────────────────── implémentation du port Invariants ───────────────────────────────


@dataclass
class DeterministicInvariants:
    """Implémentation du port `nokker.core.ports.Invariants` — couche 1 déterministe.

    `known` : coffre de valeurs de secrets réelles (chargé par l'appelant via
    `KnownSecrets.load(...)`, jamais inventé ici) ; `allowlist` : destinataires externes admis ;
    `go_senders` : qui a le droit de donner un GO (`GoAuthorizedSenders`), appliqué dans
    `check_action` via `turn.sender`.

    Multi-locataire (docs/TENANCY.md §5) : `go_senders` ne vaut que dans le locataire interne (ou hors de tout
    scope : bancs, tests). Chez un testeur, le GO vient SEULEMENT d'un compte `us_…` de CE locataire, d'après
    `account_tenant(sender) -> tn_… | None` (injecté par la frontière, `nokker.tenancy.resolve.account_tenant_fn`) ;
    Les opérateurs internes n'y ont aucun GO. Sans `account_tenant`, aucun GO chez un testeur.
    """

    known: KnownSecrets = field(default_factory=KnownSecrets)
    allowlist: Allowlist = field(default_factory=Allowlist)
    go_senders: GoAuthorizedSenders = field(default_factory=GoAuthorizedSenders)
    account_tenant: Callable[[str | None], str | None] | None = None
    _context_cache: dict = field(default_factory=dict, init=False, repr=False)

    def go_authorized(self, sender: str | None) -> bool:
        tid = current_tenant_or_none()
        if tid is None or tid == INTERNAL_TENANT:
            return not (sender or "").startswith("us_") and self.go_senders.authorized(sender)
        return (bool(sender) and str(sender).startswith("us_") and self.account_tenant is not None
                and self.account_tenant(sender) == tid)

    def check_input(self, text: str) -> InputCheck:
        red = redact(text, self.known)
        return InputCheck(text=red.text, redacted=red.names, domain_flags=domain_flags(text))

    def check_action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> Verdict:
        # Outil sans spec déclarée → traité comme WRITE (PLAN §2.3 étape 5, ports.py docstring).
        effect = call_effect(spec, call.arguments)
        subject = call.id

        if effect is Effect.SEND_BULK:
            return Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="effect:send_bulk",
                           reason="envoi de masse toujours refusé, même sur demande humaine explicite "
                                  "(règle du service)", subject=subject)

        if effect in (Effect.DESTRUCTIVE, Effect.SEND_EXTERNAL, Effect.SPEND) and turn.channel == RELAY_CHANNEL:
            # Tour venu du web (relais, X10) : un GO textuel ne lève jamais la carte forte, même d'un expéditeur
            # autorisé, et l'allowlist n'y change rien : signature au téléphone.
            return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=f"effect:{effect.value}:relayed",
                           reason="action demandée depuis le web : carte forte signée au téléphone, aucun GO "
                                  "textuel n'y supplée", subject=subject, strength="strong")

        if effect is Effect.DESTRUCTIVE:
            text_go = has_explicit_go(turn.input.text, call, spec)
            sender_ok = self.go_authorized(turn.sender)
            if text_go and sender_ok:
                return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="effect:destructive:go",
                               reason=f"GO explicite de {turn.sender} dans le tour courant "
                                      "(cible et paramètres nommés)", subject=subject)
            if text_go and not sender_ok:
                return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1,
                               rule="effect:destructive:unauthorized_sender",
                               reason=f"GO textuel détecté mais expéditeur non autorisé à le donner "
                                      f"({turn.sender!r})", subject=subject, strength="strong")
            return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                           reason="action destructive/irréversible sans GO explicite nommant la cible exacte "
                                  "et ses paramètres", subject=subject, strength="strong")

        if effect is Effect.SEND_EXTERNAL:
            recipients = _extract_recipients(call.arguments)
            if recipients and all(self.allowlist.allows(r) for r in recipients):
                return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1,
                               rule="effect:send_external:allowlist", reason="destinataire(s) dans l'allowlist",
                               subject=subject)
            bad = [r for r in recipients if not self.allowlist.allows(r)] or ["destinataire non identifiable"]
            return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:send_external",
                           reason=f"hors allowlist, brouillon + escalade : {', '.join(bad)}", subject=subject,
                           strength="standard")

        if effect is Effect.SPEND:
            return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:spend",
                           reason="dépense sans GO explicite", subject=subject, strength="standard")

        if effect is Effect.SECRET:
            # ALLOW : le caviardage du RÉSULTAT n'est plus décidé ici — `core/loop.py` appelle
            # `redact_result` (ci-dessous) sur CHAQUE résultat d'outil, avant le contexte et le
            # store (contrat `main` e076d54). `check_action` ne fait que laisser passer l'appel.
            return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="effect:secret",
                           reason="résultat transformé en poignée [secret:NOM] par redact_result "
                                  "avant stockage/contexte", subject=subject)

        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=f"effect:{effect.value}",
                       subject=subject)

    def redact_result(self, result: ToolResult, spec: ToolSpec | None) -> ToolResult:
        """Appelée par `core/loop.py` sur CHAQUE résultat d'outil, avant le contexte et le store
        (PLAN §2.3 étape 6, contrat `main` e076d54) — la garantie ne dépend plus de la source
        d'outils qui a exécuté l'appel.

        - effet `SECRET` : traitement complet (coffre + motifs forts + repli « mot de passe : »
          et entropie) — c'est un canal déclaré comme renvoyant des secrets, le traitement le
          plus large est justifié ;
        - tout autre résultat (y compris sans spec) : coffre de valeurs connues + motifs FORTS
          seulement (`weak=False` : pas de repli entropie/mot-de-passe, trop bruyant pour un
          JSON d'outil quelconque) — un filet de sécurité qui ne dépend pas de l'étiquette
          déclarée par qui a écrit le `ToolSpec` (une erreur d'étiquetage ne doit pas suffire à
          faire fuiter une valeur du coffre).
        """
        effect = spec.effect if spec is not None else Effect.WRITE
        red = redact(result.content, self.known, weak=effect is Effect.SECRET)
        if not red.names:
            return result
        handles = list(dict.fromkeys([*result.secret_handles, *red.names]))
        return ToolResult(call_id=result.call_id, name=result.name, content=red.text, is_error=result.is_error,
                          reservoir_id=result.reservoir_id, secret_handles=handles)

    def scrub_output(self, text: str) -> str:
        return redact(text, self.known).text

    def redact_context(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filet du fil (`core/loop.py::_wire_messages`, juste avant chaque appel modèle) : coffre + motifs forts sur
        tout le texte qui part (`qa/secrets.py::redact_messages`). Cache par texte : l'historique repart à chaque
        appel du tour, seul le nouveau est relu ; vidé quand le coffre change ou passe 4096 entrées."""
        items = tuple(self.known.items_by_length_desc())
        sig = hash(items)
        cache = self._context_cache
        if cache.get("__sig__") != sig or len(cache) > 4096:
            cache.clear()
            cache["__sig__"] = sig
        known = self.known if items else None

        def scrub(text: str) -> str:
            got = cache.get(text)
            if got is None:
                got = redact(text, known, weak=False, wire=True).text  # le fil : rien de ce que refuse le Router
                cache[text] = got
            return got

        return redact_messages(messages, scrub=scrub)


__all__ = [
    "Allowlist", "DOMAIN_PATTERNS", "DeterministicInvariants", "GoAuthorizedSenders", "domain_flags",
    "has_explicit_go",
]
