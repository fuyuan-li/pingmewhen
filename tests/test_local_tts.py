import base64

from relay_agent.local_tts import _pcmu_chunks, is_allowed_fake_value, spoken_sensitive_value


def test_sensitive_values_are_spoken_as_individual_digits():
    assert spoken_sensitive_value("card_number", "4242") == "4 2 4 2"
    assert spoken_sensitive_value("expiration", "12/34") == "1 2, 3 4"
    assert spoken_sensitive_value("cvv", "123") == "1 2 3"


def test_p0_accepts_only_scoped_fake_sensitive_values():
    assert is_allowed_fake_value("card_number", "4242 4242 4242 4242") is True
    assert is_allowed_fake_value("expiration", "12/34") is True
    assert is_allowed_fake_value("cvv", "123") is True
    assert is_allowed_fake_value("full_ssn", "000-00-0000") is True
    assert is_allowed_fake_value("card_number", "4111 1111 1111 1111") is False


def test_local_audio_is_chunked_as_twilio_pcmu_payloads():
    chunks = _pcmu_chunks([0.0] * 441, 22050)

    decoded = [base64.b64decode(chunk) for chunk in chunks]
    assert sum(len(chunk) for chunk in decoded) == 160
    assert all(len(chunk) <= 160 for chunk in decoded)
