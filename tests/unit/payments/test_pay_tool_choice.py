"""Drapeau `pay_tool_choice` : description de `pay_request` choisie par le drapeau, mot pour mot ; éteint = inchangé."""
from __future__ import annotations

from nokker.config import NokkerConfig
from nokker.payments.service import Payments, Settings, SoloPeople
from nokker.payments.store import MemoryPaymentStore
from nokker.payments.tools import PAY_DESCRIPTION, PAY_DESCRIPTION_TOOL_CHOICE, PayTools

ACTUEL = ("Arrêt au paiement : prépare une carte « à payer » (marchand, montant, devise, articles, lien) ; "
          "la personne approuve avec sa passkey et paie elle-même. Ne saisis jamais de numéro de carte.")
CHOIX = ("Arrêt au paiement : dès qu'une demande mène à payer (commande, achat, facture, abonnement), appelle cet "
         "outil au lieu de répondre en texte ou de demander une confirmation : la carte EST la confirmation. Prépare "
         "une carte « à payer » (marchand, montant, devise, articles, lien) ; la personne approuve avec sa passkey et "
         "paie elle-même. Ne saisis jamais de numéro de carte, et ne confie jamais un paiement à un sous-agent.")


def _svc() -> Payments:
    return Payments(store=MemoryPaymentStore(), settings=Settings.from_config(None), people=SoloPeople())


def test_textes_mot_pour_mot():
    assert PAY_DESCRIPTION == ACTUEL
    assert PAY_DESCRIPTION_TOOL_CHOICE == CHOIX


def test_drapeau_eteint_par_defaut_texte_inchange():
    assert NokkerConfig.__dataclass_fields__["pay_tool_choice"].default is False
    assert PayTools(payments=_svc()).specs()[0].description == ACTUEL
    assert PayTools(payments=_svc(), tool_choice=False).specs()[0].description == ACTUEL


def test_drapeau_allume():
    spec = PayTools(payments=_svc(), tool_choice=True).specs()[0]
    assert spec.description == CHOIX and spec.name == "pay_request"
    assert spec.parameters == PayTools(payments=_svc()).specs()[0].parameters  # schéma inchangé


def test_agent_de_paiement_garde_sa_description():
    from nokker.payments.agent import PAY_DESCRIPTION_AGENT

    svc = _svc()
    svc.agent = object()
    assert PayTools(payments=svc, tool_choice=True).specs()[0].description == PAY_DESCRIPTION_AGENT
