"""Testy audio: codec_header, wywołanie FFmpeg, ramki Opus (mockowany libopus)."""

import base64
import ctypes
import struct

import pytest

from audio import OpusEncoder, VoiceFileError, codec_header, wav_to_pcm


def test_codec_header_decodes_to_16000_1_20():
    raw = base64.b64decode(codec_header())
    assert struct.unpack("<HBB", raw) == (16000, 1, 20)


def test_wav_to_pcm_calls_ffmpeg_with_expected_args(monkeypatch):
    pcm = b"\x00\x01" * 640
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = pcm
        stderr = b""

    def fake_run(command, capture_output=False):
        captured["command"] = command
        return FakeProc()

    monkeypatch.setattr("audio.subprocess.run", fake_run)
    assert wav_to_pcm("audio/new_order.wav") == pcm
    cmd = captured["command"]
    assert cmd[0] == "ffmpeg"
    assert "-ar" in cmd and "16000" in cmd
    assert "-ac" in cmd and "1" in cmd
    assert cmd[-1] == "pipe:1"


def test_wav_to_pcm_ffmpeg_error_raises(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = b""
        stderr = b"no such file"

    monkeypatch.setattr("audio.subprocess.run", lambda command, capture_output=False: FakeProc())
    with pytest.raises(VoiceFileError, match="ffmpeg failed"):
        wav_to_pcm("audio/missing.wav")


def test_wav_to_pcm_empty_output_raises(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr("audio.subprocess.run", lambda command, capture_output=False: FakeProc())
    with pytest.raises(VoiceFileError, match="no audio"):
        wav_to_pcm("audio/silent.wav")


class FakeLibopus:
    """Podróbka API libopus: encoder 12345, każda ramka zwraca stałe 5 bajtów.

    Funkcje są zwykłymi atrybutami instancji, bo _configure przypisuje
    .argtypes (tak jak robi to ctypes CDLL).
    """

    def __init__(self):
        self.create_calls: list = []
        self.encode_calls = 0
        self.destroy_calls = 0
        self._self_ref = self

        def create(rate, channels, app, error_ptr):
            self.create_calls.append((rate, channels, app))
            return 12345

        def encode(encoder, samples_ptr, frame_size, out_ptr, max_bytes):
            self.encode_calls += 1
            ctypes.memmove(out_ptr, b"OPUS!", 5)
            return 5

        def destroy(encoder):
            self.destroy_calls += 1

        self.opus_encoder_create = create
        self.opus_encode = encode
        self.opus_encoder_destroy = destroy


def test_encode_opus_splits_into_20ms_frames():
    lib = FakeLibopus()
    encoder = OpusEncoder(lib=lib)
    try:
        packets = encoder.encode((b"\x00\x01" * 320) * 3)  # 3 ramki po 640 B
    finally:
        encoder.destroy()
    assert len(packets) == 3
    assert all(len(p) == 5 for p in packets)
    assert lib.create_calls == [(16000, 1, 2048)]  # OPUS_APPLICATION_VOIP
    assert lib.encode_calls == 3
    assert lib.destroy_calls == 1


def test_encode_opus_too_short_raises():
    lib = FakeLibopus()
    encoder = OpusEncoder(lib=lib)
    try:
        with pytest.raises(VoiceFileError, match="shorter"):
            encoder.encode(b"\x00\x01" * 100)  # 200 B < 640 B
    finally:
        encoder.destroy()


def test_opus_encoder_create_failure_raises():
    lib = type("FailingLib", (), {})()
    # zwykłe atrybuty instancji (jak CDLL) — wszystkie trzy funkcje, bo
    # _configure ustawia .argtypes na każdej
    lib.opus_encoder_create = lambda *a: 0
    lib.opus_encode = lambda *a: 0
    lib.opus_encoder_destroy = lambda *a: None
    with pytest.raises(VoiceFileError, match="opus_encoder_create"):
        OpusEncoder(lib=lib)
