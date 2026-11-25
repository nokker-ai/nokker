"""Session de code REFUSÉE parce que le bac à sable est indisponible : le tour suivant dit « bac indisponible » au modèle
(pas « ouvre une session »), dans la langue du tour ; sans échec préalable, le texte « pas de session » est inchangé ; la
marque est vidée à l'ouverture réussie et à la fin de la session."""
from __future__ import annotations

import pytest

import nokker.local.api as api_mod
from nokker.approvals.rule_labels import lang_scope
from nokker.core import model_texts as mt
from nokker.core.turn import Decision, InputCheck, ToolCall, Turn
from nokker.local.api import LocalApi
from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates
from nokker.tools.code import SandboxUnavailable, clear_session_refused, session_refused
from tests.unit.test_approval_reason_code import _Inner


def _refusal(thread_id: str, lang: str = "fr"):
    gates = PermissionGates(inner=_Inner(), permissions=LocalPermissions(mode=Mode.AUTO),
                            session_tools=frozenset({"terminal"}), code_session=lambda _t: False)
    turn = Turn(thread_id=thread_id, index=0, input=InputCheck(text="x"), channel="api")
    with lang_scope(lang):
        (v,) = gates.action(ToolCall(id="1", name="terminal", arguments={"command": "make"}), None, turn)
    assert v.decision is Decision.DENY and v.code == "no_code_session"
    return v.reason


def _api(tmp_path):
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)
    return LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "docker"}}, port=8743), root


def test_without_a_previous_failure_the_text_is_the_old_one():
    clear_session_refused("th_never")
    assert _refusal("th_never") == mt.PERM_NO_CODE_SESSION["fr"]
    assert _refusal("th_never", "en") == mt.PERM_NO_CODE_SESSION["en"]


@pytest.mark.parametrize("lang", ["fr", "en", "de", "it"])
def test_a_refused_session_says_the_sandbox_could_not_start(tmp_path, monkeypatch, lang):
    api, root = _api(tmp_path)
    auth = f"Bearer {api.token}"
    monkeypatch.setattr(api_mod, "check_sandbox", lambda: (_ for _ in ()).throw(SandboxUnavailable("daemon")))
    status, _ = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(root / "app"), "thread_id": "th_refused"})
    assert status == 503 and session_refused("th_refused")
    reason = _refusal("th_refused", lang)
    assert reason == mt.PERM_SESSION_REFUSED[lang] and reason != mt.PERM_NO_CODE_SESSION[lang]
    assert "ouvre une session" not in reason and "open a session" not in reason
    # une session ouverte avec succès, puis fermée, efface la marque
    monkeypatch.setattr(api_mod, "check_sandbox", lambda: None)
    status, _ = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(root / "app"), "thread_id": "th_refused"})
    assert status == 201 and not session_refused("th_refused")
    assert _refusal("th_refused", lang) == mt.PERM_NO_CODE_SESSION[lang]


def test_delete_clears_the_mark(tmp_path, monkeypatch):
    api, root = _api(tmp_path)
    auth = f"Bearer {api.token}"
    monkeypatch.setattr(api_mod, "check_sandbox", lambda: (_ for _ in ()).throw(SandboxUnavailable("daemon")))
    api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(root / "app"), "thread_id": "th_del"})
    assert session_refused("th_del")
    api.handle("DELETE", "/v1/local/sessions/th_del", auth, None)
    assert not session_refused("th_del")
