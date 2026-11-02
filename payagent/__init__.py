"""Agent de paiement Nokker (docs/PAY-AGENT.md) — service SÉPARÉ du noyau et de Lokkal : son processus, son conteneur,
sa clé Stripe restreinte. Seul composant qui crée, révoque et suit des Shared Payment Tokens (Stripe, API preview
2026-09-30.preview) avec le moyen de paiement enregistré d'une personne.

Il ne fait PAS confiance au noyau : chaque demande porte l'enveloppe d'un mandat signé par la passkey de la personne
(nokker/payments/mandate.py), que l'agent revérifie lui-même (signature, origine épinglée, clé de l'appareil lue en base
avec son propre rôle en lecture seule, bloc `agent_pay`, plafonds propres, un seul jeton par mandat).

Seuls modules du noyau importés : `nokker.payments.mandate` et `nokker.approvals.crypto`, tous deux purs (aucun accès
réseau ni base) : la vérification d'un mandat est la même des deux côtés.
"""
