"""Textes vus du modèle qui ne nomment plus « le noyau » : un modèle qui lit « le noyau a fait X » le répète à la
personne. Chaque texte est figé ici ; les lecteurs qui les reconnaissent sont éprouvés avec la même constante."""
from __future__ import annotations

from nokker.core.turn import Effect, ToolResult
from nokker.qa.secrets import KnownSecrets
from nokker.threads import export
from tests.unit.qa.test_invariants import inv, spec


def test_secret_result_notice_is_frozen_without_the_kernel():
    known = KnownSecrets()
    known.add("VAULT_GRAFANA", "Xk9!qpzT42Lm")
    out = inv(known=known).redact_result(
        ToolResult(call_id="c1", name="grafana_get_credentials", content="mot de passe Xk9!qpzT42Lm"),
        spec("grafana_get_credentials", Effect.SECRET))
    assert ("Résultat de l'outil secret « grafana_get_credentials » retiré (contenu secret) : la valeur reste dans le "
            "coffre et ne passe jamais par le modèle.") in out.content
    assert "noyau" not in out.content.lower()
    # le lecteur d'export reconnaît toujours l'avis par sa marque
    assert export._KERNEL_SEAL_MARK in out.content


def test_system_notes_carry_the_system_tag_not_the_kernel():
    from nokker.browserlive.live import HANDOFF_NOTE
    from nokker.core.loop import BUDGET_NOTE, RESUME_NOTE, STALL_NOTE
    for note in (BUDGET_NOTE, RESUME_NOTE, STALL_NOTE, HANDOFF_NOTE):
        assert note.startswith("[système] ") and "noyau" not in note.lower()
    assert RESUME_NOTE == ("[système] Le tour reprend après une pause demandée par l'utilisateur : des fichiers ont pu "
                           "changer entre-temps.")
    assert BUDGET_NOTE.startswith("[système] Il te reste {left} appels sur {total} pour ce tour. Termine maintenant")
    assert STALL_NOTE.startswith("[système] Tu viens de relancer `{name}` avec les mêmes arguments")
    assert HANDOFF_NOTE.startswith("[système] Passation à l'humain ({reason}) : la vue en direct")


def test_execute_code_texts_do_not_name_the_kernel():
    from nokker.tools.execute_code import ExecuteCodeConfig, _spec
    from nokker.tools.result_fence import code_kernel_note
    desc = _spec(ExecuteCodeConfig()).parameters["properties"]["code"]["description"]
    assert "ToolRefused (appel refusé) ou ApprovalPending (carte posée" in desc and "noyau" not in desc.lower()
    # la clôture reconnaît la nouvelle formule, et l'ancienne d'un historique déjà écrit
    new = "execute_code n'est appelable que pendant que tu réponds à la personne"
    assert code_kernel_note(new, False) == (new + ".", "")
    assert code_kernel_note("execute_code n'est appelable que dans un tour du noyau", False) == (new + ".", "")


def test_child_gone_reason_is_frozen_without_the_kernel():
    from nokker.approvals.executor import CHILD_GONE
    assert CHILD_GONE == ("sous-agent introuvable (terminé, annulé ou redémarré) : carte refusée, jamais exécutée avec "
                          "les outils du parent")


def test_delegate_voice_description_is_frozen_without_the_kernel():
    from nokker.tools.delegate import ROUTER_VOICE_DESCRIPTION
    assert "contexte utile ; elle choisit qui travaille et comment. Avant l'appel" in ROUTER_VOICE_DESCRIPTION
    assert "noyau" not in ROUTER_VOICE_DESCRIPTION.lower()


def test_bubble_and_guide_texts_do_not_name_the_kernel():
    import inspect

    from nokker.bubbles import tools as bt
    from nokker.guide import catalog
    src = inspect.getsource(bt)
    assert "La carte se remplit d'elle-même : n'ajoute ni bubble_update ni navigation derrière." in src
    assert "bulle affichée chez l'utilisateur ; l'aperçu se remplit tout seul. Rien d'autre à faire" in src
    cat = inspect.getsource(catalog)
    assert "ouvre la bulle, la carte se remplit seule ;" in cat
    assert "La carte se pose quand l'outil l'exige ; l'outil répond EN ATTENTE" in cat


def test_language_line_names_context_blocks_not_the_kernel():
    from nokker.context.reply_language import LANGUAGE_LINE
    assert "même si ce préfixe, les blocs de contexte, les résultats d'outils" in LANGUAGE_LINE
    assert "noyau" not in LANGUAGE_LINE.lower()


def test_connected_site_link_message_is_frozen():
    from nokker.sites.tools import CONNECT_MESSAGES
    assert CONNECT_MESSAGES["needs_link"] == (
        "« {label} » a envoyé un lien de connexion par e-mail ; le lien est récupéré dans la boîte reliée, sinon une "
        "demande est partie {where} : {link}.")


def test_household_proposal_descriptions_are_frozen():
    from nokker.household.famcal import tools as fc
    from nokker.household.projects import tools as pj
    texts = {name: desc for name, desc, *_ in fc._SPECS + pj._SPECS}
    assert "(l'échéance d'une facture QR : paper_bill_due). Chaque ligne est relue (dates ambiguës" \
        in texts["family_calendar_propose"]
    assert texts["family_project_propose"].endswith("Chaque ligne est relue ; rien n'est écrit.")
    assert not [n for n, d in texts.items() if "noyau" in d.lower()]


def test_mcp_texts_do_not_name_the_kernel():
    from nokker.tools import mcp_next
    assert mcp_next.TASK_INPUT == ("La tâche longue du serveur MCP attend une saisie qui ne peut pas être fournie : "
                                   "tâche annulée.")
    import inspect

    from nokker.tools import mcp_config
    src = inspect.getsource(mcp_config)
    assert src.count("actif au prochain redémarrage de Nokker.") == 2
    assert src.count('"active": "au prochain redémarrage de Nokker"') == 5
    assert "redémarrage du noyau" not in src


def test_data_url_error_is_frozen():
    import pytest

    from nokker.images.validate import ImageError, _from_data_url
    with pytest.raises(ImageError) as e:
        _from_data_url("https://x/y.png", "image")
    assert str(e.value) == "image : seules les data URL sont acceptées (aucune image n'est téléchargée)"


def test_script_side_texts_are_frozen():
    from nokker.tools.execute_code import BOOTSTRAP
    assert 'raise ToolError("canal fermé")' in BOOTSTRAP and "noyau" not in BOOTSTRAP.lower()


def test_subagents_unavailable_text_is_frozen():
    import inspect

    from nokker.code_sessions import subagents
    assert 'err("sous-agents indisponibles ici")' in inspect.getsource(subagents)


# Balayage : tout littéral de nokker/ qui contient « noyau » est ici, avec la raison pour laquelle le modèle ne le lit pas
# (ou ne le répète pas). Un nouveau texte vu du modèle ne se glisse donc pas : il échoue ici, et on l'écrit sans « noyau ».
_ALLOWED_FILES = {
    # journaux (logger / événements), jamais rendus au modèle
    "agents/engine.py": "motifs d'exécution d'agent : montrés à la personne (journal d'exécution)",
    "approvals/executor.py": "messages de journal (logger.exception)",
    "context/engine.py": "message de journal",
    "context/project_memory.py": "message de journal",
    "context/corthexis_tenancy.py": "erreur d'administration (migration), pas un résultat d'outil",
    "tools/delegate.py": "message de journal (logger.debug)",
    "improve/overlay.py": "message de journal",
    "homewatch/source.py": "valeur d'acteur du journal d'audit",
    # configuration et démarrage : lus par l'opérateur
    "api/server.py": "erreurs de démarrage pour l'opérateur",
    "code_sessions/config.py": "erreurs de configuration lues par l'opérateur",
    "home/source.py": "erreur de configuration lue par l'opérateur",
    "cli.py": "aide de la ligne de commande",
    "improve/cli.py": "sortie de la ligne de commande",
    "review/cli.py": "aide de la ligne de commande",
    "secrets/cli.py": "sortie de la ligne de commande",
    "secrets/openbao.py": "message d'exploitation",
    "mcp_server.py": "aide de la ligne de commande",
    "desktop/node.py": "aide de la ligne de commande du poste",
    "folder/run.py": "erreur de la ligne de commande (mode dossier)",
    # réponses d'API lues par le client / la personne, pas par le modèle
    "bubbles/hub.py": "erreur d'API vue du client",
    "code_sessions/forge.py": "erreur d'API de la forge (code git_missing)",
    "code_sessions/forge_triggers.py": "réponse HTTP d'un déclencheur de forge",
    "local/api.py": "réponse de l'API locale, vue de la personne",
    "importers/apply.py": "rapport d'import montré à la personne",
    "importers/base.py": "rapport d'import montré à la personne",
    "household/paper/service.py": "note de la liste préparée pour la personne",
}
# Seul littéral de result_fence.py qui nomme « noyau » : l'ANCIENNE formule d'un résultat déjà en historique.
_LEGACY = {("tools/result_fence.py", "execute_code n'est appelable que dans un tour du noyau")}


def test_no_literal_names_the_kernel_outside_the_justified_list():
    import ast
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "nokker"
    bad = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs):
                continue
            if re.search("noyau", n.value, re.IGNORECASE) and rel not in _ALLOWED_FILES and (rel, n.value) not in _LEGACY:
                bad.append(f"{rel}:{n.lineno}: {n.value[:80]!r}")
    assert not bad, "« noyau » dans un littéral qui peut être vu du modèle (ou ajouter à la liste justifiée) :\n" \
        + "\n".join(bad)
