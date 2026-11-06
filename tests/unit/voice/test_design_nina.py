"""scripts/voice/design-nina.py : à blanc sans `--go` (aucun appel), trois candidates enregistrées en local, aucune voix
enregistrée dans le compte sans `--save`, jamais de clonage. Réseau remplacé par un faux `urlopen`."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "voice" / "design-nina.py"


@pytest.fixture
def mod(monkeypatch):
    spec = importlib.util.spec_from_file_location("design_nina", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    calls = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data)
        calls.append((req.full_url, req.get_header("Xi-api-key"), body))
        if req.full_url.endswith("/v1/text-to-voice"):
            return io.BytesIO(json.dumps({"voice_id": "SavedVoice1"}).encode())
        previews = [{"generated_voice_id": f"Gen{i}", "audio_base_64": base64.b64encode(b"ID3" + bytes([i])).decode(),
                     "media_type": "audio/mpeg", "duration_secs": 9.5} for i in range(3)]
        return io.BytesIO(json.dumps({"previews": previews, "text": body["text"]}).encode())

    monkeypatch.setattr(m.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_fake")
    return m, calls


def test_dry_run_calls_nothing(mod, tmp_path, capsys):
    m, calls = mod
    m.main(["--out", str(tmp_path / "v")])
    m.main(["--save", "Gen0"])
    assert calls == [] and not (tmp_path / "v").exists()
    assert "à blanc" in capsys.readouterr().out


def test_go_writes_three_mp3_and_saves_nothing(mod, tmp_path):
    m, calls = mod
    m.main(["--go", "--out", str(tmp_path), "--seed", "7"])
    assert len(calls) == 1
    url, key, body = calls[0]
    assert url == "https://api.elevenlabs.io/v1/text-to-voice/design?output_format=mp3_44100_128" and key == "sk_fake"
    assert body["seed"] == 7 and body["model_id"] == "eleven_multilingual_ttv_v2"
    assert "reference_audio_base64" not in body and 100 <= len(body["text"]) <= 1000  # description seule, pas de clone
    assert "French, German, Italian and English" in body["voice_description"]
    files = sorted(p.name for p in tmp_path.glob("*.mp3"))
    assert files == ["nina-1-Gen0.mp3", "nina-2-Gen1.mp3", "nina-3-Gen2.mp3"]
    manifest = json.loads((tmp_path / "candidates.json").read_text())
    assert [c["generated_voice_id"] for c in manifest["candidates"]] == ["Gen0", "Gen1", "Gen2"]
    assert not any(c[0].endswith("/v1/text-to-voice") for c in calls)


def test_save_only_the_named_candidate(mod, capsys):
    m, calls = mod
    m.main(["--go", "--save", "Gen1", "--name", "Nina"])
    ((url, _, body),) = calls
    assert url == "https://api.elevenlabs.io/v1/text-to-voice"
    assert body["generated_voice_id"] == "Gen1" and body["voice_name"] == "Nina"
    assert "voice_id=SavedVoice1" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        m.main(["--go", "--save", "../x"])
