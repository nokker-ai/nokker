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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ─────────────────────────────── poignées ───────────────────────────────

HANDLE_RE = re.compile(r"\[secret:[^\]\[]+\]")


def _handle(name: str) -> str:
    return f"[secret:{name}]"


# Hash court écrit en LETTRES (0-9 → g-p) : avec des chiffres, `[secret:aws-access-key-ce6d24]` se lisait
# « secret: <valeur de 21 caractères, lettres et chiffres> » pour un détecteur clé/valeur (celui du Router : refus
# `400 secret_detected` de tout le tour, banc D1 du 25.10). Aucun type ne contient de chiffre : une poignée typée n'a
# donc jamais la forme d'une valeur secrète.
_DIGEST_LETTERS = str.maketrans("0123456789", "ghijklmnop")


# Poignée typée d'avant ce changement (hash en chiffres hexadécimaux), rangée dans l'historique d'un fil : réécrite
# en lettres à chaque passage, sinon le fil reste refusé par le Router à chaque appel.
_LEGACY_TYPED_HANDLE_RE = re.compile(r"\[secret:([a-z][a-z-]*[a-z]-)([0-9a-f]{6})\]")


def _legacy_handle_sub(m: re.Match[str]) -> str:
    return f"[secret:{m.group(1)}{m.group(2).translate(_DIGEST_LETTERS)}]"


def _type_name(kind: str, value: str) -> str:
    """Nom stable mais qui ne permet pas de retrouver la valeur : hash court de la valeur, en lettres."""
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:6].translate(_DIGEST_LETTERS)
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

# Bloc PEM entier ; sans marqueur de fin (lecture partielle d'un fichier, sortie coupée), l'en-tête et le corps base64
# qui suit (retours à la ligne réels ou échappés `\n` d'une chaîne JSON : clé de service GCP, `private_key`).
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    r"(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|(?:[A-Za-z0-9+/=\s]|\\[nr])*)", re.DOTALL)

# Bornes ASCII (comme le détecteur du Router) : `\b` de Python compte une lettre accentuée comme une lettre de mot.
_B = r"(?<![A-Za-z0-9_])"
_E = r"(?![A-Za-z0-9_])"

# Familles à préfixe propre, du plus spécifique au plus générique (revue du 25.10 contre les familles courantes et
# contre ce que le Router refuse : tout ce qu'il bloque doit être caviardé AVANT, sinon le tour casse).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Anthropic avant le motif générique sk- (plus spécifique, doit matcher en premier)
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),  # OpenAI et compatibles : sk-, sk-proj-, sk-svcacct-
    ("stripe-key", re.compile(_B + r"(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}" + _E)),
    ("webhook-secret", re.compile(_B + r"whsec_[A-Za-z0-9+/=_-]{20,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-token", re.compile(_B + r"github_pat_[A-Za-z0-9_]{22,}" + _E)),
    ("gitlab-token", re.compile(_B + r"gl(?:pat|dt|ptt|rt)-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack-token", re.compile(_B + r"xapp-\d-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}")),
    ("google-api-key", re.compile(_B + r"AIza[0-9A-Za-z_-]{35}")),
    ("hf-token", re.compile(_B + r"hf_[A-Za-z0-9]{30,}" + _E)),
    ("npm-token", re.compile(_B + r"npm_[A-Za-z0-9]{36}" + _E)),
    ("api-key", re.compile(_B + r"sik_[0-9a-f]{32,}" + _E)),  # clé du Router
    ("telegram-token", re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{35}\b")),
    ("aws-access-key", re.compile(_B + r"(?:AKIA|ASIA)[0-9A-Z]{16}" + _E)),
    # Secret client Azure AD (Entra) : 3 car., un chiffre, « Q~ », puis 31 à 34 car.
    ("azure-secret", re.compile(r"(?<![A-Za-z0-9_~.-])[A-Za-z0-9_~.-]{3}\dQ~[A-Za-z0-9_~.-]{31,34}(?![A-Za-z0-9_~.-])")),
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
    r"(?i)\b(aws_secret_access_key|secret_?access_?key|secret[_ ]?key|secret)(['\"]?\s*[:=]\s*['\"]?|\s+)"
    r"([A-Za-z0-9/+]{40})(?![A-Za-z0-9/+=])")
# Jeton de session AWS (identifiants temporaires `ASIA…`) : long base64, après son mot-clé.
_AWS_SESSION_RE = re.compile(r"(?i)\b(aws_session_token|session_?token)(['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9/+=]{100,})")

# Azure : chaîne de connexion (`AccountKey=`, `SharedAccessKey=`) et signature d'une URL SAS (`sig=`).
_AZURE_KEY_RE = re.compile(r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature)(=)([A-Za-z0-9+/=%]{20,})")
_AZURE_SIG_RE = re.compile(r"(?<![A-Za-z0-9_])(sig=)([A-Za-z0-9%+/=]{40,})")

# Identifiants dans une URL : `scheme://user:motdepasse@hôte`. Les valeurs d'exemple et les variables restent. Le mot de
# passe s'arrête avant `?` et `#` (délimiteurs de l'URL, encodés dans un vrai mot de passe) : `https://hôte:443?email=
# jean@exemple.ch` est un port suivi d'un paramètre, pas un identifiant (revue du train du 2.11, défaut 5).
_URL_CRED_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]{1,20}://[^\s:/@'\"<>]*:)([^\s/@'\"<>?#]{3,})@")
_URL_PLACEHOLDERS = {"password", "passwd", "pass", "secret", "changeme", "example", "motdepasse", "mdp", "pwd",
                     "postgres", "root", "admin", "user", "test"}

# `Authorization: Bearer <jeton>` (en-tête, ligne curl) : jeton littéral de 20 car. ou plus, lettres et chiffres.
_BEARER_RE = re.compile(r"\b([Bb]earer[ \t]+)([A-Za-z0-9._~+/-]{20,}=*)")

# « mot-clé : valeur » (`"api_key": "…"`, `token=…`, `client_secret: …`) : valeur de 16 car. ou plus, lettres ET
# chiffres, qui n'est ni un nom de constante ni un identifiant de code (`compute_token_v2`) — même règle que le Router.
_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9\[])((?:[a-z0-9]+[_-])*(?:api[_-]?key|apikey|secret|token|passwd|password|access[_-]?key)"
    r"['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9_\-]{16,})(?![A-Za-z0-9_\-(.\[])")

# Paramètre de requête qui porte une clé ou une signature (`?key=…`, `&sig=…`, `&signature=…`) : un identifiant, souvent
# un lien magique ou signé. Les `…token=` passent par `_KEY_VALUE_RE`. Même règle de valeur (16 car. ou plus, lettres
# ET chiffres, pas un identifiant de code) : `?key=prix`, `?key=2026-10-27` restent.
_QUERY_TOKEN_RE = re.compile(r"(?i)([?&](?:key|sig|signature)=)([A-Za-z0-9_\-%~+/=]{16,})(?![A-Za-z0-9_\-%~+/=])")

# ─────────────── valeurs publiques sans ambiguïté (fix-batch-3, revue du train du 2.11, §0 décision 3) ───────────────
#
# Deux formes qu'un motif caviarde à tort SELON LE CONTEXTE. Elles restent en clair pour le texte humain, la réponse,
# l'historique et les résultats d'outils, JAMAIS dans le filet du fil (`wire=True`, ce qui part au modèle) : le Router
# refuse le tour entier sur `AIza…` et sur `token: <16 car. lettres et chiffres>` (`400 secret_detected`). Dans le doute,
# la valeur est caviardée.
#
# 1. Hash git (40 hex) juste après `token:`/`token=` : un mot de git à moins de 160 caractères (commit, sha, rev, HEAD,
#    merge, parent, tree, rebase, cherry-pick, rev-parse), le mot-clé est `token` tout court ou préfixé d'un mot qui
#    n'est pas un identifiant (`next_token` oui, `access_token`, `auth_token`, `refresh_token`… non), et aucun mot
#    d'identifiant autour (password, secret, Authorization, Bearer, api_key, credential). Sans contexte git, 40 hex
#    restent caviardés : un jeton OAuth GitHub d'avant 2021 et bien des jetons d'API ont cette forme.
# 2. Clé `AIza…` (clé d'API Google, qui PEUT être secrète : Maps, Gemini, YouTube côté serveur, facturées au
#    propriétaire) : en clair seulement comme `apiKey` d'une configuration WEB Firebase, publique par conception
#    (`authDomain: "<projet>.firebaseapp.com"` ou `appId: "1:<n>:web:<hex>"` dans les 600 caractères), et sans rien de
#    côté serveur à côté (`private_key`, `client_email`, `service_account`, `client_secret`).
_GIT_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9\[])((?:[a-z0-9]+[_-])*token['\"]?\s*[:=]\s*['\"]?)((?-i:[0-9a-f]{40}))"
                           r"(?![A-Za-z0-9_\-])")
_GIT_WORD_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:(?i:commits?|sha1?|rev|revision|merge|parent|tree|rebase|cherry-pick|"
                          r"rev-parse)|HEAD)(?![A-Za-z0-9_-])")
_CREDENTIAL_PREFIX = ("access", "auth", "refresh", "id", "api", "bearer", "oauth", "session", "private", "personal",
                      "secret", "csrf", "xsrf", "client", "app", "bot", "github", "gitlab", "gh", "npm", "pat")
_CREDENTIAL_WORD_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:password|passwd|mot de passe|secret|authorization|bearer|"
                                 r"api[_-]?key|credentials?|identifiants?)(?![A-Za-z0-9])")
_FIREBASE_KEY_BEFORE_RE = re.compile(r"""(?i)["']?apiKey["']?\s*[:=]\s*["']$""")
_FIREBASE_WEB_RE = re.compile(r"""(?i)["']?authDomain["']?\s*[:=]\s*["'][a-z0-9-]+\.firebaseapp\.com["']|"""
                              r"""["']?appId["']?\s*[:=]\s*["']1:\d+:web:[0-9a-f]+["']""")
_SERVER_SIDE_RE = re.compile(r"(?i)private_key|client_email|service_account|client_secret")
_GOOGLE_KEY_RE = re.compile(_B + r"AIza[0-9A-Za-z_-]{35}")
_PROTECT = "\x00\x01{}\x01\x00"
_PROTECTED_RE = re.compile("\x00\x01(\\d+)\x01\x00")


def _git_hash_token(m: re.Match[str]) -> bool:
    keyword = m.group(1).lower()
    prefixes = re.split(r"[_-]", keyword.split("token")[0])
    if any(p in _CREDENTIAL_PREFIX for p in prefixes if p):
        return False
    window = m.string[max(0, m.start() - 160):m.end() + 160]
    return bool(_GIT_WORD_RE.search(window)) and not _CREDENTIAL_WORD_RE.search(window)


def _public_firebase_key(m: re.Match[str]) -> bool:
    text = m.string
    if not _FIREBASE_KEY_BEFORE_RE.search(text[max(0, m.start() - 40):m.start()]):
        return False
    window = text[max(0, m.start() - 600):m.end() + 600]
    return bool(_FIREBASE_WEB_RE.search(window)) and not _SERVER_SIDE_RE.search(window)


def _protect_public(text: str) -> tuple[str, list[str]]:
    """Met de côté les valeurs publiques sans ambiguïté (ci-dessus) avant les motifs ; `_restore` les remet à la fin."""
    saved: list[str] = []
    if "\x00\x01" in text:  # un texte qui porte déjà la marque : rien n'est mis de côté (dans le doute, caviarder)
        return text, saved

    def keep(value: str) -> str:
        saved.append(value)
        return _PROTECT.format(len(saved) - 1)

    if "token" in text.lower():
        text = _GIT_TOKEN_RE.sub(lambda m: m.group(1) + keep(m.group(2)) if _git_hash_token(m) else m.group(0), text)
    if "AIza" in text:
        text = _GOOGLE_KEY_RE.sub(lambda m: keep(m.group(0)) if _public_firebase_key(m) else m.group(0), text)
    return text, saved


def _restore(text: str, saved: list[str]) -> str:
    return _PROTECTED_RE.sub(lambda m: saved[int(m.group(1))], text) if saved else text


# Ligne `.env` / `export` : `NOM_KEY=valeur`, NOM en majuscules qui FINIT par KEY/SECRET/TOKEN/PASSWORD…
# (`KEYCLOAK_URL`, `TOKEN_ENDPOINT`, `MAX_TOKENS=4096` restent) ; valeur de 8 car. ou plus, lettres et chiffres.
_ENV_LINE_RE = re.compile(
    r"(?m)^([ \t]*(?:export[ \t]+)?(?:[A-Z][A-Z0-9_]*_)?(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASS|PWD|CREDENTIALS?)S?"
    r"\d*[ \t]*=[ \t]*)(['\"]?)([^\s'\"#]{8,})\2[ \t]*(?=#|\r?$)")

_CONSTANT_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _mixed(value: str) -> bool:
    return bool(re.search(r"[A-Za-z]", value)) and bool(re.search(r"\d", value))


def _identifier_like(value: str) -> bool:
    """Un identifiant de code (`compute_token_v2`, `SESSION_TOKEN_TTL`, `makeTokenForUser1`) plutôt qu'une valeur
    tirée au hasard : chaque morceau est un mot court, un petit nombre ou un mot versionné."""
    if _CONSTANT_RE.fullmatch(value):
        return True
    parts = [x for p in re.split(r"[_-]", value) for x in _CAMEL_SPLIT_RE.split(p) if x]
    if len(parts) < 2:
        return False
    return all((x.isalpha() and len(x) <= 15) or (x.isdigit() and len(x) <= 4)
               or bool(_VERSIONED_WORD_RE.match(x.lower())) for x in parts)


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
_VERSIONED_WORD_RE = re.compile(r"^[a-z]{1,15}\d{1,2}$")


_SNAKE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}$")
_CAMEL_ID_RE = re.compile(r"^[a-z]+(?:[A-Z][a-z]+|\d+){2,}$")
_CAMEL_PARTS_RE = re.compile(r"[A-Z]?[a-z]+|\d+")
_NUMBERED_WORD_RE = re.compile(r"^([a-z]{1,15})\d{1,4}$")


def _word(x: str) -> bool:
    """Un mot lisible : lettres seules, court, avec une voyelle s'il dépasse trois lettres (un morceau tiré au hasard
    en minuscules, « qzwk », n'en a souvent pas)."""
    return x.isalpha() and len(x) <= 15 and (len(x) <= 3 or bool(re.search(r"[aeiouy]", x.lower())))


def _code_identifier(token: str) -> bool:
    """Identifiant de code fait de mots, avec des nombres : `test_remise_10_pourcent_sur_3150` (snake_case en
    minuscules, trois morceaux ou plus) ou `testRemise10PourcentSur3150` (camelCase qui commence en minuscule).
    Banc X9 du 25.10 (§5.3) : des noms de tests caviardés dans la réponse, puis dans la mémoire de session. Chaque
    morceau doit être un mot, un nombre court, ou un mot suivi d'un numéro (`v2`, `iso8601`, `sha256`) : un morceau
    tiré au hasard (`k9x`, `9f8a7b6c`) ou une majuscule (`Acme_Backup_2026`, forme d'un mot de passe) laisse le repli
    juger."""
    core = token.strip("_")
    if _SNAKE_ID_RE.match(core):
        parts = core.split("_")
    elif _CAMEL_ID_RE.match(core):
        parts = _CAMEL_PARTS_RE.findall(core)
    else:
        return False
    if sum(1 for x in parts if x.isalpha()) < 2:
        return False
    def numbered(x: str) -> bool:
        m = _NUMBERED_WORD_RE.match(x)
        return bool(m) and _word(m.group(1))

    return all(_word(x) or (x.isdigit() and len(x) <= 8) or numbered(x) for x in parts)


_VOWELS = frozenset("aeiouyäöüàâáãåæéèêëíìîïóòôõøœúùûÿ")


def _letters_word(token: str) -> bool:
    """Un mot fait SEULEMENT de lettres (accents et ß compris, aucun chiffre ni signe) : mot composé allemand
    (« Speicherplatzverwaltung », rc.7), mot long, ou camelCase de mots. Un jeton tiré au hasard dans les lettres
    seules mêle les casses (une lettre sur deux en majuscule) : il est écarté par la limite de majuscules (au plus une
    pour six lettres, deux au minimum), puis par la part de voyelles (une « soupe » de consonnes n'est pas un mot)."""
    if not token.isalpha():
        return False
    upper = sum(1 for c in token if c.isupper())
    if upper > max(2, len(token) // 6):
        return False
    return sum(1 for c in token.lower() if c in _VOWELS) >= 0.15 * len(token)


# Identifiants juridiques publics connus, reconnus SEULEMENT dans une URL (rc.7) : Légifrance (`JURITEXT` + 12 chiffres,
# `LEGIARTI…`, `KALICONT…`) et ECLI (`ECLI:FR:CCASS:2019:C100123`, ou avec des « _ »). Hors URL, le repli juge comme
# avant ; un morceau tiré au hasard collé à l'identifiant (autre segment du chemin) reste caviardé.
_LEGAL_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:JURI|LEGI|JORF|CETA|CONS|CNIL|KALI|ACCO)(?:TEXT|ARTI|SCTA|CONT)\d{12}|"
    r"ECLI[:_][A-Z]{2}[:_][A-Z0-9.]{1,10}[:_]\d{4}[:_][A-Z0-9.]{1,30})(?![A-Za-z0-9])")
_PUBLIC_URL_RE = re.compile(r"\bhttps?://[^\s<>\"'`]+")


def _legal_id_in_url(token: str) -> bool:
    """Le jeton (vu dans une URL) n'est fait que d'identifiants juridiques connus et de morceaux lisibles du chemin."""
    stripped = _LEGAL_ID_RE.sub("loi", token)
    if stripped == token:
        return False
    parts = [x for x in _WORDS_SPLIT_RE.split(stripped) if x]
    return all(x == "loi" or (x.isalpha() and (len(x) <= 15 or _letters_word(x))) or (x.isdigit() and len(x) <= 8)
               or bool(_VERSIONED_WORD_RE.match(x)) for x in parts)


def _looks_like_safe_token(token: str, in_url: bool = False) -> bool:
    if _UUID_RE.match(token):
        return True
    if _letters_word(token.strip("/_+")):  # « …oberfläche/Datenschutzgrundverordnung » : le « / » ouvre le jeton
        return True
    if in_url and _legal_id_in_url(token):
        return True
    if _code_identifier(token):
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
    # Dans un CHEMIN, un mot en minuscules suivi d'un petit numéro de version (« miniconda3 », « python3 », « lib64 »,
    # « x86 ») est aussi un morceau lisible : `/opt/miniconda3/bin/activate` était caviardé dans la CONSIGNE même
    # (banc SWE-bench du 24.10 : 24 tâches sur 50 lançaient `source /[secret:entropy-…] testbed`).
    def readable(x: str) -> bool:
        if x.isalpha() and (len(x) <= 15 or _letters_word(x)):
            return True
        return digits_ok and ((x.isdigit() and len(x) <= 8) or bool(_VERSIONED_WORD_RE.match(x)))

    if len(parts) >= 2 and any(x.isalpha() for x in parts) and all(readable(x) for x in parts):
        return True
    if _HEX_RE.match(token) and len(token) in _SAFE_HEX_LENGTHS:
        return True
    return False


def _entropy_candidates(text: str) -> list[str]:
    out = []
    urls = [u.span() for u in _PUBLIC_URL_RE.finditer(text)] if "://" in text else []
    for m in _ENTROPY_CANDIDATE_RE.finditer(text):
        token = m.group(0)
        in_url = any(start <= m.start() < end for start, end in urls)
        if _looks_like_safe_token(token, in_url):
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


def redact(text: str, known: KnownSecrets | None = None, *, weak: bool = True, wire: bool = False) -> RedactResult:
    """Caviarde `text` : valeurs connues d'abord, puis motifs précis, puis (si `weak`) le repli
    « mot de passe : … » et le repli par entropie.

    `wire=True` : le filet du fil (`redact_messages`, ce qui part au modèle) ; les valeurs publiques selon le contexte
    (hash git après `token:`, clé Firebase web, `_protect_public`) y sont caviardées comme les autres, parce que le
    Router refuse le tour sur ces formes. Ailleurs (`wire=False`), elles restent en clair.

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
    if "[secret:" in out:
        out = _LEGACY_TYPED_HANDLE_RE.sub(_legacy_handle_sub, out)

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

    # 1d. valeurs publiques sans ambiguïté (hash git après `token:`, clé Firebase web) : mises de côté avant les motifs
    #     (motif Google, « mot-clé : valeur », entropie), remises à la fin ; jamais sur le fil (`wire`).
    saved: list[str] = []
    if not wire:
        out, saved = _protect_public(out)

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

    def _kv(kind: str, check=None):
        """Remplace la VALEUR (dernier groupe) d'un motif à mot-clé, garde le reste."""
        def sub(m: re.Match[str]) -> str:
            value = m.group(m.lastindex or 0)
            if check is not None and not check(value):
                return m.group(0)
            name = _type_name(kind, value)
            _mark(name)
            start, end = m.span(m.lastindex or 0)
            return m.group(0)[:start - m.start()] + _handle(name) + m.group(0)[end - m.start():]
        return sub

    out = _AWS_SESSION_RE.sub(_kv("aws-session-token"), out)
    out = _AZURE_KEY_RE.sub(_kv("azure-key"), out)
    out = _AZURE_SIG_RE.sub(_kv("azure-key"), out)
    out = _URL_CRED_RE.sub(_kv("url-password", lambda v: v.lower() not in _URL_PLACEHOLDERS
                                and not v.startswith(("$", "{", "<", "*", "%")) and not HANDLE_RE.search(v)
                                and not re.fullmatch(r"x+|\*+", v)), out)
    out = _BEARER_RE.sub(_kv("bearer-token", _mixed), out)
    out = _ENV_LINE_RE.sub(_kv("env-secret", lambda v: _mixed(v) and not v.startswith(("$", "~", ".", "/", "<"))
                                and not HANDLE_RE.search(v)), out)
    out = _KEY_VALUE_RE.sub(_kv("key-value", lambda v: _mixed(v) and not _identifier_like(v)), out)
    out = _QUERY_TOKEN_RE.sub(_kv("url-token", lambda v: _mixed(v) and not _identifier_like(v)), out)

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

    return RedactResult(text=_restore(out, saved), names=names)


# ─────────────────────────────── filet du fil ───────────────────────────────


def _redact_json_strings(obj: Any, scrub: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [_redact_json_strings(x, scrub) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact_json_strings(v, scrub) for k, v in obj.items()}
    return obj


def _redact_arguments(raw: str, scrub: Callable[[str], str]) -> str:
    """Arguments d'un appel d'outil (JSON en chaîne) : chaque chaîne du JSON décodé (les guillemets échappés d'un
    fichier écrit cachent sinon `token = "…"`), réencodé seulement si quelque chose a changé."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return scrub(raw)
    cleaned = _redact_json_strings(data, scrub)
    return raw if cleaned == data else json.dumps(cleaned, ensure_ascii=False)


def redact_messages(messages: list[dict[str, Any]], known: KnownSecrets | None = None, *,
                    scrub: Callable[[str], str] | None = None) -> list[dict[str, Any]]:
    """Copie des messages prêts pour `/chat/completions`, caviardés en mode strict (coffre + motifs forts, sans le
    repli par entropie) : TOUT le texte qui part vers un modèle — message système (plan, mémoire de projet, profil),
    messages `user`, réponses et arguments d'outils de l'assistant, résultats d'outils, parties texte.

    C'est le filet du fil : chaque entrée du contexte est déjà caviardée à sa porte (entrée, résultat d'outil, rappel,
    contexte reçu, nom d'image), mais certaines n'en ont pas (bloc du plan, mémoire de projet, diff relu par le juge
    reconstruit depuis les arguments du modèle). Un texte sans secret ressort à l'identique : le cache de préfixe ne
    bouge pas. `scrub` : la fonction de caviardage d'une chaîne (par défaut `redact(…, known, weak=False, wire=True)`).
    Les messages d'origine ne sont pas modifiés."""
    if scrub is None:
        def scrub(text: str) -> str:
            return redact(text, known, weak=False, wire=True).text
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        c = dict(m)
        content = c.get("content")
        if isinstance(content, str):
            c["content"] = scrub(content)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part = {**part, "text": scrub(part["text"])}
                parts.append(part)
            c["content"] = parts
        calls = c.get("tool_calls")
        if isinstance(calls, list) and calls:
            new_calls = []
            for call in calls:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    call = {**call, "function": {**fn, "arguments": _redact_arguments(fn["arguments"], scrub)}}
                new_calls.append(call)
            c["tool_calls"] = new_calls
        out.append(c)
    return out


__all__ = ["HANDLE_RE", "KnownSecrets", "RedactResult", "redact", "redact_messages"]
