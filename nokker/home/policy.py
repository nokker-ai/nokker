"""Politique de la maison (docs/HOME-ASSISTANT.md §3) : classe d'un appel de service et droits des membres du foyer.

Classe d'un appel (`classify`), déterministe, sans modèle :
  - `direct` : exécuté tout de suite par l'agent maison (lumières, volets, climat dans les bornes, médias) ;
  - `strong` : jamais exécuté par l'agent maison ; il revient « à confirmer » et ne part qu'après une carte FORTE
    décidée par un humain sur son appareil (serrures, alarmes, garage, portail, caméras, scripts, automatisations,
    chauffage hors bornes) ;
  - `deny` : refusé (domaine absent des listes, service qui vise une entité d'un autre domaine, cible non nommée).

Les lectures (états, carte des entités) sont libres pour un membre qui a le droit `read` sur le domaine.

Droits (`Rights`) : par rôle, trois listes de domaines (`read`, `direct`, `strong`), `*` pour tous. Un membre est
une identité de tour (`Turn.sender`) associée à un rôle ; une identité inconnue prend `default_role` (« guest » par
défaut). Un profil mineur d'un foyer (teen/child) prend toujours le rôle du même nom, quelle que soit la config.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

READ, DIRECT, STRONG, DENY = "read", "direct", "strong", "deny"

DEFAULT_DIRECT_DOMAINS = ("light", "cover", "climate", "media_player")
DEFAULT_STRONG_DOMAINS = ("lock", "alarm_control_panel", "camera", "script", "automation")
# Volets roulants : directs ; porte de garage et portail (classe d'appareil HA) : carte forte.
STRONG_COVER_CLASSES = frozenset({"garage", "gate"})
# Bus physiques écrits directement (KNX, écriture sur le bus depuis HA 2026.10) : jamais une action directe. Absents des
# listes (défaut) : refusés ; listés par l'utilisateur dans `direct_domains` ou `strong_domains` : carte forte.
PHYSICAL_BUS_DOMAINS = frozenset({"knx"})
CONFIG_KINDS = ("automation", "script")
# Services permis par domaine (grammaire du sous-agent ET politique) ; un domaine ajouté par l'utilisateur sans entrée
# ici n'a que DEFAULT_SERVICES.
SERVICES: dict[str, tuple[str, ...]] = {
    "light": ("turn_on", "turn_off", "toggle"),
    "cover": ("open_cover", "close_cover", "stop_cover", "set_cover_position"),
    "climate": ("set_temperature", "set_hvac_mode", "set_preset_mode", "set_fan_mode", "turn_on", "turn_off"),
    "media_player": ("media_play", "media_pause", "media_stop", "media_next_track", "media_previous_track",
                     "volume_set", "volume_mute", "turn_on", "turn_off", "select_source"),
    "lock": ("lock", "unlock", "open"),
    "alarm_control_panel": ("alarm_arm_home", "alarm_arm_away", "alarm_arm_night", "alarm_disarm"),
    "camera": ("turn_on", "turn_off", "snapshot", "play_stream", "record"),
    "script": ("turn_on", "turn_off"),
    "automation": ("trigger", "turn_on", "turn_off"),
    "knx": ("send", "read"),  # bus physique : toujours une carte forte (ou refusé), voir PHYSICAL_BUS_DOMAINS
}
DEFAULT_SERVICES = ("turn_on", "turn_off", "toggle")


def services_for(domain: str) -> tuple[str, ...]:
    return SERVICES.get(domain, DEFAULT_SERVICES)  # écritures de configuration HA (outil `maison_config`) : toujours fortes
TEMPERATURE_KEYS = ("temperature", "target_temp_low", "target_temp_high")
# Cibles d'un appel : une entité nommée, jamais une zone ou un appareil entier (la politique se juge par entité).
TARGET_KEYS = frozenset({"entity_id", "area_id", "device_id", "floor_id", "label_id"})
MAX_TARGETS = 10

DEFAULT_ROLES: dict[str, dict[str, list[str]]] = {
    "adult": {"read": ["*"], "direct": ["*"], "strong": ["*"]},
    "teen": {"read": ["*"], "direct": ["light", "cover", "climate", "media_player"], "strong": []},
    "child": {"read": ["light", "media_player", "climate", "sensor", "binary_sensor"],
              "direct": ["light", "media_player"], "strong": []},
    "guest": {"read": ["light", "cover", "climate", "media_player", "sensor", "binary_sensor", "weather"],
              "direct": ["light", "media_player"], "strong": []},
}
MINOR_ROLES = {"teen": "teen", "child": "child"}


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


@dataclass(frozen=True)
class Classified:
    klass: str  # direct | strong | deny
    reason: str


@dataclass(frozen=True)
class RoleRights:
    read: frozenset[str]
    direct: frozenset[str]
    strong: frozenset[str]

    @staticmethod
    def _has(allowed: frozenset[str], domain: str) -> bool:
        return "*" in allowed or domain in allowed

    def can(self, level: str, domain: str) -> bool:
        if level == READ:
            return self._has(self.read, domain)
        if level == DIRECT:
            return self._has(self.direct, domain)
        if level == STRONG:
            return self._has(self.strong, domain)
        return False

    def any_right(self) -> bool:
        return bool(self.read or self.direct or self.strong)


def _domains(raw: Any) -> frozenset[str]:
    if raw in (None, ""):
        return frozenset()
    if isinstance(raw, str):
        raw = [raw]
    return frozenset(str(d).strip() for d in raw if str(d).strip())


@dataclass
class HomePolicy:
    """Classes d'appels et droits des membres. `profile_of(sender)` : profil mineur d'un foyer (`teen`|`child`) ou
    None — branché sur `Household.minor_state` quand le foyer est allumé."""

    direct_domains: frozenset[str] = frozenset(DEFAULT_DIRECT_DOMAINS)
    strong_domains: frozenset[str] = frozenset(DEFAULT_STRONG_DOMAINS)
    strong_entities: frozenset[str] = frozenset()
    climate_min: float = 16.0
    climate_max: float = 24.0
    roles: dict[str, RoleRights] = field(default_factory=dict)
    members: dict[str, str] = field(default_factory=dict)
    default_role: str = "guest"
    profile_of: Callable[[str | None], str | None] | None = None
    # Liste blanche des entités vues par Nokker (motifs glob : `light.*`, `cover.salon_*`), même si Home Assistant en
    # expose davantage. Vide = aucune entité (fermé par défaut).
    entities: tuple[str, ...] = ()

    def exposed(self, entity_id: str) -> bool:
        return any(fnmatch.fnmatchcase(entity_id, pat) for pat in self.entities)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> HomePolicy:
        raw = raw or {}
        roles_raw: dict[str, Any] = {k: dict(v) for k, v in DEFAULT_ROLES.items()}
        for name, spec in (raw.get("roles") or {}).items():
            if isinstance(spec, dict):
                roles_raw[str(name)] = {**roles_raw.get(str(name), {}), **spec}
        roles = {name: RoleRights(read=_domains(spec.get("read")), direct=_domains(spec.get("direct")),
                                  strong=_domains(spec.get("strong"))) for name, spec in roles_raw.items()}
        climate = raw.get("climate") or {}
        default_role = str(raw.get("default_role") or "guest")
        if default_role not in roles:
            raise ValueError(f"home_assistant.default_role inconnu : {default_role}")
        members = {str(k): str(v) for k, v in (raw.get("members") or {}).items()}
        unknown = sorted({r for r in members.values() if r not in roles})
        if unknown:
            raise ValueError(f"home_assistant.members : rôle(s) inconnu(s) {', '.join(unknown)}")
        lo, hi = float(climate.get("min", 16.0)), float(climate.get("max", 24.0))
        if lo > hi:
            raise ValueError("home_assistant.climate : min > max")
        return cls(direct_domains=_domains(raw.get("direct_domains", DEFAULT_DIRECT_DOMAINS)),
                   strong_domains=_domains(raw.get("strong_domains", DEFAULT_STRONG_DOMAINS)),
                   strong_entities=_domains(raw.get("strong_entities")),
                   climate_min=lo, climate_max=hi, roles=roles, members=members, default_role=default_role,
                   entities=tuple(str(e).strip() for e in (raw.get("entities") or []) if str(e).strip()))

    # ── droits ──
    def role_of(self, sender: str | None) -> str:
        profile = None
        if self.profile_of is not None and sender:
            try:
                profile = self.profile_of(sender)
            except Exception:  # noqa: BLE001 — foyer illisible : le rôle le plus restreint
                return "child"
        if profile in MINOR_ROLES:
            return MINOR_ROLES[profile]
        return self.members.get(sender or "", self.default_role)

    def rights(self, sender: str | None) -> RoleRights:
        return self.roles.get(self.role_of(sender)) or RoleRights(frozenset(), frozenset(), frozenset())

    def allowed(self, sender: str | None, level: str, domain: str) -> bool:
        return self.rights(sender).can(level, domain)

    def actionable(self, sender: str | None, entity_id: str) -> bool:
        """Le membre peut-il AGIR sur cette entité (direct ou fort) ? Sert à la grammaire du sous-agent."""
        dom = domain_of(entity_id)
        listed = dom in self.direct_domains or dom in self.strong_domains or entity_id in self.strong_entities
        return listed and (self.allowed(sender, DIRECT, dom) or self.allowed(sender, STRONG, dom))

    # ── classe d'un appel ──
    def classify(self, domain: str, service: str, entity_ids: Iterable[str], data: dict[str, Any] | None,
                 attributes: Callable[[str], dict[str, Any] | None]) -> Classified:
        """`attributes(entity_id)` : attributs connus de l'entité (None = entité inconnue de Home Assistant)."""
        ids = list(entity_ids)
        if not domain or not service:
            return Classified(DENY, "domaine et service obligatoires")
        if not ids:
            return Classified(DENY, "aucune entité nommée (une zone ou un appareil entier ne se vise pas)")
        if len(ids) > MAX_TARGETS:
            return Classified(DENY, f"au plus {MAX_TARGETS} entités par appel")
        if service not in services_for(domain):
            return Classified(DENY, f"service non permis : {domain}.{service}")
        if TARGET_KEYS & set((data or {}).keys()):
            return Classified(DENY, "cible dans data refusée : nommer les entités dans entity_id")
        if domain in PHYSICAL_BUS_DOMAINS and domain not in self.direct_domains \
                and domain not in self.strong_domains:
            return Classified(DENY, f"écriture sur un bus physique ({domain}) exclue par défaut")
        strong_why: list[str] = []
        if domain in PHYSICAL_BUS_DOMAINS:
            strong_why.append(f"écriture sur un bus physique : {domain}")
        for eid in ids:
            if domain_of(eid) != domain:
                return Classified(DENY, f"{eid} n'est pas une entité du domaine {domain}")
            attrs = attributes(eid) if self.exposed(eid) else None
            if attrs is None:
                return Classified(DENY, f"entité inconnue : {eid}")
            if eid in self.strong_entities:
                strong_why.append(f"{eid} : entité sensible (config)")
            elif domain == "cover" and str(attrs.get("device_class") or "") in STRONG_COVER_CLASSES:
                strong_why.append(f"{eid} : {'garage' if attrs.get('device_class') == 'garage' else 'portail'}")
        if domain in self.strong_domains:
            strong_why.append(f"domaine sensible : {domain}")
        if domain == "climate" and service == "set_temperature":
            for key in TEMPERATURE_KEYS:
                if key in (data or {}):
                    try:
                        value = float((data or {})[key])
                    except (TypeError, ValueError):
                        return Classified(DENY, f"{key} n'est pas un nombre")
                    if not self.climate_min <= value <= self.climate_max:
                        strong_why.append(f"chauffage hors bornes ({value:g} hors de "
                                          f"{self.climate_min:g}–{self.climate_max:g})")
        if strong_why:
            return Classified(STRONG, " ; ".join(dict.fromkeys(strong_why)))
        if domain in self.direct_domains:
            return Classified(DIRECT, f"domaine direct : {domain}")
        return Classified(DENY, f"domaine non autorisé : {domain}")


__all__ = ["CONFIG_KINDS", "DEFAULT_DIRECT_DOMAINS", "DEFAULT_ROLES", "DEFAULT_STRONG_DOMAINS", "DENY", "DIRECT",
           "MAX_TARGETS", "PHYSICAL_BUS_DOMAINS", "READ", "SERVICES", "STRONG", "Classified", "HomePolicy", "RoleRights", "domain_of", "services_for"]
