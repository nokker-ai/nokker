"""Annonce de l'hôte sans tour (`POST …/announce`), annulation `keep_partial`, mode de session conservé au redémarrage."""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import threading

import pytest

from nokker.core import loop as loop_mod
from nokker.core.turn import TurnInput
from nokker.qa.permissions import LocalPermissions, Mode
from tests.unit.api.test_server import make_kernel
from tests.unit.test_cancel_stream import Sink, SlowStream, _provider
from tests.unit.test_local_api import _api, _req
from tests.unit.test_local_sessions_view import _serve
from tests.unit.test_pause_steer import WAIT, kernel


def _open(api, tmp_path, **extra):
    return api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}",
                      {"cwd": str(tmp_path / "projets" / "app"), **extra})


def _announce(api, tid, body):
    return api.handle("POST", f"/v1/local/sessions/{tid}/announce", f"Bearer {api.token}", body)


# ───────────────────────────────────────── announce ─────────────────────────────────────────


def _wired(tmp_path):
    api = _api(tmp_path)
    k = make_kernel("d'accord")
    api.on_announce = k.announce
    api.is_running = lambda tid: bool(k.controls.running(tid))
    tid = _open(api, tmp_path)[1]["thread_id"]
    return api, k, tid


def test_announce_is_queued_without_a_turn_and_seen_at_the_next_one(tmp_path):
    api, k, tid = _wired(tmp_path)
    status, body = _announce(api, tid, {"text": "  Le fichier a changé sur le disque.  "})
    assert status == 202 and body == {"thread_id": tid, "queued": True, "pending": 1, "running": False}
    assert _announce(api, tid, {"text": "Deuxième."})[1]["pending"] == 2
    assert k.store.history(tid) == []  # aucun tour déclenché, rien dans l'historique tout de suite
    turn = k.run_turn(TurnInput(thread_id=tid, user_text="où en est-on ?", sender="alice"))
    assert turn.injected[:2] == [f"{loop_mod.ANNOUNCE_PREFIX} Le fichier a changé sur le disque.",
                                 f"{loop_mod.ANNOUNCE_PREFIX} Deuxième."]  # dans l'ordre, une fois caviardées
    assert k.store.history(tid)[0].injected == turn.injected  # rangé avec le tour : rejoué tel quel ensuite
    assert _announce(api, tid, {"text": "Troisième."})[1]["pending"] == 1  # la file est vidée par le tour


def test_announce_during_a_turn_waits_for_the_next_one(tmp_path):
    api, k, tid = _wired(tmp_path)
    k.controls.begin(tid)  # un tour tourne
    status, body = _announce(api, tid, {"text": "Pendant le tour."})
    assert status == 202 and body["queued"] is True and body["running"] is True  # ni 409 ni refus : mise en file
    k.controls.end(tid)
    assert k.run_turn(TurnInput(thread_id=tid, user_text="x", sender="a")).injected == [
        f"{loop_mod.ANNOUNCE_PREFIX} Pendant le tour."]


def test_announce_errors(tmp_path, monkeypatch):
    api, _k, tid = _wired(tmp_path)
    assert _announce(api, "th_inconnu", {"text": "x"})[0] == 404
    for bad in ({}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "x" * (loop_mod.ANNOUNCE_MAX_CHARS + 1)}, []):
        assert _announce(api, tid, bad)[0] == 400, bad
    assert _announce(api, tid, {"text": "x" * loop_mod.ANNOUNCE_MAX_CHARS})[0] == 202
    monkeypatch.setattr(loop_mod, "ANNOUNCE_MAX_PENDING", 2)
    assert _announce(api, tid, {"text": "b"})[0] == 202
    status, body = _announce(api, tid, {"text": "c"})
    assert status == 409 and body["error"]["code"] == "announce_queue_full"
    api.on_announce = None
    assert _announce(api, tid, {"text": "x"})[0] == 404


def test_announce_is_scrubbed_and_known_thread_without_a_session_is_accepted(tmp_path):
    api, k, tid = _wired(tmp_path)
    api.thread_known = lambda t: t == "th_range"
    assert _announce(api, "th_range", {"text": "ok"})[0] == 202
    k.invariants = dataclasses.replace(k.invariants) if dataclasses.is_dataclass(k.invariants) else k.invariants
    k.invariants.scrub_output = lambda t: t.replace("SECRET", "[secret]")
    _announce(api, tid, {"text": "le jeton SECRET est périmé"})
    assert k._owner_notes[tid] == [f"{loop_mod.ANNOUNCE_PREFIX} le jeton [secret] est périmé"]


def test_announce_route_through_the_real_handler(tmp_path):
    api, k, tid = _wired(tmp_path)
    httpd, port = _serve(api, k)
    try:
        assert _req(port, f"/v1/local/sessions/{tid}/announce", body={"text": "x"})[0] == 401
        status, body = _req(port, f"/v1/local/sessions/{tid}/announce", token=api.token, body={"text": "Salut"})
        assert status == 202 and body["pending"] == 1
    finally:
        httpd.shutdown()


# ─────────────────────────────────────── keep_partial ───────────────────────────────────────


def _cancelled_turn(keep):
    go, sent = threading.Event(), threading.Event()
    body, calls = SlowStream(go, sent), []
    k, _, _ = kernel([], provider=_provider(body, calls))
    sink, out = Sink(), {}
    th = threading.Thread(target=lambda: out.setdefault("turn", k.run_turn(
        TurnInput(thread_id="th_c", user_text="écris beaucoup", channel="cli"), stream=sink)))
    th.start()
    assert sent.wait(WAIT)
    k.cancel("th_c", keep_partial=keep)
    go.set()
    th.join(WAIT)
    assert not th.is_alive() and body.closed
    return k, out["turn"], sink


def test_keep_partial_keeps_the_streamed_text_marked_interrupted():
    k, turn, sink = _cancelled_turn(True)
    marker = "[Réponse interrompue à la demande de la personne.]"
    assert turn.reply.startswith("Je commence une très longue réponse mot") and turn.reply.endswith("\n\n" + marker)
    assert "et encore" not in turn.reply  # rien de ce qui est arrivé après l'annulation
    assert turn.error == "cancelled" and [m.content for m in turn.messages] == [turn.reply]
    assert k.store.history("th_c")[0].reply == turn.reply  # dans l'historique rejoué au modèle
    assert not k._keep_partial and "th_c" not in k._cancelled  # demande consommée, pas collante
    assert sink.resets >= 1  # le client vide le texte partiel, puis reçoit la réponse finale qui le reprend


def test_default_cancel_still_drops_the_partial_text():
    _k, turn, _sink = _cancelled_turn(False)
    assert turn.reply == loop_mod.CANCEL_REPLY and "très longue" not in turn.reply


def test_keep_partial_without_streamed_text_falls_back_to_the_plain_reply():
    go, sent = threading.Event(), threading.Event()
    k, _, _ = kernel([], provider=_provider(SlowStream(go, sent), []))
    k.cancel("th_z", keep_partial=True)  # annulation d'avant le tour : ne le vise pas
    assert "th_z" in k._keep_partial
    go.set()
    sent.set()
    turn = k.run_turn(TurnInput(thread_id="th_z", user_text="x", channel="cli"))
    assert not k._keep_partial and turn.reply != "" and "interrompue" not in turn.reply


def test_cancel_route_reads_keep_partial(tmp_path):
    api = _api(tmp_path)
    k = make_kernel("ok")
    httpd, port = _serve(api, k)
    try:
        status, body = _req(port, "/v1/threads/th_x/cancel", token=api.token, body={"keep_partial": True})
        assert status == 202 and body["keep_partial"] is True and "th_x" in k._keep_partial
        status, body = _req(port, "/v1/threads/th_y/cancel", token=api.token, body={})
        assert status == 202 and "keep_partial" not in body and "th_y" not in k._keep_partial  # défaut inchangé
        status, body = _req(port, "/v1/threads/th_y/cancel", token=api.token, body={"keep_partial": "oui"})
        assert status == 400 and body["error"]["code"] == "bad_keep_partial"
    finally:
        httpd.shutdown()


# ───────────────────────────────── mode conservé au redémarrage (n°22) ─────────────────────────────────


def _booted(tmp_path, name):
    api = _api(tmp_path / name)
    api.permissions = LocalPermissions(mode=Mode.DEFAULT, modes_path=tmp_path / "state" / "session-modes.json")
    (tmp_path / "state").mkdir(exist_ok=True)
    return api


def _mode(api, tid, method="GET", body=None):
    return api.handle(method, f"/v1/local/sessions/{tid}/mode", f"Bearer {api.token}", body or {})


def test_session_mode_survives_a_restart(tmp_path):
    api = _booted(tmp_path, "run1")
    tid = _open(api, tmp_path / "run1", thread_id="th_mode")[1]["thread_id"]
    assert _mode(api, tid, "PUT", {"mode": "plan"})[1]["mode"] == "plan"
    path = tmp_path / "state" / "session-modes.json"
    assert json.loads(path.read_text())["modes"] == {"th_mode": "plan"} and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # « redémarrage » : un autre noyau (mémoire vide), le même fichier d'état ; l'hôte rouvre le fil
    api2 = _booted(tmp_path, "run2")
    assert api2.permissions.thread_mode == {}
    _open(api2, tmp_path / "run2", resume="th_mode")
    api2.permissions.restore_thread_mode("th_mode")  # ce que `on_session_open` fait à la reprise
    assert _mode(api2, "th_mode")[1] == {"mode": "plan", "modes": list(_mode(api2, "th_mode")[1]["modes"]),
                                         "effective": "plan"}
    # un fil jamais réglé garde le mode de l'utilisateur ; un autre fil n'hérite de rien
    assert api2.permissions.restore_thread_mode("th_autre") is None and api2.permissions.chosen_mode("th_autre") is Mode.DEFAULT


def test_latest_mode_wins_and_restore_does_not_override_a_live_mode(tmp_path):
    p = LocalPermissions(modes_path=tmp_path / "m.json")
    p.set_thread_mode("th_a", "auto")
    p.set_thread_mode("th_a", "accept_edits")
    q = LocalPermissions(modes_path=tmp_path / "m.json")
    q.thread_mode["th_a"] = Mode.PLAN  # déjà réglé dans CE processus : la relecture n'y touche pas
    assert q.restore_thread_mode("th_a") is Mode.PLAN
    assert LocalPermissions(modes_path=tmp_path / "m.json").restore_thread_mode("th_a") is Mode.ACCEPT_EDITS


def test_restored_mode_never_loosens_the_project_restriction(tmp_path):
    p = LocalPermissions(modes_path=tmp_path / "m.json")
    p.set_thread_mode("th_a", "auto")
    q = LocalPermissions(modes_path=tmp_path / "m.json")
    q.thread_project["th_a"] = (Mode.PLAN, [], [])  # le dépôt est plus strict qu'au réglage d'origine
    q.restore_thread_mode("th_a")
    assert q.chosen_mode("th_a") is Mode.AUTO and q.mode_for("th_a") is Mode.PLAN  # le plus strict gagne


@pytest.mark.parametrize("content", ["pas du json", '{"modes": [1]}', '{"modes": {"th_a": "nimporte"}}', "[]"])
def test_unreadable_store_is_ignored(tmp_path, content):
    path = tmp_path / "m.json"
    path.write_text(content)
    p = LocalPermissions(modes_path=path)
    assert p.restore_thread_mode("th_a") is None
    p.set_thread_mode("th_a", "plan")  # et se répare à l'écriture suivante
    assert LocalPermissions(modes_path=path).restore_thread_mode("th_a") is Mode.PLAN


def test_without_a_store_path_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = LocalPermissions()
    p.set_thread_mode("th_a", "plan")
    assert list(tmp_path.iterdir()) == [] and p.restore_thread_mode("th_b") is None
