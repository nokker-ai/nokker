"""Masquage sur l'appareil, côté noyau (docs/LOKKAL-PHONE.md §4.3.2, lot P9b) : règles publiées (`GET /v1/redact/rules`),
marqueurs jamais caviardés par le noyau, consigne une fois par fil, garde `masked_value` avant toute porte."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request

import pytest

from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, InputCheck, Message, ToolCall, ToolResult, Turn, TurnInput
from nokker.redact import guard, markers, rules

# ─────────────────────────────── textes figés ───────────────────────────────

CONSIGNE_SHA256 = "77761fc800023c367b292faa818ea69328b2032a2bbd57d846c018aa32a1c4c0"
REFUSAL_SHA256 = "ebbfbb1907bf50a769027739c62614ed1a934caf30c75c0ffd9309f9798f9f3f"
PERSIST_SHA256 = "2471001b61e3b74f44eda278cfdf5399237196aea546efcc40b7f8e363846ecd"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_model_texts_are_frozen():
    """Textes vus par le modèle, validés mot pour mot : toute modification change l'empreinte et doit être relue."""
    assert markers.CONSIGNE == (
        "[Valeurs masquées]\n"
        "Les marqueurs comme ⟦PHONE-1⟧ remplacent des valeurs masquées sur l'appareil de la personne : tu ne les "
        "connais pas. Pour t'y référer, recopie le marqueur tel quel ; ne le devine pas, ne le reformate pas, ne le "
        "traduis pas. La personne voit la vraie valeur à sa place. Un outil qui envoie, paie ou écrit ailleurs refuse "
        "un marqueur.\n[/Valeurs masquées]")
    assert guard.REFUSAL == ("masked_value : {outil} reçoit une valeur masquée sur l'appareil de la personne "
                             "({marqueurs}) ; rien n'a été fait ; si cette valeur est nécessaire, demande-lui de la "
                             "donner en clair.")
    assert guard.PERSIST_REFUSAL == ("masked_value : {outil} garderait une valeur masquée sur l'appareil de la "
                                     "personne ({marqueurs}) au-delà de ce fil ; rien n'a été enregistré ; si cette "
                                     "valeur doit être retenue, demande-lui de la donner en clair.")
    assert markers.UNMARKED == "[valeur masquée]"
    assert _sha(markers.CONSIGNE) == CONSIGNE_SHA256
    assert _sha(guard.REFUSAL) == REFUSAL_SHA256
    assert _sha(guard.PERSIST_REFUSAL) == PERSIST_SHA256


def test_marker_pattern_is_the_contract():
    assert markers.MARKER_PATTERN == "⟦(PHONE|EMAIL|IBAN|AHV|CARD|BIRTHDATE|ADDRESS|SECRET)-[1-9][0-9]{0,3}⟧"
    assert markers.MARKER_RE.fullmatch("⟦PHONE-1⟧") and markers.MARKER_RE.fullmatch("⟦SECRET-9999⟧")
    for bad in ("⟦PHONE-0⟧", "⟦PHONE-10000⟧", "⟦TEL-1⟧", "[PHONE-1]", "⟦phone-1⟧"):
        assert not markers.MARKER_RE.fullmatch(bad), bad


# ─────────────────────────────── règles ───────────────────────────────


def test_rules_shape_matches_the_app_sdk():
    """Forme de `RedactRules` du SDK de l'app (nokker-apps packages/sdk/src/redact.ts)."""
    body = rules.device_rules()
    assert list(body) == ["version", "rules", "vectors"]
    canon = json.dumps({"rules": body["rules"], "vectors": body["vectors"]}, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    assert body["version"] == hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    assert set(rules.KIND_CODE) == set(rules.KINDS) and tuple(rules.KIND_CODE.values()) == markers.FAMILIES
    assert rules.KIND_CODE["avs"] == "AHV"
    assert {r["kind"] for r in body["rules"]} == set(rules.KINDS)
    ids = [r["id"] for r in body["rules"]]
    assert len(ids) == len(set(ids))
    for r in body["rules"]:
        assert set(r) <= {"id", "kind", "pattern", "flags", "group", "validate", "context"}, r["id"]
        assert {"id", "kind", "pattern", "group", "validate"} <= set(r)
        assert r.get("flags", "i") == "i" and r["group"] in (0, 1, 2)
        assert r["validate"] in ("none", "iban_mod97", "luhn", "avs_ean13")
        assert rules.dialect_problem(r["pattern"]) is None, r["id"]
        assert rules.dialect_problem(r.get("context", "")) is None, r["id"]
        compiled = re.compile(r["pattern"])
        assert compiled.groups == r["group"]
        # le groupe capturé termine le motif (seul un lookahead peut le suivre)
        assert re.sub(r"\(\?![^()]*\)$", "", r["pattern"]).endswith(")"), r["id"]
    for v in body["vectors"]:
        assert set(v) == {"text", "expect"}
        assert all(set(e) == {"kind", "value"} and e["kind"] in rules.KINDS for e in v["expect"])


def test_every_vector_plays_with_python_re_and_validators():
    body = rules.device_rules()
    assert rules.check_vectors(body) == []
    kinds = {e["kind"] for v in body["vectors"] for e in v["expect"]}
    assert kinds == set(rules.KINDS)  # chaque famille a au moins un vecteur positif
    assert any(not v["expect"] for v in body["vectors"])  # et des négatifs
    used = {s["rule"] for v in body["vectors"] for s in rules.find_sensitive(v["text"], body["rules"])}
    assert used == {r["id"] for r in body["rules"]}  # chaque règle est exercée par un vecteur


def test_no_rule_masks_a_marker():
    text = " ".join(f"⟦{f}-{n}⟧" for f in markers.FAMILIES for n in (1, 42, 9999))
    for r in rules.device_rules()["rules"]:
        flags = re.IGNORECASE if r.get("flags") == "i" else 0
        assert not [m for m in re.finditer(r["pattern"], text, flags) if m.group(r["group"])], r["id"]


def test_rules_are_transposed_from_the_kernel_patterns():
    """Source unique : chaque motif vient d'un caviardage du noyau, transposé mécaniquement ; un motif qui change au
    noyau change la règle publiée."""
    from nokker.payments import cardmask
    from nokker.qa import secrets
    from nokker.tools import web
    from nokker.traces import pii

    by_id = {r["id"]: r["pattern"] for r in rules.device_rules()["rules"]}
    def tp(p, **kw):
        return [x for x, _f in rules.transpose(p, **kw)]

    assert by_id["email"] == "(" + web._EMAIL_RE.pattern + ")"
    assert [by_id["phone-ch"], by_id["phone-ch-national"]] == tp(web._PHONE_CH_RE.pattern)
    assert [by_id["phone-e164"]] == tp(web._PHONE_E164_RE.pattern)
    assert [by_id["avs"]] == tp(web._AVS_RE.pattern) and [by_id["card"]] == tp(cardmask._RUN.pattern)
    assert [by_id["iban"]] == tp(secrets._IBAN_RE.pattern)
    assert [by_id["iban-spaced"]] == tp(secrets._IBAN_SPACED_RE.pattern)
    assert [by_id["pem"]] == tp(secrets._PEM_RE.pattern, dotall=True)
    assert [by_id["password-keyword"]] == tp(secrets._PASSWORD_KEYWORD_RE.pattern, value_group=3)
    for rid, street in (("address-fr", pii._STREET_FR_RE), ("address-de", pii._STREET_DE_RE),
                        ("address-en", pii._STREET_EN_RE)):
        assert [by_id[rid]] == [rules._with_postal(x) for x in tp(street.pattern)]
    postal = tp(f"(?:{pii._POSTAL_SEP}){pii._POSTAL_BODY}")[0][1:-1]
    assert all(by_id[r].endswith("(?:" + postal + ")?)") for r in ("address-fr", "address-de", "address-en"))
    kinds = [k for k, _p in secrets._PATTERNS]
    assert [r["id"] for r in rules.device_rules()["rules"]][1:1 + len(kinds)][:3] == \
        ["anthropic-key", "api-key", "stripe-key"]
    assert all(k in by_id for k in kinds) and not any("entropy" in k for k in by_id)


def test_transposition_keeps_the_meaning_on_the_kernel_samples():
    """Le motif transposé trouve la même valeur que le motif du noyau (borne de tête ôtée)."""
    from nokker.tools import web

    for text, value in (("tel 079 123 45 67.", "079 123 45 67"), ("AVS 756.9217.0769.85;", "756.9217.0769.85"),
                        ("x7569217076985", None)):
        core = [m.group(0) for m in web._AVS_RE.finditer(text)] + [m.group(0) for m in web._PHONE_CH_RE.finditer(text)]
        dev = [s["value"] for s in rules.find_sensitive(text, rules.build_rules()) if s["kind"] in ("avs", "phone")]
        assert core == dev == ([value] if value else [])


def test_validators_follow_the_app():
    assert rules.avs_ean13("756.9217.0769.85") and not rules.avs_ean13("756.9217.0769.84")
    assert not rules.avs_ean13("123.4567.8901.23")
    assert rules.luhn("4111 1111 1111 1111") and not rules.luhn("4242 4242 4242 4241")
    assert not rules.luhn("0000 0000 0000 0000") and not rules.luhn("4242 4242 4242")  # 12 chiffres : non
    assert rules.iban_mod97("CH93 0076 2011 6238 5295 7") and not rules.iban_mod97("CH9300762011623852958")


def test_street_fr_without_inline_flag_keeps_its_meaning():
    from nokker.traces.pii import _STREET_FR_RE

    for t in ("12 rue de la Paix", "AVENUE du Mail 10", "Allée des Pins", "ALLÉE des Pins", "Chemin De L'Église"):
        assert _STREET_FR_RE.search(t), t
    assert not _STREET_FR_RE.search("la rue est calme")
    assert _STREET_FR_RE.search("au12 rue de la Paix").group(0) == "rue de la Paix"


def test_export_writes_the_exact_route_body():
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-m", "nokker.redact.export"], capture_output=True, check=True).stdout
    assert out == rules.rules_json()[0]
    assert json.loads(out) == rules.device_rules()


def test_route_requires_identity_and_answers_304_on_its_etag():
    from tests.unit.api.test_server import make_kernel, start_server

    srv = start_server(make_kernel(), api_keys={"k1": "alice"})
    body, etag = rules.rules_json()
    try:
        req = urllib.request.Request(srv.url("/v1/redact/rules"), headers={"Authorization": "Bearer k1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200 and r.read() == body
            assert r.headers["ETag"] == etag == f'"{rules.device_rules()["version"]}"'
        req = urllib.request.Request(srv.url("/v1/redact/rules"),
                                     headers={"Authorization": "Bearer k1", "If-None-Match": etag})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 304 and e.value.headers["ETag"] == etag
        req = urllib.request.Request(srv.url("/v1/redact/rules"),
                                     headers={"Authorization": "Bearer k1", "If-None-Match": '"autre"'})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(srv.url("/v1/redact/rules"), timeout=10)
        assert e.value.code == 401
    finally:
        srv.stop()


# ─────────────────────────────── le caviardage du noyau laisse les marqueurs ───────────────────────────────


@pytest.mark.parametrize("text", [
    "Rappelle ⟦PHONE-1⟧ et écris à ⟦EMAIL-2⟧",
    "mot de passe : ⟦SECRET-1⟧",
    "le mot de passe du portail est ⟦SECRET-12⟧",
    "API_KEY=⟦SECRET-3⟧",
    "token: ⟦SECRET-4⟧ et IBAN ⟦IBAN-1⟧, AVS ⟦AHV-1⟧, carte ⟦CARD-9999⟧",
    "née le ⟦BIRTHDATE-1⟧, ⟦ADDRESS-7⟧",
])
def test_kernel_redaction_never_touches_a_marker(text):
    from nokker.payments.cardmask import mask_card_text, mask_tool_text
    from nokker.qa.secrets import KnownSecrets, redact, redact_messages
    from nokker.qa.untrusted import inline, normalize

    found = markers.markers_in(text)
    assert found
    known = KnownSecrets()
    known.add("x", "valeur-connue-123")
    outs = [redact(text).text, redact(text, known, weak=False, wire=True).text,
            redact(text, known, weak=True, secret_tool=True).text, mask_card_text(text), mask_tool_text(text),
            inline(text), normalize(text),
            redact_messages([{"role": "user", "content": text}], known)[0]["content"]]
    for out in outs:
        assert markers.markers_in(out) == found, out


def test_card_field_value_that_is_a_marker_stays():
    from nokker.payments.cardmask import mask_card_text

    line = '- textbox "Card number" [ref=e12]: ⟦CARD-1⟧'
    assert mask_card_text(line) == line
    assert mask_card_text('- textbox "Card number" [ref=e12]: 4242 4242 4242 4242').endswith(": ••••")


def test_redaction_around_markers_still_redacts_the_rest():
    from nokker.qa.secrets import redact

    key = "sk-" + "proj-" + "AbCdEfGh1234" * 2
    out = redact(f"⟦PHONE-1⟧ et la clé {key} ⟦PHONE-1⟧").text
    assert key not in out and "[secret:api-key-" in out and out.count("⟦PHONE-1⟧") == 2


# ─────────────────────────────── consigne ───────────────────────────────


def _engine(tmp_path):
    from nokker.context.engine import ContextEngineV0
    from tests.unit.context.test_engine import FakeRegistry

    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Atlas.")
    return ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix))


def _turn(text, index=0, attached=""):
    t = Turn(thread_id="t1", index=index, input=InputCheck(text=text))
    t.attached = attached
    return t


def test_consigne_once_per_thread_just_before_attachments(tmp_path):
    eng = _engine(tmp_path)
    t0 = _turn("bonjour")
    eng.assemble(t0, [], [], [], None)
    assert markers.CONSIGNE not in t0.head
    att = "[Pièces jointes]\n[Fichier : a.txt (3 caractères)]\nabc\n[/Fichier]\n[/Pièces jointes]"
    t1 = _turn("rappelle ⟦PHONE-1⟧ demain", 1, attached=att)
    eng.assemble(t1, [t0], [], [], None)
    assert t1.head.count(markers.CONSIGNE) == 1
    assert t1.head.endswith(markers.CONSIGNE + "\n\n" + att)
    t2 = _turn("et ⟦EMAIL-1⟧ aussi", 2)
    eng.assemble(t2, [t0, t1], [], [], None)
    assert markers.CONSIGNE not in t2.head


def test_consigne_after_a_marker_that_came_by_a_tool_result(tmp_path):
    eng = _engine(tmp_path)
    t0 = _turn("mon agenda ?")
    eng.assemble(t0, [], [], [], None)
    t0.messages.append(Message("tool", "rdv avec ⟦PHONE-2⟧", tool_call_id="c1", name="calendar_events"))
    t1 = _turn("merci", 1)
    eng.assemble(t1, [t0], [], [], None)
    assert t1.head.endswith(markers.CONSIGNE)


def test_no_marker_no_consigne(tmp_path):
    eng = _engine(tmp_path)
    t = _turn("le texte [PHONE-1] n'est pas un marqueur")
    eng.assemble(t, [], [], [], None)
    assert markers.CONSIGNE not in t.head


def test_v1_engine_through_the_kernel(tmp_path):
    """Moteur V1 (never clear) par la vraie boucle : la consigne part une fois, intacte, avec le marqueur intact."""
    from tests.unit.context.harness import make_session

    s = make_session(tmp_path=tmp_path)
    s.say("bonjour")
    s.say("appelle ⟦PHONE-1⟧ demain")
    s.say("et ⟦PHONE-1⟧ encore")
    sent = s.provider.seen[-1]
    users = [m["content"] for m in sent if m["role"] == "user"]
    assert sum(u.count(markers.CONSIGNE) for u in users) == 1
    assert markers.CONSIGNE in users[1] and users[-1].endswith("et ⟦PHONE-1⟧ encore")


# ─────────────────────────────── garde masked_value ───────────────────────────────


class _CountingGates:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def action(self, call, spec, turn):
        self.calls += 1
        return self.inner.action(call, spec, turn)

    def switch(self, reply, turn, tier):
        return self.inner.switch(reply, turn, tier)


def _kernel(calls, specs):
    from nokker.approvals.memory import InMemoryApprovals
    from tests.unit.test_loop_approvals import make_kernel

    backend = InMemoryApprovals()
    k, tools = make_kernel([Message("assistant", tool_calls=calls), Message("assistant", "ok")], approvals=backend)
    tools.specs.update(specs)
    k.gates = _CountingGates(k.gates)
    return k, tools, backend


SPECS = {
    "mail_send": ToolSpec("mail_send", "envoi", {}, Effect.SEND_EXTERNAL),
    "mail_read": ToolSpec("mail_read", "lecture", {}, Effect.READ),
    "pay_request": ToolSpec("pay_request", "paiement", {}, Effect.SPEND),
    "todo": ToolSpec("todo", "liste", {}, Effect.WRITE),
}


def _tool_contents(turn):
    return [m.content for m in turn.messages if m.role == "tool"]


def test_mail_send_with_a_marker_is_refused_before_any_gate():
    call = ToolCall(id="c1", name="mail_send", arguments={"to": "⟦EMAIL-1⟧", "body": "rappelle ⟦PHONE-1⟧ ou ⟦EMAIL-1⟧"})
    k, tools, backend = _kernel([call], SPECS)
    turn = k.run_turn(TurnInput("t1", "envoie-lui", sender="alice@example.ch"))
    assert tools.called == [] and k.gates.calls == 0 and backend.pending() == []
    assert _tool_contents(turn) == [(
        "masked_value : mail_send reçoit une valeur masquée sur l'appareil de la personne (⟦EMAIL-1⟧, ⟦PHONE-1⟧) ; "
        "rien n'a été fait ; si cette valeur est nécessaire, demande-lui de la donner en clair.")]


def test_marker_nested_in_dict_and_list_is_found():
    args = {"order": {"lines": [{"note": "x"}, {"contact": ["a", "tél ⟦PHONE-3⟧"]}]}, "card": "⟦CARD-2⟧"}
    call = ToolCall(id="c1", name="pay_request", arguments=args)
    k, tools, backend = _kernel([call], SPECS)
    turn = k.run_turn(TurnInput("t1", "paie"))
    assert tools.called == [] and k.gates.calls == 0 and backend.pending() == []
    assert _tool_contents(turn) == [guard.REFUSAL.format(outil="pay_request", marqueurs="⟦PHONE-3⟧, ⟦CARD-2⟧")]
    assert markers.markers_in(args) == ["⟦PHONE-3⟧", "⟦CARD-2⟧"]


def test_read_only_tool_with_a_marker_is_allowed():
    call = ToolCall(id="c1", name="mail_read", arguments={"query": "⟦EMAIL-1⟧"})
    k, tools, _ = _kernel([call], SPECS)
    k.run_turn(TurnInput("t1", "cherche"))
    assert tools.called == ["mail_read"] and k.gates.calls == 1


def test_tool_without_marker_and_thread_scoped_delegate_go_through_the_gates():
    calls = [ToolCall(id="c1", name="delegate", arguments={"goal": "trouve qui est ⟦PHONE-1⟧"}),
             ToolCall(id="c2", name="mail_send", arguments={"to": "a@example.ch", "body": "salut"}),
             ToolCall(id="c3", name="todo", arguments={"todos": [{"id": "1", "content": "⟦PHONE-1⟧"}]})]
    k, tools, _ = _kernel(calls, {**SPECS, "todo": ToolSpec("todo", "plan du fil", {}, Effect.READ)})
    k.run_turn(TurnInput("t1", "x"))
    assert tools.called == ["delegate", "mail_send", "todo"] and k.gates.calls == 3


def test_guard_follows_the_effect_of_this_call():
    from nokker.tools.effects import EffectsTable

    spec = EffectsTable().annotate(ToolSpec("calendar_create", "agenda", {}))
    persist = guard.refusal("calendar_create", spec, {"title": "⟦PHONE-1⟧"})  # écriture gardée hors du fil
    assert persist == guard.PERSIST_REFUSAL.format(outil="calendar_create", marqueurs="⟦PHONE-1⟧")
    # invitation : envoi ET écriture gardée — la persistance passe d'abord (même ordre que le faux noyau de l'app)
    assert guard.refusal("calendar_create", spec, {"title": "x", "attendees": ["⟦EMAIL-1⟧"]}) == \
        guard.PERSIST_REFUSAL.format(outil="calendar_create", marqueurs="⟦EMAIL-1⟧")
    assert guard.refusal("mail_send", SPECS["mail_send"], {"to": "⟦EMAIL-1⟧"}) == \
        guard.REFUSAL.format(outil="mail_send", marqueurs="⟦EMAIL-1⟧")
    assert guard.refusal("inconnu", None, {"a": "⟦PHONE-1⟧"}).startswith("masked_value : inconnu garderait")
    assert guard.refusal("send_bulk_email", ToolSpec("send_bulk_email", "", {}, Effect.SEND_BULK),
                         {"to": ["⟦EMAIL-1⟧"]}) is not None
    assert guard.refusal("mail_send", SPECS["mail_send"], {"to": "a@b.ch"}) is None
    assert guard.refusal("mail_read", SPECS["mail_read"], {"q": "⟦EMAIL-1⟧"}) is None


def test_browser_fill_is_third_party_input_and_refused_with_the_exact_text():
    spec = ToolSpec("browser_fill", "saisie", {}, Effect.WRITE, source="mcp:browser-pool")
    call = ToolCall(id="c1", name="browser_fill", arguments={"ref": "e3", "value": "⟦PHONE-1⟧"})
    k, tools, backend = _kernel([call], {"browser_fill": spec})
    turn = k.run_turn(TurnInput("t1", "remplis"))
    assert tools.called == [] and k.gates.calls == 0 and backend.pending() == []
    assert _tool_contents(turn) == [(
        "masked_value : browser_fill reçoit une valeur masquée sur l'appareil de la personne (⟦PHONE-1⟧) ; rien n'a "
        "été fait ; si cette valeur est nécessaire, demande-lui de la donner en clair.")]
    for name in ("browser_type", "browser_select_option", "browser_press_key", "browser_fill_form", "sites_connect"):
        assert guard.refusal(name, ToolSpec(name, "", {}, Effect.WRITE), {"x": ["⟦PHONE-1⟧"]}).endswith(
            "demande-lui de la donner en clair.") and " reçoit " in guard.refusal(name, None, {"x": "⟦PHONE-1⟧"})
    # outil WRITE d'une app MCP tierce (absent de la table du noyau) : saisie chez un tiers
    notion = ToolSpec("notion_create_page", "", {}, Effect.WRITE, source="mcp:notion")
    assert " reçoit " in guard.refusal("notion_create_page", notion, {"t": "⟦PHONE-1⟧"})


@pytest.mark.parametrize("name,effect,source", [
    ("mail_draft", Effect.WRITE, "mcp:workspace"), ("memory_write", Effect.WRITE, "mcp:sokkan-memory"),
    ("memory_note", Effect.WRITE, "builtin"), ("write_file", Effect.WRITE, "builtin:code"),
    ("patch", Effect.WRITE, "builtin:code"), ("kanban_create_task", Effect.WRITE, "mcp:kanban"),
    ("calendar_update", Effect.WRITE, "mcp:workspace"), ("folder_write", Effect.WRITE, "builtin"),
    ("page_create", Effect.WRITE, "builtin"), ("checkpoint_create", Effect.WRITE, "builtin"),
    ("session_send", Effect.READ, "builtin"), ("agent_draft", Effect.READ, "builtin"),
    ("memory_note", Effect.READ, "tenant:memory"),  # nokker.ai : READ voulu (quarantaine), LEAVES_THREAD le garde
    ("escalate_to_human", None, ""),
])
def test_tools_that_keep_text_beyond_the_thread_refuse_a_marker(name, effect, source):
    spec = ToolSpec(name, "", {}, effect, source=source) if effect is not None else None
    call = ToolCall(id="c1", name=name, arguments={"text": "note : ⟦PHONE-1⟧ et ⟦IBAN-2⟧"})
    k, tools, backend = _kernel([call], {name: spec} if spec else {})
    turn = k.run_turn(TurnInput("t1", "garde ça"))
    assert tools.called == [] and k.gates.calls == 0 and backend.pending() == []
    assert _tool_contents(turn) == [guard.PERSIST_REFUSAL.format(outil=name, marqueurs="⟦PHONE-1⟧, ⟦IBAN-2⟧")]


def test_a_subagent_thread_goes_through_the_same_guard():
    """Un sous-agent tourne sur `<fil>/sa-<id>` avec la même boucle : son write_file avec un marqueur est refusé."""
    call = ToolCall(id="c1", name="write_file", arguments={"path": "notes.md", "content": "⟦PHONE-1⟧"})
    k, tools, _ = _kernel([call], {"write_file": ToolSpec("write_file", "", {}, Effect.WRITE)})
    turn = k.run_turn(TurnInput("t1/sa-abc123", "consigne du parent : ⟦PHONE-1⟧", sender="alice"))
    assert tools.called == [] and k.gates.calls == 0
    assert _tool_contents(turn) == [guard.PERSIST_REFUSAL.format(outil="write_file", marqueurs="⟦PHONE-1⟧")]


def test_unmark_is_pure():
    assert markers.unmark("appelle ⟦PHONE-1⟧ ou ⟦PHONE-1⟧, pas ⟦TEL-1⟧") == \
        "appelle [valeur masquée] ou [valeur masquée], pas ⟦TEL-1⟧"
    assert markers.unmark("") == "" and markers.unmark("rien") == "rien"


def test_direct_and_script_paths_refuse_too():
    k, tools, backend = _kernel([], SPECS)
    out = k.direct_call("t1", "mail_send", {"to": "⟦EMAIL-1⟧"})
    assert isinstance(out, ToolResult) and out.is_error and out.reason == "masked_value"
    assert out.content == guard.REFUSAL.format(outil="mail_send", marqueurs="⟦EMAIL-1⟧")
    from nokker.core.ports import ToolContext

    turn = Turn(thread_id="t1", index=0, input=InputCheck(text=""))
    res = k.script_tool_call("mail_send", {"to": ["⟦EMAIL-1⟧"]}, turn, ToolContext(thread_id="t1", turn_index=0, channel="api"))
    assert res == {"status": "refused", "content": guard.REFUSAL.format(outil="mail_send", marqueurs="⟦EMAIL-1⟧")}
    assert tools.called == [] and k.gates.calls == 0 and backend.pending() == []


def test_postal_split_keeps_the_meaning_of_the_old_alternative():
    """`traces/pii.py` : `(,\\s*|\\n\\s*|\\bCH-)(…)` devenu deux motifs appliqués l'un après l'autre, même résultat."""
    from collections import Counter

    from nokker.traces import pii

    up = pii._UP
    old = re.compile(rf"(,\s*|\n\s*|\bCH-)([1-9]\d{{3}}\s+[{up}][a-zà-ÿ]{{2,}}(?:[\s\-][{up}][a-zà-ÿ]+)?)")
    corpus = ["rue du Rhône 42, 1204 Genève", "x,CH-1204 Genève", "CH-1204 Genève, 1205 Lancy", "aCH-1204 Genève",
              "Bahnhofstrasse 1\n8001 Zürich", "le 1204 Genève", ", 0123 Nulle", "CH-8001 Zürich-Altstetten",
              "Tél, 1204 genève", "A, 1204 Genève\nB\n1205 Plan-Les-Ouates, CH-3000 Bern"]
    for text in corpus:
        want = old.sub(lambda m: m.group(1) + "[adresse]", text)
        got = pii._POSTAL_CH_RE.sub(lambda m: m.group(1) + "[adresse]",
                                    pii._POSTAL_RE.sub(lambda m: m.group(1) + "[adresse]", text))
        assert got == want, text
        c = Counter()
        assert pii.scrub_pii(text, counts=c).count("[adresse]") >= want.count("[adresse]")


def test_consolidation_never_stores_a_marker():
    """Un épisode qui porte ⟦PHONE-1⟧ est résumé et stocké sans aucun marqueur (« [valeur masquée] » à sa place)."""
    from nokker.tenancy.context import tenant_scope
    from tests.unit.memory.test_consolidation import DAY, NOW, TP, Ep, P, World

    w = World()
    t = f"{TP}:fil-1"
    w.signals.episodes_by_thread[t] = [
        Ep(f"ep_{i}", f"Plombier ⟦PHONE-{i + 1}⟧", f"Le plombier Delta se joint au ⟦PHONE-1⟧, étape {i}.",
           NOW - (40 - i) * DAY, i) for i in range(4)]
    w.model.episodes = lambda u: {"title": "Plombier", "text": "Le plombier Delta se joint au ⟦PHONE-1⟧."}
    rep = w.run(P)
    stored = json.dumps([{k: v for k, v in vars(n).items() if k != "embedding"} for n in w.notes_of(P).values()],
                        ensure_ascii=False)
    with tenant_scope(TP):
        full = w.c.report(P, rep["run"]["id"])
    journal = repr(vars(w.store))  # journal des opérations (instantanés, preuves) et notes
    assert "⟦" not in stored and "⟦" not in json.dumps([rep, full, w.sent], ensure_ascii=False)
    assert not markers.MARKER_RE.search(journal)
    assert "Le plombier Delta se joint au [valeur masquée]." in stored


# ─────────────── chemins qui écrivent hors du fil SANS outil : jamais de marqueur ───────────────


def test_end_of_episode_facts_are_unmarked_before_the_memory(tmp_path):
    """Proposition de faits (raffinement d'un épisode clos → `MemoryPort.propose`) : la mémoire longue est hors du fil.
    Le résumé de l'épisode, lui, reste DANS le fil (rappel par fil) et garde son marqueur."""
    from nokker.context.consolidate import Consolidator as EpisodeConsolidator
    from nokker.context.types import SummaryResult
    from tests.unit.context.harness import make_session
    from tests.unit.context.test_consolidate_and_bubbles import FakeMemory, _fill

    class Refiner:
        def summarize(self, turns, previous=None):
            return SummaryResult(title="Plombier ⟦PHONE-1⟧", summary="Rappeler le plombier au ⟦PHONE-1⟧.",
                                 keywords=["plombier"], facts=["le plombier se joint au ⟦PHONE-1⟧"], model="fake")

    s = make_session(tmp_path=tmp_path)
    _fill(s)
    mem = FakeMemory()
    rep = EpisodeConsolidator(s.manager, refiner=Refiner(), memory=mem).run_once()
    assert mem.proposed and all(f == "le plombier se joint au [valeur masquée]" for _t, fs in mem.proposed for f in fs)
    assert "⟦PHONE-1⟧" in s.manager.episodes.get(rep.refined[0]).summary  # dans le fil : inchangé


def test_pending_notes_and_project_notes_are_unmarked(tmp_path):
    """Quarantaine de la mémoire (`learning/pending.py::_hold`) et notes de projet (`_write_note`, seule écriture d'une
    note apprise, approuvée ou directe)."""
    from nokker.context.project_memory import ProjectMemory, ProjectMemoryTools
    from nokker.learning.pending import PendingMemory
    from nokker.learning.store import MemoryLearningStore
    from tests.unit.learning.test_learning import FakeEngine, ctx, make_repo

    pending = PendingMemory(store=MemoryLearningStore(), account_of_thread=lambda _t: "us_a")
    pending.hold_facts("th_1", ["rappeler ⟦PHONE-1⟧ lundi"])
    pending.hold_note(thread_id="th_1", turn_index=1, kind="fact", scope="project", title="IBAN ⟦IBAN-1⟧",
                      text="payer sur ⟦IBAN-1⟧", derived_from="user", project="p-0123456789abcdef", links=[])
    held = pending.list("us_a")
    assert held and not any(markers.has_marker(n.title) or markers.has_marker(n.text) for n in held)

    repo = make_repo(tmp_path)
    pm = ProjectMemory(root=tmp_path / "pm", engine=FakeEngine(tmp_path / "pm"))
    pm.open_session("th_1", repo, tmp_path)
    r = ProjectMemoryTools(pm).call("memory_note", {"kind": "fact", "title": "Contact ⟦PHONE-2⟧",
                                                    "text": "le contact est ⟦PHONE-2⟧", "derived_from": "user"}, ctx())
    assert not r.is_error
    files = list((tmp_path / "pm").rglob("*.md"))
    assert files and all("⟦" not in f.read_text(encoding="utf-8") for f in files)
    assert any("[valeur masquée]" in f.read_text(encoding="utf-8") for f in files)


def test_thread_title_and_snippet_never_carry_a_marker():
    from nokker.threads.base import provisional_title
    from nokker.threads.memory import InMemoryThreads

    th = InMemoryThreads()
    t, created = th.ensure("th_m", "alice", "Appelle ⟦PHONE-1⟧ pour le devis")
    assert created and t.title == "Appelle [valeur masquée] pour le devis"
    assert provisional_title("⟦EMAIL-1⟧") == "[valeur masquée]"
    t2 = th.create("Fil ⟦IBAN-1⟧", "alice")
    assert t2.title == "Fil [valeur masquée]"
    th.update(t.id, title="Devis ⟦PHONE-1⟧", last_snippet="Je rappelle ⟦PHONE-1⟧ demain.")
    got = th.require(t.id)
    assert got.title == "Devis [valeur masquée]" and got.last_snippet == "Je rappelle [valeur masquée] demain."


def test_training_export_keeps_markers_and_never_holds_a_value(tmp_path):
    """Export vers l'entraînement : les marqueurs RESTENT (le modèle apprend à les recopier), passage PII compris ; le
    noyau n'a aucun dictionnaire (il vit sur l'appareil) : aucune valeur, aucun champ qui en porterait."""
    from nokker.traces import pii
    from tests.unit.traces.test_traces import (
        TESTER,
        MemoryTraceSink,
        Message,
        export,
        opts,
        recorder,
        run,
        source_from,
    )

    text = "Rappelle ⟦PHONE-1⟧ et écris à ⟦EMAIL-2⟧ ; IBAN ⟦IBAN-1⟧, AVS ⟦AHV-1⟧, née le ⟦BIRTHDATE-1⟧, ⟦ADDRESS-3⟧."
    assert pii.scrub_pii(text) == text
    sink = MemoryTraceSink()
    run([Message("assistant", "Je rappelle ⟦PHONE-1⟧.")], text=text, tenant=TESTER, thread=f"{TESTER}:fil",
        sender="us_aaaaaaaaaaaa", rec=recorder(sink))
    res = export(source_from(sink.rows), tmp_path, opts(include_eval=True))
    blob = (res.path / "calls.jsonl").read_text(encoding="utf-8")
    assert markers.markers_in(json.loads(blob.splitlines()[0])) == markers.markers_in(text)
    assert "[valeur masquée]" not in blob
    lines = [json.loads(x) for x in blob.splitlines()]
    assert all(set(ln) == {"prompt", "tools", "completion", "meta"} for ln in lines)
    assert not re.search(r'"(redaction|vault|dictionary|dict|unmasked|values?)"\s*:', blob)


def test_todo_list_stays_in_its_thread(tmp_path, monkeypatch):
    """`todo` reste autorisé avec un marqueur : sa liste est celle de l'espace de travail DU fil (un `TodoStore` en
    mémoire par fil, sous-agent compris), lue seulement par `todo` de ce fil et publiée en `todo.updated` à l'audience
    de CE fil (bubbles/hub.py) ; la route des sessions de code ne la lit que pour le fil de la session."""
    import sys
    import types

    from nokker.core.ports import ToolContext
    from nokker.tools import code
    from nokker.tools.code import CodeToolSource

    fake = types.ModuleType("tools.todo_tool")

    def todo_tool(todos, merge, store):
        if todos is not None:
            store["items"] = list(todos)
        return json.dumps({"todos": store.get("items", [])}, ensure_ascii=False)

    fake.todo_tool = todo_tool
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    monkeypatch.setitem(sys.modules, "tools.todo_tool", fake)
    src = CodeToolSource(root=str(tmp_path), backend="local")
    monkeypatch.setattr(src, "_open", lambda tid: code.Workspace(env=None, ops=None, todos={}, cwd=str(tmp_path)))
    item = [{"id": "1", "content": "rappeler ⟦PHONE-1⟧", "status": "pending"}]
    src.call("todo", {"todos": item}, ToolContext(thread_id="t1", turn_index=0, channel="api"))
    mine = src.call("todo", {}, ToolContext(thread_id="t1", turn_index=1, channel="api")).content
    other = src.call("todo", {}, ToolContext(thread_id="t2", turn_index=0, channel="api")).content
    child = src.call("todo", {}, ToolContext(thread_id="t1/sa-1", turn_index=0, channel="api")).content
    assert "⟦PHONE-1⟧" in mine and "⟦" not in other and "⟦" not in child
    assert src.workspace("t1").todos is not src.workspace("t2").todos

    from tests.unit.test_fixes_e2e_contracts import _hub, _turn

    hub, seen = _hub()
    content = json.dumps({"todos": item})
    hub.tool_executed(_turn(), ToolCall(id="c", name="todo", arguments={"todos": item}), None,
                      ToolResult(call_id="c", name="todo", content=content))
    events = [(n, d) for n, d in seen if "⟦" in json.dumps(d, ensure_ascii=False)]
    assert [n for n, _d in events] == ["todo.updated"] and events[0][1]["thread_id"] == "th_x"
    assert guard.refusal("todo", ToolSpec("todo", "", {}, Effect.READ), {"todos": item}) is None


def test_seeded_cloud_thread_gets_no_marker():
    """« Continuer dans le cloud » (`cloud/seed.py`) recopie une session dans un NOUVEAU fil : les marqueurs de l'appareil
    sont numérotés par fil, ils y arrivent démarqués (texte, réponse, extrait)."""
    from nokker.cloud.seed import seed_thread
    from nokker.store.pg import MemoryStore
    from nokker.threads.memory import InMemoryThreads

    store, threads = MemoryStore(), InMemoryThreads()

    class Hub:
        scrub = staticmethod(lambda s: s)

        def __init__(self):
            self.threads = threads

        def create_thread(self, title, sender):
            return threads.create(title, sender)

    from nokker.cloud.mirror import mirror_thread
    from nokker.tenancy.context import tenant_scope

    src = mirror_thread("tn_a", "mc_0123456789abcdef", "s1")

    with tenant_scope("tn_a"):
        store.append(Turn(thread_id=src, index=0, input=InputCheck(text="appelle ⟦PHONE-1⟧"),
                          reply="J'appelle ⟦PHONE-1⟧."))
        out = seed_thread(hub=Hub(), store=store, sender="us_a", tenant="tn_a", owner="us_a",
                          body={"seed_from": {"machine_id": "mc_0123456789abcdef", "thread_id": "s1"}})
        turns = store.history(out["thread_id"])
    assert turns and all("⟦" not in t.input.text + t.reply for t in turns)
    assert turns[0].input.text == "appelle [valeur masquée]"
    assert "⟦" not in threads.require(out["thread_id"]).last_snippet
