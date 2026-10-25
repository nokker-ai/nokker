"""Chaque chemin vers le modèle passe par le caviardage (lot fix-batch-1, défaut 1).

Chemins avec une porte à eux, déjà couverts ailleurs : message (`check_input`), résultat d'outil (`redact_result`),
rappel mémoire et contexte reçu (`scrub_output`), nom d'image, deltas X22 (`LiveToolInputs`, test_live_input.py).
Ici : ce qui n'en avait pas — bloc système (plan, mémoire de projet), arguments d'outils rejoués, et tout ce que le
juge envoie (diff relu, arguments d'une carte). Jetons morcelés et factices.
"""
from __future__ import annotations

import hashlib
import json
import re

from nokker.core.loop import Kernel
from nokker.core.ports import Assembled
from nokker.core.turn import InputCheck, Message, Tier, ToolCall, Turn, TurnInput
from nokker.qa.gates import GatesV0
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.judge import HttpJudgeTransport
from nokker.qa.review import CardAdvisor, DiffReviewer
from nokker.qa.secrets import KnownSecrets
from tests.unit.test_loop_contract import Esc, LedgerFake, Mem, Pol, ScriptedProvider, StoreFake, Tools

AKIA = "AKIA" + "Z7Q3VNXW" + "5LPR2K4M"
AWS_SECRET = "wJal8rXUtn3F" + "EMI9K7MDENGb" + "PxRfiCYz4kQm2vT6"
ROUTER_KEY_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:api[_-]?key|secret|token|passwd|password)['\"]?\s*[:=]\s*['\"]?"
    r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{16,}")


class PlanCtx:
    """Contexte avec un bloc système qui n'a pas de porte de caviardage (consignes du plan)."""

    def assemble(self, turn, history, recall, loaded, tier):
        system = f"Tu es Atlas.\n\n[Contexte du plan]\nidentifiant de déploiement {AKIA}\n[/Contexte du plan]"
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": turn.input.text}]
        msgs += [m.to_openai() for m in turn.messages]
        return Assembled(messages=msgs, tools=[], prefix_sha256=hashlib.sha256(b"p").hexdigest())

    def admit_result(self, r):
        return r


def wire_kernel(script, known=None):
    inv = DeterministicInvariants(known=known or KnownSecrets())
    ship = ScriptedProvider(script)
    k = Kernel(store=StoreFake(), context=PlanCtx(), memory=Mem(), invariants=inv, gates=GatesV0(invariants=inv),
               escalator=Esc(), policy=Pol(), providers={Tier.SHIP: ship}, tools=Tools(), ledger=LedgerFake())
    return k, ship


def test_system_block_and_user_message_leave_redacted_and_handles_do_not_trip_the_router():
    k, ship = wire_kernel([Message("assistant", "noté")])
    turn = k.run_turn(TurnInput("t1", f"ma clé : {AKIA} / secret {AWS_SECRET}, range-la"))
    sent = json.dumps(ship.seen[0], ensure_ascii=False)
    assert AKIA not in sent and AWS_SECRET not in sent
    assert "[secret:aws-access-key-" in sent
    for m in ship.seen[0]:
        if m["role"] in ("system", "user"):  # rôles que le Router refuse (au lieu de masquer) s'il y voit un secret
            assert not ROUTER_KEY_VALUE.search(m["content"]), m["content"]
    assert turn.reply == "noté"


def test_tool_arguments_replayed_to_the_model_are_redacted_but_the_turn_keeps_them():
    """Le modèle a écrit une clé d'exemple dans un fichier : son appel repart à l'appel suivant (rôle assistant)."""
    call = ToolCall(id="c1", name="calendar_list", arguments={"note": f"KEY_ID = {AKIA}"})
    k, ship = wire_kernel([Message("assistant", tool_calls=[call]), Message("assistant", "fait")])
    turn = k.run_turn(TurnInput("t1", "note la config"))
    assert AKIA not in json.dumps(ship.seen[1], ensure_ascii=False)
    assert turn.messages[0].tool_calls[0].arguments["note"].endswith(AKIA)  # le tour garde ce qui a été fait


def test_known_value_in_system_block_is_redacted_on_the_wire():
    ks = KnownSecrets()
    ks.add("deploy_key", "Xk9!qpzT42Lm")

    class Ctx(PlanCtx):
        def assemble(self, turn, history, recall, loaded, tier):
            a = super().assemble(turn, history, recall, loaded, tier)
            a.messages[0]["content"] += "\nmot de passe du NAS : Xk9!qpzT42Lm"
            return a

    k, ship = wire_kernel([Message("assistant", "ok")], known=ks)
    k.context = Ctx()
    k.run_turn(TurnInput("t1", "bonjour"))
    assert "Xk9!qpzT42Lm" not in ship.seen[0][0]["content"] and "[secret:deploy_key]" in ship.seen[0][0]["content"]


class FakeHttp:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, headers=None):  # noqa: A002 — même signature que httpx
        self.sent.append(json)

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "OK"}}], "model": "juge", "usage": {}}

        return R()


def transport():
    t = HttpJudgeTransport(base_url="http://juge.invalid/v1", model="juge", retry_wait_s=0)
    t._client = FakeHttp()
    return t


def test_reviewer_diff_built_from_model_arguments_is_redacted_before_the_judge():
    t = transport()
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="ajoute un test du détecteur"))
    call = ToolCall(id="w1", name="write_file",
                    arguments={"path": "tests/test_scan.py", "content": f'FAKE = "{AKIA}"\nSECRET = "{AWS_SECRET}"\n'})
    turn.messages = [Message(role="assistant", tool_calls=[call]),
                     Message(role="tool", content='{"ok": true}', tool_call_id="w1", name="write_file")]
    res = DiffReviewer(transport=t).review(turn)
    assert res is not None and res.ok
    sent = json.dumps(t._client.sent, ensure_ascii=False)
    assert AKIA not in sent and AWS_SECRET not in sent and "tests/test_scan.py" in sent


def test_card_advice_arguments_are_redacted_before_the_judge():
    t = transport()
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="envoie la clé"))
    CardAdvisor(transport=t).advise(turn, "mail_send", {"to": "x@example.com", "body": AKIA}, "envoi externe",
                                    "strong")
    assert AKIA not in json.dumps(t._client.sent, ensure_ascii=False)
