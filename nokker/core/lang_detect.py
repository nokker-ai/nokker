"""Langue du MESSAGE de la personne (fr/en/de/it), sans dépendance : comptage de mots-outils propres à une langue.

Sert à `Kernel.resolve_lang` quand ni la requête (`Accept-Language`, `?lang=`) ni le compte ne disent la langue : sur
Lokkal (le client terminal n'envoie pas d'`Accept-Language`, un Lokkal n'a pas de langue de compte), tout retombait sur le
français et le contexte du tour (date, environnement) entraînait le modèle à répondre en français à une personne qui
écrit en anglais.

La détection ne tranche que sur un signal net : au moins deux mots-outils de la langue gagnante, et au moins deux fois
plus que la seconde. « ok », « oui », « merci » seuls ne basculent donc rien (le fil garde sa langue). Les mots communs
à deux langues (« la », « il », « a », « in », « was », « non », « come »…) ne figurent dans aucune liste."""
from __future__ import annotations

import re

MAX_CHARS = 4000
MIN_HITS = 2

_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
_URL = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+")
_WORD = re.compile(r"[^\W\d_]+", re.U)

# Fragments de COMMANDE : ils ne comptent pas comme prose (« mkdir -p a-b-dir && ls -d a-b-dir » n'est pas de l'allemand
# parce que « dir » y figure). Règle (docs/TEXTES-MODELE.md) :
#  - jeton « commande » : contient `/ \ = & | ~ $ > <` ou `_` ; un point entre deux caractères (`a.txt`, pas le point
#    final) ; commence par `-` + lettre/`-` (option : `-p`, `--version`, `-la`) ; ou identifiant à 2 tirets internes ou
#    plus (`bench-lang-dir`). L'apostrophe (« l'utilisateur ») et UN tiret entre deux mots (« peut-être », « Rück-Fahrt »)
#    restent de la prose ;
#  - ligne, ou partie après « : », qui ressemble à une commande (commence par un nom de commande connu suivi d'une option,
#    d'un jeton commande, de `||`/`|`/`;`, ou d'un seul mot après « : » ; commence par un chemin ; ou contient `&&`) :
#    ignorée ;
#  - nom de commande connu juste avant une option ou un jeton commande (`dir -l`, `ls /tmp`) : ignoré. Ailleurs « dir »
#    reste un mot allemand (« Ich gebe dir das Buch »).
_COMMANDS = frozenset(
    "ls dir mkdir rmdir cd pwd git docker cat grep find rm cp mv touch echo sudo apt apt-get pip pip3 npm npx node python "
    "python3 make curl wget ssh scp rsync chmod chown tar kill killall ps top htop head tail sed awk which man nano vim "
    "tree ping df du free systemctl journalctl cargo go java mvn gcc less more tee sort uniq wc xargs".split())
_CMD_CHARS = re.compile(r"[/\\=&|~$><_]|\w\.\w")
_OPTION = re.compile(r"^-{1,2}[^\W\d_][\w-]*$")
_EDGE = "\"'«»“”‘’()[]{},;:!?.*"
_COLON = re.compile(r":\s+")
_PIPE = re.compile(r"\s(?:\|\|?|;)\s|;\s*$")


def _is_command_token(tok: str) -> bool:
    core = tok.strip(_EDGE)
    if not core:
        return False
    return bool(_OPTION.match(core) or _CMD_CHARS.search(core) or core.count("-") >= 2)


def _looks_like_command(fragment: str, *, after_colon: bool) -> bool:
    toks = fragment.split()
    if not toks:
        return False
    first = toks[0].strip(_EDGE).lower()
    if toks[0].startswith(("/", "./", "../", "~")):
        return True
    if "&&" in fragment:
        return first in _COMMANDS or any(_is_command_token(t) for t in toks[1:])
    if first not in _COMMANDS:
        return False
    rest = toks[1:]
    if any(_is_command_token(t) for t in rest) or _PIPE.search(fragment):
        return True
    return after_colon and len(rest) <= 1


def _drop_commands(text: str) -> str:
    out = []
    for line in text.split("\n"):
        for m in _COLON.finditer(line):
            if _looks_like_command(line[m.end():], after_colon=True):
                line = line[:m.start()]
                break
        if _looks_like_command(line, after_colon=False):
            continue
        toks = line.split()
        keep = []
        for i, tok in enumerate(toks):
            if _is_command_token(tok):
                continue
            if tok.strip(_EDGE).lower() in _COMMANDS and i + 1 < len(toks) and _is_command_token(toks[i + 1]):
                continue
            keep.append(tok)
        out.append(" ".join(keep))
    return "\n".join(out)

_WORDS: dict[str, frozenset[str]] = {
    "fr": frozenset(
        "les des du est et je nous vous ils elles pas pour dans avec que qui sur ce cette ces mon mes au aux mais où donc "
        "comme suis sont ai avons avez très bien oui bonjour bonsoir merci beaucoup salut fais peux veux quel quelle "
        "quoi comment pourquoi aussi tout tous être avoir une de en ça ma ta sa son leur leurs moi toi pourrais "
        "voudrais veuillez stp svp quand ou j qu".split()),
    "en": frozenset(
        "the and you your are with for this that have has not what how why when where can could would should please "
        "thanks thank hello yes they their there which who just but from about my we our don doesn isn it is of to be "
        "been hi hey me i'm i've do does did will want need make let".split()),
    "de": frozenset(
        "der die das den dem des ein eine einen einem ich du er sie es wir ihr nicht und oder aber mit für von zu im ist "
        "sind bin hast hat habe haben wird werden kann kannst können mir mich dir dich auch noch schon sehr bitte danke "
        "hallo ja nein wie wo wann warum wer welche dass wenn dann nur über auf aus bei nach vor um zum zur ob".split()),
    "it": frozenset(
        "lo gli di che è e un una per con su sono sei siamo ho hai abbiamo hanno mi ti ci vi cosa perché quando dove chi "
        "quale questo questa questi molto anche più già ancora grazie ciao buongiorno sì prego fare puoi posso vorrei "
        "della dello delle degli nel nella nei nelle alla alle allo agli dal dalla sul sulla dei ma".split()),
}
# « ma » est aussi français (« ma voiture »), « de » aussi italien dans « di » : retirés des deux listes pour ne pas
# compter deux fois. « me », « do », « will », « want », « need », « make », « let » restent anglais : ils ne sont ni
# français, ni allemand, ni italien en prose courante.
_WORDS["it"] = _WORDS["it"] - {"ma"}
_WORDS["en"] = _WORDS["en"] - {"i'm", "i've"}
_GERMAN_CHARS = re.compile(r"[ßäöü]")


def scores(text: str) -> dict[str, int]:
    """Nombre de mots-outils de chaque langue dans `text` (code, liens et adresses retirés)."""
    body = _drop_commands(_URL.sub(" ", _CODE.sub(" ", (text or "")[:MAX_CHARS]))).lower()
    words = _WORD.findall(body)
    out = {lang: sum(1 for w in words if w in vocab) for lang, vocab in _WORDS.items()}
    if _GERMAN_CHARS.search(body):
        out["de"] += 1
    return out


def detect_lang(text: str | None) -> str | None:
    """`fr`/`en`/`de`/`it` quand le message le dit nettement, sinon None (court, ambigu, sans mot reconnu)."""
    if not text or not text.strip():
        return None
    ranked = sorted(scores(text).items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top >= MIN_HITS and top >= 2 * second:
        return best
    return None


__all__ = ["detect_lang", "scores"]
