"""`local.strict_resume` : `resume` d'un fil inconnu → 404 (vrai `make_handler`, boucle locale) ; un fil à session
vivante ou à tour rangé se reprend ; `thread_id` (fil choisi) et le comportement d'avant (drapeau éteint) inchangés."""
from __future__ import annotations

from nokker.core.turn import TurnInput
from nokker.local.api import LOCAL_OWNER
from tests.unit.api.test_server import make_kernel
from tests.unit.test_local_api import _api, _req
from tests.unit.test_local_sessions_view import _serve


def _world(tmp_path, strict):
    api = _api(tmp_path)
    api.strict_resume = strict
    kernel = make_kernel("ok")
    httpd, port = _serve(api, kernel)
    return api, kernel, httpd, port, str(tmp_path / "projets" / "app")


def test_resume_of_an_unknown_thread_is_404_and_opens_nothing(tmp_path):
    api, _kernel, httpd, port, cwd = _world(tmp_path, True)
    try:
        status, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd, "resume": "th_inconnu"})
        assert status == 404 and body["error"]["code"] == "thread_not_found"
        assert "th_inconnu" not in api.code.sessions()
        assert _req(port, "/v1/local/sessions", token=api.token)[1]["sessions"] == []
        # `thread_id` : le fil CHOISI à la création reste accepté (201), comme avant
        status, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd, "thread_id": "th_choisi"})
        assert status == 201 and body["thread_id"] == "th_choisi"
    finally:
        httpd.shutdown()


def test_resume_of_a_live_session_or_a_stored_thread_works(tmp_path):
    api, kernel, httpd, port, cwd = _world(tmp_path, True)
    try:
        tid = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd})[1]["thread_id"]
        assert _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd, "resume": tid})[0] == 201  # vivante
        kernel.run_turn(TurnInput(thread_id="th_range", user_text="bonjour", sender=LOCAL_OWNER))
        # « redémarrage » : plus de session vivante, mais le tour est rangé (Postgres en vrai) → le fil se reprend
        status, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd, "resume": "th_range"})
        assert status == 201 and body["thread_id"] == "th_range" and "th_range" in api.code.sessions()
    finally:
        httpd.shutdown()


def test_flag_off_resume_of_an_unknown_thread_is_unchanged(tmp_path):
    api, _kernel, httpd, port, cwd = _world(tmp_path, False)
    try:
        status, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd, "resume": "th_inconnu"})
        assert status == 201 and body["thread_id"] == "th_inconnu"
    finally:
        httpd.shutdown()
