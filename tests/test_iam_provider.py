"""Тесты провайдера IAM токенов."""

import json

import httpx
import respx
from Crypto.PublicKey import RSA

from app.modules.utils.iam import (
    IamTokenProvider,
    TOKEN_ENDPOINT,
    load_service_account_key_from_string,
)


def _build_service_account_key_json() -> str:
    private_key = RSA.generate(2048)
    pem = private_key.export_key(format="PEM", pkcs=8).decode()
    data = {
        "service_account_id": "aje1234567890abcdef",
        "id": "ab1cdef2ghij3klm4567",
        "created_at": "2024-01-01T00:00:00Z",
        "key_algorithm": "RSA_2048",
        "private_key": pem,
    }
    return json.dumps(data)


@respx.mock
def test_iam_provider_fetches_and_caches() -> None:
    key_json = _build_service_account_key_json()
    key = load_service_account_key_from_string(key_json)
    route = respx.post(TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "iamToken": "token-123",
                "expiresAt": "2030-01-01T00:00:00.000Z",
            },
        )
    )

    provider = IamTokenProvider(key=key)

    token_first = provider.get_token()
    token_second = provider.get_token()

    assert token_first == "token-123"
    assert token_second == "token-123"
    assert len(route.calls) == 1
    payload = json.loads(route.calls[0].request.content.decode())
    assert "jwt" in payload
