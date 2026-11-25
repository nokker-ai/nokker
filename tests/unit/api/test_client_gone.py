"""Un client qui coupe avant l'écriture de la réponse JSON (DELETE /v1/local/sessions/{fil} puis fermeture, onglet fermé) :
une ligne de debug, ni trace ni ERROR ; les autres erreurs d'écriture restent visibles."""
from __future__ import annotations

import io
import logging

import pytest

from nokker.api.server import make_handler
from tests.unit.api.test_approvals_routes import make_kernel_with_approvals


class _Wfile:
    def __init__(self, exc: BaseException | None):
        self.exc, self.buf = exc, io.BytesIO()

    def write(self, data):
        if self.exc is not None:
            raise self.exc
        return self.buf.write(data)

    def flush(self):
        pass


def _handler(exc):
    from nokker.approvals.memory import InMemoryApprovals

    kernel, _ = make_kernel_with_approvals(InMemoryApprovals())
    cls = make_handler(kernel, {"tok": "owner"}, approvals=InMemoryApprovals())
    h = cls.__new__(cls)
    h.wfile = _Wfile(exc)
    h.request_version, h.requestline, h.client_address = "HTTP/1.1", "DELETE /v1/local/sessions/th_1 HTTP/1.1", ("127.0.0.1", 1)
    h.command, h.path, h.close_connection = "DELETE", "/v1/local/sessions/th_1", False
    return h


@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "reset")])
@pytest.mark.parametrize("method", ["_json", "_json_private"])
def test_client_that_left_before_the_reply_is_ignored_with_one_debug_line(exc, method, caplog):
    h = _handler(exc)
    caplog.set_level(logging.DEBUG, logger="nokker.api.server")
    getattr(h, method)(200, {"ok": True})  # ne lève pas : le gestionnaire continue
    gone = [r for r in caplog.records if "client parti" in r.getMessage()]
    assert len(gone) == 1 and gone[0].levelno == logging.DEBUG and gone[0].exc_info is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert h.close_connection is True


@pytest.mark.parametrize("method", ["_json", "_json_private"])
def test_other_write_errors_are_not_masked(method):
    with pytest.raises(OSError, match="disk"):
        getattr(_handler(OSError(5, "disk")), method)(200, {"ok": True})


def test_a_normal_reply_is_written():
    h = _handler(None)
    h._json(200, {"ok": True})
    assert b'{"ok": true}' in h.wfile.buf.getvalue()
