"""
NeuroStack - Handoff Webhook Dispatcher
Handles webhook notifications to external systems for handoff events.
"""

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import httpx

from src.config.logging import get_logger
from src.core.exceptions import WebhookConfigError, WebhookDeliveryError, WebhookError
from src.handoff.models import (
    HandoffReason,
    HandoffSector,
    HandoffSLA,
    HandoffStatus,
    HandoffUrgency,
    WebhookConfig,
    WebhookDelivery,
    WebhookPayload,
    WebhookStatus,
)
from src.observability.metric_dimensions import ContractCounter as Counter
from src.observability.metric_dimensions import ContractHistogram as Histogram

logger = get_logger(__name__)


# ============================================
# Prometheus Metrics
# ============================================

WEBHOOK_OPERATIONS = Counter(
    "neurostack_handoff_webhook_operations_total",
    "Total number of webhook operations",
    ["operation", "status"],
)

WEBHOOK_LATENCY = Histogram(
    "neurostack_handoff_webhook_latency_seconds",
    "Webhook operation latency in seconds",
    ["operation"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

WEBHOOK_RETRIES = Counter(
    "neurostack_handoff_webhook_retries_total",
    "Total number of webhook retries",
    [],
)


# ============================================
# Webhook Dispatcher
# ============================================


class WebhookDispatcher:
    """
    Dispatches webhooks to external systems for handoff events.

    Features:
    - Async HTTP delivery with timeout
    - Automatic retries with exponential backoff
    - HMAC signature for security
    - Delivery tracking and logging
    """

    def __init__(
        self,
        default_timeout: int = 30,
        max_retries: int = 3,
        retry_delay: int = 5,
    ):
        """
        Initialize the webhook dispatcher.

        Args:
            default_timeout: Default timeout in seconds
            max_retries: Maximum number of retries
            retry_delay: Base delay between retries in seconds
        """
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._client: Optional[httpx.AsyncClient] = None

        # In-memory config store (should be replaced with persistent storage)
        self._configs: dict[UUID, WebhookConfig] = {}

        # Delivery history (should be replaced with persistent storage)
        self._deliveries: list[WebhookDelivery] = []

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.default_timeout),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ============================================
    # Configuration Management
    # ============================================

    def set_config(self, tenant_id: UUID, config: WebhookConfig) -> None:
        """
        Set webhook configuration for a tenant.

        Args:
            tenant_id: Tenant identifier
            config: Webhook configuration
        """
        if not config.url:
            raise WebhookConfigError("Webhook URL is required")

        self._configs[tenant_id] = config
        logger.info(
            "Webhook config set",
            tenant_id=str(tenant_id),
            url=config.url,
            enabled=config.enabled,
        )

    def get_config(self, tenant_id: UUID) -> Optional[WebhookConfig]:
        """
        Get webhook configuration for a tenant.

        Args:
            tenant_id: Tenant identifier

        Returns:
            WebhookConfig or None if not configured
        """
        return self._configs.get(tenant_id)

    def remove_config(self, tenant_id: UUID) -> bool:
        """
        Remove webhook configuration for a tenant.

        Args:
            tenant_id: Tenant identifier

        Returns:
            True if config was removed, False if not found
        """
        if tenant_id in self._configs:
            del self._configs[tenant_id]
            logger.info("Webhook config removed", tenant_id=str(tenant_id))
            return True
        return False

    def is_configured(self, tenant_id: UUID) -> bool:
        """Check if webhook is configured for tenant."""
        config = self._configs.get(tenant_id)
        return config is not None and config.enabled

    # ============================================
    # Payload Creation
    # ============================================

    def create_payload(
        self,
        handoff_id: UUID,
        tenant_id: UUID,
        session_id: UUID,
        status: HandoffStatus,
        urgency: HandoffUrgency,
        reason: HandoffReason,
        summary: str,
        key_info: Optional[dict[str, Any]] = None,
        turn_count: int = 0,
        funnel_stage: str = "unknown",
        recent_messages: Optional[list[dict[str, str]]] = None,
        sector: Optional[HandoffSector] = None,
        sla: Optional[HandoffSLA] = None,
        verification_status: Optional[str] = None,
    ) -> WebhookPayload:
        """
        Create a webhook payload (doc: setor + SLA).
        """
        event_type = f"handoff.{status.value}"
        return WebhookPayload(
            event_type=event_type,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
            session_id=session_id,
            status=status,
            urgency=urgency,
            reason=reason,
            sector=sector,
            sla=sla,
            summary=summary,
            key_info=key_info or {},
            verification_status=verification_status,
            turn_count=turn_count,
            funnel_stage=funnel_stage,
            recent_messages=recent_messages or [],
        )

    # ============================================
    # Signature Generation
    # ============================================

    def generate_signature(
        self,
        payload: dict[str, Any],
        secret: str,
    ) -> str:
        """
        Generate HMAC signature for payload.

        Args:
            payload: Payload dictionary
            secret: Secret key for signing

        Returns:
            HMAC-SHA256 signature as hex string
        """
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        signature = hmac.new(
            secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={signature}"

    # ============================================
    # Delivery
    # ============================================

    async def send(
        self,
        tenant_id: UUID,
        payload: WebhookPayload,
    ) -> WebhookDelivery:
        """
        Send a webhook notification.

        Args:
            tenant_id: Tenant identifier
            payload: Webhook payload to send

        Returns:
            WebhookDelivery with result

        Raises:
            WebhookConfigError: If webhook not configured
            WebhookDeliveryError: If delivery fails after retries
        """
        config = self._configs.get(tenant_id)
        if not config:
            raise WebhookConfigError(f"No webhook configured for tenant {tenant_id}")

        if not config.enabled:
            logger.info(
                "Webhook disabled for tenant",
                tenant_id=str(tenant_id),
            )
            delivery = WebhookDelivery(
                handoff_id=payload.handoff_id,
                tenant_id=tenant_id,
                url=config.url,
                status=WebhookStatus.FAILED,
                request_body=payload.to_dict(),
                error_message="Webhook disabled",
            )
            self._deliveries.append(delivery)
            return delivery

        # Create delivery record
        delivery = WebhookDelivery(
            handoff_id=payload.handoff_id,
            tenant_id=tenant_id,
            url=config.url,
            request_body=payload.to_dict(),
        )

        # Try to send with retries
        start_time = time.perf_counter()
        last_error: Optional[Exception] = None

        for attempt in range(1, config.retry_count + 1):
            delivery.attempt = attempt

            try:
                result = await self._send_request(config, payload)

                # Success
                delivery.status = WebhookStatus.SENT
                delivery.response_status = result.get("status_code")
                delivery.response_body = result.get("body")
                delivery.sent_at = datetime.now(timezone.utc)

                latency = time.perf_counter() - start_time
                WEBHOOK_LATENCY.labels(operation="send").observe(latency)
                WEBHOOK_OPERATIONS.labels(operation="send", status="success").inc()

                logger.info(
                    "Webhook sent successfully",
                    tenant_id=str(tenant_id),
                    handoff_id=str(payload.handoff_id),
                    attempt=attempt,
                    status_code=result.get("status_code"),
                )

                self._deliveries.append(delivery)
                return delivery

            except (ConnectionError, TimeoutError) as e:
                last_error = e
                logger.warning(
                    "Webhook delivery attempt failed - connection/timeout",
                    tenant_id=str(tenant_id),
                    handoff_id=str(payload.handoff_id),
                    attempt=attempt,
                    error=str(e),
                    error_type=type(e).__name__,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "Webhook delivery attempt failed - unexpected error",
                    tenant_id=str(tenant_id),
                    handoff_id=str(payload.handoff_id),
                    attempt=attempt,
                    error=str(e),
                )

                if attempt < config.retry_count:
                    # Exponential backoff
                    delay = config.retry_delay_seconds * (2 ** (attempt - 1))
                    delivery.status = WebhookStatus.RETRYING
                    WEBHOOK_RETRIES.inc()
                    await asyncio.sleep(delay)

        # All retries failed
        delivery.status = WebhookStatus.FAILED
        delivery.error_message = str(last_error) if last_error else "Unknown error"

        if isinstance(last_error, WebhookDeliveryError):
            delivery.response_status = last_error.status_code
            delivery.response_body = last_error.response_body

        latency = time.perf_counter() - start_time
        WEBHOOK_LATENCY.labels(operation="send").observe(latency)
        WEBHOOK_OPERATIONS.labels(operation="send", status="failed").inc()

        logger.error(
            "Webhook delivery failed after all retries",
            tenant_id=str(tenant_id),
            handoff_id=str(payload.handoff_id),
            attempts=config.retry_count,
            error=str(last_error),
        )

        self._deliveries.append(delivery)
        return delivery

    async def _send_request(
        self,
        config: WebhookConfig,
        payload: WebhookPayload,
    ) -> dict[str, Any]:
        """
        Send the actual HTTP request.

        Args:
            config: Webhook configuration
            payload: Payload to send

        Returns:
            Dict with status_code and body

        Raises:
            WebhookDeliveryError: If request fails
        """
        client = await self._get_client()

        # Prepare headers
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "NeuroStack-Webhook/1.0",
            "X-Webhook-Event": payload.event_type,
            "X-Webhook-Timestamp": payload.timestamp.isoformat(),
            **config.headers,
        }

        # Add signature if secret is configured
        payload_dict = payload.to_dict()
        if config.secret:
            signature = self.generate_signature(payload_dict, config.secret)
            headers["X-Webhook-Signature"] = signature

        try:
            response = await client.post(
                config.url,
                json=payload_dict,
                headers=headers,
                timeout=config.timeout_seconds,
            )

            # Check for success (2xx status codes)
            if 200 <= response.status_code < 300:
                return {
                    "status_code": response.status_code,
                    "body": response.text[:1000],  # Limit response body size
                }
            else:
                raise WebhookDeliveryError(
                    f"HTTP {response.status_code}",
                    status_code=response.status_code,
                    response_body=response.text[:1000],
                )

        except httpx.TimeoutException as e:
            raise WebhookDeliveryError(f"Request timeout: {e}")
        except httpx.ConnectError as e:
            raise WebhookDeliveryError(f"Connection error: {e}")
        except httpx.RequestError as e:
            raise WebhookDeliveryError(f"Request error: {e}")

    # ============================================
    # Fire and Forget
    # ============================================

    async def send_async(
        self,
        tenant_id: UUID,
        payload: WebhookPayload,
    ) -> None:
        """
        Send webhook in background without waiting.

        Args:
            tenant_id: Tenant identifier
            payload: Webhook payload to send
        """
        asyncio.create_task(self._send_background(tenant_id, payload))

    async def _send_background(
        self,
        tenant_id: UUID,
        payload: WebhookPayload,
    ) -> None:
        """Background task for sending webhook."""
        try:
            await self.send(tenant_id, payload)
        except WebhookError as e:
            logger.error(
                "Background webhook delivery failed",
                tenant_id=str(tenant_id),
                handoff_id=str(payload.handoff_id),
                error=str(e),
            )

    # ============================================
    # Delivery History
    # ============================================

    def get_deliveries(
        self,
        tenant_id: Optional[UUID] = None,
        handoff_id: Optional[UUID] = None,
        limit: int = 100,
    ) -> list[WebhookDelivery]:
        """
        Get webhook delivery history.

        Args:
            tenant_id: Filter by tenant
            handoff_id: Filter by handoff
            limit: Maximum records to return

        Returns:
            List of WebhookDelivery records
        """
        result = self._deliveries

        if tenant_id:
            result = [d for d in result if d.tenant_id == tenant_id]
        if handoff_id:
            result = [d for d in result if d.handoff_id == handoff_id]

        return result[-limit:]

    def get_delivery_stats(
        self,
        tenant_id: UUID,
    ) -> dict[str, Any]:
        """
        Get delivery statistics for a tenant.

        Args:
            tenant_id: Tenant identifier

        Returns:
            Statistics dictionary
        """
        deliveries = [d for d in self._deliveries if d.tenant_id == tenant_id]

        if not deliveries:
            return {
                "total": 0,
                "sent": 0,
                "failed": 0,
                "success_rate": 0.0,
            }

        sent = sum(1 for d in deliveries if d.status == WebhookStatus.SENT)
        failed = sum(1 for d in deliveries if d.status == WebhookStatus.FAILED)

        return {
            "total": len(deliveries),
            "sent": sent,
            "failed": failed,
            "success_rate": sent / len(deliveries) if deliveries else 0.0,
        }


# ============================================
# Singleton Instance
# ============================================

_dispatcher: Optional[WebhookDispatcher] = None


def get_webhook_dispatcher(
    default_timeout: int = 30,
    max_retries: int = 3,
) -> WebhookDispatcher:
    """Get or create the global WebhookDispatcher instance."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = WebhookDispatcher(
            default_timeout=default_timeout,
            max_retries=max_retries,
        )
    return _dispatcher


async def close_webhook_dispatcher() -> None:
    """Close the global WebhookDispatcher instance."""
    global _dispatcher
    if _dispatcher is not None:
        await _dispatcher.close()
        _dispatcher = None
