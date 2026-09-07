"""
NeuroStack - AbacatePay Client
REST wrapper for AbacatePay payment gateway (PIX + Card subscriptions).
No official Python SDK exists — uses httpx directly.
"""

import hashlib
import hmac
from typing import Any, Optional

import httpx

from src.config.logging import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api.abacatepay.com/v1"

MOCK_SUBSCRIPTION = {
    "subscription_id": "sub_mock_dev_001",
    "subscription_url": "https://pay.abacatepay.com/mock/checkout",
}

MOCK_PIX_CHARGE = {
    "charge_id": "chg_mock_dev_001",
    "qr_code": "00020126580014br.gov.bcb.pix0136mock-pix-key-for-development520400005303986540510.005802BR5913NeuroStack6009SAO PAULO62070503***6304ABCD",
    "qr_code_url": "",  # Em produção AbacatePay retorna uma URL real; mock não tem imagem
}


def _response_json_object(response: httpx.Response) -> dict[str, object]:
    """Decode and validate an upstream JSON object response."""
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise RuntimeError("invalid_upstream_response") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("invalid_upstream_response")

    result: dict[str, object] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise RuntimeError("invalid_upstream_response")
        result[key] = value
    return result


def _nested_response_data(payload: dict[str, object]) -> dict[str, object]:
    """Return a validated nested ``data`` object when the API wraps one."""
    nested = payload.get("data")
    if not isinstance(nested, dict):
        return payload

    result: dict[str, object] = {}
    for key, value in nested.items():
        if not isinstance(key, str):
            raise RuntimeError("invalid_upstream_response")
        result[key] = value
    return result


def _string_field(payload: dict[str, object], *keys: str) -> str:
    """Return the first non-empty string field from an API payload."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class AbacatePayClient:
    """Thin REST client for AbacatePay API."""

    def __init__(self, api_key: str = "", env: str = "development") -> None:
        self._api_key = api_key
        self._env = env

        if env in ("development", "test"):
            self._mock = True
            return

        if not api_key:
            raise RuntimeError(
                f"ABACATEPAY_API_KEY não configurada para ambiente '{env}'. "
                "Defina a variável de ambiente antes de iniciar o servidor."
            )
        self._mock = False

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def create_subscription(
        self,
        amount_cents: int,
        frequency: str,
        method: str,
        description: str,
        customer: dict[str, str],
        metadata: Optional[dict[str, Any]] = None,
        max_installments: Optional[int] = None,
    ) -> dict[str, Any]:
        if self._mock:
            logger.info(
                "AbacatePay MOCK: create_subscription",
                amount_cents=amount_cents,
                frequency=frequency,
            )
            return {**MOCK_SUBSCRIPTION}

        payload: dict[str, Any] = {
            "amount": amount_cents,
            "frequency": frequency,
            "method": method,
            "description": description,
            "customer": customer,
            "metadata": metadata or {},
        }
        if max_installments and max_installments > 1:
            payload["max_installments"] = max_installments

        resp = httpx.post(
            f"{BASE_URL}/billing/subscriptions",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = _nested_response_data(_response_json_object(resp))
        return {
            "subscription_id": _string_field(data, "id"),
            "subscription_url": _string_field(data, "url", "payment_url"),
        }

    def create_pix_charge(
        self,
        amount_cents: int,
        description: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if self._mock:
            logger.info("AbacatePay MOCK: create_pix_charge", amount_cents=amount_cents)
            return {**MOCK_PIX_CHARGE}

        payload = {
            "amount": amount_cents,
            "description": description,
            "metadata": metadata or {},
            "method": "PIX",
        }
        resp = httpx.post(
            f"{BASE_URL}/billing/charges",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = _nested_response_data(_response_json_object(resp))
        return {
            "charge_id": _string_field(data, "id"),
            "qr_code": _string_field(data, "brcode", "qr_code"),
            "qr_code_url": _string_field(data, "qr_code_url", "image_url"),
        }

    @staticmethod
    def verify_webhook_signature(
        payload_body: bytes, signature: str, secret: str
    ) -> bool:
        expected = hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
