"""Connecteurs du testeur (T3, contrat C2 et C3) : mail IMAP/SMTP et agenda iCal/CalDAV, par mot de passe d'application.

    build_connector(kind, settings, secret, locality=) -> ToolSource   # noms/effets = nokker/tools/effects.py
    test_connection(kind, settings, secret) -> TestResult         # n'écrit rien chez le fournisseur
    ConnectorProvider(store, account_for_tenant).sources_for(tenant_id) -> list[ToolSource]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nokker.connectors.errors import ConnectorError, scrub
from nokker.core.ports import ToolSource


LOCALITIES = ("cloud", "local")  # docs/LOKKAL.md §6.7 : posé chez nokker.ai, ou dans Lokkal


def build_connector(kind: str, settings: dict[str, Any], secret: str, *, locality: str | None = None) -> ToolSource:
    """`locality` est portée par chaque outil (`ToolSpec.locality`) : c'est elle que filtrent le miroir et la passerelle."""
    if kind == "mail":
        from nokker.connectors.mail import MailConnector

        return MailConnector(settings, secret, locality=locality)
    if kind == "calendar":
        if settings.get("mode") in ("google", "graph"):  # agendas reliés par OAuth (docs/CONNECTORS-OAUTH.md)
            from nokker.connectors.cloudcal import ApiCalendar

            return ApiCalendar(settings, secret, locality=locality)
        from nokker.connectors.calendar import CalendarConnector

        return CalendarConnector(settings, secret, locality=locality)
    raise ConnectorError("bad_settings", f"nature inconnue : {kind}")


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # pas une classe de test pour pytest

    ok: bool
    message: str
    code: str | None = None


TEST_DEADLINE_S = 20.0  # bien sous les délais des mandataires (ingress 60 s, Cloudflare 100 s) : le web a son JSON


def _test_once(kind: str, settings: dict[str, Any], secret: str) -> TestResult:
    try:
        return TestResult(True, build_connector(kind, settings, secret).test())  # type: ignore[attr-defined]
    except ConnectorError as exc:
        return TestResult(False, scrub(exc.message, secret), exc.code)
    except Exception as exc:  # noqa: BLE001 — un test de connexion ne fait jamais tomber l'API
        return TestResult(False, scrub(f"{exc.__class__.__name__}", secret), "protocol_error")


def test_connection(kind: str, settings: dict[str, Any], secret: str, *, deadline_s: float = TEST_DEADLINE_S
                    ) -> TestResult:
    """Essai de connexion borné à `deadline_s` AU TOTAL (redirections, IMAP puis SMTP compris) : au-delà, réponse
    `unreachable` tout de suite ; l'essai en cours finit seul, borné par ses propres délais réseau."""
    import concurrent.futures

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="connector-test")
    try:
        return pool.submit(_test_once, kind, settings, secret).result(timeout=deadline_s)
    except concurrent.futures.TimeoutError:
        return TestResult(False, f"le serveur ne répond pas assez vite (plus de {int(deadline_s)} s)", "unreachable")
    finally:
        pool.shutdown(wait=False)


test_connection.__test__ = False  # type: ignore[attr-defined]

__all__ = ["LOCALITIES", "TEST_DEADLINE_S", "ConnectorError", "TestResult", "build_connector", "test_connection"]
