"""Vision `auto` (docs/LOKKAL.md §7.6) : réglage `vision: true|false|auto` par palier ; `auto` = UNE sonde (petite image
de couleur, réponse attendue) au premier tour qui porte une image, jamais en tâche de fond, résultat gardé dans l'état
local ; sans certitude, texte seul comme avant. Faux fournisseurs seulement : aucun appel de modèle réel."""
from __future__ import annotations

import base64
import json
import threading
import zlib
from dataclasses import dataclass, field

import pytest

from nokker.config import NokkerConfig, load_config
from nokker.core.ports import Completion
from nokker.core.turn import Message, TurnInput, Usage
from nokker.images import RawImage
from nokker.providers import vision_probe as vp
from nokker.providers.hedged import HedgedProvider
from nokker.providers.sokkan import SokkanProvider
from nokker.providers.vision_probe import VisionProbe, probe_image
from tests.unit.images.test_images import _kernel, png


def color_of(data_url: str) -> str:
    """Ce que « voit » un faux modèle à vision : la couleur du premier pixel de la PNG."""
    raw = base64.b64decode(data_url.split(",", 1)[1])
    i = raw.index(b"IDAT")
    n = int.from_bytes(raw[i - 4:i], "big")
    pixels = zlib.decompress(raw[i + 4:i + 4 + n])
    rgb = tuple(pixels[1:4])
    return {v: k for k, v in vp.COLORS.items()}[rgb]


def images_in(messages) -> list[str]:
    return [p["image_url"]["url"] for m in messages if isinstance(m.get("content"), list)
            for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url"]


@dataclass
class FakeModel:
    """`sees` : lit l'image et répond sa couleur ; `blind` : ignore l'image ; `refuse` : HTTP 400 sur une image ;
    `down` : panne réseau."""

    kind: str = "sees"
    model: str = "llava-local"
    vision: bool = False
    vision_probe: object = None
    calls: list = field(default_factory=list)

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.calls.append(messages)
        urls = images_in(messages)
        if urls and self.kind == "refuse":
            raise RuntimeError("HTTP 400 de http://x/v1: image input is not supported")
        if urls and self.kind == "down":
            raise RuntimeError("échec réseau après 3 tentative(s) : connection refused")
        if urls and self.kind == "sees":
            text = f"The square is {color_of(urls[-1])}." if session_key == vp.SESSION_KEY else "je vois l'image"
        elif self.kind == "blind" and session_key == vp.SESSION_KEY:
            text = "I can't see images, but it could be red, green or blue."
        else:
            text = "rouge" if urls else "ok"
        return Completion(message=Message(role="assistant", content=text), usage=Usage(prompt=10),
                          model=self.model, latency_s=0.01)


def probe(tmp_path, name="vision-probes.json"):
    return VisionProbe(base_url="http://127.0.0.1:8080/v1", model="llava-local", path=tmp_path / name)


# ───────────────────────────── réglage ─────────────────────────────


def test_tier_setting_true_false_auto(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("tiers:\n"
                 "  ship: {base_url: http://x/v1, model: a, vision: auto}\n"
                 "  deep: {base_url: http://x/v1, model: b, vision: true}\n"
                 "  small: {base_url: http://x/v1, model: c, vision: peut-être}\n")
    cfg = load_config(str(f))
    assert (cfg.tiers["ship"].vision, cfg.tiers["ship"].vision_auto) == (False, True)  # jamais « vrai » sans sonde
    assert (cfg.tiers["deep"].vision, cfg.tiers["deep"].vision_auto) == (True, False)
    assert (cfg.tiers["small"].vision, cfg.tiers["small"].vision_auto) == (False, False)  # inconnu : texte seul
    assert cfg.vision_mode_for_model("a") == "auto" and cfg.vision_mode_for_model("B") == "true"
    assert cfg.vision_mode_for_model("autre") == "false"
    local = NokkerConfig(local={"state_dir": str(tmp_path), "vision": "auto"})
    assert local.vision_mode_for_model("mon-modele") == "auto"  # modèle choisi à l'onboarding (clé, local)
    assert local.vision_probe_path() == tmp_path / "vision-probes.json"
    assert NokkerConfig().vision_probe_path() is None


def test_auto_tier_gets_a_probe_and_hedged_tiers_stay_text_only(tmp_path):
    from nokker.api.server import build_kernel

    f = tmp_path / "c.yaml"
    f.write_text("tiers:\n"
                 "  ship: {base_url: http://x/v1, model: a, vision: auto}\n"
                 "  deep: {base_url: http://x/v1, model: b, vision: auto, fallback_model: c}\n"
                 f"local: {{state_dir: {tmp_path}}}\n")
    k = build_kernel(load_config(str(f)))
    from nokker.core.turn import Tier

    ship, deep = k.providers[Tier.SHIP], k.providers[Tier.DEEP]
    assert ship.vision is False and isinstance(ship.vision_probe, VisionProbe)
    assert ship.vision_probe.path == tmp_path / "vision-probes.json"
    assert isinstance(deep, HedgedProvider) and getattr(deep, "vision_probe", None) is None


# ───────────────────────────── sonde ─────────────────────────────


def test_probe_image_is_small_and_its_color_is_random():
    seen = set()
    for _ in range(40):
        color, data = probe_image()
        assert len(data) < 200 and data.startswith(b"\x89PNG")
        seen.add(color)
    assert len(seen) >= 3 and seen <= set(vp.COLORS)


def test_a_seeing_model_is_probed_once_and_remembered(tmp_path):
    m = FakeModel("sees")
    p = probe(tmp_path)
    assert p.check(m) == (True, True)
    assert p.check(m) == (True, False) and len(m.calls) == 1  # une seule sonde
    again = probe(tmp_path)  # redémarrage : relu de l'état local, pas de nouvel appel
    assert again.check(FakeModel("refuse")) == (True, False)
    data = json.loads((tmp_path / "vision-probes.json").read_text())
    assert data["http://127.0.0.1:8080/v1|llava-local"]["vision"] is True
    assert "key" not in json.dumps(data).lower()  # jamais de clé d'API dans l'état


@pytest.mark.parametrize("kind", ["refuse", "blind"])
def test_refused_or_wrong_answer_means_text_only_and_is_remembered(tmp_path, kind):
    m = FakeModel(kind)
    p = probe(tmp_path)
    assert p.check(m) == (False, True)
    assert p.check(m) == (False, False) and len(m.calls) == 1
    assert probe(tmp_path).check(FakeModel("sees")) == (False, False)


def test_a_failure_without_certainty_is_not_remembered_and_not_retried_at_once(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(vp.time, "monotonic", lambda: clock[0])
    m = FakeModel("down")
    p = probe(tmp_path)
    assert p.check(m) == (False, True)  # texte seul pour ce tour
    assert p.check(m) == (False, False) and len(m.calls) == 1  # pas de nouvel essai tout de suite
    assert not (tmp_path / "vision-probes.json").exists()  # rien de certain : rien gardé
    clock[0] += vp.RETRY_AFTER_S + 1
    m.kind = "sees"
    assert p.check(m) == (True, True) and len(m.calls) == 2


def test_concurrent_turns_probe_only_once(tmp_path):
    gate = threading.Event()

    class Slow(FakeModel):
        def complete(self, *a, **kw):
            gate.wait(2)
            return super().complete(*a, **kw)

    m = Slow("sees")
    p = probe(tmp_path)
    out = []
    ts = [threading.Thread(target=lambda: out.append(p.check(m))) for _ in range(4)]
    for t in ts:
        t.start()
    gate.set()
    for t in ts:
        t.join(5)
    assert len(m.calls) == 1 and all(v for v, _ in out) and sum(1 for _, fresh in out if fresh) == 1


# ───────────────────────────── noyau ─────────────────────────────


def _auto_kernel(tmp_path, kind="sees"):
    k, _, store = _kernel(tmp_path, vision=False)
    from nokker.core.turn import Tier

    m = FakeModel(kind, vision_probe=probe(tmp_path, "state.json"))
    k.providers[Tier.SHIP] = m
    return k, m, store


def test_kernel_probes_at_the_first_turn_with_an_image_then_sends_images(tmp_path):
    k, m, store = _auto_kernel(tmp_path)
    t0 = k.run_turn(TurnInput(thread_id="th", user_text="bonjour"))
    assert t0.reply == "ok" and len(m.calls) == 1  # pas d'image : jamais de sonde
    ref = store.put("th", RawImage(png(), "image/png", "cap.png"))
    t1 = k.run_turn(TurnInput(thread_id="th", user_text="quelle couleur ?", images=[ref]))
    assert len(m.calls) == 3  # la sonde, puis l'appel du tour
    assert images_in(m.calls[-1])  # l'image part au modèle
    assert [v.rule for v in t1.verdicts].count("images:vision_probe") == 1
    assert not any(v.rule == "images:not_read" for v in t1.verdicts)
    ref2 = store.put("th", RawImage(png((0, 0, 255)), "image/png", "b.png"))
    t2 = k.run_turn(TurnInput(thread_id="th", user_text="et celle-ci ?", images=[ref2]))
    assert len(m.calls) == 4 and not any(v.rule == "images:vision_probe" for v in t2.verdicts)


def test_kernel_with_a_blind_model_stays_text_only(tmp_path):
    k, m, store = _auto_kernel(tmp_path, kind="refuse")
    ref = store.put("th", RawImage(png(), "image/png", "cap.png"))
    t = k.run_turn(TurnInput(thread_id="th", user_text="regarde", images=[ref]))
    assert t.reply == "ok" and not images_in(m.calls[-1])
    assert "[image jointe non lue : modèle sans vision" in m.calls[-1][-1]["content"]
    assert any(v.rule == "images:not_read" for v in t.verdicts)
    probe_verdict = next(v for v in t.verdicts if v.rule == "images:vision_probe")
    assert "non" in probe_verdict.reason


def test_sokkan_provider_keeps_its_probe_when_detached():
    p = VisionProbe(base_url="http://x/v1", model="m", path=None)
    a = SokkanProvider(base_url="http://x/v1", api_key="", model="m", vision_probe=p)
    assert a.detached().vision_probe is p and a.vision is False
