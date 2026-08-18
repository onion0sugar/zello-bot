"""Głos: WAV → FFmpeg → PCM 16 kHz mono → libopus → ramki Opus (20 ms).

Pipeline (bez plików pośrednich na dysku):

    WAV → ffmpeg -f s16le -ac 1 -ar 16000 → PCM
    → opus_encode (ctypes, libopus) → ramki Opus → Zello WebSocket

libopus ładujemy przez ctypes — wystarczą trzy funkcje:
opus_encoder_create / opus_encode / opus_encoder_destroy.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import logging
import struct
import subprocess

logger = logging.getLogger("audio")

FFMPEG_BIN = "ffmpeg"
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SIZE_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_SIZE_MS // 1000  # 320 próbek / ramkę
FRAME_BYTES = FRAME_SAMPLES * 2                      # 640 bajtów PCM / ramkę
MAX_OPUS_BYTES = 4000
OPUS_APPLICATION_VOIP = 2048


class VoiceFileError(Exception):
    """Problem z plikiem WAV, FFmpeg lub libopus."""


def codec_header() -> str:
    """base64 kodeka: 16000 Hz, 1 frame/packet, 20 ms (patrz API Zello)."""
    return base64.b64encode(struct.pack("<HBB", SAMPLE_RATE, 1, FRAME_SIZE_MS)).decode("ascii")


def wav_to_pcm(wav_path: str) -> bytes:
    """WAV → surowe PCM s16le 16 kHz mono przez FFmpeg (potok, brak pliku .opus)."""
    command = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
        "-i", wav_path, "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
    ]
    proc = subprocess.run(command, capture_output=True)
    if proc.returncode != 0:
        raise VoiceFileError(
            f"ffmpeg failed: {proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    if not proc.stdout:
        raise VoiceFileError(f"ffmpeg produced no audio from: {wav_path}")
    return proc.stdout


class OpusEncoder:
    """Minimalny wrapper ctypes wokół libopus (bez zewnętrznych zależności)."""

    def __init__(self, lib=None):
        self._lib = lib if lib is not None else self._load_libopus()
        self._configure(self._lib)
        error = ctypes.c_int()
        self._encoder = self._lib.opus_encoder_create(
            SAMPLE_RATE, CHANNELS, OPUS_APPLICATION_VOIP, ctypes.byref(error)
        )
        if not self._encoder:
            raise VoiceFileError(f"opus_encoder_create failed (error {error.value})")

    @staticmethod
    def _load_libopus():
        found = ctypes.util.find_library("opus")
        candidates = [found] if found else []
        candidates += ["libopus.so.0", "libopus.so", "opus.dll", "libopus.dylib"]
        for name in candidates:
            try:
                return ctypes.CDLL(name)
            except OSError:
                continue
        raise VoiceFileError("libopus not found — zainstaluj: sudo apt-get install libopus0")

    @staticmethod
    def _configure(lib) -> None:
        lib.opus_encoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
        ]
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
        ]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]

    def encode(self, pcm: bytes) -> list[bytes]:
        """PCM s16le → lista pakietów Opus (jeden na ramkę 20 ms = 640 bajtów)."""
        packets: list[bytes] = []
        input_buffer = (ctypes.c_int16 * FRAME_SAMPLES)()
        output_buffer = (ctypes.c_ubyte * MAX_OPUS_BYTES)()
        for offset in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
            ctypes.memmove(input_buffer, pcm[offset : offset + FRAME_BYTES], FRAME_BYTES)
            size = self._lib.opus_encode(
                self._encoder, input_buffer, FRAME_SAMPLES, output_buffer, MAX_OPUS_BYTES
            )
            if size < 0:
                raise VoiceFileError(f"opus_encode failed (error {size})")
            packets.append(bytes(output_buffer[:size]))
        if not packets:
            raise VoiceFileError("audio is shorter than one 20 ms frame")
        return packets

    def destroy(self) -> None:
        self._lib.opus_encoder_destroy(self._encoder)


def encode_opus(pcm: bytes) -> list[bytes]:
    """PCM → pakiety Opus (nowy encoder na każde wywołanie)."""
    encoder = OpusEncoder()
    try:
        return encoder.encode(pcm)
    finally:
        encoder.destroy()
