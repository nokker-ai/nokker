"""Veille du foyer côté noyau (docs/HOME-WATCH.md) contre la passerelle réelle et de faux appareils : incident
dangereux notifié, aucune image vers le modèle, actions refusées sans carte forte, droits par membre, cloisonnement
entre locataires, drapeau éteint identique."""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.core.loop import Kernel
from nokker.core.ports import Assembled, Completion, ToolContext
from nokker.core.turn import (
    Decision,
    Effect,
    Gate,
    Message,
    Tier,
    ToolCall,
    Turn,
    TurnInput,
    Usage,
    Verdict,
)
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.homewatch.fakes import BRIDGE_TOKEN, INJECTION, PAUSE_POLICY, Foyer

ADULT, TEEN, GUEST = "alice", "bob", "carol@example.org"
OTHER_TENANT = "tn_0123456789ab"


class FakeHub:
    def __init__(self):
        self.notified, self.bubbles = [], {}

    def notify_user(self, user, *, title, text, from_thread=None):
        self.notified.append((user, title, text))

    def create_kernel_bubble(self, thread_id, *, kind, title="", state=None, status="idle", actions=None):
        b = SimpleNamespace(id=f"bu_{len(self.bubbles):012x}", thread_id=thread_id, kind=kind, title=title,
                            state=state or {}, status=status)
        self.bubbles[b.id] = b
        return b

    def get_bubble(self, bid, user):
        from nokker.bubbles.types import BubbleNotFound

        b = self.bubbles.get(bid)
        if b is None or user not in (ADULT, TEEN, GUEST):
            raise BubbleNotFound(bid)
        return b, False


@pytest.fixture
def foyer(tmp_path):
    f = Foyer(tmp_path)
    yield f
    f.close()


def make_source(foyer, tmp_path, **extra):
    from nokker.homewatch.source import HomeWatchSource

    raw = {"enabled": True, "bridge_url": foyer.api_url, "media_base_url": foyer.media_url, "persist": False,
           "members": {ADULT: "adult", TEEN: "teen"}, **extra}
    src = HomeWatchSource.from_config(raw, token=BRIDGE_TOKEN, state_dir=tmp_path)
    src.hub = FakeHub()
    src.pushed = []
    src.push = lambda m, t, x: src.pushed.append((m, t, x))
    return src


def mkturn(sender):
    from nokker.core.turn import InputCheck

    return Turn(thread_id="fil-1", index=0, input=InputCheck(text="x"), sender=sender)


def decide(approvals, card, via):
    """Pose la décision dans le magasin (la vérification de la preuve d'appareil est testée dans tests/unit/approvals)."""
    stored = approvals._approvals[card.id]
    stored.status, stored.decided_via, stored.decided_by = "approved", via, ADULT


def ctx(sender=ADULT, approval_id=None):
    return ToolContext(thread_id="fil-1", turn_index=0, channel="api", sender=sender, approval_id=approval_id)


# ── 1. incident dangereux → notification immédiate ; le reste → brief ──

def test_dangerous_incident_notifies_household_once(foyer, tmp_path):
    src = make_source(foyer, tmp_path)
    with tenant_scope(INTERNAL_TENANT):
        fresh = src.poll_cameras()
        assert [r["id"] for r in fresh] == ["1730500000.1-abc"]
        assert [n[0] for n in src.hub.notified] == [ADULT]  # l'ado et l'invité n'ont pas le droit `cameras`
        user, title, text = src.hub.notified[0]
        assert title == "Caméra « portail » : incident dangereux"
        assert "Personne inconnue force le portail" in text and "vignette dans Nokker" in text
        assert [p[0] for p in src.pushed] == [ADULT]
        for needle in ("http", "/media", "webp", "sig=", "RIFF"):
            assert needle not in text
        assert src.poll_cameras() == [] and len(src.hub.notified) == 1  # déjà notifié : une seule fois
        brief = src.brief_text(ADULT)
    assert "dangereux 1, déjà notifiés ; suspect 1 ; normal 1 ; non résumé 1" in brief
    assert "Livreur dépose un colis" in brief and "<untrusted-data kind=\"home-watch-brief\"" in brief
    assert src.brief_text(TEEN) is None and src.brief_text(GUEST) is None


def test_brief_data_carries_the_watch_only_when_hooked():
    from nokker.plan.service import Plan
    from nokker.plan.store import MemoryPlan

    with tenant_scope(INTERNAL_TENANT):
        p = Plan(store=MemoryPlan(), clock=lambda: 1730500000.0)
        base = p.brief_data(ADULT)
        assert "maison_veille" not in base
        p.home_watch = lambda sender: "[Veille du foyer]" if sender == ADULT else None
        assert p.brief_data(ADULT) == {**base, "maison_veille": "[Veille du foyer]"}
        assert p.brief_data(TEEN) == base
        p.home_watch = lambda sender: 1 / 0  # une veille en panne ne casse pas le brief
        assert p.brief_data(ADULT) == base


# ── 2. aucune image envoyée au modèle (tour complet, faux modèle qui enregistre tout) ──

PREFIX = "Tu es Atlas."


class Recorder:
    def __init__(self, script):
        self.script, self.seen, self.model = list(script), [], "faux"

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(json.dumps(messages, ensure_ascii=False, default=str))
        return Completion(message=self.script.pop(0), usage=Usage(prompt=10), model="faux", latency_s=0.0)


class Ctx:
    def assemble(self, turn, history, recall, loaded, tier):
        msgs = [{"role": "system", "content": PREFIX}, {"role": "user", "content": turn.input.text}]
        msgs += [m.to_openai() for m in turn.messages]
        return Assembled(messages=msgs, tools=[], prefix_sha256=hashlib.sha256(PREFIX.encode()).hexdigest())

    def admit_result(self, r):
        return r


class Inv:
    def check_input(self, text):
        from nokker.core.turn import InputCheck

        return InputCheck(text=text)

    def check_action(self, call, spec, turn):
        eff = spec.effect if spec else Effect.WRITE
        if eff is Effect.DESTRUCTIVE:
            return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                           reason="sans GO", strength="strong")
        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=f"effect:{eff.value}")

    def redact_result(self, result, spec):
        return result

    def scrub_output(self, text):
        return text


class Gates:
    def __init__(self):
        self.inv = Inv()

    def action(self, call, spec, turn):
        return [self.inv.check_action(call, spec, turn)]

    def switch(self, reply, turn, tier):
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]


class Tools:
    def __init__(self, src):
        self.src, self.called = src, []

    def core(self):
        return []

    def spec(self, n):
        return next((s for s in self.src.specs() if s.name == n), None)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, c):
        self.called.append(n)
        return self.src.call(n, a, c)


class Store:
    def __init__(self):
        self.turns = []

    def history(self, t, limit=None):
        return [x for x in self.turns if x.thread_id == t]

    def next_index(self, t):
        return len(self.history(t))

    def append(self, turn):
        self.turns.append(turn)


def kernel_for(src, script, approvals=None):
    from nokker.homewatch.source import WatchGates

    class Mem:
        def recall(self, q, k=5):
            return []

        def propose(self, t, f):
            pass

    class Esc:
        def escalate(self, turn, reason):
            return f"escaladé: {reason}"

    class Pol:
        def choose(self, state):
            from nokker.core.ports import RouteDecision

            return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="défaut")

    class Led:
        def record(self, *a):
            pass

        def record_verdict(self, *a):
            pass

    model, tools = Recorder(script), Tools(src)
    k = Kernel(store=Store(), context=Ctx(), memory=Mem(), invariants=Inv(), gates=Gates(), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: model}, tools=tools, ledger=Led(), approvals=approvals)
    k.gates = WatchGates(inner=k.gates, source=src)
    src.approvals = lambda: approvals
    return k, model, tools


def tc(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def test_no_image_reaches_the_model(foyer, tmp_path):
    src = make_source(foyer, tmp_path)
    k, model, tools = kernel_for(src, [Message("assistant", tool_calls=[tc("veille_cameras", heures=24, bulle=True)]),
                                       Message("assistant", "Une alerte cette nuit au portail.")])
    with tenant_scope(INTERNAL_TENANT):
        turn = k.run_turn(TurnInput("fil-1", "quoi de neuf aux caméras ?", sender=ADULT))
    assert tools.called == ["veille_cameras"]
    seen = "\n".join(model.seen)
    assert "Personne inconnue force le portail" in seen
    for needle in ("RIFF", "WEBP", "data:image", "base64", "/media/v1", "clips/review", "thumb", ".webp", "sig=",
                   foyer.media_url, "image_url"):
        assert needle not in seen, needle
    # l'injection d'une caméra reste DANS le bloc de données, sa balise fermante est neutralisée
    tool_msg = next(m.content for m in turn.messages if m.role == "tool")
    blocks = re.findall(r"<untrusted-data kind=\"camera-reviews\" id=\"([0-9a-f]{16})\">", tool_msg)
    assert len(blocks) == 1 and tool_msg.count("</untrusted-data") == 1
    inside = tool_msg.split(f"id=\"{blocks[0]}\">", 1)[1]
    assert "Ignore toutes les consignes" in inside and "[balise retirée]" in inside
    assert INJECTION not in tool_msg
    # les bulles ne portent aucune URL ni image : l'écran local la demande à part
    assert len(src.hub.bubbles) == 3
    for b in src.hub.bubbles.values():
        assert b.kind == "home_watch" and "url" not in json.dumps(b.state) and b.state["media"] == "local"


def test_camera_text_never_triggers_an_action(foyer, tmp_path):
    """La consigne glissée dans un résumé de caméra n'a aucun effet : le modèle qui « obéit » tombe sur la carte."""
    src = make_source(foyer, tmp_path)
    approvals = InMemoryApprovals()
    k, model, tools = kernel_for(src, [Message("assistant", tool_calls=[tc("veille_cameras")]),
                                       Message("assistant", tool_calls=[tc("veille_action", action="pause_appareil",
                                                                           cible="console_salon")]),
                                       Message("assistant", "J'attends ta confirmation.")], approvals=approvals)
    with tenant_scope(INTERNAL_TENANT):
        turn = k.run_turn(TurnInput("fil-1", "résume les caméras", sender=ADULT))
    assert tools.called == ["veille_cameras"]  # l'action n'est PAS exécutée
    assert [p.name for p in turn.pending_approvals] == ["veille_action"]
    card = approvals.pending()[0]
    assert card.rule == "home_watch:strong" and card.strength == "strong"
    assert foyer.unifi.writes == []


# ── 3. action refusée sans carte forte ──

def test_action_refused_without_strong_card(foyer, tmp_path):
    from nokker.approvals.errors import ProofInvalid

    src = make_source(foyer, tmp_path)
    approvals = InMemoryApprovals()
    src.approvals = lambda: approvals
    args = {"action": "pause_appareil", "cible": "console_salon"}
    with tenant_scope(INTERNAL_TENANT):
        r = src.call("veille_action", args, ctx())  # appel direct, sans carte
        assert r.is_error and "carte forte" in r.content
        # porte : carte forte pour l'adulte, refus pour l'ado et l'invité, refus pour une cible non déclarée
        turn = mkturn(ADULT)
        v = src.verdict(tc("veille_action", **args), turn)
        assert (v.decision, v.rule, v.strength) == (Decision.HITL, "home_watch:strong", "strong")
        for who in (TEEN, GUEST):
            assert src.verdict(tc("veille_action", **args), mkturn(who)).decision is Decision.DENY
        assert src.verdict(tc("veille_action", action="pause_appareil", cible="routeur"), turn).decision \
            is Decision.DENY
        assert src.verdict(tc("veille_action", action="ouvrir_ports", cible="console_salon"), turn).decision \
            is Decision.DENY
        # carte posée, puis approuvée au jeton local : refusé par le backend ET par l'outil
        card = approvals.request(turn, tc("veille_action", **args), v, None)
        with pytest.raises(ProofInvalid, match="clé d'appareil"):
            approvals.decide_local(card.id, "approve", ADULT)
        decide(approvals, card, "local-token")
        r = src.call("veille_action", args, ctx(approval_id=card.id))
        assert r.is_error and "preuve insuffisante" in r.content and foyer.unifi.writes == []
        # arguments modifiés après la carte : refusé
        decide(approvals, card, "webauthn")
        r = src.call("veille_action", {**args, "cible": "tablette"}, ctx(approval_id=card.id))
        assert r.is_error and "arguments modifiés" in r.content
        # décideur sans droit : refusé
        assert src.call("veille_action", args, ctx(sender=TEEN, approval_id=card.id)).is_error
        # carte forte approuvée sur un appareil, par un adulte : exécuté
        r = src.call("veille_action", args, ctx(approval_id=card.id))
    assert not r.is_error and r.content == "[Veille] fait : mise en pause « console_salon »."
    assert foyer.unifi.policies[PAUSE_POLICY]["enabled"] is True and len(foyer.unifi.writes) == 1


def test_live_feed_needs_strong_card_and_stays_local(foyer, tmp_path):
    from nokker.homewatch import http as hw_http

    src = make_source(foyer, tmp_path)
    approvals = InMemoryApprovals()
    src.approvals = lambda: approvals
    with tenant_scope(INTERNAL_TENANT):
        turn = mkturn(ADULT)
        assert src.verdict(tc("veille_direct", camera="jardin"), turn).decision is Decision.DENY  # hors liste
        v = src.verdict(tc("veille_direct", camera="portail"), turn)
        assert v.decision is Decision.HITL and v.strength == "strong"
        assert src.call("veille_direct", {"camera": "portail"}, ctx()).is_error
        card = approvals.request(turn, tc("veille_direct", camera="portail"), v, None)
        decide(approvals, card, "telegram-attested")
        r = src.call("veille_direct", {"camera": "portail"}, ctx(approval_id=card.id))
    assert not r.is_error and "Aucune image n'est transmise ici" in r.content and "http" not in r.content
    bid = next(iter(src.hub.bubbles))
    # l'écran local demande l'URL signée : adulte oui, ado non ; autre locataire : 404
    hw_http.configure(src, src.hub)
    try:
        out = _route(bid, ADULT, INTERNAL_TENANT)
        assert out[0] == 200 and out[1]["url"].startswith(foyer.media_url + "/media/v1/live/portail?exp=")
        assert _route(bid, TEEN, INTERNAL_TENANT)[0] == 403
        assert _route(bid, ADULT, OTHER_TENANT)[0] == 404
        assert _route("bu_inconnu", ADULT, INTERNAL_TENANT)[0] == 404
    finally:
        hw_http.configure(None)


def _route(bid, sender, tenant):
    from nokker.homewatch import http as hw_http

    out = []
    h = SimpleNamespace(path=f"/v1/home-watch/media?bubble={bid}", _resolve_identity=lambda body: sender,
                        _tenant_for=lambda s: tenant, _json=lambda status, obj: out.append((status, obj)))
    assert hw_http.handle(h, "GET", "/v1/home-watch/media")
    return out[0]


# ── 4. cloisonnement : membres et locataires ──

def test_rights_per_member(foyer, tmp_path):
    src = make_source(foyer, tmp_path)
    with tenant_scope(INTERNAL_TENANT):
        for tool in ("veille_cameras", "veille_reseau", "veille_nas"):
            t = mkturn(TEEN)
            assert src.verdict(tc(tool), t).decision is Decision.DENY
            assert src.call(tool, {}, ctx(sender=GUEST)).is_error
        r = src.call("veille_reseau", {}, ctx())
        assert not r.is_error and "2 appareil(s) connecté(s)" in r.content and "aa:bb:cc:…:02" in r.content
        assert "aa:bb:cc:00:00:02" not in r.content  # MAC masquée vers le modèle
        assert "console_salon" in r.content
    # profil mineur d'un foyer : rôle teen même déclaré adulte
    src.policy.profile_of = lambda s: "teen" if s == ADULT else None
    with tenant_scope(INTERNAL_TENANT):
        assert src.call("veille_cameras", {}, ctx()).is_error


def test_tenant_isolation(foyer, tmp_path):
    src = make_source(foyer, tmp_path)
    with tenant_scope(INTERNAL_TENANT):
        src.poll_cameras()
    with tenant_scope(OTHER_TENANT):
        t = mkturn(ADULT)
        for tool in ("veille_cameras", "veille_reseau", "veille_nas", "veille_direct", "veille_action"):
            assert src.verdict(tc(tool, camera="portail", action="pause_appareil", cible="console_salon"), t).decision \
                is Decision.DENY
            r = src.call(tool, {"camera": "portail"}, ctx())
            assert r.is_error and "Personne inconnue" not in r.content
        assert src.brief_text(ADULT) is None
    assert src.store.reviews(OTHER_TENANT, 0) == []
    assert src.store.tenants() == [INTERNAL_TENANT]


def test_new_device_detection_goes_to_brief(foyer, tmp_path):
    src = make_source(foyer, tmp_path)
    with tenant_scope(INTERNAL_TENANT):
        src.poll_slow()  # premier passage = référence
        assert src.store.new_devices(INTERNAL_TENANT, 0) == []
        foyer.unifi.clients.append({"type": "WIRELESS", "id": "c3", "name": "Galaxy inconnu",
                                    "connectedAt": "2026-11-02T08:01:00Z", "ipAddress": "192.168.1.53",
                                    "macAddress": "de:ad:be:ef:00:03", "uplinkDeviceId": "u1",
                                    "access": {"type": "DEFAULT"}})
        src.poll_slow()
        brief = src.brief_text(ADULT)
        r = src.call("veille_reseau", {"inconnus_seulement": True}, ctx())
        nas = src.call("veille_nas", {}, ctx())
    assert "1 nouvel(s) appareil(s) inconnu(s) : Galaxy inconnu (de:ad:be:…:03)" in brief
    assert "Disque 2 normal smart=failing" in brief and "sauvegarde « Photos »" in brief
    assert src.hub.notified == []  # `notify_new_devices` éteint par défaut : brief seulement
    assert "NOUVEAU | Galaxy inconnu" in r.content and "Tablette Alice" not in r.content
    assert "dernière réussie" in nas.content and "photos" in nas.content


# ── 5. drapeau éteint : rien n'est construit ──

def test_flag_off_builds_nothing(monkeypatch):
    from nokker.api.server import _home_watch_source
    from nokker.config import NokkerConfig
    from nokker.homewatch import http as hw_http
    from nokker.homewatch.source import home_watch_source

    monkeypatch.setenv("NOKKER_HOME_BRIDGE_TOKEN", BRIDGE_TOKEN)
    cfg = NokkerConfig()
    assert cfg.home_watch == {}
    assert home_watch_source(cfg, token=BRIDGE_TOKEN) is None
    assert _home_watch_source(cfg, local_api=object()) is None
    cfg.home_watch = {"enabled": True}
    assert _home_watch_source(cfg, local_api=None) is None  # hors Lokkal : jamais
    h = SimpleNamespace(path="/v1/home-watch/media")
    assert hw_http.handle(h, "GET", "/v1/home-watch/media") is False  # route absente (404 du serveur)


def test_flag_on_without_token_stays_off(monkeypatch):
    from nokker.api.server import _home_watch_source
    from nokker.config import NokkerConfig

    monkeypatch.delenv("NOKKER_HOME_BRIDGE_TOKEN", raising=False)
    cfg = NokkerConfig()
    cfg.home_watch = {"enabled": True}
    assert _home_watch_source(cfg, local_api=SimpleNamespace(state_dir=None)) is None


def test_media_signature_matches_bridge():
    from nokker.homebridge.watch import media_key, media_sig
    from nokker.homewatch.client import BridgeClient

    c = BridgeClient(base_url="http://home-bridge:8800", token=BRIDGE_TOKEN, media_base_url="http://10.0.0.2:8801",
                     clock=lambda: 1000)
    url, exp = c.media_url("thumb", "1730500000.1-abc")
    assert url.endswith(f"sig={media_sig(media_key(BRIDGE_TOKEN), '/media/v1/thumb/1730500000.1-abc', exp)}")
