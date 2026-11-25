"""Context Engine v0 (`core.ports.ContextEngine`) — PLAN-P1 §2.4. Le v1 complet (réservoir réel, frontières
d'épisode, consolidation) est le chantier A-1 ; ceci respecte le contrat minimal :

  [1 préfixe statique] [2 profil] [3 épisodes clos (vide en v0 — pas de frontières)] [4 tours bruts]
  [5 bloc du tour : message + rappel + index court des outils non cœur + schémas chargés]

Invariant vérifié par construction (et testé) : le préfixe NE dépend ni du palier ni de l'heure — il est
calculé une fois depuis le fichier + les schémas des outils cœur, jamais recalculé par appel. `layer_tokens`
est une estimation par classes de contenu (`context/tokens.py`, calée sur le Router) en attendant un vrai
tokenizer ; ce n'est pas la mesure qui a produit les chiffres du PLAN (ceux-là viennent du tokenizer réel
du modèle, cf. notes internes §F4).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nokker.approvals.rule_labels import current_lang, norm_lang
from nokker.attachments import fence_head
from nokker.core import model_texts as mt
from nokker.context import host_prompt, prefix_mode, prefix_tools
from nokker.context.imported import IMPORT_SOURCE, render_imported
from nokker.context.tokens import estimate_tokens  # noqa: F401 — réexporté (engine_v1, project_memory, tests)
from nokker.core.ports import Assembled, Recall, ToolRegistry, ToolSpec
from nokker.core.turn import Tier, ToolResult, Turn
from nokker.images.render import IMAGE_PART, shed_line
from nokker.memory.provenance import recall_line
from nokker.qa.untrusted import neutralize_date, neutralize_env
from nokker.redact.markers import CONSIGNE as MASKED_CONSIGNE
from nokker.redact.markers import CONSIGNE_DESKTOP as MASKED_CONSIGNE_DESKTOP
from nokker.redact.markers import consigne_needed, desktop_consigne_needed
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant
from nokker.tools.select import compact_index, unavailable_line

logger = logging.getLogger("nokker.context.engine")

INVARIANTS_PREFIX_PATH = str(Path(__file__).resolve().parent.parent / "prompts" / "prefix-invariants.md")
DEFAULT_PREFIX_PATH = str(Path(__file__).resolve().parent.parent / "prompts" / "prefix-nokker.md")
ZURICH = ZoneInfo("Europe/Zurich")

# Jours (lundi en premier) et mois par langue, écrits à la main : pas de dépendance à la locale du système.
_WEEKDAYS = {
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
    "it": ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"],
}
_MONTHS = {
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
           "octobre", "novembre", "décembre"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
           "Oktober", "November", "Dezember"],
    "it": ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
           "ottobre", "novembre", "dicembre"],
}


def format_now(now: datetime, lang: str | None = None) -> str:
    """Date et heure du tour dans la langue du tour (fr/en/de/it ; sinon celle du contexte, sinon fr), puis le fuseau et
    l'ISO 8601 pour toute résolution programmatique :
    fr « mardi 20 octobre 2026, 14:05 (Europe/Zurich) — ISO 2026-10-20T14:05:00+02:00 » (figé octet pour octet),
    en « Tuesday, October 20, 2026, 14:05 (…) », de « Dienstag, 20. Oktober 2026, 14:05 (…) »,
    it « martedì 20 ottobre 2026, 14:05 (…) ». Jours et mois écrits à la main (pas de dépendance à la locale du système,
    qui peut être absente ou différente selon l'hôte) ; 24 h, fuseau Europe/Zurich dans toutes les langues."""
    code = norm_lang(lang) or current_lang()
    weekday = _WEEKDAYS[code][now.weekday()]
    month = _MONTHS[code][now.month - 1]
    day = {"en": f"{weekday}, {month} {now.day}, {now.year}", "de": f"{weekday}, {now.day}. {month} {now.year}"}.get(
        code, f"{weekday} {now.day} {month} {now.year}")
    return f"{day}, {now:%H:%M} (Europe/Zurich) — ISO {now.isoformat(timespec='seconds')}"


def format_now_fr(now: datetime) -> str:
    """Rendu français de `format_now` (« mardi 20 octobre 2026, 14:05 (Europe/Zurich) — ISO … »)."""
    return format_now(now, "fr")


def date_block(now: datetime, turn: Turn | None = None) -> str:
    """Bloc `[Date et heure actuelles]` de l'en-tête d'un tour, dans la langue du tour (`Turn.lang`, sinon celle du
    contexte). L'en-tête est figé par tour (`Turn.head`) : changer de langue d'un tour à l'autre change l'en-tête du
    tour suivant seulement, jamais celui d'un tour déjà envoyé."""
    lang = norm_lang(getattr(turn, "lang", None)) or current_lang()
    return f"{mt.pick(mt.DATE_LABEL, lang)}\n{format_now(now, lang)}\n[/Date]"


@dataclass
class ContextEngineV0:
    tools: ToolRegistry  # pour l'index court des outils non cœur (couche 5) — pas dans le Protocol
    prefix_path: str = DEFAULT_PREFIX_PATH
    profile_path: str | None = None
    reservoir_char_limit: int = 4000  # au-delà : tronqué + mention (réservoir réel = A-1)
    recall_text_limit: int = 1200  # caractères max par note rappelée dans le bloc du tour
    # Horloge injectable (P1-S1 itération 3, 17.10.2026) : le modèle n'avait AUCUNE date, donc ne
    # pouvait pas résoudre « mardi prochain »/« le 24 »/« vendredi » en date absolue, et appelait
    # `clarify` pour demander — mesuré en réel sur `kanban-ajout-tache`/`calendrier-type-entite`.
    # Défaut = horloge réelle (Europe/Zurich) ; un appelant (banc, test) peut figer une date précise.
    # TOUJOURS dans le bloc du tour (couche 5), JAMAIS dans le préfixe (A4 : hash stable entre tours
    # et paliers — un préfixe qui daterait changerait de hash à chaque appel, cassant le cache).
    now_fn: Callable[[], datetime] = field(default=lambda: datetime.now(ZURICH))
    # Préfixe produit (docs/TENANCY.md §5). Posé (`build_kernel`) : préfixe et profil suivent le locataire courant —
    # interne → `prefix_path` + `profile_path`, testeur → ce fichier, sans profil ; hors d'un `tenant_scope`, NoTenant.
    # None : un seul préfixe pour tous (tests, bancs qui construisent l'engine eux-mêmes).
    product_prefix_path: str | None = None
    # Préfixe par modèle servant le tour (`context/turn_model.py`) : {id exact du modèle: chemin}. Seulement pour le locataire interne (ou sans préfixe
    # produit) ; id absent de la table : repli sur `prefix_path`. Chemin illisible : erreur au démarrage, jamais un repli silencieux. None : rien ne change.
    prefix_by_model: dict[str, str] | None = None
    # Variantes de préfixe par SESSION (`prefix_variant`, `context/prefix_mode.py`) : {nom: chemin}, liste blanche lue au démarrage. Même
    # périmètre que `prefix_by_model` (locataire interne ou sans préfixe produit). None : rien ne change.
    prefix_variants: dict[str, str] | None = None
    # Images jointes (X7) envoyées en entier dans la fenêtre : celles du tour courant puis les plus récentes, jusqu'à ce
    # nombre ; les plus anciennes sont délestées en une ligne de texte (une image pèse des milliers de jetons).
    keep_images: int = 4
    # Mémoire de projet des sessions locales (lot X1, `context/project_memory.py`) : son noyau court entre dans le
    # message système après le profil. None (défaut, et drapeau `local.project_memory` éteint) : rien ne change.
    project_memory: Any = None
    # Plan (lot X18, `nokker/plan/service.py`) : consignes héritées de la carte sur laquelle travaille le fil (session
    # rattachée, ou passage d'un agent listé sur la carte), après la mémoire de projet. None (drapeau `plan` éteint) :
    # rien ne change.
    plan: Any = None
    # Profil mineur d'un foyer (lot X23, `nokker/household/guard.py::young_prefix`) : locataire courant → `teen` |
    # `child` | None. Posé (drapeau `household_minors`) : le préfixe produit d'un profil mineur reçoit l'addendum
    # `prompts/prefix-young.md` et sa tranche d'âge. None (défaut) : rien ne change.
    young_profile: Any = None
    # Drapeau `reply_language` (`context/reply_language.py`) : consigne de langue en tête de la section Style du préfixe,
    # appliquée au chargement (avant toute empreinte). False (défaut) : préfixe lu tel quel, à l'octet près.
    reply_language: bool = False
    # Drapeau `reply_language_tail` : ligne `reply_language.TAIL_LINE` en fin d'en-tête du tour (après la date, le rappel et
    # les pièces jointes, juste avant le message). Le préfixe ne change pas. False (défaut) : en-tête inchangé à l'octet.
    reply_language_tail: bool = False
    # Drapeau `feature_notes` (`nokker/guide/notes.py::FeatureNotes`) : bloc `[Fonctions]` (0 à 3 notes choisies pour le
    # message, filtrées par drapeaux et profil mineur) dans l'en-tête du tour, après le rappel mémoire et avant le
    # contexte reçu. None (défaut) : en-tête inchangé à l'octet.
    feature_notes: Any = None
    # Mode de permissions d'une session locale rappelé au modèle (`thread_id -> texte`, "" = rien) : relu à CHAQUE tour,
    # figé dans l'en-tête du tour (comme le reste), donc un `PUT …/sessions/{fil}/mode` entre deux tours est vu au tour
    # suivant. Sans lui le modèle ne connaît le mode que par les refus déjà dans l'historique (« lecture seule »), qui
    # restent vrais pour lui après le changement. None (défaut) : en-tête inchangé à l'octet.
    mode_note: Callable[[str], str] | None = None
    # Bloc `[Environnement]` (`core/self_context.py`) : où tourne le fil, canal, machines liées. Posé par `serve` ; None = pas
    # de bloc. Entre dans l'en-tête figé du tour (`Turn.head`), jamais dans le préfixe stable.
    self_context: Callable[[Turn], str] | None = None
    # Suite de carte : le texte de fin du tour qui a posé la carte (écrit AVANT la décision) n'est plus rejoué devant le
    # bloc `[Approbation]` (`replayed_messages`). Vrai par défaut (le texte disait d'attendre une validation déjà donnée) ;
    # Faux : historique complet, identique à l'octet d'avant.
    drop_card_end_text: bool = True

    def __post_init__(self) -> None:
        self._layers = {"internal": (self._load_prefix(self.prefix_path),
                                     self._load(self.profile_path) if self.profile_path else "")}
        self._model_layers: dict[str, tuple[str, str]] = {}
        for model, path in (self.prefix_by_model or {}).items():
            if not isinstance(model, str) or not model or not isinstance(path, str) or not Path(path).is_file():
                raise ValueError(f"prefix_by_model : fichier de préfixe illisible pour le modèle {model!r} : {path!r}")
            try:
                self._model_layers[model] = (self._load_prefix(path), self._layers["internal"][1])
            except OSError as e:
                raise ValueError(f"prefix_by_model : fichier de préfixe illisible pour le modèle {model!r} : {path!r} ({e})") from e
        self._variant_layers: dict[str, tuple[str, str]] = {}
        for name, path in (self.prefix_variants or {}).items():
            if not isinstance(name, str) or not name or not isinstance(path, str) or not Path(path).is_file():
                raise ValueError(f"prefix_variants : fichier de préfixe illisible pour la variante {name!r} : {path!r}")
            try:
                self._variant_layers[name] = (self._load_prefix(path), self._layers["internal"][1])
            except OSError as e:
                raise ValueError(f"prefix_variants : fichier de préfixe illisible pour la variante {name!r} : {path!r} ({e})") from e
        if self._variant_layers:  # mise au point : les noms et leurs chemins (jamais le contenu)
            logger.info("context engine : variantes de préfixe (nom -> fichier) : %s",
                        "; ".join(f"{m} -> {(self.prefix_variants or {})[m]}" for m in self._variant_layers))
        if self._model_layers:  # mise au point d'une démo : les ids connus et leurs chemins (jamais le contenu)
            logger.info("context engine : préfixes par modèle (id exact -> fichier) : %s ; repli : %s",
                        "; ".join(f"{m} -> {(self.prefix_by_model or {})[m]}" for m in self._model_layers), self.prefix_path)
        if self.product_prefix_path is not None:
            self._layers["product"] = (self._load_prefix(self.product_prefix_path), "")
        # Hash par locataire, figé au premier assemblage du locataire : ne dépend d'aucun tour, palier ou horloge
        # (invariant testé). Sans préfixe produit : calculé à la construction, comme avant.
        self._sha_by_tenant: dict[str, str] = {}
        self._sha_lock = threading.Lock()
        if self.product_prefix_path is None:
            self._sha_by_tenant[""] = _compute_prefix_sha256(self._prefix_text, self.tools.core())

    def _young(self) -> str | None:
        if self.young_profile is None or self.product_prefix_path is None:
            return None
        tid = current_tenant()
        if tid == INTERNAL_TENANT:
            return None
        try:
            return self.young_profile(tid)
        except Exception:  # noqa: BLE001 — profil illisible : le préfixe le plus prudent (moins de 13 ans)
            logger.warning("profil mineur de %s illisible", tid, exc_info=True)
            return "child"

    def _model_override(self) -> str | None:
        """Id du modèle servant le tour s'il a SON préfixe (id exact), sinon None. Locataire interne (ou sans préfixe produit) seulement."""
        if not self._model_layers or (self.product_prefix_path is not None and current_tenant() != INTERNAL_TENANT):
            return None
        from nokker.context.turn_model import active_model

        m = active_model()
        return m if m in self._model_layers else None

    def _layer(self) -> tuple[str, str]:
        """(préfixe, profil) du fil courant : le préfixe du locataire, ou, pour une session `prefix: "host"`, les seuls
        invariants de sûreté (`prompts/prefix-invariants.md`) avec le même profil ; ou, pour une session qui a choisi une
        variante déclarée (`prefix_variant`), le préfixe de cette variante."""
        base = self._base_layer()
        if self._prefix_host():
            return (self._invariants_prefix(), base[1])
        variant = self._variant()
        if variant is not None:
            return self._variant_layers[variant]
        return base

    def _variant(self) -> str | None:
        """Nom de la variante de préfixe choisie par la session du fil courant, si elle est déclarée ; sinon None. Locataire interne
        (ou sans préfixe produit) seulement, comme `prefix_by_model` ; un nom qui n'est plus déclaré retombe sur le préfixe par défaut."""
        if not self._variant_layers or (self.product_prefix_path is not None and current_tenant() != INTERNAL_TENANT):
            return None
        from nokker.tools.scope import current_thread

        thread = current_thread()
        name = prefix_mode.variant_registry().get(thread) if thread else ""
        return name if name in self._variant_layers else None

    def _prefix_host(self) -> bool:
        from nokker.tools.scope import current_thread

        thread = current_thread()
        return bool(thread) and prefix_mode.registry().get(thread) == "host"

    def _invariants_prefix(self) -> str:
        text = getattr(self, "_invariants_text", None)
        if text is None:
            text = self._invariants_text = self._load_prefix(INVARIANTS_PREFIX_PATH)
        return text

    def _base_layer(self) -> tuple[str, str]:
        """(préfixe, profil) du locataire courant."""
        override = self._model_override()
        if override is not None:
            return self._model_layers[override]
        if self.product_prefix_path is None or current_tenant() == INTERNAL_TENANT:
            return self._layers["internal"]
        young = self._young()
        if young:
            key = f"young:{young}"
            if key not in self._layers:
                from nokker.household.guard import young_prefix

                base = self._layers["product"][0]
                self._layers[key] = (f"{base.rstrip()}\n\n{young_prefix(young)}", "")
            return self._layers[key]
        return self._layers["product"]

    def _hidden_tools(self) -> frozenset[str]:
        """Outils retirés du fil courant (`deny_tools` de sa session, `tenancy/resources.py`) ; vide hors d'un fil ou sans registre par fil."""
        from nokker.tools.scope import current_thread

        thread, reg, seen = current_thread(), self.tools, 0
        while reg is not None and not callable(getattr(reg, "hidden_for", None)) and seen < 8:
            reg, seen = getattr(reg, "inner", None), seen + 1
        hidden = getattr(reg, "hidden_for", None)
        if not thread or not callable(hidden):
            return frozenset()
        try:
            return frozenset(hidden(thread))
        except Exception:  # un registre illisible ne casse pas l'assemblage : préfixe complet
            logger.warning("outils retirés du fil illisibles", exc_info=True)
            return frozenset()

    def _host_prompt(self) -> str:
        """Prompt système posé par l'hôte pour le fil courant (`context/host_prompt.py`), "" sans prompt ou hors d'un fil."""
        from nokker.tools.scope import current_thread

        thread = current_thread()
        return host_prompt.registry().get(thread) if thread else ""

    @property
    def _prefix_text(self) -> str:
        """Préfixe du fil : le texte de la couche, rendu pour son catalogue (lignes qui nomment un outil retiré omises),
        puis le prompt système de l'hôte s'il y en a un : APRÈS les invariants du noyau, jamais devant, octet pour octet."""
        text = prefix_tools.render(self._layer()[0], self._hidden_tools())
        host = self._host_prompt()
        return f"{text.rstrip()}\n\n{host}" if host else text

    @property
    def _profile_text(self) -> str:
        return self._layer()[1]

    def stable_prefix(self) -> str:
        """Texte du préfixe stable du locataire courant (tête du message système), pour un provider qui pose lui-même
        ses points de cache (`providers/anthropic.py`). Lecture seule : l'assemblage n'en dépend pas."""
        return self._prefix_text

    @property
    def _prefix_sha256(self) -> str:
        scope = getattr(self.tools, "cache_key", None) or getattr(self.tools, "scope_key", None)  # outils selon le fil
        key = "" if self.product_prefix_path is None else \
            f"{current_tenant()}|{self._young() or ''}|{scope() if callable(scope) else ''}"
        denied = prefix_tools.relevant(self._layer()[0], self._hidden_tools())
        if denied:  # préfixe rendu autrement : une empreinte par jeu d'outils retirés qu'il nomme, stable pour ce jeu
            key = f"{key}|deny:{','.join(sorted(denied))}"
        if self._prefix_host():  # invariants seuls : une empreinte à part ; sans le mode, la clé est celle d'avant
            key = f"{key}|prefix:host"
        elif self._variant() is not None:  # variante de session : une empreinte par variante, stable ; sans variante, clé d'avant
            key = f"{key}|variant:{self._variant()}"
        host = self._host_prompt()
        if host:  # prompt de l'hôte : une empreinte par texte ; sans prompt, la clé est celle d'avant
            key = f"{key}|host:{host_prompt.fingerprint(host)}"
        override = self._model_override()
        if override is not None:  # sha figé par (locataire, modèle) : stable dans un tour, distinct d'un modèle à l'autre
            key = f"{key}|model:{override}"
        sha = self._sha_by_tenant.get(key)
        if sha is None:
            sha = _compute_prefix_sha256(self._prefix_text, self.tools.core())
            with self._sha_lock:
                sha = self._sha_by_tenant.setdefault(key, sha)
        return sha

    def _load_prefix(self, path: str | None) -> str:
        text = self._load(path)
        if self.reply_language:
            from nokker.context import reply_language

            text = reply_language.apply(text)
        return text

    @staticmethod
    def _load(path: str | None) -> str:
        if not path:
            return ""
        p = Path(path)
        if not p.exists():
            logger.warning("context engine: fichier absent, couche vide : %s", path)
            return ""
        return p.read_text(encoding="utf-8")

    def tools_index_block(self, loaded_tools: list[ToolSpec] | None = None) -> str:
        """Index des outils non cœur, noms par famille (`tools/select.py::compact_index`). Dans le message système,
        après le préfixe : stable d'un tour à l'autre tant que les sources d'outils ne changent pas, et indépendant
        des schémas chargés pour le tour (les descriptions arrivent avec le schéma, ou par tool_find).

        Session `prefix: "host"` seulement : l'index sert à trouver les outils DIFFÉRÉS ; quand tous les outils non cœur ont déjà
        leur schéma dans `tools` (`tool_loading: all`, ou tous chargés), il ne dit rien que `tools` ne dise déjà et n'est pas rendu.
        Les autres préfixes gardent l'index tel qu'avant, octet pour octet (ligne « serveur indisponible » comprise dans les deux cas)."""
        names = [name for name, _ in self.tools.index()]
        block = compact_index(names)
        if block and loaded_tools is not None and self._prefix_host():
            have = {s.name for s in loaded_tools}
            if all(n in have for n in names):
                block = ""
        down = getattr(self.tools, "unavailable", None)  # APRÈS index() : c'est lui qui découvre la panne
        servers = sorted({name for name, _why in down()}) if callable(down) else []
        return "\n".join(x for x in (block, unavailable_line(servers) if servers else "") if x)

    def system_parts(self, index_block: str) -> list[str]:
        """Message système unique, du plus stable au moins stable (cache de préfixe) : préfixe, index d'outils,
        profil. V1 y ajoute la mémoire de session, qui change à la clôture d'un épisode."""
        parts = [self._prefix_text]
        if index_block:
            parts.append(index_block)
        if self._profile_text:
            parts.append(self._profile_text)
        return parts

    def project_block(self, turn: Turn) -> str:
        """Noyau de la mémoire de projet pour le fil du tour (figé par tour), ou "" sans mémoire de projet."""
        if self.project_memory is None:
            return ""
        from nokker.memory.session_mode import registry as _memory_modes

        # `memory: off|host` : aucune note apprise, seulement les consignes du dépôt (instructions, pas de la mémoire)
        only = _memory_modes().kernel_memory_off(turn.thread_id)
        try:
            if only:
                return self.project_memory.core_block(turn.thread_id, turn.index, turn.input.text,
                                                      instructions_only=True)
            return self.project_memory.core_block(turn.thread_id, turn.index, turn.input.text)
        except Exception:  # la mémoire ne casse jamais un assemblage
            logger.exception("mémoire de projet : noyau indisponible")
            return ""

    def plan_block(self, turn: Turn) -> str:
        """Consignes héritées du plan pour le fil du tour, ou "" (pas de plan, fil sans carte)."""
        if self.plan is None:
            return ""
        try:
            return self.plan.block_for_thread(turn.thread_id)
        except Exception:  # le plan ne casse jamais un assemblage
            logger.exception("plan : bloc indisponible")
            return ""

    def feature_notes_block(self, turn: Turn) -> str:
        """Bloc `[Fonctions]` du tour, ou "" (drapeau éteint, rien de pertinent, service en panne)."""
        if self.feature_notes is None:
            return ""
        try:
            return self.feature_notes.block_for(turn.input.text, minor=self._young() is not None)
        except Exception:  # les notes ne cassent jamais un assemblage
            logger.exception("notes de fonctions : bloc indisponible")
            return ""

    def mode_note_for(self, turn: Turn) -> str:
        """Rappel du mode de la session pour CE tour, ou "" (crochet absent, rien à dire, crochet en panne)."""
        if self.mode_note is None:
            return ""
        try:
            return self.mode_note(turn.thread_id) or ""
        except Exception:  # un rappel de mode ne casse jamais un assemblage
            logger.exception("rappel du mode de la session indisponible")
            return ""

    def self_context_for(self, turn: Turn) -> str:
        """Bloc `[Environnement]` de CE tour, ou "" (crochet absent, rien de connu, crochet en panne)."""
        if self.self_context is None:
            return ""
        try:
            return self.self_context(turn) or ""
        except Exception:  # un bloc d'environnement ne casse jamais un assemblage
            logger.exception("bloc d'environnement indisponible")
            return ""

    def host_context_parts(self, turn: Turn, visible_heads: list[str]) -> list[str]:
        """Blocs de contexte de l'HÔTE de la session (`context_blocks`, docs/HOST-EMBED.md §20) à écrire dans l'en-tête de ce
        tour ; [] sans option posée (en-tête d'avant, à l'octet près). `visible_heads` : en-têtes rejoués, du plus récent au
        plus ancien. Un échec ne casse jamais l'assemblage."""
        from nokker.context import host_blocks

        blocks = host_blocks.registry().exact(turn.thread_id)
        if blocks is None:
            return []
        try:
            return host_blocks.render(blocks, visible_heads)
        except Exception:  # noqa: BLE001
            logger.exception("blocs de contexte de l'hôte indisponibles")
            return []

    def turn_head(self, turn: Turn, compute: Callable[[], list[str]]) -> str:
        """En-tête du tour, calculé au premier assemblage puis figé sur le tour (`Turn.head`) : les appels suivants
        du même tour et l'historique des tours suivants le reprennent à l'identique. Drapeau `reply_language_tail` :
        une ligne de langue en dernier, juste avant le message de l'utilisateur."""
        if not turn.head:
            parts = compute()
            if self.reply_language_tail:
                from nokker.context.reply_language import tail_line

                parts = [*parts, tail_line()]
            turn.head = "\n\n".join(parts)
        return turn.head

    def images_kept(self, history: list[Turn], turn: Turn) -> set[tuple[int, str]]:
        """(index du tour, sha256) des images envoyées en entier : celles du tour courant, puis les plus récentes de la
        fenêtre jusqu'à `keep_images`. Les autres sont délestées en une ligne de texte (comme un gros résultat)."""
        kept: set[tuple[int, str]] = {(turn.index, i.sha256) for i in turn.images}
        budget = max(0, self.keep_images - len(kept))
        for past in reversed(history):
            for img in reversed(past.images):
                if budget <= 0:
                    return kept
                kept.add((past.index, img.sha256))
                budget -= 1
        return kept

    @staticmethod
    def user_message(t: Turn, text: str, kept: set[tuple[int, str]]) -> dict:
        """Message `user` d'un tour : chaîne seule sans image ; avec images, parties `text` + une partie `nokker_image`
        par image gardée (le noyau la change en `image_url` ou en note selon le palier, nokker/images/render.py) ; une
        image délestée devient une ligne de texte, sans octets."""
        if not t.images:
            return {"role": "user", "content": text}
        parts: list[dict] = [{"type": "text", "text": text}] if text else []
        for img in t.images:
            if (t.index, img.sha256) in kept:
                # `turn` et `size` : sur un palier sans vision, une image d'un tour précédent devient la ligne de
                # délestage (nokker/images/render.py), seule celle du tour courant est signalée non lue.
                parts.append({"type": IMAGE_PART, "sha256": img.sha256, "media_type": img.media_type,
                              "name": img.name, "turn": t.index, "size": img.size})
            else:
                parts.append({"type": "text", "text": shed_line(img.name, img.media_type, img.size, t.index)})
        return {"role": "user", "content": parts}

    @staticmethod
    def past_user(past: Turn, with_head: bool = True) -> str:
        """Message `user` d'un tour tel qu'il a été envoyé : en-tête figé + texte. Sans en-tête (tour d'avant
        `Turn.head`, ou en-tête délesté par le never clear) : contexte reçu + texte, comme avant. Un en-tête d'avant
        r46 voit ses [Fichier]/[Dossier] joints portés dans la clôture commune ICI, au rejeu seulement
        (`attachments.fence_head`, idempotent) : `Turn.head` persisté n'est jamais réécrit."""
        if past.head and with_head:
            return "\n\n".join([fence_head(past.head), past.input.text])
        if past.injected:
            return "\n\n".join([render_injected(past.injected), past.input.text])
        return past.input.text

    def assemble(self, turn: Turn, history: list[Turn], recall: list[Recall], loaded_tools: list[ToolSpec],
                 tier: Tier) -> Assembled:
        core_tools = self.tools.core()
        messages: list[dict] = []

        # Couche 1+2 — préfixe stable + profil (optionnel), FUSIONNÉS en un seul message « system »,
        # EN TÊTE. Mesuré en réel le 17.10.2026 (P1-S1, contre le Router SOKKAN en prod) : une requête
        # qui porte un message `system` qui n'est pas `messages[0]`, ou plus d'un message `system`,
        # échoue systématiquement en `502 Bad Gateway` amont (reproduit en isolant chaque combinaison
        # de rôles : `sys,user`→200, `user,sys`→502, `sys,sys`→502, `sys,user,sys`→502,
        # `sys,user,user`→200, `sys,user,assistant,user`→200 — aucune des deux docs, OpenAI ni Router,
        # ne documente cette limite ; elle n'est pas dans le contrat de ce module, qui n'a pas à la
        # deviner, mais elle casse la couche 5 d'origine, qui ajoutait le rappel et l'index d'outils
        # comme des messages `system` SUPPLÉMENTAIRES après le tour humain). Un seul message `system`
        # en tête + tout le reste en rôle `user`/`assistant`/`tool` contourne la limite sans changer ce
        # que le modèle reçoit comme INFORMATION. Le hash du préfixe (A4, `_compute_prefix_sha256`) ne
        # change pas : il porte sur `self._prefix_text` seul, pas sur ce message combiné.
        index_block = self.tools_index_block(loaded_tools)
        project_text = self.project_block(turn)
        plan_text = self.plan_block(turn)
        system_parts = self.system_parts(index_block)
        if project_text:
            system_parts.append(project_text)
        if plan_text:
            system_parts.append(plan_text)
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # Couche 3 — épisodes clos : vide en v0 (aucune frontière d'épisode, cf. docstring). Budget 0.
        episodes_text = ""

        # Couche 4 — tours bruts de l'épisode courant, append-only, chacun avec SON en-tête d'origine (`past_user`).
        kept = self.images_kept(history, turn)
        for i, past in enumerate(history):
            messages.append(self.user_message(past, self.past_user(past), kept))
            following = history[i + 1] if i + 1 < len(history) else turn
            messages.extend(m.to_openai(thinking=True) for m in (replayed_messages(past, following) if self.drop_card_end_text
                                                    else past.messages))

        # Couche 5 — en-tête du tour (date, rappel CortHeXis, contexte reçu) + message courant, REGROUPÉS DANS LE MÊME
        # message `user` (même raison que ci-dessus : un message `system` qui n'est pas en tête fait échouer l'appel
        # en 502 ; ce tour reste le DERNIER message, rôle `user`, ce que tout amont accepte).
        # FORMAT = CONTRAT avec l'entraînement de Nina-70B (dépôt interne) :
        # `[Rappel mémoire]\n…\n[/Rappel]\n\n<message>` — rappel AVANT le message, balise fermante. Ne pas changer l'un
        # sans l'autre (cf docs/CONTEXT-FORMAT.md). L'index des outils non cœur est dans le message système depuis le
        # 20.10.2026 (option A, cache entre tours) ; l'en-tête est figé par tour et rejoué tel quel par l'historique.
        def head_parts() -> list[str]:
            # Date FIGÉE avec l'en-tête du tour (`turn_head` ne calcule qu'une fois par tour) : recalculée à chaque appel,
            # elle changeait le bloc du tour et tout ce qui le suit (préfixe réutilisable 75-85 % au lieu de ~100 %,
            # mesuré sur le canari le 20.10.2026).
            parts = [date_block(self.now_fn(), turn)]
            env = self.self_context_for(turn)
            if env:  # où tourne le fil, canal, machines liées : juste après la date, avant le rappel
                parts.append(env)
            # Langue RÉSOLUE du tour (`Turn.lang`, valeur fermée) : une ligne écrite par le noyau, figée avec l'en-tête.
            # Seulement sur un VRAI signal de langue (`Turn.lang_signal` : requête, compte, message détecté, fil, `NOKKER_LANG`) :
            # sur le défaut du code la ligne affirmerait un fait inventé. Session `lang_hint: false` : pas de ligne.
            from nokker.context.reply_language import turn_line

            line = turn_line(turn)
            if line:
                parts.append(line)
            own = [r for r in recall if getattr(r, "source", "") != IMPORT_SOURCE]
            imported = [r for r in recall if getattr(r, "source", "") == IMPORT_SOURCE]
            if own:
                # Ligne d'avant inchangée ; fait de tiers : marqueur de source + inline() (nokker/memory/provenance.py).
                rappel = "\n".join(recall_line(r, self.recall_text_limit) for r in own)
                parts.append(f"[Rappel mémoire]\n{rappel}\n[/Rappel]")
            if imported:
                # Données importées d'un autre agent (lot 30 d, texte validé par la relecture du format) : bloc à part, après le rappel,
                # jamais mêlées à la mémoire de Nokker. Seulement si `local.import_codex_memory` les a produites.
                parts.append(render_imported(imported, self.recall_text_limit))
            notes = self.feature_notes_block(turn)
            if notes:  # drapeau `feature_notes` : après le rappel mémoire, avant le contexte reçu
                parts.append(notes)
            note = self.mode_note_for(turn)
            if note:  # mode de la session : après les notes de fonctions, avant le contexte reçu
                parts.append(note)
            # Blocs de contexte de l'hôte (`context_blocks`) : après le mode, avant le contexte reçu, ordre fixe des kinds.
            parts.extend(self.host_context_parts(turn, [h.head for h in reversed(history) if h.head]))
            if turn.injected:
                # Contexte reçu d'un autre fil (docs/BUBBLES.md §3-§4, docs/CONTEXT-FORMAT.md) : après le rappel.
                parts.append(render_injected(turn.injected))
            if desktop_consigne_needed(turn, history):
                # Marqueur venu du bureau (docs/DESKTOP.md §3.6) : la variante, à la place de CONSIGNE, une fois.
                parts.append(MASKED_CONSIGNE_DESKTOP)
            elif consigne_needed(turn, history):
                # Valeurs masquées sur l'appareil (docs/LOKKAL-PHONE.md §4.3.2) : une fois par fil, au premier tour qui
                # porte un marqueur, juste avant les pièces jointes.
                parts.append(MASKED_CONSIGNE)
            if turn.attached:
                # Pièces jointes texte/dossier/url (docs/CONTEXT-FORMAT.md) : en dernier, juste avant le message.
                parts.append(turn.attached)
            return parts

        head = self.turn_head(turn, head_parts)
        messages.append(self.user_message(turn, self.past_user(turn), kept))

        # Rejoue l'ÉPISODE COURANT de CE tour : les tool_calls/résultats déjà produits PAR CE TOUR,
        # avant ce call_index (`core/loop.py` rappelle `assemble()` à chaque appel du MÊME tour, pas
        # seulement une fois par tour). Bug réel trouvé en mesurant contre le Router (P1-S1,
        # 17.10.2026) : sans cette ligne, le modèle ne voit JAMAIS ses propres appels d'outils déjà
        # faits dans ce tour — chaque appel suivant lui repose EXACTEMENT la question d'origine, sans
        # aucune trace de ce qu'il vient de faire. Conséquence observée en réel : `memory_search` et
        # `infra_disk_usage` rappelés en boucle (jusqu'à 14 fois) jusqu'à `max_calls`, le modèle
        # « redémarrant » à chaque appel sans savoir qu'il a déjà le résultat. Le Protocol
        # `ContextEngine` (core/ports.py) passe le `Turn` ENTIER précisément pour ça — ce n'était pas
        # exploité. Rôles produits ici : `assistant` (avec tool_calls) et `tool` seulement, jamais
        # `system` (cf. couche 1+2/5 ci-dessus : un `system` qui n'est pas en tête fait échouer la
        # requête en 502) — donc aucun conflit avec cette contrainte.
        messages.extend(m.to_openai(thinking=True) for m in turn.messages)

        tools_payload = [s.to_openai() for s in core_tools] + [s.to_openai() for s in loaded_tools]

        layer_tokens = {
            "prefix": estimate_tokens(self._prefix_text),
            "tools_index": estimate_tokens(index_block),
            "profile": estimate_tokens(self._profile_text),
            "episodes": estimate_tokens(episodes_text),
            "current": estimate_tokens("".join(self.past_user(p) for p in history)),
            "turn_block": estimate_tokens("\n\n".join([head, turn.input.text])),
        }
        if self.project_memory is not None:
            layer_tokens["project"] = estimate_tokens(project_text)
        if plan_text:
            layer_tokens["plan"] = estimate_tokens(plan_text)
        return Assembled(messages=messages, tools=tools_payload, prefix_sha256=self._prefix_sha256,
                          layer_tokens=layer_tokens)

    def admit_result(self, result: ToolResult) -> ToolResult:
        if len(result.content) <= self.reservoir_char_limit:
            return result
        truncated = result.content[:self.reservoir_char_limit]
        # V0 seulement (sans `context_enabled`) : pas de réservoir ni de `recall_context` exposé, donc ne JAMAIS
        # promettre au modèle un outil qu'il ne peut pas appeler. V1 (ContextManager) a son propre réservoir.
        mention = (f"\n[...tronqué à {self.reservoir_char_limit} caractères sur {len(result.content)} — "
                   "le réservoir réel arrive avec le chantier A-1 ; pas de relecture possible pour l'instant]")
        return ToolResult(call_id=result.call_id, name=result.name, content=truncated + mention,
                           is_error=result.is_error, reservoir_id=None, secret_handles=result.secret_handles)


def replayed_messages(past: Turn, following: Turn) -> list:
    """Messages d'un tour passé tels que le modèle les relit. Un tour qui s'est fini sur une carte (`approval_ids`) et
    que la décision relance (`following` est la suite de carte, origine `approval`) perd son texte de fin : écrit AVANT
    la décision (« une carte est apparue… »), il resterait le dernier message de l'assistant, périmé, devant le bloc
    `[Approbation]` qui dit déjà ce qui a été tranché. Les appels d'outils et leurs résultats restent."""
    msgs = past.messages
    if past.approval_ids and following.input.origin == "approval" and msgs and msgs[-1].role == "assistant" \
            and not msgs[-1].tool_calls:
        return msgs[:-1]
    return msgs


def render_injected(blocks: list[str]) -> str:
    """`[Contexte reçu]\n<un bloc par ligne>\n[/Contexte]` — format docs/CONTEXT-FORMAT.md."""
    return neutralize_date(neutralize_env("[Contexte reçu]\n" + "\n".join(blocks) + "\n[/Contexte]"))


def _compute_prefix_sha256(prefix_text: str, core_tools: list[ToolSpec]) -> str:
    canon_tools = sorted(
        ({"name": t.name, "description": t.description, "parameters": t.parameters} for t in core_tools),
        key=lambda d: d["name"],
    )
    payload = prefix_text + "\n" + json.dumps(canon_tools, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["DEFAULT_PREFIX_PATH", "ZURICH", "ContextEngineV0", "date_block", "estimate_tokens", "format_now",
           "format_now_fr", "render_injected", "replayed_messages"]
