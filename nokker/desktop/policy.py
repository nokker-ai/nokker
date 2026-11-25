"""Politique du Bureau (docs/DESKTOP.md §3), pure et sans dépendance : applis permises et refusées, classement d'une
action (écrire / envoyer / supprimer), grammaire des touches, champs secrets, mode physique.

Le noyau s'en sert pour la carte (effet de l'appel, AVANT le dépôt dans la file) ; le nœud la rejoue sur l'élément
VIVANT (le nom lu par `expect` doit correspondre, et une touche Entrée sur un bouton d'envoi est refusée)."""
from __future__ import annotations

import re
import unicodedata

from nokker.core.turn import Effect

MODES = ("virtual", "session")
PHYSICAL = ("never", "allow")  # `ask` viendra avec B2 (curseur visible, pas à pas) : décision dev du 06.11
PHYSICAL_DEFAULT = {"virtual": "allow", "session": "never"}

# ── applis ──
# Jamais pilotées, quelle que soit la liste de la personne : un terminal ou un lanceur exécute du code hors de la
# politique du code ; mots de passe et réglages ; le verrouillage ; le web passe par sites-chrome ; Nokker lui-même.
DENIED_APPS = frozenset({
    # terminaux et lanceurs de commandes
    "gnome-terminal", "gnome-terminal-server", "kgx", "gnome-console", "konsole", "xterm", "uxterm", "rxvt",
    "urxvt", "alacritty", "kitty", "terminator", "tilix", "xfce4-terminal", "lxterminal", "mate-terminal",
    "qterminal", "wezterm", "foot", "guake", "yakuake", "st", "sakura", "terminology", "cool-retro-term", "ptyxis",
    "blackbox", "gnome-shell", "krunner", "rofi", "dmenu", "albert", "ulauncher", "xdg-open",
    # mots de passe et clés
    "seahorse", "keepassxc", "keepass", "keepass2", "bitwarden", "1password", "enpass", "kwalletmanager",
    "kwalletmanager5", "gnome-keyring", "secrets", "gnome-passwordsafe", "pass", "qtpass",
    # réglages, administration, verrouillage
    "gnome-control-center", "systemsettings", "systemsettings5", "xfce4-settings-manager", "unity-control-center",
    "gnome-tweaks", "dconf-editor", "gnome-disks", "gparted", "synaptic", "gnome-software", "discover",
    "software-properties-gtk", "pkexec", "polkit-gnome-authentication-agent-1", "gnome-screensaver",
    "xscreensaver", "light-locker", "i3lock", "swaylock", "xfce4-screensaver", "cinnamon-screensaver",
    # navigateurs (le web reste dans sites-chrome et les lanes du navigateur)
    "firefox", "firefox-esr", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome",
    "brave", "brave-browser", "microsoft-edge", "opera", "vivaldi", "epiphany", "falkon", "midori", "librewolf",
    "tor-browser",
    # Nokker
    "nokker", "nokker-desktop",
})


def norm_app(name: object) -> str:
    """Nom d'appli comparable : minuscules, sans espaces ni suffixe `.desktop`, sans numéro de version final
    (`python3.12` et `python3` sont la même appli, cas relevé au spike)."""
    s = str(name or "").strip().lower()
    for suffix in (".desktop", ".exe", ".bin"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = re.sub(r"\s+", "-", s)
    m = re.match(r"^(.*?[a-z]+[0-9]*)\.[0-9]+(?:\.[0-9]+)*$", s)
    return m.group(1) if m else s


def app_verdict(app: object, allowed: tuple[str, ...] | list[str] | frozenset[str]) -> str | None:
    """None si l'appli est permise ; sinon le code du refus : `app_denied` (liste permanente) ou `app_not_allowed`."""
    a = norm_app(app)
    if not a or a in DENIED_APPS or a.split("-", 1)[0] in {"org.gnome.terminal", "org.kde.konsole"}:
        return "app_denied"
    if a.endswith(("-terminal", "terminal")) or a.startswith(("org.gnome.console", "org.gnome.ptyxis")):
        return "app_denied"
    return None if a in {norm_app(x) for x in allowed} else "app_not_allowed"


# ── classement d'une action ──
def _fold(text: object) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# Mots entiers, après pliage (minuscules, sans accents) : FR, EN, DE. Une cible dont le nom en contient un change de
# classe ; ni une autorisation « pour cette tâche » ni un mode ne couvrent ces classes.
SEND_WORDS = frozenset({
    "envoyer", "envoie", "envoi", "send", "senden", "absenden", "abschicken", "versenden", "verschicken",
    "publier", "publish", "post", "poster", "posten", "veroffentlichen", "submit", "soumettre", "einreichen",
    "payer", "pay", "bezahlen", "zahlen", "acheter", "buy", "kaufen", "commander", "order", "bestellen",
    "checkout", "transferer", "transfer", "uberweisen", "partager", "share", "teilen", "signer", "sign",
    "unterschreiben", "unterzeichnen", "tweet", "reply", "repondre", "antworten", "forward", "transferer",
    "weiterleiten", "invite", "inviter", "einladen",
})
DESTROY_WORDS = frozenset({
    "supprimer", "suppression", "delete", "loschen", "entfernen", "remove", "retirer", "effacer", "erase",
    "vider", "empty", "leeren", "corbeille", "trash", "papierkorb", "discard", "verwerfen", "jeter", "rejeter",
    "abandonner", "ecraser", "overwrite", "uberschreiben", "remplacer", "replace", "ersetzen", "formater", "format",
    "formatieren", "desinstaller", "uninstall", "deinstallieren", "purge", "purger", "wipe", "reset",
    "reinitialiser", "zurucksetzen", "destroy", "detruire",
})
# Locutions qui détruisent sans mot ci-dessus : « ne pas enregistrer » ferme sans garder les modifications.
DESTROY_PHRASES = ("ne pas enregistrer", "don t save", "dont save", "do not save", "nicht speichern",
                   "close without saving", "fermer sans enregistrer", "ohne speichern")
CLICKABLE_ROLES = frozenset({"button", "push button", "toggle button", "menu item", "menuitem", "check menu item",
                             "radio menu item", "link", "split button", "menu button", "tab", "page tab"})


def target_class(role: object, name: object) -> Effect:
    """Effet d'un CLIC sur (rôle, nom) : SEND_EXTERNAL, DESTRUCTIVE ou WRITE. Un élément cliquable sans nom lisible
    est classé au plus haut (envoi) : on ne sait pas ce qu'il fait."""
    folded = _fold(name)
    words = set(folded.split())
    if any(p in folded for p in DESTROY_PHRASES) or words & DESTROY_WORDS:
        return Effect.DESTRUCTIVE
    if words & SEND_WORDS:
        return Effect.SEND_EXTERNAL
    if not folded and str(role or "").strip().lower() in CLICKABLE_ROLES:
        return Effect.SEND_EXTERNAL
    return Effect.WRITE


# ── touches ──
_MODS = ("ctrl", "shift", "alt")
_MOD_ALIASES = {"control": "ctrl", "ctl": "ctrl", "maj": "shift", "option": "alt"}
_NAMED = frozenset({
    "return", "enter", "tab", "escape", "esc", "backspace", "delete", "del", "space", "home", "end", "pageup",
    "pagedown", "up", "down", "left", "right", "insert", "menu", *(f"f{i}" for i in range(1, 13)),
})
_KEY_ALIASES = {"esc": "escape", "del": "delete", "enter": "return"}
# Toujours interdits : système, bureau, changement de console, exécution.
_FORBIDDEN = frozenset({"alt+f2", "alt+f4", "alt+tab", "alt+shift+tab", "ctrl+escape", "alt+f1", "alt+space"})


def parse_keys(keys: object) -> str | None:
    """Combinaison normalisée (`ctrl+shift+s`) ou None si elle sort de la grammaire fermée : modificateurs ctrl,
    shift, alt (chacun une fois, dans cet ordre), puis UNE touche nommée ou UN caractère imprimable. Interdits :
    super/meta/hyper, ctrl+alt+…, et les raccourcis du système (`_FORBIDDEN`)."""
    raw = str(keys or "").strip()
    if not raw or len(raw) > 40:
        return None
    parts = [p.strip() for p in raw.split("+")] if raw != "+" else ["+"]
    if raw.endswith("++"):  # « ctrl++ » : la touche est « + »
        parts = [p.strip() for p in raw[:-2].split("+")] + ["+"]
    if any(not p for p in parts):
        return None
    *mods, key = parts
    norm_mods = []
    for m in mods:
        m = _MOD_ALIASES.get(m.lower(), m.lower())
        if m not in _MODS or m in norm_mods:
            return None
        norm_mods.append(m)
    if "ctrl" in norm_mods and "alt" in norm_mods:
        return None
    k = key.lower() if len(key) > 1 else key
    k = _KEY_ALIASES.get(k, k)
    if len(k) > 1 and k not in _NAMED:
        return None
    if len(k) == 1 and (not k.isprintable() or k.isspace()):
        return None
    combo = "+".join([*sorted(norm_mods, key=_MODS.index), k.lower() if len(k) == 1 and norm_mods else k])
    return None if combo.lower() in _FORBIDDEN else combo


def keys_class(combo: str) -> Effect:
    """Effet d'une combinaison déjà normalisée : Suppr = destruction ; ctrl+Entrée = envoi (messageries, formulaires) ;
    le reste écrit. Une Entrée seule est revue sur le nœud (élément qui a le focus)."""
    parts = combo.split("+")
    key, mods = parts[-1].lower(), set(parts[:-1])
    if key == "delete":
        return Effect.DESTRUCTIVE
    if key == "return" and ("ctrl" in mods or "alt" in mods):
        return Effect.SEND_EXTERNAL
    return Effect.WRITE


def act_class(arguments: dict) -> Effect:
    """Effet d'un appel `desktop_act` (pour `ToolSpec.effect_of`, donc pour la carte) : d'après l'action, la cible
    attendue (`expect_role`, `expect_name`) et les touches. Arguments illisibles : l'appelant retombe sur l'effet
    déclaré de l'outil (le plus prudent)."""
    action = str(arguments.get("action") or "")
    if action == "click":
        return target_class(arguments.get("expect_role"), arguments.get("expect_name"))
    if action == "press":
        combo = parse_keys(arguments.get("keys"))
        if combo is None:
            raise ValueError("touches illisibles")
        return keys_class(combo)
    if action in ("type", "set_value", "select", "check"):
        return Effect.WRITE
    raise ValueError(f"action inconnue : {action!r}")


def desktop_subject(tool: str, arguments: dict) -> str | None:
    """Sujet d'une règle de permission pour un appel du bureau : `appli:classe`. L'appli est celle qu'annonce l'appel
    (`app` pour launch ; sinon la fenêtre est inconnue du noyau, d'où `*` : une règle par appli ne couvre alors que
    launch). Classe : write | send_external | destructive."""
    a = arguments or {}
    if tool == "desktop_open":
        app = norm_app(a.get("app")) if a.get("action") == "launch" else ""
        return f"{app or '*'}:write"
    try:
        cls = act_class(a)
    except ValueError:
        return None
    return f"{norm_app(a.get('app')) or '*'}:{cls.value}"


# ── champs secrets ──
SECRET_ROLES = frozenset({"password text", "passwordtext", "password"})
_SECRET_NAME = re.compile(r"(mot de passe|\bmdp\b|password|passwort|kennwort|passphrase|passcode|\bpin\b|\bcvc\b|"
                          r"\bcvv\b|code secret|code de s[ée]curit[ée]|security code|sicherheitscode|"
                          r"code de v[ée]rification|verification code|best[äa]tigungscode|\bsecret\b|\botp\b|"
                          r"\b2fa\b|\btan\b)", re.IGNORECASE)


def is_secret_field(role: object, name: object, states: object = ()) -> bool:
    """Champ secret : état `secure`, rôle de mot de passe, ou nom qui le dit (un `Gtk.Entry` masqué n'est pas `secure`
    et l'amont l'expose en clair : relevé au spike)."""
    st = {str(s).lower() for s in (states or ())}
    if "secure" in st or "protected" in st:
        return True
    if str(role or "").strip().lower() in SECRET_ROLES:
        return True
    return bool(_SECRET_NAME.search(unicodedata.normalize("NFKC", str(name or ""))))


def physical_mode(policy: object, mode: str) -> str:
    """`allow` ou `never` : la valeur réglée si elle est connue, sinon le défaut du mode (session : never)."""
    p = str(policy or "").strip().lower()
    return p if p in PHYSICAL else PHYSICAL_DEFAULT.get(mode, "never")


__all__ = ["CLICKABLE_ROLES", "DENIED_APPS", "MODES", "PHYSICAL", "PHYSICAL_DEFAULT", "act_class", "app_verdict",
           "desktop_subject",
           "is_secret_field", "keys_class", "norm_app", "parse_keys", "physical_mode", "target_class"]
