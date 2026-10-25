"""Images des tours PRÉCÉDENTS sur un palier sans vision (reste de rc7-fix, docs/LOKKAL.md §7.6).

Un tour avec image part sur un palier qui a la vision (`PolicyV1.vision`). Le tour texte trivial qui suit part sur le
petit palier sans vision : ses images de la fenêtre (jointes aux tours d'avant) ne concernent pas la question ; elles
deviennent la ligne de délestage habituelle (« [image délestée : … jointe au tour i] »), sans `attachment.skipped` ni
verdict `images:not_read`. Seules les images jointes à CE tour, sur un palier sans vision, sont signalées non lues."""
from __future__ import annotations

import json

from nokker.core.loop import Kernel
from nokker.core.turn import Tier, TurnInput
from nokker.images import NO_VISION_NOTE, ImageStore, RawImage, materialize
from nokker.ledger.costs import InMemoryLedger
from nokker.route.policy import PolicyV1
from nokker.store.pg import MemoryStore
from tests.unit.api.test_server import AllowGates, AllowInvariants, EmptyTools, NoEscalator, NullMem
from tests.unit.images.test_images import GIF, Notices, SeeingProvider, _engine, png


def _kernel(tmp_path, obs):
    small, ship = SeeingProvider(vision=False, model="petit-35b"), SeeingProvider(vision=True)
    store = ImageStore(tmp_path / "images")
    policy = PolicyV1(available=lambda: (Tier.SMALL, Tier.SHIP), vision=lambda: (Tier.SHIP,))
    k = Kernel(store=MemoryStore(), context=_engine(tmp_path), memory=NullMem(), invariants=AllowInvariants(),
               gates=AllowGates(), escalator=NoEscalator(), policy=policy,
               providers={Tier.SMALL: small, Tier.SHIP: ship}, tools=EmptyTools(), ledger=InMemoryLedger(),
               images=store, observer=obs)
    return k, small, ship, store


def test_trivial_text_turn_after_an_image_turn_sheds_the_old_image_silently(tmp_path):
    obs = Notices()
    k, small, ship, store = _kernel(tmp_path, obs)
    ref = store.put("th", RawImage(png(), "image/png", "capture.png"))
    first = k.run_turn(TurnInput(thread_id="th", user_text="merci !", images=[ref]))
    # tour avec image : palier qui a la vision, l'image part en entier, rien de signalé
    assert len(ship.seen) == 1 and small.seen == []
    assert ship.seen[0][-1]["content"][1]["type"] == "image_url"
    assert obs.got == [] and not any(v.rule == "images:not_read" for v in first.verdicts)
    # tour texte trivial : petit palier sans vision ; l'image du tour 0 devient une ligne de délestage
    second = k.run_turn(TurnInput(thread_id="th", user_text="merci !"))
    assert len(small.seen) == 1
    sent = json.dumps(small.seen[0], ensure_ascii=False)
    assert "[image délestée : capture.png (image/png" in sent and "jointe au tour 0]" in sent
    assert "non lue" not in sent and "base64" not in sent
    assert obs.got == []                                                   # aucun événement à l'utilisateur
    assert not any(v.rule == "images:not_read" for v in second.verdicts)


def test_current_turn_images_are_still_reported_on_a_tier_without_vision(tmp_path):
    """Pas de palier avec vision servi : l'image de CE tour est signalée (une fois), celle d'avant délestée."""
    obs = Notices()
    k, small, ship, store = _kernel(tmp_path, obs)
    k.providers = {Tier.SMALL: small}
    k.policy = PolicyV1(available=lambda: (Tier.SMALL,), vision=lambda: ())
    old = store.put("th", RawImage(png(), "image/png", "avant.png"))
    k.run_turn(TurnInput(thread_id="th", user_text="regarde", images=[old]))
    assert obs.got == [("th", 1, "no_vision")]
    new = store.put("th", RawImage(GIF, "image/gif", "maintenant.gif"))
    turn = k.run_turn(TurnInput(thread_id="th", user_text="et celle-ci ?", images=[new]))
    assert obs.got[-1] == ("th", 1, "no_vision")                           # seulement l'image de ce tour
    assert [v.rule for v in turn.verdicts].count("images:not_read") == 1
    sent = json.dumps(small.seen[-1], ensure_ascii=False)
    assert "[image délestée : avant.png" in sent and "jointe au tour 0]" in sent
    assert NO_VISION_NOTE[:-1] + " — maintenant.gif]" in sent


def test_materialize_counts_only_the_current_turn():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "avant"},
                                         {"type": "nokker_image", "sha256": "a" * 64, "media_type": "image/png",
                                          "name": "a.png", "turn": 0, "size": 4096}]},
            {"role": "user", "content": [{"type": "text", "text": "maintenant"},
                                         {"type": "nokker_image", "sha256": "b" * 64, "media_type": "image/png",
                                          "name": "b.png", "turn": 1, "size": 1024}]}]
    out, skipped = materialize(msgs, vision=False, blobs=None, thread_id="th", current_turn=1)
    assert skipped == 1
    assert out[0]["content"] == "avant\n\n[image délestée : a.png (image/png, 4 Ko), jointe au tour 0]"
    assert out[1]["content"] == "maintenant\n\n" + NO_VISION_NOTE[:-1] + " — b.png]"
    # sans tour courant connu : comme avant, toute image non lue est comptée
    assert materialize(msgs, vision=False, blobs=None, thread_id="th")[1] == 2
