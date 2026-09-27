from mcp_core.errors import ChallengeRequiredError


def test_challenge_required_error_is_machine_readable():
    payload = ChallengeRequiredError("anti-bot wall", provider="ozon").to_dict()

    assert payload["error"] == "challenge_required"
    assert payload["challenge_type"] == "captcha"
    assert payload["status_code"] == 403
