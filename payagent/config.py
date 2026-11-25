"""Configuration de l'agent de paiement : variables d'environnement seulement, jamais de secret dans un fichier."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

STRIPE_VERSION = "2026-09-30.preview"


@dataclass(frozen=True)
class AgentConfig:
    stripe_key: str = ""            # clé RESTREINTE rk_… (docs/PAY-AGENT.md §5) ; jamais une sk_
    stripe_base: str = "https://api.stripe.com"
    webhook_secret: str = ""        # whsec_… du point de terminaison webhook de l'agent
    kernel_token: str = ""          # jeton porteur des appels du noyau vers l'agent
    callback_url: str = ""          # route du noyau qui reçoit les changements d'état (POST, signé)
    callback_secret: str = ""       # HMAC-SHA256 des rappels vers le noyau
    webauthn_origin: str = ""       # origine WebAuthn épinglée (https://app.nokker.ai) : le noyau ne la choisit pas
    return_url: str = ""            # return_url des SPT (retour après 3-D Secure), page du web Nokker
    dsn: str = ""                   # rôle nokker_pay_agent (tables pay_agent_*, 4 colonnes de devices) ; vide = mémoire
    devices_dsn: str = ""           # facultatif : autre DSN pour lire `devices` (défaut : `dsn`)
    max_amount_minor: int = 50_000  # plafond propre à l'agent, par jeton, quel que soit le mandat
    currencies: tuple[str, ...] = ("CHF", "EUR")
    max_ttl_s: int = 3600           # durée de vie maximale d'un SPT, bornée en plus par la fin du mandat
    sellers: tuple[str, ...] = field(default_factory=tuple)  # profils Stripe de vendeurs acceptés ; vide = aucun
    host: str = "0.0.0.0"
    port: int = 8790
    # ── émetteur (docs/PAY-AGENT.md §10) : `wise` (carte virtuelle au nom de la personne) ou `stripe_spt` ──
    issuer: str = "wise"
    wise_client_id: str = ""        # identifiants client credentials du partenaire Wise Platform (mTLS au déploiement)
    wise_client_secret: str = ""
    wise_base: str = "https://api.wise.com"
    vault: str = "evervault"        # coffre PCI des données de carte (payagent/vault.py)
    evervault_app_id: str = ""
    evervault_api_key: str = ""
    evervault_function: str = ""    # Function qui capture la carte chez l'émetteur (§10.4)
    psp_hosts: tuple[str, ...] = field(default_factory=tuple)  # hôtes de prestataires de paiement où le coffre substitue
    deny_hosts: tuple[str, ...] = field(default_factory=tuple)  # marchands refusés (suffixes d'hôte)
    checkout_ttl_s: int = 900       # durée de vie des jetons de navigation du coffre pour un checkout
    # ── own_card (Lokkal, docs/PAY-AGENT.md §11) : carte dans l'OpenBao local, montage À PART de celui du noyau ──
    card_mount: str = "payagent"    # montage KV v2 de l'agent seul (la policy du noyau ne le couvre pas)
    checkout_cdp_url: str = ""      # navigateur de checkout dédié (Chromium du compose, CDP) ; vide = pas de checkout
    test_http_hosts: tuple[str, ...] = field(default_factory=tuple)  # marchand de TEST en http (jamais en production)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AgentConfig:
        e = os.environ if env is None else env

        def lst(name: str, default: str = "") -> tuple[str, ...]:
            return tuple(x.strip() for x in e.get(name, default).split(",") if x.strip())

        return cls(stripe_key=e.get("PAYAGENT_STRIPE_KEY", ""),
                   stripe_base=e.get("PAYAGENT_STRIPE_BASE", "https://api.stripe.com"),
                   webhook_secret=e.get("PAYAGENT_WEBHOOK_SECRET", ""),
                   kernel_token=e.get("PAYAGENT_KERNEL_TOKEN", ""),
                   callback_url=e.get("PAYAGENT_CALLBACK_URL", ""),
                   callback_secret=e.get("PAYAGENT_CALLBACK_SECRET", ""),
                   webauthn_origin=e.get("PAYAGENT_WEBAUTHN_ORIGIN", ""),
                   return_url=e.get("PAYAGENT_RETURN_URL", ""),
                   dsn=e.get("PAYAGENT_DSN", ""), devices_dsn=e.get("PAYAGENT_DEVICES_DSN", "") or e.get("PAYAGENT_DSN", ""),
                   max_amount_minor=int(e.get("PAYAGENT_MAX_AMOUNT_MINOR", "50000")),
                   currencies=lst("PAYAGENT_CURRENCIES", "CHF,EUR"),
                   max_ttl_s=int(e.get("PAYAGENT_MAX_TTL_S", "3600")),
                   sellers=lst("PAYAGENT_SELLERS"),
                   host=e.get("PAYAGENT_HOST", "0.0.0.0"), port=int(e.get("PAYAGENT_PORT", "8790")),
                   issuer=e.get("PAYAGENT_ISSUER", "wise"),
                   wise_client_id=e.get("PAYAGENT_WISE_CLIENT_ID", ""),
                   wise_client_secret=e.get("PAYAGENT_WISE_CLIENT_SECRET", ""),
                   wise_base=e.get("PAYAGENT_WISE_BASE", "https://api.wise.com"),
                   vault=e.get("PAYAGENT_VAULT", "evervault"),
                   evervault_app_id=e.get("PAYAGENT_EVERVAULT_APP_ID", ""),
                   evervault_api_key=e.get("PAYAGENT_EVERVAULT_API_KEY", ""),
                   evervault_function=e.get("PAYAGENT_EVERVAULT_FUNCTION", ""),
                   psp_hosts=tuple(h.lower() for h in lst("PAYAGENT_PSP_HOSTS")),
                   deny_hosts=tuple(h.lower().strip(".") for h in lst("PAYAGENT_DENY_HOSTS")),
                   checkout_ttl_s=int(e.get("PAYAGENT_CHECKOUT_TTL_S", "900")),
                   card_mount=e.get("PAYAGENT_CARD_MOUNT", "payagent"),
                   checkout_cdp_url=e.get("PAYAGENT_CHECKOUT_CDP_URL", ""),
                   test_http_hosts=tuple(h.lower() for h in lst("PAYAGENT_TEST_HTTP_HOSTS")))

    def problems(self) -> list[str]:
        """Ce qui empêche de démarrer (le service refuse de tourner à moitié configuré)."""
        out = []
        secrets_ = [("PAYAGENT_KERNEL_TOKEN", self.kernel_token), ("PAYAGENT_CALLBACK_SECRET", self.callback_secret)]
        if self.issuer == "stripe_spt":
            if not self.stripe_key.startswith("rk_"):
                out.append("PAYAGENT_STRIPE_KEY : clé restreinte rk_… attendue")
            secrets_.append(("PAYAGENT_WEBHOOK_SECRET", self.webhook_secret))
        elif self.issuer == "wise":
            if not self.wise_client_id or len(self.wise_client_secret) < 16:
                out.append("PAYAGENT_WISE_CLIENT_ID / PAYAGENT_WISE_CLIENT_SECRET manquants")
            if not self.wise_base.startswith("https://"):
                out.append("PAYAGENT_WISE_BASE : https:// attendu")
            if self.vault != "evervault":
                out.append("PAYAGENT_VAULT : evervault attendu")
            elif not (self.evervault_app_id and self.evervault_api_key and self.evervault_function):
                out.append("PAYAGENT_EVERVAULT_APP_ID / _API_KEY / _FUNCTION manquants")
        elif self.issuer == "own_card":
            # coffre : NOKKER_OPENBAO_ADDR + fichiers AppRole de l'agent (lus par nokker.secrets.openbao)
            if not os.environ.get("NOKKER_OPENBAO_ADDR"):
                out.append("NOKKER_OPENBAO_ADDR manquant (OpenBao local, profil vault)")
            if self.checkout_cdp_url and not self.checkout_cdp_url.startswith(("http://", "ws://")):
                out.append("PAYAGENT_CHECKOUT_CDP_URL : http:// ou ws:// attendu (réseau du compose)")
        else:
            out.append("PAYAGENT_ISSUER : wise, stripe_spt ou own_card attendu")
        for name, val in secrets_:
            if len(val) < 16:
                out.append(f"{name} manquant ou trop court")
        local = self.issuer == "own_card" and re.match(r"^http://(localhost|127\.0\.0\.1)(:\d{1,5})?$",
                                                         self.webauthn_origin or "")
        if not (self.webauthn_origin.startswith("https://") or local):
            # own_card (Lokkal) : passkey locale, contexte sûr http://localhost:<port> (rpId localhost)
            out.append("PAYAGENT_WEBAUTHN_ORIGIN : origine https:// attendue (http://localhost:<port> pour own_card)")
        if not self.dsn:
            out.append("PAYAGENT_DSN manquant (rôle nokker_pay_agent)")
        return out
