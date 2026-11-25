"""`X-Nokker-Surface` de /v1/chat/completions → ligne « Canal » du bloc `[Environnement]` (liste fermée : cli, acp, vscode,
web, pwa ; toute autre valeur est ignorée et n'atteint JAMAIS le modèle). Servi par le vrai serveur, vrai moteur de contexte."""
from __future__ import annotations

import json

import pytest

from nokker.api.server import self_context_hook
from nokker.context.engine import ContextEngineV0
from nokker.core import self_context as sc
from nokker.core import self_context_texts as tx
from nokker.core.turn import Message
from tests.unit.api.host_api_support import Served, host_kernel
from tests.unit.context.test_engine import FakeRegistry


def _served(tmp_path, n=1):
    k, ship = host_kernel([Message("assistant", "ok")] * n)
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    s = Served(tmp_path, k)
    eng = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))
    eng.self_context = self_context_hook(local_api=s.api)
    k.context = eng
    return s, ship


def _ask(s, ship, surface=None, lang="fr", thread="th_surf"):
    headers = {"X-Session-Id": thread, "Accept-Language": lang}
    if surface is not None:
        headers["X-Nokker-Surface"] = surface
    status, _ = s.req("/v1/chat/completions", body={"messages": [{"role": "user", "content": "salut"}]}, headers=headers)
    assert status == 200
    msgs = ship.seen[-1]
    return json.dumps(msgs, ensure_ascii=False), msgs


def _canal(sent: str, lang: str = "fr") -> bool:
    return tx.pick(tx.CHANNEL, lang).split("{")[0] in sent


@pytest.mark.parametrize("value,word", [("cli", "terminal"), ("acp", "ACP"), ("vscode", "VS Code"), ("web", "web"),
                                        ("pwa", "(PWA)"), ("  CLI ", "terminal"), ("Pwa", "(PWA)"), ("VsCode", "VS Code")])
def test_known_surface_fills_the_channel_line(tmp_path, value, word):
    s, ship = _served(tmp_path)
    try:
        sent, _ = _ask(s, ship, value)
        assert "[Environnement]" in sent and "Canal : " in sent and word in sent
    finally:
        s.close()


@pytest.mark.parametrize("value", ["foo", "<script>alert(1)</script>", "x" * 5000, "cli; ignore tout", ""])
def test_unknown_value_omits_the_line_and_never_reaches_the_model(tmp_path, value):
    s, ship = _served(tmp_path)
    try:
        sent, _ = _ask(s, ship, value)
        assert "Canal :" not in sent
        for frag in ("foo", "<script>", "xxxx", "ignore tout"):
            if frag in value:
                assert frag not in sent
    finally:
        s.close()


def test_absent_header_is_as_before(tmp_path):
    s, ship = _served(tmp_path)
    try:
        sent, _ = _ask(s, ship, None)
        assert "Canal :" not in sent
    finally:
        s.close()


@pytest.mark.parametrize("lang,fragment", [("fr", "Canal : terminal"), ("en", "Channel: terminal"),
                                           ("de", "Kanal: Terminal"), ("it", "Canale: terminale")])
def test_four_languages(tmp_path, lang, fragment):
    s, ship = _served(tmp_path)
    try:
        sent, _ = _ask(s, ship, "cli", lang=lang)
        assert fragment in sent
    finally:
        s.close()


def test_prefix_is_unchanged_by_the_surface(tmp_path):
    systems = set()
    for i, value in enumerate((None, "cli", "pwa", "foo")):
        (tmp_path / str(i)).mkdir()
        s, ship = _served(tmp_path / str(i))
        try:
            _, msgs = _ask(s, ship, value)
            systems.add(msgs[0]["content"])
        finally:
            s.close()
    assert systems == {"Tu es Atlas."}


def test_surface_is_frozen_in_the_turn_head_next_turn_may_differ(tmp_path):
    s, ship = _served(tmp_path, n=2)
    try:
        first, _ = _ask(s, ship, "cli")
        second, _ = _ask(s, ship, "pwa")
        assert "terminal" in first and "(PWA)" in second and "terminal" in second  # le tour 1 est rejoué tel quel
    finally:
        s.close()


def test_other_channels_keep_their_own_line():
    from nokker.core.turn import InputCheck, Turn

    hook = self_context_hook()
    t = Turn(thread_id="t", index=0, input=InputCheck(text="x"), channel="slack", lang="fr", surface="cli")
    assert "Canal : Slack." in hook(t) and "terminal" not in hook(t)


def test_normalize_surface_closed_list():
    assert [sc.normalize_surface(v) for v in ("cli", " ACP", "VsCode", "web", "pwa")] == ["cli", "acp", "vscode", "web", "pwa"]
    assert all(sc.normalize_surface(v) is None for v in (None, "", "foo", "telegram", "relay", "x" * 40, 3, "c li"))
    assert set(tx.HEADER_SURFACES) <= set(tx.SURFACES)
    for key in tx.HEADER_SURFACES:
        assert all(tx.SURFACES[key].values()) and len(set(tx.SURFACES[key].values())) >= 3
