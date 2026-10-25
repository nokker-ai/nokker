"""X7 — images dans la demande : validation par les octets, magasin dédupliqué, référence dans le tour, message avec et
sans vision, effacement (docs/LOKKAL.md §7.6)."""
from __future__ import annotations

import base64
import json
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from nokker.config import NokkerConfig, TierConfig, load_config
from nokker.context.engine import ContextEngineV0
from nokker.core.loop import Kernel
from nokker.core.ports import Completion
from nokker.core.turn import ImageRef, InputCheck, Message, Tier, Turn, TurnInput, Usage
from nokker.images import (
    MAX_IMAGE_BYTES, NO_VISION_NOTE, ImageError, ImageStore, RawImage, materialize, parse_attachments,
    sniff_media_type, split_content,
)
from nokker.images.store import thread_key
from nokker.ledger.costs import InMemoryLedger
from nokker.providers.hedged import HedgedProvider
from nokker.providers.sokkan import SokkanProvider
from nokker.store.pg import MemoryStore, turn_from_dict, turn_to_dict
from tests.unit.api.test_server import AllowGates, AllowInvariants, EmptyTools, NoEscalator, NullMem, ShipOnlyPolicy


def png(rgb=(255, 0, 0), w=2, h=2) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 12


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ───────────────────────────── validation ─────────────────────────────

def test_type_is_read_from_the_bytes_not_from_the_name_or_declared_type():
    assert sniff_media_type(png()) == "image/png"
    assert sniff_media_type(JPEG) == "image/jpeg"
    assert sniff_media_type(GIF) == "image/gif"
    assert sniff_media_type(WEBP) == "image/webp"
    # un jpeg nommé .png et déclaré png reste un jpeg
    [img] = parse_attachments([{"data": b64(JPEG), "media_type": "image/png", "name": "capture.png"}])
    assert img.media_type == "image/jpeg" and img.name == "capture.png"
    # un texte nommé .png est refusé
    with pytest.raises(ImageError, match="octets"):
        parse_attachments([{"data": b64(b"pas une image du tout"), "name": "faux.png"}])


def test_limits_size_count_and_declared_type():
    with pytest.raises(ImageError, match="5 Mo"):
        parse_attachments([{"data": b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_IMAGE_BYTES)}])
    with pytest.raises(ImageError, match="au plus 4"):
        parse_attachments([{"data": b64(png())}] * 5)
    assert len(parse_attachments([{"data": b64(png())}] * 4)) == 4
    with pytest.raises(ImageError, match="non pris en charge"):
        parse_attachments([{"data": b64(png()), "media_type": "image/svg+xml"}])
    with pytest.raises(ImageError, match="base64"):
        parse_attachments([{"data": "@@@pas du base64@@@"}])
    with pytest.raises(ImageError, match="vide"):
        parse_attachments([{"data": ""}])


def test_data_url_accepted_remote_url_refused():
    [img] = parse_attachments([{"url": f"data:image/png;base64,{b64(png())}"}])
    assert img.media_type == "image/png"
    with pytest.raises(ImageError, match="data URL"):
        parse_attachments([{"url": "https://exemple.ch/a.png"}])


def test_path_only_inside_the_session_folder(tmp_path):
    (tmp_path / "caps").mkdir()
    (tmp_path / "caps" / "a.png").write_bytes(png())
    outside = tmp_path.parent / f"{tmp_path.name}-dehors.png"
    outside.write_bytes(png())
    try:
        [img] = parse_attachments([{"path": "caps/a.png"}], cwd=str(tmp_path))
        assert img.name == "a.png" and img.media_type == "image/png"
        (tmp_path / "lien.png").symlink_to(outside)
        for bad in (str(outside), "../" + outside.name, "lien.png"):
            with pytest.raises(ImageError, match="hors du dossier"):
                parse_attachments([{"path": bad}], cwd=str(tmp_path))
        with pytest.raises(ImageError, match="session locale"):
            parse_attachments([{"path": "caps/a.png"}], cwd=None)
    finally:
        outside.unlink()


def test_split_openai_content_parts():
    text, items = split_content([{"type": "text", "text": "quelle couleur ?"},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])
    assert text == "quelle couleur ?" and items == [{"url": "data:image/png;base64,AAAA"}]
    assert split_content("bonjour") == ("bonjour", [])
    with pytest.raises(ImageError, match="input_audio"):
        split_content([{"type": "input_audio"}])


# ───────────────────────────── magasin ─────────────────────────────

def test_store_dedups_by_sha_and_reads_per_thread(tmp_path):
    store = ImageStore(tmp_path)
    data = png()
    r1 = store.put("th_a", RawImage(data, "image/png", "a.png"))
    r2 = store.put("th_b", RawImage(data, "image/png", "b.png"))
    assert r1.sha256 == r2.sha256 and r1.size == len(data)
    blobs = list((tmp_path / "tn_ninabot" / "blobs").glob("*/*"))
    assert len(blobs) == 1 and oct(blobs[0].stat().st_mode & 0o777) == "0o600"
    assert store.get("th_a", r1.sha256) == data
    assert store.get("th_c", r1.sha256) is None  # fil qui ne la référence pas
    assert store.get("tn_autre:th_a", r1.sha256) is None  # autre locataire : autre dossier
    # rien de lisible sur le disque : ni l'id du fil ni le nom de fichier
    assert not any("th_a" in str(p) or "a.png" in str(p) for p in tmp_path.rglob("*"))
    assert (tmp_path / "tn_ninabot" / "refs" / thread_key("th_a") / r1.sha256).is_file()


def test_erase_thread_keeps_bytes_shared_with_another_thread(tmp_path):
    store = ImageStore(tmp_path)
    shared = store.put("th_a", RawImage(png(), "image/png"))
    store.put("th_b", RawImage(png(), "image/png"))
    own = store.put("th_a", RawImage(png((0, 0, 255)), "image/png"))
    assert store.erase_thread("th_a") == 1  # l'image propre au fil part, la partagée reste
    assert store.get("th_a", own.sha256) is None and store.get("th_a", shared.sha256) is None
    assert store.get("th_b", shared.sha256) is not None
    assert store.erase_thread("th_b") == 1
    assert list((tmp_path / "tn_ninabot" / "blobs").glob("*/*")) == []


def test_erase_tenant_removes_everything_of_that_tenant_only(tmp_path):
    store = ImageStore(tmp_path)
    store.put("tn_x1:th", RawImage(png(), "image/png"))
    store.put("tn_x1:th2", RawImage(GIF, "image/gif"))
    keep = store.put("tn_y2:th", RawImage(png(), "image/png"))
    assert store.count_tenant("tn_x1") == 2
    assert store.erase_tenant("tn_x1") == 2
    assert not (tmp_path / "tn_x1").exists()
    assert store.get("tn_y2:th", keep.sha256) is not None
    with pytest.raises(ValueError):
        store.erase_tenant("../etc")


# ───────────────────────────── tour et rendu ─────────────────────────────

def test_turn_keeps_only_the_reference():
    ref = ImageRef(sha256="a" * 64, media_type="image/png", size=123, name="capture.png")
    t = Turn(thread_id="th", index=0, input=InputCheck(text="regarde"), images=[ref])
    d = turn_to_dict(t)
    assert d["images"] == [{"sha256": "a" * 64, "media_type": "image/png", "size": 123, "name": "capture.png"}]
    assert turn_from_dict(json.loads(json.dumps(d))).images == [ref]
    assert turn_from_dict({k: v for k, v in d.items() if k != "images"}).images == []  # tours d'avant X7


def _engine(tmp_path, keep=4) -> ContextEngineV0:
    p = tmp_path / "prefix.md"
    p.write_text("Tu es Atlas.")
    return ContextEngineV0(tools=EmptyTools(), prefix_path=str(p), keep_images=keep)


def _ref(c: str) -> ImageRef:
    return ImageRef(sha256=c * 64, media_type="image/png", size=2048, name=f"{c}.png")


def test_engine_puts_image_parts_and_sheds_old_images(tmp_path):
    eng = _engine(tmp_path, keep=2)
    old = Turn(thread_id="th", index=0, input=InputCheck(text="avant"), images=[_ref("a"), _ref("b")], reply="ok")
    old.messages.append(Message(role="assistant", content="ok"))
    cur = Turn(thread_id="th", index=1, input=InputCheck(text="et celle-ci ?"), images=[_ref("c")])
    msgs = eng.assemble(cur, [old], [], [], Tier.SHIP).messages
    past, now = msgs[1]["content"], msgs[-1]["content"]
    assert now[0]["type"] == "text" and "et celle-ci ?" in now[0]["text"]
    assert now[1] == {"type": "nokker_image", "sha256": "c" * 64, "media_type": "image/png", "name": "c.png",
                      "turn": 1, "size": 2048}
    # budget 2 : l'image courante + la plus récente du passé ; la plus ancienne délestée en texte
    assert past[1]["type"] == "text" and "image délestée : a.png" in past[1]["text"] and "tour 0" in past[1]["text"]
    assert past[2]["type"] == "nokker_image" and past[2]["sha256"] == "b" * 64
    # sans image, le message reste une chaîne (rien ne change pour les tours d'avant)
    plain = eng.assemble(Turn(thread_id="th", index=0, input=InputCheck(text="salut")), [], [], [], Tier.SHIP)
    assert isinstance(plain.messages[-1]["content"], str)


def test_materialize_with_and_without_vision(tmp_path):
    store = ImageStore(tmp_path)
    data = png()
    ref = store.put("th", RawImage(data, "image/png", "cap.png"))
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": [{"type": "text", "text": "quelle couleur ?"},
                                         {"type": "nokker_image", "sha256": ref.sha256, "media_type": "image/png",
                                          "name": "cap.png"}]}]
    out, skipped = materialize(msgs, vision=True, blobs=store, thread_id="th")
    assert skipped == 0 and out[0] is msgs[0]
    assert out[1]["content"][1] == {"type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{b64(data)}"}}
    assert msgs[1]["content"][1]["type"] == "nokker_image"  # jamais modifié en place
    out, skipped = materialize(msgs, vision=False, blobs=store, thread_id="th")
    assert skipped == 1
    assert out[1]["content"] == "quelle couleur ?\n\n" + NO_VISION_NOTE[:-1] + " — cap.png]"
    assert "base64" not in json.dumps(out)
    store.erase_thread("th")
    out, skipped = materialize(msgs, vision=True, blobs=store, thread_id="th")
    assert skipped == 0 and "indisponible" in out[1]["content"]


# ───────────────────────────── noyau ─────────────────────────────

@dataclass
class SeeingProvider:
    vision: bool
    model: str = "qwen/qwen3.5-397b-a17b"
    seen: list = field(default_factory=list)

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(messages)
        return Completion(message=Message(role="assistant", content="rouge"), usage=Usage(prompt=10),
                          model=self.model, latency_s=0.01)


@dataclass
class Notices:
    got: list = field(default_factory=list)

    def context_for(self, thread_id):
        return []

    def attachment_skipped(self, turn, count, reason):
        self.got.append((turn.thread_id, count, reason))


def _kernel(tmp_path, vision: bool, observer=None) -> tuple[Kernel, SeeingProvider, ImageStore]:
    prov = SeeingProvider(vision=vision)
    store = ImageStore(tmp_path / "images")
    k = Kernel(store=MemoryStore(), context=_engine(tmp_path), memory=NullMem(), invariants=AllowInvariants(),
               gates=AllowGates(), escalator=NoEscalator(), policy=ShipOnlyPolicy(), providers={Tier.SHIP: prov},
               tools=EmptyTools(), ledger=InMemoryLedger(), images=store, observer=observer)
    return k, prov, store


def test_kernel_sends_image_url_to_a_vision_tier(tmp_path):
    k, prov, store = _kernel(tmp_path, vision=True)
    data = png()
    ref = store.put("th", RawImage(data, "image/png", "cap.png"))
    turn = k.run_turn(TurnInput(thread_id="th", user_text="quelle couleur ?", images=[ref]))
    assert turn.reply == "rouge"
    user = prov.seen[0][-1]["content"]
    assert user[1]["image_url"]["url"] == f"data:image/png;base64,{b64(data)}"
    stored = k.store.history("th")[0]
    assert stored.images == [ref]
    assert b64(data) not in json.dumps(turn_to_dict(stored))  # le tour append-only ne garde que la référence
    assert not any(v.rule == "images:not_read" for v in turn.verdicts)


def test_kernel_without_vision_sends_a_note_and_warns_once(tmp_path):
    obs = Notices()
    k, prov, store = _kernel(tmp_path, vision=False, observer=obs)
    refs = [store.put("th", RawImage(png(), "image/png", "a.png")), store.put("th", RawImage(GIF, "image/gif"))]
    turn = k.run_turn(TurnInput(thread_id="th", user_text="regarde", images=refs))
    content = prov.seen[0][-1]["content"]
    assert isinstance(content, str) and content.count("[image jointe non lue : modèle sans vision") == 2
    assert "base64" not in json.dumps(prov.seen)
    assert obs.got == [("th", 2, "no_vision")]
    assert [v.rule for v in turn.verdicts].count("images:not_read") == 1


def test_vision_capability_from_config_and_hedged(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("tiers:\n  ship:\n    base_url: http://x/v1\n    model: qwen/qwen3.5-397b-a17b\n"
                        "    vision: true\n  deep:\n    base_url: http://x/v1\n    model: autre\n")
    cfg = load_config(str(cfg_file))
    assert cfg.tiers["ship"].vision is True and cfg.tiers["deep"].vision is False
    assert cfg.vision_for_model("Qwen3.5-397B-A17B") and not cfg.vision_for_model("llama3")
    assert NokkerConfig(local={"state_dir": str(tmp_path)}).images_root() == str(tmp_path / "images")
    assert NokkerConfig().images_root() == ""
    a = SokkanProvider(base_url="http://x", api_key="", model="m", vision=True)
    b = SokkanProvider(base_url="http://x", api_key="", model="n")
    assert HedgedProvider(a, a).vision is True and HedgedProvider(a, b).vision is False
    assert TierConfig(base_url="u", model="m").vision is False


def test_lokkal_config_declares_vision_on_the_397b():
    cfg = load_config(str(Path(__file__).resolve().parents[3] / "deploy" / "lokkal" / "core-config.yaml"))
    assert all(t.vision for t in cfg.tiers.values() if "397b" in t.model.lower())
