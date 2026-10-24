#!/usr/bin/env python3
"""Nokker /healthz aggregator — nokker (noyau), not upstream Hermes.

Migrated unchanged from hermes-nokker (P0 "Fork" livrable 2) in F0: this stays a
product-side, stdlib-only aggregator, now started directly by docker/entrypoint.sh instead
of an s6 service (the new image installs hermes-agent as a pip dependency, not from source,
so it has no s6-overlay runtime of its own).

P0 "Fork" brief, livrable 2: a single container (gateway + API server +
dashboard, one image) needs one /healthz that is 200 only when BOTH the
gateway/API-server process (api_server.py's unauthenticated GET /health)
and the dashboard (hermes_cli/web_routers/status.py's GET /api/health) are
answering locally. Neither upstream endpoint alone covers "gateway + API
sont vivants" as a single probe, and upstream has nothing literally named
/healthz, so rather than patch api_server.py or status.py (both vendored,
rebased quarterly — see UPSTREAM.md), this is a standalone stdlib-only
aggregator wired in purely additively: a new s6 service
(docker/s6-rc.d/healthz/) that the existing `COPY docker/s6-rc.d/` and
`COPY --link . .` Dockerfile steps already pick up. Zero lines of upstream
Hermes code are touched.

Stdlib only (http.server + urllib) — no dependency on Hermes' venv, so it
starts even if the app layer fails later in boot.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TIMEOUT_S = 2.0

API_SERVER_PORT = int(os.environ.get("API_SERVER_PORT", "8642"))
DASHBOARD_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9119"))
HEALTHZ_HOST = os.environ.get("NOKKER_HEALTHZ_HOST", "0.0.0.0")
HEALTHZ_PORT = int(os.environ.get("NOKKER_HEALTHZ_PORT", "8643"))
DASHBOARD_ENABLED = os.environ.get("HERMES_DASHBOARD", "").lower() in (
    "1", "true", "yes",
)


def _probe(url: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nokker-healthz/1"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            ok = 200 <= resp.status < 300
            return ok, f"http {resp.status}"
    except Exception as exc:  # report, never crash the probe loop
        return False, f"{type(exc).__name__}: {exc}"


class Handler(BaseHTTPRequestHandler):
    server_version = "nokker-healthz/1"

    def log_message(self, fmt, *args):  # quiet; s6 captures stdout anyway
        pass

    def do_GET(self):  # BaseHTTPRequestHandler API
        if self.path not in ("/healthz", "/healthz/"):
            self.send_response(404)
            self.end_headers()
            return

        gw_ok, gw_detail = _probe(f"http://127.0.0.1:{API_SERVER_PORT}/health")
        checks = {"gateway_api": {"ok": gw_ok, "detail": gw_detail}}

        if DASHBOARD_ENABLED:
            dash_ok, dash_detail = _probe(
                f"http://127.0.0.1:{DASHBOARD_PORT}/api/health"
            )
            checks["dashboard"] = {"ok": dash_ok, "detail": dash_detail}
        else:
            dash_ok = True  # not part of this deployment's contract

        body = json.dumps(
            {"status": "ok" if (gw_ok and dash_ok) else "degraded", "checks": checks}
        ).encode()
        self.send_response(200 if (gw_ok and dash_ok) else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer((HEALTHZ_HOST, HEALTHZ_PORT), Handler)
    print(
        f"[nokker-healthz] listening on {HEALTHZ_HOST}:{HEALTHZ_PORT} "
        f"(gateway_api=127.0.0.1:{API_SERVER_PORT}/health, "
        f"dashboard={f'127.0.0.1:{DASHBOARD_PORT}/api/health' if DASHBOARD_ENABLED else 'disabled'})",
        file=sys.stderr,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
