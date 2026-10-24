"""nokker/qa/secrets.py — détection et caviardage de secrets (PLAN-P1 §2.3 étapes 1 et 8, F3).

Règle absolue : AUCUNE valeur de secret n'est jamais journalisée, loggée ni renvoyée par ce
module — seul le texte caviardé et les NOMS stables des poignées `[secret:NOM]` en sortent.

Deux familles de détection, combinées dans `redact()` :
  1. coffre de valeurs CONNUES (`KnownSecrets`, chargées depuis l'env ou un fichier au
     démarrage) — comparaison exacte, nom = nom de la variable/entrée ; deux contournements
     courants sont couverts pour les valeurs connues : encodage base64 (standard/URL-safe, avec
     ou sans padding) et « espacement » caractère par caractère ;
  2. motifs — clés API usuelles, jetons, PEM, JWT, IBAN (validé mod-97), « mot de passe : …»,
     puis un repli par entropie pour le reste, avec des gardes explicites contre les faux
     positifs (hash git, UUID, sha256 en hexadécimal) — nom = `type-<hash court>`.

Contournements NON couverts (limites documentées dans docs/INVARIANTS.md) : une valeur connue
réécrite avec des synonymes/traduction, un chiffrement autre que base64, un secret coupé en
morceaux sur plusieurs messages, ou un secret qui n'est dans AUCUN coffre connu et qui ne
ressemble à aucun motif/à aucun profil d'entropie (ex. un mot de passe court, bas en entropie,
du type « azerty2026 »).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ─────────────────────────────── poignées ───────────────────────────────

HANDLE_RE = re.compile(r"\[secret:[^\]\[]+\]")


def _handle(name: str) -> str:
    return f"[secret:{name}]"


def _type_name(kind: str, value: str) -> str:
    """Nom stable mais qui ne permet pas de retrouver la valeur : hash court de la valeur."""
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:6]
    return f"{kind}-{digest}"


# ─────────────────────────────── coffre de valeurs connues ───────────────────────────────


@dataclass
class KnownSecrets:
    """Valeurs connues au démarrage (env ou fichier), comparées EXACTEMENT.

    Les valeurs ne sont jamais journalisées : ni par ce module, ni par `__repr__` (volontairement
    non redéfini pour rester le dataclass par défaut — à ne JAMAIS passer à un logger tel quel ;
    utiliser `KnownSecrets.names()` pour un affichage sûr).
    """

    _values: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, value: str) -> None:
        if value:
            self._values[name] = value

    def merge(self, other: "KnownSecrets") -> "KnownSecrets":
        self._values.update(other._values)
        return self

    def names(self) -> list[str]:
        """Noms connus, SANS les valeurs — sûr à journaliser."""
        return list(self._values)

    def items_by_length_desc(self) -> list[tuple[str, str]]:
        # plus longue valeur d'abord : si une valeur est sous-chaîne d'une autre, on caviarde
        # la plus spécifique sans laisser de reste de l'autre dans le texte.
        return sorted(self._values.items(), key=lambda kv: len(kv[1]), reverse=True)

    @classmethod
    def from_env(cls, names: list[str] | None = None, prefix: str | None = None) -> "KnownSecrets":
        """Charge depuis l'environnement : noms exacts (`names`) et/ou par préfixe (`prefix`)."""
        ks = cls()
        for key, value in os.environ.items():
            if not value:
                continue
            if (names and key in names) or (prefix and key.startswith(prefix)):
                ks.add(key, value)
        return ks

    @classmethod
    def from_file(cls, path: str | Path) -> "KnownSecrets":
        """Charge depuis un fichier : JSON (`{"NOM": "valeur"}`) ou `NOM=valeur` ligne à ligne
        (format `.env`, `#` = commentaire). Fichier absent → coffre vide, pas une erreur."""
        ks = cls()
        p = Path(path)
        if not p.exists():
            return ks
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".json":
            data = json.loads(text)
            for name, value in data.items():
                if isinstance(value, str):
                    ks.add(name, value)
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                ks.add(name.strip(), value.strip().strip("'\""))
        return ks

    @classmethod
    def load(cls, *, env_names: list[str] | None = None, env_prefix: str | None = None,
              file_path: str | Path | None = None) -> "KnownSecrets":
        ks = cls()
        if env_names or env_prefix:
            ks.merge(cls.from_env(env_names, env_prefix))
        if file_path:
            ks.merge(cls.from_file(file_path))
        return ks


def _b64_variants(value: str) -> list[str]:
    """Encodages base64 courants d'une valeur connue (standard et URL-safe, avec et sans
    padding `=`) — pour attraper un secret recopié « encodé » plutôt qu'en clair."""
    if not value:
        return []
    raw = value.encode("utf-8", "replace")
    variants: set[str] = set()
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        enc = encoder(raw).decode("ascii")
        variants.add(enc)
        variants.add(enc.rstrip("="))
    return [v for v in variants if v and len(v) >= 4]


# ─────────────────────────────── motifs connus ───────────────────────────────

_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Anthropic avant le motif générique sk- (plus spécifique, doit matcher en premier)
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("telegram-token", re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{35}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
]

_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{9,30}\b")

# « mot de passe / password / mdp / pw / passwort : VALEUR » — VALEUR = un seul « mot » (pas
# d'espace), ce qui évite de caviarder toute la phrase qui suit.
_PASSWORD_KEYWORD_RE = re.compile(r"(?i)\b(mot de passe|mdp|password|pw|passwort)\s*[:=]\s*(\S+)")

# Même chose en phrase (carnet d'incidents D2, 24.10) : « le mot de passe du portail c'est VALEUR ». Sans « : », la
# valeur doit AVOIR l'air d'un mot de passe (lettre + chiffre ou symbole, 6 caractères ou plus) : « le mot de passe est
# dans le coffre » reste en clair.
_PASSWORD_PHRASE_RE = re.compile(
    r"(?i)\b(mot de passe|mdp|password|passwort)((?:\s+(?:du|de la|de l'|de|des|pour|of|for|for the)\s+[^\s,;:]+){0,2})"
    r"\s+(c'est|c’est|est|is|ist)\s+(\S+)")


def _looks_like_password(value: str) -> bool:
    return (len(value) >= 6 and any(c.isalpha() for c in value)
            and any(c.isdigit() or not c.isalnum() for c in value) and not HANDLE_RE.fullmatch(value))


# Clé secrète AWS (40 caractères base64) : sans préfixe propre, attrapée seulement juste après son mot-clé (carnet
# d'incidents D4 : « secret wJal… » dans un mail lu, où le repli par entropie ne tourne pas). Un hash git (40 hex)
# après « secret » reste en clair.
_AWS_SECRET_RE = re.compile(
    r"(?i)\b(aws_secret_access_key|secret_access_key|secret[_ ]?key|secret)(\s*[:=]\s*|\s+)([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])")


def _iban_valid(code: str) -> bool:
    """Clé de contrôle mod-97 (ISO 13616) — sans elle, le motif seul fait trop de faux positifs
    (deux lettres + deux chiffres + alphanumérique est très commun hors IBAN)."""
    code = code.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{9,30}", code):
        return False
    rearranged = code[4:] + code[:4]
    try:
        digits = "".join(str(int(ch, 36)) for ch in rearranged)
        return int(digits) % 97 == 1
    except ValueError:
        return False


# ─────────────────────────────── repli par entropie ───────────────────────────────
#
# Gardes explicites contre les faux positifs (consigne F3) : hash git (7/8/40 hex), sha256
# (64 hex), UUID — tous exclus avant même de calculer l'entropie. Le motif n'inclut PAS le
# tiret ni le point : ça exclut au passage la plupart des identifiants techniques « parlants »
# (noms de modèle, versions semver, slugs, id de branche), qui ont une entropie élevée mais ne
# sont pas des secrets (limite documentée dans docs/INVARIANTS.md : un vrai secret qui
# contiendrait un tiret ou un point n'est PAS attrapé par ce repli — seulement par les motifs
# précis ci-dessus ou le coffre de valeurs connues).

_SAFE_HEX_LENGTHS = {7, 8, 10, 12, 32, 40, 64}
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ENTROPY_CANDIDATE_RE = re.compile(r"\b[A-Za-z0-9+/_]{20,100}={0,2}\b")
_ENTROPY_THRESHOLD = 3.5  # bits/caractère (Shannon) — repli, pas une preuve


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


_WORDS_SPLIT_RE = re.compile(r"[/_+]")


def _looks_like_safe_token(token: str) -> bool:
    if _UUID_RE.match(token):
        return True
    # Mots lisibles joints par « / », « _ » ou « + » (« beauty/lifestyle/UGC », « docs/nokker/roadmap »,
    # « send_message ») : faux positif vu en réel le 19.10 dans une réponse. Un secret base64 a des chiffres
    # ou des morceaux de lettres longs et sans voyelles ; ici chaque morceau est un mot court, lettres seules.
    # Un morceau tout en chiffres court (année, numéro) compte aussi (« publications/2026/donnees_generales », chemin
    # d'URL caviardé en réel le 20.10) ; un morceau qui MÉLANGE lettres et chiffres reste suspect (base64).
    parts = [x for x in _WORDS_SPLIT_RE.split(token) if x]
    # Un nombre court n'est « sûr » que dans un CHEMIN (le jeton contient un « / » : publications/2026/donnees_generales) ;
    # joint seulement par « _ » ou « + », c'est la forme d'un mot de passe « mots + année » (Acme_Backup_2026_NAS),
    # caviardé avant b573b0d et qui doit le rester (revue du 20.10).
    digits_ok = "/" in token
    if (len(parts) >= 2 and any(x.isalpha() for x in parts)
            and all((x.isalpha() and len(x) <= 15) or (digits_ok and x.isdigit() and len(x) <= 8) for x in parts)):
        return True
    if _HEX_RE.match(token) and len(token) in _SAFE_HEX_LENGTHS:
        return True
    return False


def _entropy_candidates(text: str) -> list[str]:
    out = []
    for m in _ENTROPY_CANDIDATE_RE.finditer(text):
        token = m.group(0)
        if _looks_like_safe_token(token):
            continue
        classes = sum(bool(re.search(p, token)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
        if classes < 2:
            continue  # trop uniforme (un seul type de caractère) : pas assez de signal
        if _shannon_entropy(token) >= _ENTROPY_THRESHOLD:
            out.append(token)
    return out


# ─────────────────────────────── caviardage ───────────────────────────────


@dataclass
class RedactResult:
    text: str
    names: list[str] = field(default_factory=list)  # noms des poignées, dans l'ordre d'apparition


def redact(text: str, known: KnownSecrets | None = None, *, weak: bool = True) -> RedactResult:
    """Caviarde `text` : valeurs connues d'abord, puis motifs précis, puis (si `weak`) le repli
    « mot de passe : … » et le repli par entropie.

    `weak=False` désactive les deux motifs les moins sûrs (mot de passe sans coffre, entropie) —
    réservé aux résultats d'outils non déclarés `SECRET` (`qa/invariants.py::redact_result`),
    où ils seraient trop bruyants sur un contenu technique quelconque. `weak=True` (défaut) est
    utilisé pour le texte humain (`check_input`/`scrub_output`) et pour un résultat SECRET.

    Idempotent sur sa propre sortie (une poignée `[secret:...]` n'est jamais re-caviardée).
    """
    if not text:
        return RedactResult(text=text, names=[])

    names: list[str] = []

    def _mark(name: str) -> None:
        if name not in names:
            names.append(name)

    out = text

    # 1. valeurs connues (exactes, les plus longues d'abord)
    if known:
        for name, value in known.items_by_length_desc():
            if value and value in out:
                out = out.replace(value, _handle(name))
                _mark(name)

    # 1b. contournement « encodé en base64 » d'une valeur connue (jailbreak courant : le modèle
    # recopie le secret encodé plutôt qu'en clair, en pensant que ça échappe au scrubber).
    if known:
        for name, value in known.items_by_length_desc():
            for variant in _b64_variants(value):
                if variant and variant in out:
                    out = out.replace(variant, _handle(name))
                    _mark(name)

    # 1c. contournement « espacé » d'une valeur connue (un caractère par « mot », ou des espaces
    # insérés entre les caractères). Réservé aux valeurs CONNUES (regex construite par valeur,
    # coûteux et trop permissif si on le faisait pour un token générique quelconque) ; longueur
    # minimale 4 pour éviter de caviarder un mot ordinaire qui contiendrait par hasard les mêmes
    # lettres espacées.
    if known:
        for name, value in known.items_by_length_desc():
            if len(value) < 4:
                continue
            spaced = re.compile(r"[ \t]*".join(re.escape(ch) for ch in value))
            out, n = spaced.subn(_handle(name), out)
            if n:
                _mark(name)

    # 2. blocs PEM (avant les motifs génériques : ils contiennent souvent des `/` qui
    #    casseraient des coupures de ligne si on les traitait token par token)
    def _pem_sub(m: re.Match[str]) -> str:
        name = _type_name("pem", m.group(0))
        _mark(name)
        return _handle(name)

    out = _PEM_RE.sub(_pem_sub, out)

    # 3. motifs connus (clés API, jetons, JWT…)
    for kind, pattern in _PATTERNS:
        def _sub(m: re.Match[str], kind: str = kind) -> str:
            name = _type_name(kind, m.group(0))
            _mark(name)
            return _handle(name)

        out = pattern.sub(_sub, out)

    # 4. IBAN, validé par la clé de contrôle (sinon le motif seul est trop permissif)
    def _iban_sub(m: re.Match[str]) -> str:
        candidate = m.group(0)
        if not _iban_valid(candidate):
            return candidate
        name = _type_name("iban", candidate)
        _mark(name)
        return _handle(name)

    out = _IBAN_RE.sub(_iban_sub, out)

    def _aws_secret_sub(m: re.Match[str]) -> str:
        value = m.group(3)
        if re.fullmatch(r"[0-9a-fA-F]{40}", value) or not (re.search(r"[a-z]", value) and re.search(r"[A-Z]", value)
                                                          and re.search(r"\d", value)):
            return m.group(0)
        name = _type_name("aws-secret-key", value)
        _mark(name)
        return f"{m.group(1)}{m.group(2)}{_handle(name)}"

    out = _AWS_SECRET_RE.sub(_aws_secret_sub, out)

    if weak:
        # 5. « mot de passe / password / mdp / pw : VALEUR »
        def _pw_sub(m: re.Match[str]) -> str:
            keyword, value = m.group(1), m.group(2)
            if HANDLE_RE.fullmatch(value):
                return m.group(0)  # déjà une poignée (ex. posée par une passe précédente)
            name = _type_name("password", value)
            _mark(name)
            return f"{keyword} {_handle(name)}"

        out = _PASSWORD_KEYWORD_RE.sub(_pw_sub, out)

        def _pw_phrase_sub(m: re.Match[str]) -> str:
            raw = m.group(4)
            value = raw.rstrip(",.;)")
            if not _looks_like_password(value):
                return m.group(0)
            name = _type_name("password", value)
            _mark(name)
            return f"{m.group(1)}{m.group(2)} {m.group(3)} {_handle(name)}{raw[len(value):]}"

        out = _PASSWORD_PHRASE_RE.sub(_pw_phrase_sub, out)

        # 6. repli par entropie, uniquement HORS des poignées déjà posées (sinon on re-scanne le
        #    hash court d'une poignée et on la double-caviarde).
        segments = HANDLE_RE.split(out)
        handles = HANDLE_RE.findall(out)
        rebuilt: list[str] = []
        for i, segment in enumerate(segments):
            for token in _entropy_candidates(segment):
                name = _type_name("entropy", token)
                segment = segment.replace(token, _handle(name))
                _mark(name)
            rebuilt.append(segment)
            if i < len(handles):
                rebuilt.append(handles[i])
        out = "".join(rebuilt)

    return RedactResult(text=out, names=names)


__all__ = ["HANDLE_RE", "KnownSecrets", "RedactResult", "redact"]
