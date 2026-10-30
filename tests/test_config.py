import json
import os

from turbo_whisper.config import Config


def test_existing_config_migrates_without_losing_unknown_fields():
    path = Config.get_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "api_url": "http://localhost:8000/transcribe",
                "history": ["old clip"],
                "future_option": {"enabled": True},
            }
        )
    )
    config = Config.load()
    assert config.history[0]["text"] == "old clip"
    assert config.api_url == "http://localhost:8000/transcribe"
    assert config.push_to_talk is False
    assert config.transcription_timeout == 30
    config.save()
    saved = json.loads(path.read_text())
    assert saved["future_option"] == {"enabled": True}
    assert "_unknown_fields" not in saved
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_new_installs_do_not_send_audio_to_an_unrelated_server():
    config = Config.load()
    assert config.api_url == "https://api.openai.com/v1/audio/transcriptions"
    assert not config.claude_integration


def test_history_deduplicates_and_retains_latest():
    config = Config(history_max=2)
    for text in ["first", "second", "first", "third"]:
        config.add_to_history(text)
    assert [item["text"] for item in Config.load().history] == ["third", "first"]
