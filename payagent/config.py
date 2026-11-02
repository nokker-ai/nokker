"""Configuration de l'agent de paiement : variables d'environnement seulement, jamais de secret dans un fichier."""
from __future__ import annotations

import os
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
                   host=e.get("PAYAGENT_HOST", "0.0.0.0"), port=int(e.get("PAYAGENT_PORT", "8790")))

    def problems(self) -> list[str]:
        """Ce qui empêche de démarrer (le service refuse de tourner à moitié configuré)."""
        out = []
        if not self.stripe_key.startswith("rk_"):
            out.append("PAYAGENT_STRIPE_KEY : clé restreinte rk_… attendue")
        for name, val in (("PAYAGENT_WEBHOOK_SECRET", self.webhook_secret), ("PAYAGENT_KERNEL_TOKEN", self.kernel_token),
                          ("PAYAGENT_CALLBACK_SECRET", self.callback_secret)):
            if len(val) < 16:
                out.append(f"{name} manquant ou trop court")
        if not self.webauthn_origin.startswith("https://"):
            out.append("PAYAGENT_WEBAUTHN_ORIGIN : origine https:// attendue")
        if not self.dsn:
            out.append("PAYAGENT_DSN manquant (rôle nokker_pay_agent)")
        return out
