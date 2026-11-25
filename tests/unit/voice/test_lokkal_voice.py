"""Sur Lokkal, la voix de « Ton Nokker » choisie par le titulaire local (`local-owner`) sert aussi à la parole d'une
réponse (`/v1/threads/{id}/messages/{i}/speech/timed`), comme pour un compte nokker.ai et comme la narration du
navigateur. Par le VRAI serveur, jeton local, sur la machine."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from nokker import persona as persona_mod
from nokker.api.server import _persona_owner
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.core.turn import InputCheck, Turn
from nokker.local.api import LOCAL_OWNER
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.threads.memory import InMemoryThreads
from nokker.voice import web as voice_web
from tests.unit.voice.test_avatar_speech import PERSONA, _speech, key, timed  # noqa: F401 — fixtures réutilisées
from tests.unit.voice.test_web_speech import REPLY


def test_persona_owner_rule():
    assert _persona_owner("us_0123456789abcdef", lokkal=False)
    assert _persona_owner(LOCAL_OWNER, lokkal=True)
    assert not _persona_owner(LOCAL_OWNER, lokkal=False)  # sur nokker.ai, « local-owner » n'est personne
    assert not _persona_owner("bob", lokkal=True) and not _persona_owner(None, lokkal=True)


def test_local_owner_reply_speech_uses_the_chosen_voice(timed, tmp_path):  # noqa: F811
    from nokker.api.server import make_handler
    from tests.unit.api.test_server import make_kernel
    from tests.unit.test_local_api import _api

    api = _api(tmp_path)
    voice_web.configure(_speech(timed.url))
    persona_mod.configure(persona_mod.from_config(PERSONA, {}, None))
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda *a: None,
                    known_users=frozenset({LOCAL_OWNER}), spawn=lambda fn: fn())
    kernel = make_kernel()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, hub=hub,
                                                               local_api=api))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        with tenant_scope(INTERNAL_TENANT):
            t = hub.create_thread("Voix", LOCAL_OWNER)
            kernel.store.append(Turn(thread_id=t.id, index=0, input=InputCheck(text="Et la bouilloire ?"),
                                     sender=LOCAL_OWNER, reply=REPLY))
        persona_mod.service().put(LOCAL_OWNER, INTERNAL_TENANT, {"voice_id": "leo"})
        tid = t.id.split(":", 1)[1] if ":" in t.id else t.id
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/threads/{tid}/messages/0/speech/timed",
                                     method="POST", data=json.dumps({"lang": "fr"}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {api.token}"})
        req.add_unredirected_header("Host", "127.0.0.1:8742")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                st = r.status
        except urllib.error.HTTPError as e:
            st = e.code
        assert st == 200
        assert timed.requests[-1]["path"].startswith("/v1/text-to-speech/LeoVoice456/with-timestamps")
    finally:
        httpd.shutdown()
        voice_web.configure(None)
        persona_mod.configure(None)
