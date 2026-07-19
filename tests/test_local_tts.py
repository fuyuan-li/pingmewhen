import base64

from relay_agent.local_tts import (
    _pcmu_chunks,
    is_valid_sensitive_value,
    looks_like_protected_value,
    spoken_sensitive_value,
)


def test_sensitive_values_are_spoken_as_individual_digits():
    assert spoken_sensitive_value("card_number", "4242") == "4 2 4 2"
    assert spoken_sensitive_value("expiration", "12/34") == "1 2, 3 4"
    assert spoken_sensitive_value("cvv", "123") == "1 2 3"
    assert spoken_sensitive_value("ssn_last_four", "6789") == "6 7 8 9"
    assert spoken_sensitive_value("date_of_birth", "1990-07-18") == "July 18, 1990"


def test_protected_values_are_detected_before_general_type_to_speak():
    assert looks_like_protected_value("4242 4242 4242 4242") is True
    assert looks_like_protected_value("000-00-0000") is True
    assert looks_like_protected_value("Tuesday works for me") is False


def test_production_sensitive_values_accept_real_field_shapes():
    assert is_valid_sensitive_value("card_number", "4111 1111 1111 1111") is True
    assert is_valid_sensitive_value("expiration", "12/29") is True
    assert is_valid_sensitive_value("cvv", "987") is True
    assert is_valid_sensitive_value("full_ssn", "123-45-6789") is True
    assert is_valid_sensitive_value("ssn_last_four", "6789") is True
    assert is_valid_sensitive_value("date_of_birth", "1990-07-18") is True
    assert is_valid_sensitive_value("expiration", "2029-12") is True
    assert is_valid_sensitive_value("expiration", "19/29") is False
    assert is_valid_sensitive_value("full_ssn", "1234") is False
    assert is_valid_sensitive_value("date_of_birth", "not-a-date") is False


def test_local_audio_is_chunked_as_twilio_pcmu_payloads():
    chunks = _pcmu_chunks([0.0] * 441, 22050)

    decoded = [base64.b64decode(chunk) for chunk in chunks]
    assert sum(len(chunk) for chunk in decoded) == 160
    assert all(len(chunk) <= 160 for chunk in decoded)
