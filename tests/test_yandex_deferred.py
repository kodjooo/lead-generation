"""Тесты клиента Yandex deferred пошагового API."""

import base64
import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx
from zoneinfo import ZoneInfo

from app.modules.yandex_deferred import (
    DeferredQueryParams,
    InvalidResponseError,
    NightWindowViolation,
    OperationTimeout,
    OperationResponse,
    YandexDeferredClient,
    OPERATIONS_URL,
    SEARCH_ASYNC_URL,
)


class FakeClock:
    """Простые часы для детерминированного тестирования ожиданий."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@respx.mock
def test_create_deferred_search_success() -> None:
    clock = FakeClock(datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("Europe/Moscow")))
    client = YandexDeferredClient(
        iam_token="token",
        folder_id="folder",
        sleep_func=clock.sleep,
        now_func=clock.now,
    )

    route = respx.post(SEARCH_ASYNC_URL).mock(
        return_value=httpx.Response(200, json={"id": "op-123", "done": False})
    )

    params = DeferredQueryParams(query_text="site:example.com маркетинг")
    response = client.create_deferred_search(params)

    assert response.id == "op-123"
    assert not response.done
    assert route.called
    request_json = json.loads(route.calls[0].request.content.decode())
    assert request_json["query"]["query_text"] == "site:example.com маркетинг"
    assert request_json["group_spec"]["docs_in_group"] == 1


def test_create_outside_night_window_raises() -> None:
    clock = FakeClock(datetime(2024, 1, 1, 10, 0, tzinfo=ZoneInfo("Europe/Moscow")))
    client = YandexDeferredClient(
        iam_token="token",
        folder_id="folder",
        sleep_func=clock.sleep,
        now_func=clock.now,
        enforce_night_window=True,
    )

    params = DeferredQueryParams(query_text="b2b leads")

    with pytest.raises(NightWindowViolation):
        client.create_deferred_search(params)


@respx.mock
def test_wait_until_ready_decodes_payload() -> None:
    clock = FakeClock(datetime(2024, 1, 1, 1, 30, tzinfo=ZoneInfo("Europe/Moscow")))
    client = YandexDeferredClient(
        iam_token="token",
        folder_id="folder",
        sleep_func=clock.sleep,
        now_func=clock.now,
        poll_interval_seconds=60,
    )

    respx.post(SEARCH_ASYNC_URL).mock(
        return_value=httpx.Response(200, json={"id": "op-456", "done": False})
    )

    raw_xml = "<doc><url>https://example.com</url></doc>".encode()
    encoded = base64.b64encode(raw_xml).decode()

    respx.get(f"{OPERATIONS_URL}/op-456").mock(
        side_effect=[
            httpx.Response(200, json={"id": "op-456", "done": False}),
            httpx.Response(
                200,
                json={
                    "id": "op-456",
                    "done": True,
                    "response": {"rawData": encoded},
                },
            ),
        ]
    )

    params = DeferredQueryParams(query_text="маркетинг")
    operation = client.create_deferred_search(params)
    assert operation.id == "op-456"

    result = client.wait_until_ready("op-456")
    assert result.done is True
    assert result.decode_raw_data() == raw_xml


@respx.mock
def test_wait_until_ready_timeout() -> None:
    clock = FakeClock(datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("Europe/Moscow")))
    client = YandexDeferredClient(
        iam_token="token",
        folder_id="folder",
        sleep_func=clock.sleep,
        now_func=clock.now,
        poll_interval_seconds=30,
        max_wait_minutes=1,
    )

    respx.get(f"{OPERATIONS_URL}/op-789").mock(
        return_value=httpx.Response(200, json={"id": "op-789", "done": False})
    )

    with pytest.raises(OperationTimeout):
        client.wait_until_ready("op-789")


def test_operation_response_decode_missing_rawdata() -> None:
    response = OperationResponse.from_dict({"id": "op-1", "done": True, "response": {}})
    with pytest.raises(InvalidResponseError):
        response.decode_raw_data()
