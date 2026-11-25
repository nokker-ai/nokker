"""Schéma de `data` par service Home Assistant, pour la grammaire de l'agent maison (docs/HOME-ASSISTANT.md §4).

D'après `get_services` de Home Assistant 2026.11.0 (champs et bornes des sélecteurs), réduit à ce que l'agent maison a
besoin d'écrire. Les listes de valeurs qui dépendent de l'appareil (modes de chauffage, préréglages, sources, vitesses
d'aspiration…) sont prises dans les attributs des entités de la carte (`enums`) : le modèle ne peut plus écrire
`hvac_mode: eco` ni `position: "open"`. Un service absent de la table n'accepte aucune donnée (`{}`).
"""
from __future__ import annotations

from typing import Any

HVAC_MODES = ["off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"]
MEDIA_TYPES = ["track", "artist", "album", "playlist", "music", "radio", "podcast", "video", "channel"]
# attribut d'entité → nom de la liste de valeurs (union sur les entités de la carte)
ENUM_ATTRS = {"hvac_modes": "hvac_mode", "preset_modes": "preset_mode", "fan_modes": "fan_mode",
              "swing_modes": "swing_mode", "swing_horizontal_modes": "swing_horizontal_mode",
              "source_list": "source", "sound_mode_list": "sound_mode", "fan_speed_list": "fan_speed",
              "available_modes": "mode", "operation_list": "operation_mode", "effect_list": "effect",
              "options": "option", "available_tones": "tone"}


def _int(lo: int, hi: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": lo, "maximum": hi}


def _num(lo: float, hi: float) -> dict[str, Any]:
    return {"type": "number", "minimum": lo, "maximum": hi}


STR = {"type": "string"}
BOOL = {"type": "boolean"}


def _enum(values: list[str] | None) -> dict[str, Any]:
    vals = sorted({str(v) for v in values or [] if str(v)})
    return {"type": "string", "enum": vals} if vals else STR


def data_schemas(enums: dict[str, list[str]], media_players: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """(domaine, service) → schéma JSON de `data` (`required` compris). `enums` : valeurs vues dans les attributs des
    entités de la carte (clés de ENUM_ATTRS.values()) ; `media_players` : enceintes de la carte (join, annonce, écran)."""
    e = {k: _enum(enums.get(k)) for k in set(ENUM_ATTRS.values())}
    hvac = _enum(enums.get("hvac_mode") or HVAC_MODES)
    players = {"type": "string", "enum": sorted(media_players)} if media_players else STR
    temp = _num(5, 35)

    def obj(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": props}
        if required:
            out["required"] = list(required)
        return out

    s: dict[tuple[str, str], dict[str, Any]] = {
        # couleur : UNE seule façon de la dire (groupe d'exclusion « Color descriptors » de HA)
        ("light", "turn_on"): {"anyOf": [
            obj({"brightness_pct": _int(0, 100), "transition": _num(0, 300), **extra})
            for extra in ({}, {"color_temp_kelvin": _int(1500, 9000)}, {"color_name": STR},
                          {"rgb_color": {"type": "array", "items": _int(0, 255), "minItems": 3, "maxItems": 3}},
                          {"effect": e["effect"]}, {"brightness_step_pct": _int(-100, 100)})]},
        ("light", "turn_off"): obj({"transition": _num(0, 300)}),
        ("climate", "set_temperature"): obj({"temperature": temp, "target_temp_low": temp, "target_temp_high": temp,
                                             "hvac_mode": hvac}),
        ("climate", "set_hvac_mode"): obj({"hvac_mode": hvac}, ("hvac_mode",)),
        ("climate", "set_preset_mode"): obj({"preset_mode": e["preset_mode"]}, ("preset_mode",)),
        ("climate", "set_fan_mode"): obj({"fan_mode": e["fan_mode"]}, ("fan_mode",)),
        ("climate", "set_humidity"): obj({"humidity": _int(0, 100)}, ("humidity",)),
        ("climate", "set_swing_mode"): obj({"swing_mode": e["swing_mode"]}, ("swing_mode",)),
        ("climate", "set_swing_horizontal_mode"): obj({"swing_horizontal_mode": e["swing_horizontal_mode"]},
                                                      ("swing_horizontal_mode",)),
        ("cover", "set_cover_position"): obj({"position": _int(0, 100)}, ("position",)),
        ("cover", "set_cover_tilt_position"): obj({"tilt_position": _int(0, 100)}, ("tilt_position",)),
        ("valve", "set_valve_position"): obj({"position": _int(0, 100)}, ("position",)),
        ("fan", "turn_on"): obj({"percentage": _int(0, 100), "preset_mode": e["preset_mode"]}),
        ("fan", "set_percentage"): obj({"percentage": _int(0, 100)}, ("percentage",)),
        ("fan", "set_preset_mode"): obj({"preset_mode": e["preset_mode"]}, ("preset_mode",)),
        ("fan", "oscillate"): obj({"oscillating": BOOL}, ("oscillating",)),
        ("fan", "set_direction"): obj({"direction": {"type": "string", "enum": ["forward", "reverse"]}},
                                      ("direction",)),
        ("humidifier", "set_humidity"): obj({"humidity": _int(0, 100)}, ("humidity",)),
        ("humidifier", "set_mode"): obj({"mode": e["mode"]}, ("mode",)),
        ("water_heater", "set_temperature"): obj({"temperature": _num(20, 80)}, ("temperature",)),
        ("water_heater", "set_operation_mode"): obj({"operation_mode": e["operation_mode"]}, ("operation_mode",)),
        ("water_heater", "set_away_mode"): obj({"away_mode": BOOL}, ("away_mode",)),
        ("media_player", "volume_set"): obj({"volume_level": _num(0, 1)}, ("volume_level",)),
        ("media_player", "volume_mute"): obj({"is_volume_muted": BOOL}, ("is_volume_muted",)),
        ("media_player", "media_seek"): obj({"seek_position": _num(0, 86400)}, ("seek_position",)),
        ("media_player", "select_source"): obj({"source": e["source"]}, ("source",)),
        ("media_player", "select_sound_mode"): obj({"sound_mode": e["sound_mode"]}, ("sound_mode",)),
        ("media_player", "shuffle_set"): obj({"shuffle": BOOL}, ("shuffle",)),
        ("media_player", "repeat_set"): obj({"repeat": {"type": "string", "enum": ["off", "all", "one"]}},
                                            ("repeat",)),
        ("media_player", "play_media"): obj({"media_content_id": {"type": "string", "maxLength": 200},
                                             "media_content_type": {"type": "string", "enum": MEDIA_TYPES},
                                             "enqueue": {"type": "string", "enum": ["play", "next", "add", "replace"]},
                                             "announce": BOOL}, ("media_content_id",)),
        ("media_player", "join"): obj({"group_members": {"type": "array", "items": players, "minItems": 1,
                                                         "maxItems": 10}}, ("group_members",)),
        ("tts", "speak"): obj({"media_player_entity_id": players, "message": {"type": "string", "maxLength": 300},
                               "language": STR}, ("media_player_entity_id", "message")),
        ("assist_satellite", "announce"): obj({"message": {"type": "string", "maxLength": 300}}, ("message",)),
        ("vacuum", "set_fan_speed"): obj({"fan_speed": e["fan_speed"]}, ("fan_speed",)),
        ("todo", "add_item"): obj({"item": {"type": "string", "maxLength": 120}, "due_date": STR,
                                   "description": STR}, ("item",)),
        ("todo", "update_item"): obj({"item": {"type": "string", "maxLength": 120},
                                      "status": {"type": "string", "enum": ["needs_action", "completed"]},
                                      "rename": STR}, ("item",)),
        ("calendar", "create_event"): obj({"summary": {"type": "string", "maxLength": 120},
                                           "start_date_time": STR, "end_date_time": STR, "start_date": STR,
                                           "end_date": STR, "description": STR, "location": STR}, ("summary",)),
        ("input_number", "set_value"): obj({"value": {"type": "number"}}, ("value",)),
        ("input_select", "select_option"): obj({"option": e["option"]}, ("option",)),
        ("input_text", "set_value"): obj({"value": {"type": "string", "maxLength": 255}}, ("value",)),
        ("input_datetime", "set_datetime"): obj({"datetime": STR, "date": STR, "time": STR}),
        ("timer", "start"): obj({"duration": STR}),
        ("timer", "change"): obj({"duration": STR}, ("duration",)),
        ("select", "select_option"): obj({"option": e["option"]}, ("option",)),
        ("number", "set_value"): obj({"value": {"type": "number"}}, ("value",)),
        ("text", "set_value"): obj({"value": {"type": "string", "maxLength": 255}}, ("value",)),
        ("siren", "turn_on"): obj({"tone": e["tone"], "duration": _int(1, 600), "volume_level": _num(0, 1)}),
        # sortie choisie : une caméra affichée sur un écran nommé (action Nokker, docs/HOME-ASSISTANT.md §12)
        ("camera", "show"): obj({"media_player_entity_id": players,
                                 "mode": {"type": "string", "enum": ["image", "flux"]}}, ("media_player_entity_id",)),
    }
    for svc in ("lock", "unlock", "open"):
        s[("lock", svc)] = obj({"code": STR})
    for svc in ("alarm_arm_home", "alarm_arm_away", "alarm_arm_night", "alarm_arm_vacation",
                "alarm_arm_custom_bypass", "alarm_disarm", "alarm_trigger"):
        s[("alarm_control_panel", svc)] = obj({"code": STR})
    return s


EMPTY: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": {}}


def enums_from(states: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for st in states:
        attrs = st.get("attributes") or {}
        for attr, key in ENUM_ATTRS.items():
            vals = attrs.get(attr)
            if isinstance(vals, list):
                out.setdefault(key, set()).update(str(v) for v in vals[:60] if isinstance(v, (str, int, float)))
    return {k: sorted(v) for k, v in out.items()}


__all__ = ["EMPTY", "ENUM_ATTRS", "data_schemas", "enums_from"]
