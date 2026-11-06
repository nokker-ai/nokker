"""Ancien emplacement du client Stripe SPT, gardé pour les imports existants : le code vit dans
`payagent/issuers/stripe_spt.py` (émetteur `stripe_spt`, inchangé)."""
from payagent.issuers.stripe_spt import (Stripe, StripeError, Transport, _flatten, sign_webhook, urllib_transport,
                                         verify_webhook)

__all__ = ["Stripe", "StripeError", "Transport", "_flatten", "sign_webhook", "urllib_transport", "verify_webhook"]
