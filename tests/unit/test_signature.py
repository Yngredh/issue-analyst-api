import hashlib
import hmac

import pytest

from issue_analyst_api.domain.signature import compute_signature, is_valid_signature

SECRET = b"unit-test-secret"
BODY = b'{"action": "opened"}'


def _github_signature(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_compute_signature_matches_github_format() -> None:
    assert compute_signature(SECRET, BODY) == _github_signature(SECRET, BODY)


def test_valid_signature_is_accepted() -> None:
    assert is_valid_signature(SECRET, BODY, _github_signature(SECRET, BODY))


def test_uppercase_hex_is_accepted() -> None:
    header = "sha256=" + hmac.new(SECRET, BODY, hashlib.sha256).hexdigest().upper()
    assert is_valid_signature(SECRET, BODY, header)


def test_missing_signature_is_rejected() -> None:
    assert not is_valid_signature(SECRET, BODY, None)


def test_signature_with_other_secret_is_rejected() -> None:
    assert not is_valid_signature(SECRET, BODY, _github_signature(b"other-secret", BODY))


def test_signature_of_other_body_is_rejected() -> None:
    assert not is_valid_signature(SECRET, BODY, _github_signature(SECRET, BODY + b" "))


@pytest.mark.parametrize(
    "header",
    [
        "",
        "sha256=",
        "sha1=" + hashlib.sha1(BODY).hexdigest(),
        hmac.new(SECRET, BODY, hashlib.sha256).hexdigest(),  # sem prefixo
        "sha256=" + "z" * 64,  # não hexadecimal
        "sha256=" + "a" * 63,  # tamanho errado
        "sha256=" + "a" * 66,
        " sha256=" + "a" * 64,
    ],
)
def test_malformed_signature_is_rejected(header: str) -> None:
    assert not is_valid_signature(SECRET, BODY, header)
