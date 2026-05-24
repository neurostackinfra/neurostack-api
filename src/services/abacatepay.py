"""
NeuroStack - AbacatePay Client
REST wrapper for AbacatePay payment gateway (PIX + Card subscriptions).
No official Python SDK exists — uses requests directly.
"""

import hashlib
import hmac
import uuid
from typing import Any, Optional

import requests

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
            logger.info("AbacatePay MOCK: create_subscription", amount_cents=amount_cents, frequency=frequency)
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

        resp = requests.post(
            f"{BASE_URL}/billing/subscriptions",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", resp.json())
        return {
            "subscription_id": data.get("id", ""),
            "subscription_url": data.get("url", data.get("payment_url", "")),
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
        resp = requests.post(
            f"{BASE_URL}/billing/charges",
            json=payload,
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", resp.json())
        return {
            "charge_id": data.get("id", ""),
            "qr_code": data.get("brcode", data.get("qr_code", "")),
            "qr_code_url": data.get("qr_code_url", data.get("image_url", "")),
        }

    @staticmethod
    def verify_webhook_signature(payload_body: bytes, signature: str, secret: str) -> bool:
        expected = hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
