"""Classe d'un appel relayé qui dépend de ses ARGUMENTS (revue du train du 2.11, §2 : « `mail_move` vers la corbeille y
passe en écriture interne »).

Côté noyau, `mail_move` vers la corbeille est DESTRUCTIVE (`tools/effects.py`, carnet d'incidents B2) ; côté nokker.ai,
la classe du relais (`TOOL_CLASSES`) ne regardait que les invités. Règle : la classe d'un appel relayé n'est JAMAIS plus
faible que l'effet que le noyau local donnerait au même appel, et la machine pose la même carte (forte pour la corbeille).
Aucun réseau externe : faux connecteurs et faux IMAP."""
from __future__ import annotations

import json

import pytest

from nokker.cloud.relay import _RANK, CLASS_EFFECT, ToolClass, call_class, tool_class, tool_view
from nokker.cloud.summary import summarize
from nokker.connectors import build_connector
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, ToolCall
from nokker.local.relayed import RelayedConnectors
from nokker.tools.effects import ARGUMENT_EFFECTS, DEFAULT_EFFECTS, EffectsTable
from tests.unit.test_relayed_connectors import FakeMail
from tests.unit.test_relayed_strong import world  # noqa: F401 — même monde que les cartes fortes

TRASH_DESTINATIONS = ["label:TRASH", "label:Trash", "label:trash", "label:Corbeille", "label:[Gmail]/Trash",
                      "label:[Gmail]/Corbeille", "label:Deleted Items", "label:Deleted Messages",
                      "label:Éléments supprimés", "label:INBOX.Trash", "label:Bin", " label: Trash "]
KEPT = ["archive", "spam", "inbox", "label:Factures", "label:Trash-notes", "label:Clients/Corbeille à papier"]


def _specs():
    mail = build_connector("mail", {"preset": "gmail", "address": "a@example.com"}, "pw")
    cal = build_connector("calendar", {"mode": "caldav", "url": "https://cal.example.com/dav/", "username": "a"}, "pw")
    return {s.name: s for s in [*mail.specs(), *cal.specs()]}


@pytest.mark.parametrize("dest", TRASH_DESTINATIONS)
def test_mail_move_to_the_trash_is_destructive_on_both_sides(dest):
    move = _specs()["mail_move"]
    args = {"message_id": "INBOX:7:1", "destination": dest}
    assert ARGUMENT_EFFECTS["mail_move"](args) is Effect.DESTRUCTIVE  # le noyau local
    assert move.effect_for(args) is Effect.DESTRUCTIVE  # le connecteur lui-même (bêta, web)
    assert tool_class(move, args) is ToolClass.DESTRUCTIVE  # la porte de nokker.ai
    assert call_class("mail_move", args) is ToolClass.DESTRUCTIVE  # le résumé de la page de décision
    assert summarize("mail_move", args)["headline"] == "Mettre un message à la corbeille"


@pytest.mark.parametrize("dest", KEPT)
def test_mail_move_elsewhere_stays_an_internal_write(dest):
    move = _specs()["mail_move"]
    args = {"message_id": "INBOX:7:1", "destination": dest}
    assert move.effect_for(args) is Effect.WRITE
    assert tool_class(move, args) is ToolClass.INTERNAL_WRITE
    assert call_class("mail_move", args) is ToolClass.INTERNAL_WRITE


def test_relay_class_is_never_weaker_than_the_local_effect():
    """Pour chaque outil de connecteur et un jeu d'arguments qui fait varier l'effet local (`ARGUMENT_EFFECTS`), la
    classe de nokker.ai est au moins celle de l'effet du noyau local (table + argument), et du résumé affiché."""
    rank = {Effect.READ: 0, Effect.WRITE: 1, Effect.SEND_EXTERNAL: 2, Effect.DESTRUCTIVE: 3}
    table = EffectsTable.load()
    samples = [{}, {"attendees": ["anne@example.com"]}, {"accept": True},
               *({"destination": d} for d in TRASH_DESTINATIONS + KEPT)]
    for name, spec in _specs().items():
        local = table.annotate(spec)
        for args in samples:
            cls = tool_class(spec, args)
            assert cls is not None, name
            floor = max(rank[local.effect_for(args)], rank[spec.effect_for(args)])
            assert rank[CLASS_EFFECT[cls]] >= floor, (name, args, cls)
            assert _RANK[call_class(name, args)] >= floor, (name, args)


def test_unannotated_spec_still_gets_the_argument_floor():
    """Un connecteur (ou une version) dont le `ToolSpec` ne classe pas ses appels : la table d'effets par argument du
    noyau s'applique quand même chez nokker.ai."""
    bare = ToolSpec(name="mail_move", description="", parameters={}, effect=Effect.WRITE)
    assert tool_class(bare, {"destination": "label:TRASH"}) is ToolClass.DESTRUCTIVE
    assert tool_class(bare, {"destination": "archive"}) is ToolClass.INTERNAL_WRITE


def test_machine_makes_a_strong_card_for_a_move_to_the_trash():
    from tests.unit.test_relayed_connectors import _gates, _turn

    src = RelayedConnectors(base_url="http://127.0.0.1:9", token=lambda: None)
    view = tool_view(_specs()["mail_move"])
    specs, _, _ = src._parse({"connectors": [{"id": "mail", "tools": [view]}]})
    [relayed] = specs
    assert relayed.effect_for({"destination": "archive"}) is Effect.WRITE
    assert relayed.effect_for({"destination": "label:TRASH"}) is Effect.DESTRUCTIVE
    gates = _gates("auto")
    [own] = gates.action(ToolCall(id="c", name="mail_move", arguments={"message_id": "m", "destination": "archive"}),
                         relayed, _turn())
    [trash] = gates.action(ToolCall(id="c", name="mail_move",
                                    arguments={"message_id": "m", "destination": "label:TRASH"}), relayed, _turn())
    assert (own.strength, trash.strength) == ("standard", "strong")


class _MoveMail(FakeMail):
    """Le faux connecteur des tests du relais, avec `mail_move` tel que le déclare le vrai (`connectors/mail.py`)."""

    def specs(self):
        real = _specs()["mail_move"]
        return [*super().specs(), ToolSpec(name=real.name, description=real.description, parameters=real.parameters,
                                           effect=real.effect, effect_of=real.effect_of, source="connector:mail",
                                           locality=self.locality)]


def test_relayed_move_to_the_trash_needs_the_phone_signed_card(world):  # noqa: F811
    """Bout en bout : une carte LOCALE approuvée (preuve non signée) suffit pour ranger, pas pour la corbeille."""
    from nokker.approvals.crypto import args_sha256

    w = world
    w.relay.build = lambda kind, settings, secret, locality=None: _MoveMail(settings, secret, locality)
    FakeMail.calls = []
    for dest, expected in (("label:TRASH", 403), ("label:[Gmail]/Corbeille", 403), ("archive", 200)):
        args = {"message_id": "INBOX:7:1", "destination": dest}
        approval = {"id": f"ap_{dest[-5:].strip(':/[]').lower() or 'x'}_{expected}", "args_sha256": args_sha256(args),
                    "decided_via": "local-token", "decided_by": "local-owner", "strength": "standard"}
        status, body = w.http("POST", "/v1/gateway/connectors/mail/call",
                              {"tool": "mail_move", "arguments": args, "approval": approval}, token=w.mt)
        assert status == expected, (dest, body)
        if expected == 403:
            assert body["error"]["code"] == "strong_approval_required" and "téléphone" in body["error"]["message"]
    assert [c[1]["destination"] for c in FakeMail.calls] == ["archive"]  # la corbeille n'a pas été atteinte


def test_imap_connector_refuses_a_move_into_its_trash_folder():
    """Filet du connecteur : un nom de corbeille que la table ne connaît pas (`Kosz` en polonais, marqué `\\Trash` par le
    serveur) n'est pas une corbeille « par rangement » : `mail_move` refuse et renvoie vers `mail_trash` (carte forte)."""
    from tests.unit.connectors.test_mail import FakeIMAP, conn, run

    imap = FakeIMAP()
    imap.folders["Kosz"] = imap.folders.pop("[Gmail]/Trash")
    imap.attrs["Kosz"] = imap.attrs.pop("[Gmail]/Trash")
    c, imap, _ = conn(imap=imap)
    r, out = run(c, "mail_move", message_id="INBOX:7:1", destination="label:Kosz")
    assert r.is_error and "mail_trash" in out and 1 in imap.folders["INBOX"]
    _, out = run(c, "mail_move", message_id="INBOX:7:1", destination="label:Clients")
    assert out["folder"] == "Clients"


def test_view_of_a_classified_tool_stays_serialisable_and_matches_the_effects_table():
    for name, spec in _specs().items():
        if name in DEFAULT_EFFECTS:
            assert spec.effect is DEFAULT_EFFECTS[name], name
        json.dumps(tool_view(spec))  # `effect_of` ne part jamais dans la liste
