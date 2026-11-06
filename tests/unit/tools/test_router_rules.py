"""`router_rules` : règles déterministes du routeur (lecture seule = aucune action demandée ; plancher deep = marqueur de
jugement). Phrases écrites à la main, hors des bancs (aucune tâche du banc d'orchestration ici) : PHRASES_1 a servi à
régler les listes, PHRASES_2 a été écrite AVANT le premier passage (1 faute sur 30, corrigée : « trag … ein »)."""
from __future__ import annotations

import pytest

from nokker.tools.router_rules import actions, deep_markers

# (texte, action demandée, jugement demandé)
PHRASES_1 = [
("Réponds à tous les mails de la semaine qui demandent une confirmation.",1,0),
("Déplace tous mes rendez-vous de jeudi à vendredi.",1,0),
("Écris à chacun des participants de la réunion de lundi pour l'annuler.",1,0),
("Peux-tu supprimer les newsletters de plus d'un mois ?",1,0),
("Archive les factures payées dans le dossier Compta, merci.",1,0),
("Tu peux m'inscrire au cours de yoga de mardi ?",1,0),
("Ajoute le dentiste le 12 à 14h dans mon agenda.",1,0),
("Mets les mails de la régie dans le dossier Appartement.",1,0),
("Annule mon rendez-vous chez le garagiste.",1,0),
("Please move the invoices from last month to the Accounting folder.",1,0),
("Can you reply to Marc and tell him I'm in?",1,0),
("Cancel tomorrow's meeting with the plumber.",1,0),
("Verschiebe bitte den Termin mit dem Elektriker auf Freitag.",1,0),
("Kannst du die Werbemails löschen?",1,0),
("Sag das Treffen am Montag ab.",1,0),
("Quels mails de la banque n'ai-je pas encore lus ?",0,0),
("Résume-moi les trois derniers mails de l'école.",0,0),
("Trouve trois créneaux libres communs pour une réunion d'une heure la semaine prochaine.",0,0),
("Relève dans mes mails du mois les rendez-vous proposés par les artisans, avec dates et prix.",0,0),
("Cherche les conditions de résiliation publiées par mon opérateur mobile.",0,0),
("Compare trois robots aspirateurs : prix, autonomie, avis.",0,0),
("Ne déplace rien, dis-moi juste quels mails sont des promotions.",0,0),
("Sans rien envoyer, prépare-moi le texte d'une réponse à la régie.",0,0),
("Don't send anything yet, just list who I owe an answer to.",0,0),
("Which of my unread emails are from the school?",0,0),
("Welche Mails von der Hausverwaltung sind noch ungelesen?",0,0),
("Qu'est-ce que ma commande contient d'après le mail de confirmation ?",0,0),
("La classe de Léo part en course d'école quand ?",0,0),
("Entre les deux crèches, laquelle nous conviendrait le mieux vu nos horaires ?",0,1),
("Pourquoi mon chauffage consomme autant depuis octobre ? J'ai les relevés par mail.",0,1),
("Relis le contrat de bail avant que je le signe et dis-moi ce qui pose problème.",0,1),
("Propose-moi une organisation pour le déménagement sur six semaines.",0,1),
("Est-ce que je devrais prendre l'abonnement annuel ou mensuel ? Conseille-moi.",0,1),
("What are the risks of moving our mail server over the weekend?",0,1),
("Should I refinance the mortgage now or wait? Here are the two offers.",0,1),
("Warum ist meine Stromrechnung so hoch? Die Ablesungen sind in den Mails.",0,1),
("Welche Versicherung passt besser zu uns, Helvia oder Securalp?",0,1),
("Fais le point sur ce qui manque dans le dossier de candidature de Léa.",0,1),
("Range les promos et dis-moi ensuite pourquoi j'en reçois autant.",1,1),
("Analyse les causes de la panne et corrige la configuration.",1,1),
]
PHRASES_2 = [
("Vide la corbeille de ma boîte mail.",1,0),
("Classe les relevés bancaires par mois dans le dossier Banque.",1,0),
("Peux-tu répondre à Sophie que jeudi me va ?",1,0),
("Il faut décaler la réunion de parents à 18h.",1,0),
("Transfère le mail du notaire à Alice.",1,0),
("Désinscris-moi de la newsletter de Promo Boutik.",1,0),
("Marque tous les mails de la crèche comme lus.",1,0),
("Book a table for four on Saturday at Chez Marius.",1,0),
("Forward the plumber's quote to my wife.",1,0),
("Lösche alle Mails vom Spamordner.",1,0),
("Trag den Zahnarzttermin am 3. Dezember ein.",1,0),
("Reserve-moi deux places pour le concert de samedi.",1,0),
("Combien coûte l'abonnement demi-tarif d'après la page des CFF ?",0,0),
("Liste les rendez-vous de la semaine prochaine.",0,0),
("Donne-moi le numéro de suivi du colis Zalando.",0,0),
("Qui m'a écrit au sujet de la fuite d'eau ?",0,0),
("Relève les horaires de la déchetterie et les jours de ramassage du papier.",0,0),
("What time does the pharmacy close on Sundays?",0,0),
("List the invoices I received in October with their amounts.",0,0),
("Wann ist der nächste Elternabend laut der Mail der Schule?",0,0),
("Le mail de la commune parle de quoi exactement ?",0,0),
("Fais-moi un tableau des prix des trois garages pour le service des 60 000 km.",0,0),
("On hésite entre louer et acheter une voiture électrique, qu'est-ce qui est le plus raisonnable pour nous ?",0,1),
("Regarde le contrat de travail et dis-moi les points faibles avant que je réponde.",0,1),
("Comment organiser les vacances d'été avec la garde alternée ? Propose une architecture de planning.",0,1),
("Pourquoi la facture d'eau a augmenté au dernier trimestre ?",0,1),
("Which mortgage offer is better for us given we might move in five years?",0,1),
("Can you diagnose why the backup job keeps failing every Monday?",0,1),
("Was empfiehlst du: Glasfaser von Swisscom oder Salt, bei unserem Verbrauch?",0,1),
("Évalue les deux devis de toiture et recommande-m'en un.",0,1),
]


@pytest.mark.parametrize("text,action,deep", PHRASES_1 + PHRASES_2)
def test_action_and_judgment(text, action, deep):
    assert bool(actions(text)) is bool(action), actions(text)
    assert bool(deep_markers(text)) is bool(deep), deep_markers(text)


@pytest.mark.parametrize("text", ["Ne range rien pour l'instant.", "Sans rien déplacer, liste les promotions.",
                                  "Don't delete anything, just count them.", "Bitte nichts löschen, nur zählen."])
def test_negation_cancels_the_action(text):
    assert actions(text) == []


@pytest.mark.parametrize("text", ["Où en est ma commande ?", "La classe de Léo part quand ?", "La marque du four ?",
                                  "Le programme de samedi ?", "Avant de signer, quels sont les délais ?"])
def test_nouns_and_the_persons_own_actions_are_not_requests(text):
    assert actions(text) == []


def test_several_texts_and_accents():
    assert actions("lister les promotions", "Range-les dans Promotions") == ["range"]
    assert deep_markers("Pourquoi ?", "POURQUOI") == ["pourquoi"]
    assert actions("") == [] and deep_markers("") == []
