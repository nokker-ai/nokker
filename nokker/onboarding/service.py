"""Parcours d'acceptation d'une invitation, en UNE requête côté plateforme (docs/nokker onboarding) :

    conditions et clé vérifiées → jeton consommé sur front1 (redeem) → compte → acceptation enregistrée →
    appareil enrôlé et confirmé par l'invitation → identité « device » liée.

Rien n'est créé avant d'avoir la preuve d'acceptation : « pas d'acceptation, pas de compte ». Si une étape locale
échoue après `redeem`, le jeton est rendu (`release`) pour que le testeur réessaie avec le même lien ; un compte créé à
moitié (sans appareil) est repris au passage suivant. Si le processus meurt entre `redeem` et `release`, le jeton reste
brûlé : l'opérateur renvoie une invitation, qui remplace la précédente.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nokker.approvals.base import BaseApprovals
from nokker.approvals.crypto import load_p256_public_key_spki
from nokker.onboarding import terms
from nokker.tenancy.context import tenant_scope
from nokker.onboarding.acceptances import Acceptances
from nokker.onboarding.consents import Consents
from nokker.onboarding.front import Front, FrontError
from nokker.tenancy.store import IdentityTaken, Tenancy

logger = logging.getLogger("nokker.onboarding")


class OnboardingError(Exception):
    """`code` : invalid_token, terms_mismatch, invalid_key, account_exists, front_unavailable, internal."""

    STATUS = {"invalid_token": 410, "terms_mismatch": 409, "invalid_key": 400, "account_exists": 409,
              "front_unavailable": 503, "internal": 500}

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def status(self) -> int:
        return self.STATUS.get(self.code, 400)


@dataclass
class Onboarding:
    front: Front
    tenancy: Tenancy
    approvals: BaseApprovals
    acceptances: Acceptances
    consents: Consents | None = None

    def invitation(self, token: str) -> dict[str, Any]:
        """Lecture seule pour l'écran d'accueil : `{valid, lang, prenom}` — ne consomme rien."""
        try:
            return self.front.check(token)
        except FrontError as e:
            raise OnboardingError("front_unavailable" if e.code in {"unavailable", "unauthorized"} else e.code) from e

    def accept(self, token: str, terms_sha256: str, public_key_spki: str, device_name: str,
               ip: str | None, training_consent: bool = False) -> dict[str, str]:
        lang = terms.lang_of_version(terms_sha256)
        if lang is None:
            raise OnboardingError("terms_mismatch")  # texte périmé : on ne consomme pas le jeton
        try:
            load_p256_public_key_spki(public_key_spki)
        except Exception:  # noqa: BLE001 — toute clé illisible est refusée avant de consommer le jeton
            raise OnboardingError("invalid_key") from None
        try:
            inv = self.front.redeem(token)
        except FrontError as e:
            if e.code == "invalid_token":
                raise OnboardingError("invalid_token") from None
            logger.error("redeem en échec : %s", e.code)
            raise OnboardingError("front_unavailable") from None
        try:
            return self._create(inv, lang, terms_sha256, public_key_spki, device_name, ip, training_consent)
        except OnboardingError:
            self._release(inv["invitation_id"])
            raise
        except Exception:
            logger.exception("création du compte en échec (invitation %s)", inv["invitation_id"])
            self._release(inv["invitation_id"])
            raise OnboardingError("internal") from None

    def _release(self, invitation_id: int) -> None:
        try:
            self.front.release(invitation_id)
        except FrontError as e:
            logger.error("release de l'invitation %s impossible (%s) : l'opérateur doit renvoyer une invitation",
                         invitation_id, e.code)

    def _create(self, inv: dict[str, Any], lang: str, terms_sha256: str, spki: str, device_name: str,
                ip: str | None, training_consent: bool = False) -> dict[str, str]:
        invitation_id = str(inv["invitation_id"])
        try:
            account = self.tenancy.create_account(inv["email"], inv["lang"] or lang, invitation_id)
        except IdentityTaken:
            account = self.tenancy.account_by_email(inv["email"])
            if account is None or self.approvals.list_devices(account.account_id):
                raise OnboardingError("account_exists") from None
            # compte resté sans appareil après un échec précédent : on le reprend
        # La route est publique (pas encore de session) : le locataire est celui du compte qu'on vient de créer, fixé ici
        # pour l'écriture sous RLS (terms_acceptances) et la visibilité des appareils (docs/TENANCY.md §2).
        with tenant_scope(account.tenant_id):
            self.acceptances.record(account.account_id, invitation_id, terms_sha256, lang, ip)
            if self.consents is not None:  # choix explicite, jamais pré-coché : on journalise aussi le refus
                self.consents.record(account.account_id, "training", training_consent, "signup", terms_sha256, ip)
            device = self.approvals.enroll_device(account.account_id, (device_name or "Mon appareil")[:80], spki)
            try:
                self.approvals.confirm_device_by_invitation(device.id, invitation_id)
                self.tenancy.bind_identity(account.account_id, "device", device.id)
            except Exception:
                self.approvals.delete_device(device.id)
                raise
        return {"account_id": account.account_id, "device_id": device.id, "lang": account.lang}
