"""Outils hors famille (MCP tiers) : chargés quand la demande recoupe leur nom ou leur description."""
from nokker.core.ports import ToolSpec
from nokker.tools.select import relevant_tools


def _spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters={"type": "object", "properties": {}},
                    source="mcp:video")


VIDEO = [
    _spec("video_brief", "Lit le brief du film (script, durée, langue, format, plans, règles de qualité)."),
    _spec("video_deliver", "Livre la vidéo dans le dossier promo avec une note. Aucun envoi externe."),
    _spec("video_envoyer", "ENVOIE à l'utilisateur, dans cette conversation (Telegram compris), un ou plusieurs "
                           "films promo déjà livrés."),
    _spec("video_wait", "Attend la fin des tâches de fond et rend l'état de chacune."),
    _spec("mail_send", "Envoie un mail."),
]


def _names(text: str) -> list[str]:
    return [s.name for s in relevant_tools(text, set(), VIDEO)]


def test_demande_sans_nom_d_outil_charge_l_outil_mcp():
    assert "video_envoyer" in _names("Envoie-moi le film promo te-montre.")
    assert "video_envoyer" in _names("Tu peux m'envoyer la vidéo ?")


def test_un_seul_mot_commun_ne_charge_rien():
    assert _names("Quel film voir ce soir ?") == []
    assert _names("Bonjour, ça va ?") == []


def test_les_familles_connues_ne_passent_pas_par_le_lexique():
    # mail_send appartient à la famille Mail : seul son déclencheur le charge, pas le recoupement lexical.
    assert "mail_send" not in _names("Envoie le film promo")


def test_plafond_et_ordre_du_registre():
    many = [_spec(f"video_outil{i}", "Envoie un film promo.") for i in range(9)]
    got = [s.name for s in relevant_tools("envoie le film promo", set(), many)]
    assert got == [f"video_outil{i}" for i in range(4)]
