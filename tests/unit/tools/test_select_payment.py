"""Famille « Paiement » de tools/select.py : `pay_request` chargé par les mots du paiement et de la commande (FR, DE, IT,
EN), inerte sans l'outil au registre (drapeau `payments` éteint), sans effet sur les autres familles."""
from __future__ import annotations

import pytest

from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect
from nokker.tools.select import FAMILIES, compact_index, relevant_tools


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="", parameters={"type": "object", "properties": {}}, effect=Effect.READ,
                    source="t")


BROWSER = [_spec(n) for n in ("browser_navigate", "browser_snapshot", "browser_click")]
PAY = _spec("pay_request")
OTHERS = [_spec("paper_import"), _spec("delegate"), _spec("checkpoint_list"), _spec("web_search")]


def _names(text: str, specs: list[ToolSpec]) -> set[str]:
    return {s.name for s in relevant_tools(text, set(), specs)}


def test_commande_charge_le_navigateur_et_pay_request():
    got = _names("Commande les deux cartouches d'encre HP 305 chez Brack, comme d'habitude.", [*BROWSER, PAY])
    assert "pay_request" in got and {"browser_navigate", "browser_click"} <= got


@pytest.mark.parametrize("text", [
    "Paie la facture du garagiste", "Il faut payer le loyer", "achète du café", "Bestell mir zwei Patronen",
    "Kauf das Ticket", "bitte bezahlen", "ordina due cartucce", "compra il biglietto", "paga la bolletta",
    "order two cartridges", "buy the ticket", "purchase a licence", "pay the invoice", "go to checkout"])
def test_mots_du_paiement_en_quatre_langues(text):
    assert "pay_request" in _names(text, [PAY])


@pytest.mark.parametrize("text", ["Résume ce compte rendu", "fais un audit de sécurité", "renomme la fonction",
                                  "lis les logs du service", "corrige le test qui échoue"])
def test_rien_sans_mot_du_paiement(text):
    assert "pay_request" not in _names(text, [PAY])


def test_famille_inerte_sans_l_outil():
    """Drapeau `payments` éteint : pas d'outil au registre, donc rien chargé et aucune ligne « Paiement » à l'index."""
    assert _names("Commande deux cartouches et paie-les", BROWSER) == {s.name for s in BROWSER}
    assert "Paiement" not in compact_index([s.name for s in BROWSER])
    assert "- Paiement : pay_request" in compact_index(["pay_request", "browser_navigate"])


def test_non_regression_des_autres_familles():
    assert [f.label for f in FAMILIES].index("Paiement") < [f.label for f in FAMILIES].index("Navigateur")
    specs = [*BROWSER, *OTHERS]
    assert _names("Commande les cartouches", specs) == {s.name for s in BROWSER}  # comme avant la famille
    assert _names("lance en parallèle", specs) == {"delegate"}
    assert _names("cherche la météo", specs) == {"web_search"}
    assert _names("trie mes factures", specs) == {"paper_import"}
    assert _names("bonjour", [*specs, PAY]) == set()
