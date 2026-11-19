"""Verificação da assinatura HMAC SHA-256 enviada pelo GitHub em `X-Hub-Signature-256` (RF01)."""

import hashlib
import hmac
import re

SIGNATURE_PREFIX = "sha256="
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")


def compute_signature(secret: bytes, body: bytes) -> str:
    """Calcula o valor esperado do header (`sha256=<hex>`) para o corpo bruto."""
    return SIGNATURE_PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()


def is_valid_signature(secret: bytes, body: bytes, header_value: str | None) -> bool:
    """Confere a assinatura do corpo bruto em tempo constante.

    Header ausente, fora do formato `sha256=<64 hex>` ou com HMAC divergente → `False`.
    """
    if header_value is None or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    received_hex = header_value[len(SIGNATURE_PREFIX) :]
    if _HEX_DIGEST.fullmatch(received_hex) is None:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, bytes.fromhex(received_hex))
