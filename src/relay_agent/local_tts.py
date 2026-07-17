from __future__ import annotations

import base64
import ctypes
import sys
import time
import warnings
from typing import Protocol


class LocalTTSUnavailable(RuntimeError):
    pass


class LocalTTSRenderer(Protocol):
    def render(self, field: str, value: str) -> list[str]: ...


FAKE_SENSITIVE_VALUES = {
    "card_number": "4242424242424242",
    "expiration": "1234",
    "cvv": "123",
    "full_ssn": "000000000",
}


def is_allowed_fake_value(field: str, value: str) -> bool:
    return "".join(character for character in value if character.isdigit()) == FAKE_SENSITIVE_VALUES.get(field)


def spoken_sensitive_value(field: str, value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        raise ValueError("Enter a valid fake value.")
    if field == "expiration" and len(digits) >= 4:
        return f"{' '.join(digits[:2])}, {' '.join(digits[2:])}"
    return " ".join(digits)


def _linear_resample(samples: list[float], source_rate: float, target_rate: int = 8000) -> list[float]:
    if not samples or source_rate <= 0:
        return []
    target_length = max(1, round(len(samples) * target_rate / source_rate))
    scale = source_rate / target_rate
    result = []
    for target_index in range(target_length):
        position = target_index * scale
        left = min(int(position), len(samples) - 1)
        right = min(left + 1, len(samples) - 1)
        fraction = position - left
        result.append(samples[left] + (samples[right] - samples[left]) * fraction)
    return result


def _linear_pcm_to_mulaw(sample: float) -> int:
    value = max(-1.0, min(1.0, sample))
    pcm = int(value * 32767)
    sign = 0x80 if pcm < 0 else 0
    magnitude = min(abs(pcm), 32635) + 132
    exponent = max(0, min(7, magnitude.bit_length() - 8))
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _pcmu_chunks(samples: list[float], source_rate: float) -> list[str]:
    encoded = bytes(_linear_pcm_to_mulaw(sample) for sample in _linear_resample(samples, source_rate))
    return [base64.b64encode(encoded[index : index + 160]).decode("ascii") for index in range(0, len(encoded), 160)]


class MacOSLocalTTS:
    def render(self, field: str, value: str) -> list[str]:
        if sys.platform != "darwin":
            raise LocalTTSUnavailable("Secure local voice currently requires macOS.")
        try:
            import AVFAudio
            import objc
            from Foundation import NSDate, NSRunLoop
        except ImportError as error:
            raise LocalTTSUnavailable("The macOS speech framework is unavailable.") from error

        utterance_text = spoken_sensitive_value(field, value)
        synthesizer = AVFAudio.AVSpeechSynthesizer.alloc().init()
        utterance = AVFAudio.AVSpeechUtterance.speechUtteranceWithString_(utterance_text)
        samples: list[float] = []
        source_rate = 0.0
        finished = False

        def receive(buffer) -> None:
            nonlocal finished, source_rate
            frame_count = int(buffer.frameLength())
            if frame_count == 0:
                finished = True
                return
            source_rate = float(buffer.format().sampleRate())
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", objc.ObjCPointerWarning)
                channel_pointer = buffer.floatChannelData().pointerAsInteger
            channels = ctypes.cast(channel_pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_float)))
            samples.extend(channels[0][index] for index in range(frame_count))

        synthesizer.writeUtterance_toBufferCallback_(utterance, receive)
        deadline = time.monotonic() + 20
        while not finished and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
        utterance_text = ""
        value = ""
        if not finished or not samples:
            raise LocalTTSUnavailable("macOS did not return synthesized speech audio.")
        return _pcmu_chunks(samples, source_rate)
