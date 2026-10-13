"""Opt-in wake clip capture (MISTAI_WAKE_SAVE_CLIPS) for the wake harness."""

import json
import wave

import speech_recognition as sr

from core.api import Api
from core.config import wake_clips_enabled


def _audio(seconds=0.5, rate=16000, width=2):
    return sr.AudioData(b"\x00\x01" * int(rate * seconds), rate, width)


def test_clip_saving_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MISTAI_WAKE_SAVE_CLIPS", raising=False)
    assert wake_clips_enabled() is False
    monkeypatch.setenv("MISTAI_WAKE_SAVE_CLIPS", "1")
    assert wake_clips_enabled() is True


def test_clip_seconds():
    assert Api._clip_seconds(_audio(0.5)) == 0.5


def test_saved_clip_has_wav_and_sidecar(tmp_path):
    audio = _audio(0.5)
    Api._save_wake_clip(audio, "hey miss", 668.4, 0.5, directory=str(tmp_path))

    (meta_path,) = tmp_path.glob("*.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta == {
        "wav": meta_path.stem + ".wav",
        "transcript": "hey miss",
        "energy_threshold": 668.4,
        "clip_seconds": 0.5,
    }
    with wave.open(str(tmp_path / meta["wav"]), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 8000)


def test_untranscribed_clip_records_error_type_only(tmp_path):
    Api._save_wake_clip(_audio(), None, 217, 0.5, error="RequestError",
                        directory=str(tmp_path))
    (meta_path,) = tmp_path.glob("*.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["transcript"] == "untranscribed"
    assert meta["error"] == "RequestError"


def test_save_failure_never_raises(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    Api._save_wake_clip(_audio(), "mist", 300, 0.5, directory=str(blocker))
    assert "Could not save clip" in capsys.readouterr().out
