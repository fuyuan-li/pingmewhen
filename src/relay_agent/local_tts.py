from __future__ import annotations

import base64
import ctypes
from datetime import datetime
import re
import sys
import time
import warnings
from typing import Protocol


class LocalTTSUnavailable(RuntimeError):
    pass


class LocalTTSRenderer(Protocol):
    def render_text(self, text: str) -> list[str]: ...

    def render_sensitive(self, field: str, value: str) -> list[str]: ...


def sensitive_input_spec(field: str) -> dict[str, str | int]:
    return {
        "card_number": {
            "input_kind": "masked_numeric",
            "placeholder": "13–19 digits",
            "hint": "Card number · 13–19 digits · hidden as you type",
            "max_length": 19,
        },
        "expiration": {
            "input_kind": "month",
            "placeholder": "MM / YYYY",
            "hint": "Expiration month and year",
            "max_length": 6,
        },
        "cvv": {
            "input_kind": "masked_numeric",
            "placeholder": "3 or 4 digits",
            "hint": "Security code · 3 or 4 digits · hidden as you type",
            "max_length": 4,
        },
        "full_ssn": {
            "input_kind": "masked_numeric",
            "placeholder": "•••-••-••••",
            "hint": "Format: ###-##-#### · 9 digits · hidden as you type",
            "max_length": 9,
        },
        "ssn_last_four": {
            "input_kind": "masked_numeric",
            "placeholder": "••••",
            "hint": "Last four digits of Social Security number · hidden as you type",
            "max_length": 4,
        },
        "date_of_birth": {
            "input_kind": "date",
            "placeholder": "Choose date of birth",
            "hint": "Use the date picker; the value stays local.",
            "max_length": 10,
        },
    }.get(field, {"input_kind": "text", "placeholder": "Type a non-sensitive response", "hint": ""})


def is_valid_sensitive_value(field: str, value: str) -> bool:
    digits = "".join(character for character in value if character.isdigit())
    if field == "card_number":
        return 13 <= len(digits) <= 19
    if field == "expiration":
        if re.fullmatch(r"\d{4}-\d{2}", value.strip()):
            return 1 <= int(value.strip()[-2:]) <= 12
        if len(digits) not in {4, 6}:
            return False
        month = int(digits[:2])
        return 1 <= month <= 12
    if field == "cvv":
        return len(digits) in {3, 4}
    if field == "full_ssn":
        return len(digits) == 9
    if field == "ssn_last_four":
        return len(digits) == 4
    if field == "date_of_birth":
        for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                datetime.strptime(value.strip(), pattern)
                return True
            except ValueError:
                pass
        return False
    return False


def looks_like_protected_value(value: str) -> bool:
    digits = "".join(character for character in value if character.isdigit())
    return bool(
        13 <= len(digits) <= 19
        or re.search(r"\b\d{3}[ -]?\d{2}[ -]?\d{4}\b", value)
        or re.search(r"\b(?:password|passcode|account pin|api key|auth token|authentication token)\b", value, re.I)
    )


def spoken_sensitive_value(field: str, value: str) -> str:
    if field == "date_of_birth":
        for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                parsed = datetime.strptime(value.strip(), pattern)
                return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
            except ValueError:
                pass
        raise ValueError("Enter a valid date of birth.")
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        raise ValueError("Enter a valid protected value.")
    if field == "expiration" and len(digits) >= 4:
        if re.fullmatch(r"\d{4}-\d{2}", value.strip()):
            digits = f"{value.strip()[-2:]}{value.strip()[:4]}"
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
    def render_text(self, text: str) -> list[str]:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("Type something for the representative.")
        return self._render_utterance(cleaned)

    def render_sensitive(self, field: str, value: str) -> list[str]:
        return self._render_utterance(spoken_sensitive_value(field, value))

    def render(self, field: str, value: str) -> list[str]:
        return self.render_sensitive(field, value)

    def _render_utterance(self, utterance_text: str) -> list[str]:
        if sys.platform != "darwin":
            raise LocalTTSUnavailable("Secure local voice currently requires macOS.")
        try:
            import AVFAudio
            import objc
            from Foundation import NSDate, NSRunLoop
        except ImportError as error:
            raise LocalTTSUnavailable("The macOS speech framework is unavailable.") from error

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
        if not finished or not samples:
            raise LocalTTSUnavailable("macOS did not return synthesized speech audio.")
        return _pcmu_chunks(samples, source_rate)
