import io
import struct
import wave
from unittest.mock import Mock

from turbo_whisper import recorder
from turbo_whisper.config import Config


def test_linux_microphone_selection_and_wave_encoding(monkeypatch):
    raw = struct.pack("<hhhh", -32768, 0, 32767, 0)
    process = Mock(stdout=io.BytesIO(raw))
    spawn = Mock(return_value=process)
    monkeypatch.setattr(recorder.sys, "platform", "linux")
    monkeypatch.setattr(recorder.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(recorder.subprocess, "Popen", spawn)
    native_audio = Mock(side_effect=AssertionError("Pulse recording must not initialize ALSA"))
    monkeypatch.setattr(recorder.pyaudio, "PyAudio", native_audio)
    audio = recorder.AudioRecorder(Config(input_device_index="alsa_input.synthetic"))
    levels = []
    audio.start(lambda level, buffer: levels.append(level))
    audio._record_thread.join(timeout=1)
    wav = audio.stop()
    assert "--device=alsa_input.synthetic" in spawn.call_args.args[0]
    process.terminate.assert_called_once()
    with wave.open(io.BytesIO(wav)) as source:
        assert source.getframerate() == 16000
        assert source.getsampwidth() == 2
        assert source.readframes(4) == raw
    assert levels[0] > 0.49  # int16 minimum must not overflow in abs().


def test_empty_recording_is_not_a_header_only_wav(monkeypatch):
    audio = recorder.AudioRecorder(Config())
    assert audio.stop() == b""
