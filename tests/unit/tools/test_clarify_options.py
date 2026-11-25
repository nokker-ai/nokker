"""Question à choix (docs/NOKKAN-HOST-API.md §1, drapeau `host_clarify_options`) : `clarify` enrichi d'options.

- drapeau éteint : schéma, résultat et tour de `clarify` identiques à avant, aucune route, aucun événement ;
- drapeau allumé : options + « Autre (à préciser) » toujours présente, repli en texte numéroté, événements
  `question.asked` / `question.answered`, routes `/v1/local/questions*` par le VRAI serveur, boutons sur Telegram.
"""
from __future__ import annotations

from nokker.config import NokkerConfig
from nokker.core.ports import ToolContext
from nokker.core.turn import Message, ToolCall, Turn, TurnInput
from nokker.telegram.adapter import TelegramAdapter
from nokker.telegram.pairing import MemoryPairing
from nokker.tenancy import MemoryTenancy
from nokker.tools.clarify import OTHER_LABEL, QuestionBoard, QuestionError, normalize, render_text
from nokker.tools.registry import builtin_core_source
from tests.unit.api.host_api_support import Served, host_kernel
from tests.unit.test_thread_serialization import _wait

# Le schéma de `clarify` tel qu'il est AVANT ce chantier (aae68868), recopié ici à la main : c'est un texte vu par le
# modèle, gelé. Drapeau éteint, il ne bouge pas d'un octet.
LEGACY = {"name": "clarify",
          "description": "Pose une seule question fermée quand il manque une information qui change l'action.",
          "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}
CTX = ToolContext(thread_id="t", turn_index=0, channel="api")


def _clarify(**args):
    return Message("assistant", tool_calls=[ToolCall(id="c1", name="clarify", arguments=args)])


def _spec(src):
    s = next(s for s in src.specs() if s.name == "clarify")
    return {"name": s.name, "description": s.description, "parameters": s.parameters}


# ─────────────── drapeau éteint = inchangé ───────────────


def test_flag_is_off_by_default():
    assert NokkerConfig().host_clarify_options is False


def test_flag_off_schema_and_result_are_the_legacy_ones():
    src = builtin_core_source()
    assert _spec(src) == LEGACY
    assert [s.name for s in src.specs()] == ["clarify", "tool_find", "escalate_to_human"]
    # des options envoyées quand même par le modèle sont ignorées : la question seule, comme avant
    out = src.call("clarify", {"question": " Quel jour ? ", "options": ["lundi", "mardi"]}, CTX)
    assert out.content == "Quel jour ?" and not out.is_error
    assert src.call("clarify", {}, CTX).content == "(question vide)"


def test_flag_off_turn_has_no_question_no_event_no_route(tmp_path):
    k, _ = host_kernel([_clarify(question="Quel jour ?", options=["lundi", "mardi"])])
    assert k.questions is None
    s = Served(tmp_path, k)
    try:
        status, out = s.chat("th_off", "réserve une salle")
        assert status == 200 and out["choices"][0]["message"]["content"] == "Quel jour ?"
        assert "nokker" not in out  # réponse d'avant, sans extension
        assert s.events("question.asked") == []
        assert s.req("/v1/local/questions")[0] == 404
        assert s.req("/v1/local/questions/q_x/answer", body={"choice": "1"})[0] == 404
    finally:
        s.close()


# ─────────────── schéma et repli en texte ───────────────


def test_flag_on_schema_adds_options_and_keeps_name_description_effect():
    on, off = builtin_core_source(clarify_options=True), builtin_core_source()
    spec = _spec(on)
    assert spec["name"] == "clarify" and spec["description"] == LEGACY["description"]
    assert spec["parameters"]["required"] == ["question"]
    assert spec["parameters"]["properties"]["question"] == {"type": "string"}
    opts = spec["parameters"]["properties"]["options"]
    assert opts["type"] == "array" and opts["items"] == {"type": "string"} and opts["maxItems"] == 6
    a, b = (next(x for x in src.specs() if x.name == "clarify") for src in (on, off))
    assert (a.effect, a.core, a.source) == (b.effect, b.core, b.source)
    assert [_spec_of(on, n) for n in ("tool_find", "escalate_to_human")] == \
        [_spec_of(off, n) for n in ("tool_find", "escalate_to_human")]


def _spec_of(src, name):
    return next(s for s in src.specs() if s.name == name)


def test_normalize_bounds_dedupes_and_drops_the_model_own_other():
    q, labels = normalize({"question": "  Quel format ?  ",
                           "options": ["PDF", " pdf ", "Word", "Autre", "other (specify)", "", 7, None, True,
                                       {"label": "Texte"}, "x" * 200]})
    assert q == "Quel format ?"
    assert labels == ["PDF", "Word", "7", "Texte", "x" * 80]
    assert normalize({"question": "q", "options": [str(i) for i in range(20)]})[1] == [str(i) for i in range(6)]
    assert normalize({"question": "q", "options": "lundi"})[1] == []  # pas une liste : aucune option


def test_text_fallback_numbers_the_options_and_always_ends_with_other():
    assert render_text("Quel jour ?", ["lundi", "mardi"]) == "Quel jour ?\n\n1. lundi\n2. mardi\n3. Autre (à préciser)"
    assert render_text("Quel jour ?", []) == "Quel jour ?"  # sans option : la question d'origine
    out = builtin_core_source(clarify_options=True).call("clarify", {"question": "Quel jour ?",
                                                                     "options": ["lundi", "mardi"]}, CTX)
    assert out.content.endswith(f"3. {OTHER_LABEL}") and not out.is_error


# ─────────────── tableau des questions ───────────────


def _board(**kw):
    closed = []
    return QuestionBoard(on_closed=closed.append, **kw), closed


def _ask(board, thread="t1", options=("lundi", "mardi")):
    d = {"question": "Quel jour ?", "options": [{"id": str(i), "label": o} for i, o in enumerate(options, 1)]}
    return board.ask(thread, 0, "c1", d)[0]


def test_board_answer_by_choice_other_and_free_text():
    board, closed = _board()
    q = _ask(board)
    view = q.view()
    assert view["options"] == [{"id": "1", "label": "lundi"}, {"id": "2", "label": "mardi"}]
    assert view["other"] == {"id": "other", "label": "Autre (à préciser)"} and view["status"] == "pending"
    for bad, code in (({"choice": "9"}, "unknown_option"), ({"choice": "other"}, "text_required"), ({}, "text_required"),
                      ({"choice": ["1"]}, "bad_answer"), ({"text": 3}, "bad_answer")):
        try:
            board.answer(q.id, **bad)
            raise AssertionError(bad)
        except QuestionError as e:
            assert e.code == code, bad
    assert q.status == "pending" and closed == []
    done, said = board.answer(q.id, choice="2", by="local-owner")
    assert said == "mardi" and done.answer["choice"] == "2" and done.status == "answered"
    assert closed[-1]["status"] == "answered" and closed[-1]["answer"]["label"] == "mardi"
    try:
        board.answer(q.id, choice="1")
        raise AssertionError("déjà répondue")
    except QuestionError as e:
        assert (e.status, e.code) == (409, "not_pending")
    try:
        board.answer("q_inconnue", choice="1")
        raise AssertionError("inconnue")
    except QuestionError as e:
        assert (e.status, e.code) == (404, "unknown_question")
    q2 = _ask(board, "t2")
    assert board.answer(q2.id, choice="other", text=" jeudi matin ")[1] == "jeudi matin"
    q3 = _ask(board, "t3")
    assert board.answer(q3.id, text="plutôt vendredi")[1] == "plutôt vendredi"
    assert q3.answer["choice"] == "other"


def test_board_one_pending_per_thread_message_closes_and_secrets_are_scrubbed():
    board, closed = _board(scrub=lambda t: t.replace("hunter2", "[secret:pw]"))
    first = _ask(board, options=("hunter2", "autre chose"))
    assert first.options[0]["label"] == "[secret:pw]"
    second = _ask(board)
    assert first.status == "superseded" and closed[-1]["question_id"] == first.id
    assert [q.id for q in board.pending("t1")] == [second.id] and board.pending("t2") == []
    got = board.close_for_turn("t1", "mon mot de passe est hunter2")
    assert got.id == second.id and got.answer == {"choice": "other", "label": OTHER_LABEL,
                                                  "text": "mon mot de passe est [secret:pw]", "by": None,
                                                  "via": "message"}
    assert board.close_for_turn("t1", "encore") is None and board.pending() == []


# ─────────────── boucle + vrai serveur ───────────────


def _on(script):
    k, ship = host_kernel(script, clarify_options=True)
    return k, ship


def test_turn_ends_on_the_question_and_host_gets_it_in_response_and_events(tmp_path):
    k, ship = _on([_clarify(question="Quel jour ?", options=["lundi", "mardi"])])
    s = Served(tmp_path, k)
    k.questions = QuestionBoard(scrub=k.invariants.scrub_output, on_closed=s.hub.question_closed)
    try:
        status, out = s.chat("th_q", "réserve une salle")
        assert status == 200
        assert out["choices"][0]["message"]["content"] == "Quel jour ?\n\n1. lundi\n2. mardi\n3. Autre (à préciser)"
        q = out["nokker"]["question"]
        assert q["question"] == "Quel jour ?" and q["status"] == "pending" and q["thread_id"] == "th_q"
        assert q["options"] == [{"id": "1", "label": "lundi"}, {"id": "2", "label": "mardi"}]
        assert q["other"] == {"id": "other", "label": "Autre (à préciser)"}
        asked = s.events("question.asked")
        assert len(asked) == 1 and asked[0][1] == q
        order = [name for name, _ in s.events()]
        assert order.index("question.asked") < order.index("turn.done")
        # ce que le modèle garde dans le fil : le texte de la réponse, jamais la forme servie aux clients
        turn = k.store.turns[-1]
        assert turn.messages[-1].content == turn.reply and "question_id" not in turn.reply
        assert len(ship.seen) == 1  # un seul appel modèle : la question termine le tour
        status, listed = s.req("/v1/local/questions")
        assert status == 200 and listed == {"questions": [q]}
        assert s.req("/v1/local/questions?thread_id=th_autre")[1] == {"questions": []}
        assert s.req(f"/v1/local/questions/{q['question_id']}")[1] == q
        assert s.req("/v1/local/questions", token=None)[0] == 401
    finally:
        s.close()


def test_answer_route_runs_the_follow_up_turn_with_the_chosen_label(tmp_path):
    k, ship = _on([_clarify(question="Quel jour ?", options=["lundi", "mardi"]), Message("assistant", "Réservé mardi.")])
    s = Served(tmp_path, k)
    k.questions = QuestionBoard(scrub=k.invariants.scrub_output, on_closed=s.hub.question_closed)
    try:
        qid = s.chat("th_a", "réserve une salle")[1]["nokker"]["question"]["question_id"]
        url = f"/v1/local/questions/{qid}/answer"
        assert s.req(url, body={"choice": "2"}, token=None)[0] == 401
        status, err = s.req(url, body={"choice": "7"})
        assert status == 400 and err["error"]["code"] == "unknown_option"
        status, err = s.req(url, body={"choice": "other"})
        assert status == 400 and err["error"]["code"] == "text_required"
        assert s.req("/v1/local/questions/q_inconnue/answer", body={"choice": "1"})[0] == 404
        assert len(ship.seen) == 1  # aucun refus n'a lancé de tour
        status, out = s.req(url, body={"choice": "2"})
        assert status == 202 and out["status"] == "answered" and out["continuation"] in ("running", "queued")
        assert out["answer"]["choice"] == "2" and out["answer"]["label"] == "mardi" and out["answer"]["by"] == "local-owner"
        assert _wait(lambda: len(k.store.turns) == 2 and k.store.turns[-1].reply == "Réservé mardi.")
        follow = k.store.turns[-1]
        assert follow.input.text == "mardi" and follow.sender == "local-owner" and follow.thread_id == "th_a"
        assert ship.seen[1][1] == {"role": "user", "content": "mardi"}  # le modèle reçoit le libellé, rien d'autre
        answered = s.events("question.answered")
        assert len(answered) == 1 and answered[0][1]["answer"]["label"] == "mardi"
        status, err = s.req(url, body={"choice": "1"})
        assert status == 409 and err["error"]["code"] == "not_pending"
        assert s.req("/v1/local/questions")[1] == {"questions": []}
    finally:
        s.close()


def test_free_text_answer_by_route_and_by_plain_message(tmp_path):
    k, ship = _on([_clarify(question="Quel jour ?", options=["lundi", "mardi"]), Message("assistant", "Noté jeudi."),
                   _clarify(question="Quelle heure ?", options=["9 h", "14 h"]), Message("assistant", "Noté 16 h.")])
    s = Served(tmp_path, k)
    k.questions = QuestionBoard(scrub=k.invariants.scrub_output, on_closed=s.hub.question_closed)
    try:
        qid = s.chat("th_f", "réserve une salle")[1]["nokker"]["question"]["question_id"]
        status, out = s.req(f"/v1/local/questions/{qid}/answer", body={"choice": "other", "text": "jeudi"})
        assert status == 202 and out["answer"] == {"choice": "other", "label": "Autre (à préciser)", "text": "jeudi",
                                                   "by": "local-owner", "via": "api"}
        assert _wait(lambda: len(k.store.turns) == 2 and k.store.turns[-1].input.text == "jeudi")
        # deuxième question : la personne répond par un message ordinaire, la question se ferme toute seule
        q2 = s.chat("th_f", "et l'heure ?")[1]["nokker"]["question"]
        assert s.chat("th_f", "plutôt 16 h")[1]["choices"][0]["message"]["content"] == "Noté 16 h."
        closed = [d for _, d in s.events("question.answered") if d["question_id"] == q2["question_id"]]
        assert closed and closed[0]["answer"]["via"] == "message" and closed[0]["answer"]["text"] == "plutôt 16 h"
        assert s.req("/v1/local/questions")[1] == {"questions": []}
    finally:
        s.close()


def test_question_without_options_is_the_legacy_text_with_only_other(tmp_path):
    k, _ = _on([_clarify(question="Pour quel client ?")])
    s = Served(tmp_path, k)
    k.questions = QuestionBoard(on_closed=s.hub.question_closed)
    try:
        out = s.chat("th_n", "prépare le devis")[1]
        assert out["choices"][0]["message"]["content"] == "Pour quel client ?"
        q = out["nokker"]["question"]
        assert q["options"] == [] and q["other"]["id"] == "other"
    finally:
        s.close()


def test_streamed_turn_carries_the_question_in_the_final_chunk(tmp_path):
    k, _ = _on([_clarify(question="Quel jour ?", options=["lundi", "mardi"])])
    s = Served(tmp_path, k)
    k.questions = QuestionBoard(on_closed=s.hub.question_closed)
    try:
        status, chunks = s.chat("th_s", "réserve une salle", stream_tokens=True)
        final = next(c for c in chunks if (c.get("nokker") or {}).get("final"))
        assert status == 200 and final["nokker"]["question"]["options"][1] == {"id": "2", "label": "mardi"}
        assert final["choices"][0]["delta"]["content"].startswith("Quel jour ?")
    finally:
        s.close()


def test_refused_clarify_registers_no_question():
    k, _ = _on([_clarify(question="Quel jour ?", options=["lundi", "mardi"])])
    k.questions = QuestionBoard()

    class Deny:
        def action(self, call, spec, turn):
            from nokker.core.turn import Decision, Gate, Verdict

            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="test:deny")]

        def switch(self, reply, turn, tier):
            return []

    k.gates = Deny()
    turn = k.run_turn(TurnInput("th_r", "réserve une salle"))
    assert turn.question is None and k.questions.pending() == []


# ─────────────── Telegram : boutons ───────────────


class Bot:
    def __init__(self):
        self.sent, self.keyboards, self.acked = [], [], []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def send_keyboard(self, chat_id, text, rows):
        self.keyboards.append((chat_id, text, rows))

    def answer_callback(self, callback_id):
        self.acked.append(callback_id)

    def typing(self, chat_id):
        pass


def _telegram(board):
    tenancy, pairing, bot, turns = MemoryTenancy(), MemoryPairing(), Bot(), []
    account = tenancy.create_account("a@x.ch")
    tenancy.bind_identity(account.account_id, "telegram", "111")

    def run_turn(inp):
        turns.append(inp)
        turn = Turn(thread_id=inp.thread_id, index=len(turns) - 1, input=None, reply=f"réponse à {inp.user_text}")
        if inp.user_text == "réserve":
            q, _ = (board or QuestionBoard()).ask(inp.thread_id, turn.index, "c1", {
                "question": "Quel jour ?", "options": [{"id": "1", "label": "lundi"}, {"id": "2", "label": "mardi"}]})
            turn.question, turn.reply = q.view(), "Quel jour ?\n\n1. lundi\n2. mardi\n3. Autre (à préciser)"
        return turn

    adapter = TelegramAdapter(tenancy=tenancy, pairing=pairing, run_turn=run_turn, bot=bot, questions=board)
    adapter._pool.submit = lambda fn, *a: fn(*a)  # synchrone pour le test
    return adapter, bot, turns


def _msg(text, tg_id=111):
    return {"message": {"chat": {"id": tg_id, "type": "private"}, "from": {"id": tg_id, "language_code": "fr"},
                        "text": text}}


def _press(data, tg_id=111, chat_id=111):
    return {"callback_query": {"id": "cb1", "data": data, "from": {"id": tg_id, "language_code": "fr"},
                               "message": {"chat": {"id": chat_id, "type": "private"}}}}


def test_telegram_without_board_sends_plain_text_and_ignores_presses():
    adapter, bot, turns = _telegram(None)
    adapter.handle_update(_msg("réserve"))
    assert bot.keyboards == [] and bot.sent == [(111, "Quel jour ?\n\n1. lundi\n2. mardi\n3. Autre (à préciser)")]
    adapter.handle_update(_press("q:q_x:1"))
    assert len(turns) == 1 and bot.acked == [] and len(bot.sent) == 1


def test_telegram_buttons_one_per_option_plus_other_and_a_press_answers():
    board = QuestionBoard()
    adapter, bot, turns = _telegram(board)
    adapter.handle_update(_msg("réserve"))
    assert bot.sent == []
    chat, text, rows = bot.keyboards[0]
    qid = board.pending()[0].id
    assert (chat, text) == (111, "Quel jour ?")
    assert rows == [[{"text": "lundi", "callback_data": f"q:{qid}:1"}], [{"text": "mardi", "callback_data": f"q:{qid}:2"}],
                    [{"text": "Autre (à préciser)", "callback_data": f"q:{qid}:other"}]]
    assert all(len(r[0]["callback_data"].encode()) <= 64 for r in rows)  # limite de l'API Bot de Telegram
    adapter.handle_update(_press(f"q:{qid}:2"))
    assert bot.acked == ["cb1"] and turns[-1].user_text == "mardi" and turns[-1].channel == "telegram"
    assert bot.sent[-1] == (111, "réponse à mardi") and board.get(qid).answer["via"] == "telegram"
    adapter.handle_update(_press(f"q:{qid}:1"))  # second appui : la question est close, aucun tour
    assert len(turns) == 2 and bot.sent[-1][1] == "Cette question n'attend plus de réponse."


def test_telegram_other_asks_to_write_and_a_foreign_chat_cannot_answer():
    board = QuestionBoard()
    adapter, bot, turns = _telegram(board)
    adapter.handle_update(_msg("réserve"))
    qid = board.pending()[0].id
    adapter.handle_update(_press(f"q:{qid}:other"))
    assert bot.sent[-1] == (111, "D'accord : écris ta réponse ici.") and len(turns) == 1
    assert board.get(qid).status == "pending"
    adapter.handle_update(_press(f"q:{qid}:1", tg_id=999, chat_id=999))  # id non appairé
    assert len(turns) == 1 and "/start" in bot.sent[-1][1]
    adapter.handle_update(_press(f"q:{qid}:1", tg_id=111, chat_id=222))  # autre chat du même compte : autre fil
    assert len(turns) == 1 and board.get(qid).status == "pending"


def test_telegram_keyboard_refused_falls_back_to_text():
    board = QuestionBoard()
    adapter, bot, _ = _telegram(board)

    def boom(*a):
        raise RuntimeError("400 Bad Request")

    bot.send_keyboard = boom
    adapter.handle_update(_msg("réserve"))
    assert bot.sent == [(111, "Quel jour ?\n\n1. lundi\n2. mardi\n3. Autre (à préciser)")]
