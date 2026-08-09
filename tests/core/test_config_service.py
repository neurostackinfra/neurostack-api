"""
NeuroStack - Config Service Unit Tests.

Covers the flat<->bucket routing in `unflatten_patch` / `flatten_config`
for fields wired into `FIELD_TO_BUCKET` — specifically the `timezone`
field on the `operations` bucket that drives the clinic-local greeting.

If the `FIELD_TO_BUCKET["timezone"] = "operations"` entry were removed,
`unflatten_patch` would silently drop the field (see
`src/services/config_service.py` — `if bucket is None: continue`) and
these tests would fail.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.middleware.auth import TenantContext
from src.api.routes.config import patch_config
from src.api.schemas.config import ConfigPatchRequest
from src.models.clinic_config import ClinicScheduling
from src.services.config_service import (
    ALL_BUCKETS,
    ARRAY_BUCKETS,
    FIELD_TO_BUCKET,
    flatten_config,
    unflatten_patch,
)


def _empty_config_row(**overrides):
    """Build a lightweight stand-in for a `ClinicConfigTable` row.

    `flatten_config` reads buckets via `getattr(row, bucket, None)`, so a
    `SimpleNamespace` with the expected attributes is enough — no ORM or
    database required.
    """
    row = SimpleNamespace(
        clinic_id=None,
        version=1,
        **{bucket: ([] if bucket in ARRAY_BUCKETS else {}) for bucket in ALL_BUCKETS},
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class TestTimezoneConfigRoutingAndRoundTrip:
    """`timezone` must live on the `operations` bucket in both directions."""

    def test_timezone_maps_to_operations_bucket(self):
        assert FIELD_TO_BUCKET["timezone"] == "operations"

    def test_unflatten_patch_routes_timezone_to_operations(self):
        grouped = unflatten_patch({"timezone": "America/New_York"})

        assert "operations" in grouped
        assert grouped["operations"] == {"timezone": "America/New_York"}

    def test_unflatten_patch_does_not_leak_timezone_to_other_buckets(self):
        grouped = unflatten_patch({"timezone": "America/New_York"})

        for bucket, payload in grouped.items():
            if bucket == "operations" or bucket in ARRAY_BUCKETS:
                continue
            assert (
                "timezone" not in payload
            ), f"timezone leaked into non-operations bucket {bucket!r}"

    def test_flatten_config_surfaces_timezone_at_top_level(self):
        row = _empty_config_row(
            operations={"timezone": "America/New_York", "working_hours": "8-18"}
        )

        flat = flatten_config(row)

        assert flat["timezone"] == "America/New_York"
        # Sibling field on the same bucket also promotes — sanity check.
        assert flat["working_hours"] == "8-18"

    def test_round_trip_patch_then_flatten_preserves_timezone(self):
        """Simulate the PATCH -> persist -> GET path end-to-end."""
        grouped = unflatten_patch({"timezone": "America/New_York"})

        row = _empty_config_row()
        for bucket, payload in grouped.items():
            setattr(row, bucket, payload)

        flat = flatten_config(row)

        assert flat["timezone"] == "America/New_York"


class TestSchedulingLinkConfigRoutingAndRoundTrip:
    """Scheduling-link fields must live on the `scheduling` bucket.

    These three flat fields (`scheduling_link_enabled`,
    `scheduling_link_url`, `scheduling_link_message`) are the first
    keys to travel through the `scheduling` JSONB bucket via the patch
    API — before this feature, `scheduling` was only used internally by
    the SRM. This test locks the round-trip so a future refactor that
    accidentally drops the mapping surfaces immediately.
    """

    def test_scheduling_link_fields_map_to_scheduling_bucket(self):
        assert FIELD_TO_BUCKET["scheduling_link_enabled"] == "scheduling"
        assert FIELD_TO_BUCKET["scheduling_link_url"] == "scheduling"
        assert FIELD_TO_BUCKET["scheduling_link_message"] == "scheduling"

    def test_unflatten_patch_routes_scheduling_link_fields(self):
        grouped = unflatten_patch(
            {
                "scheduling_link_enabled": True,
                "scheduling_link_url": "https://clinica.example/agendar",
                "scheduling_link_message": "Use o link para agendar:",
            }
        )

        assert "scheduling" in grouped
        assert grouped["scheduling"] == {
            "scheduling_link_enabled": True,
            "scheduling_link_url": "https://clinica.example/agendar",
            "scheduling_link_message": "Use o link para agendar:",
        }

    def test_unflatten_patch_does_not_leak_scheduling_link_fields(self):
        grouped = unflatten_patch(
            {
                "scheduling_link_enabled": True,
                "scheduling_link_url": "https://clinica.example/agendar",
                "scheduling_link_message": "Use o link para agendar:",
            }
        )

        for bucket, payload in grouped.items():
            if bucket == "scheduling" or bucket in ARRAY_BUCKETS:
                continue
            for field in (
                "scheduling_link_enabled",
                "scheduling_link_url",
                "scheduling_link_message",
            ):
                assert (
                    field not in payload
                ), f"{field} leaked into non-scheduling bucket {bucket!r}"

    def test_flatten_config_surfaces_scheduling_link_fields(self):
        row = _empty_config_row(
            scheduling={
                "scheduling_link_enabled": True,
                "scheduling_link_url": "https://clinica.example/agendar",
                "scheduling_link_message": "Use o link para agendar:",
            }
        )

        flat = flatten_config(row)

        assert flat["scheduling_link_enabled"] is True
        assert flat["scheduling_link_url"] == "https://clinica.example/agendar"
        assert flat["scheduling_link_message"] == "Use o link para agendar:"

    def test_round_trip_patch_then_flatten_preserves_scheduling_link(self):
        """Simulate PATCH -> persist scheduling bucket -> GET flat config."""
        grouped = unflatten_patch(
            {
                "scheduling_link_enabled": True,
                "scheduling_link_url": "https://clinica.example/agendar",
                "scheduling_link_message": "Use o link para agendar:",
            }
        )

        row = _empty_config_row()
        for bucket, payload in grouped.items():
            setattr(row, bucket, payload)

        flat = flatten_config(row)

        assert flat["scheduling_link_enabled"] is True
        assert flat["scheduling_link_url"] == "https://clinica.example/agendar"
        assert flat["scheduling_link_message"] == "Use o link para agendar:"


class TestSchedulingLinkUrlValidation:
    """`ClinicScheduling.scheduling_link_url` must be https-only.

    This validator is the enforcement point behind the `patch_config`
    revalidation of the merged `scheduling` bucket — HTTP or other
    schemes must raise `ValidationError` so the API surfaces 422 instead
    of persisting an unsafe URL that would be delivered to patients.
    """

    def test_https_url_is_accepted(self):
        model = ClinicScheduling(scheduling_link_url="https://clinica.example/agendar")
        assert model.scheduling_link_url == "https://clinica.example/agendar"

    def test_none_is_accepted(self):
        model = ClinicScheduling(scheduling_link_url=None)
        assert model.scheduling_link_url is None

    def test_empty_string_is_normalized_to_none(self):
        model = ClinicScheduling(scheduling_link_url="")
        assert model.scheduling_link_url is None

    def test_http_url_is_rejected(self):
        with pytest.raises(ValidationError) as exc:
            ClinicScheduling(scheduling_link_url="http://clinica.example/agendar")
        assert "https" in str(exc.value)

    def test_ftp_url_is_rejected(self):
        with pytest.raises(ValidationError):
            ClinicScheduling(scheduling_link_url="ftp://clinica.example/agendar")

    def test_bare_string_is_rejected(self):
        with pytest.raises(ValidationError):
            ClinicScheduling(scheduling_link_url="clinica.example/agendar")

    def test_javascript_scheme_is_rejected(self):
        with pytest.raises(ValidationError):
            ClinicScheduling(scheduling_link_url="javascript:alert(1)")

    def test_https_without_host_is_rejected(self):
        with pytest.raises(ValidationError):
            ClinicScheduling(scheduling_link_url="https://")


class TestSchedulingLinkPatchBoundary:
    """The PATCH boundary validates merged state and audits URL changes."""

    @staticmethod
    def _row(**scheduling: Any) -> SimpleNamespace:
        return _empty_config_row(scheduling=scheduling)

    @staticmethod
    def _session() -> SimpleNamespace:
        async def flush() -> None:
            pass

        async def commit() -> None:
            pass

        return SimpleNamespace(flush=flush, commit=commit)

    @pytest.mark.asyncio
    async def test_non_https_patch_returns_422_without_persisting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = TenantContext(tenant_id=uuid4(), role="clinic_admin")
        row = self._row(
            scheduling_link_enabled=True,
            scheduling_link_url="https://old.example/agendar",
            scheduling_link_message="Agende aqui:",
        )

        async def keep_tenant(*args: Any, **kwargs: Any) -> TenantContext:
            return tenant

        async def get_row(*args: Any, **kwargs: Any) -> SimpleNamespace:
            return row

        monkeypatch.setattr("src.api.routes.config._resolve_config_tenant", keep_tenant)
        monkeypatch.setattr("src.api.routes.config._get_or_create_config", get_row)

        with pytest.raises(HTTPException) as exc:
            await patch_config(
                request=SimpleNamespace(query_params={}),
                body=ConfigPatchRequest(
                    scheduling_link_url="http://unsafe.example/agendar"
                ),
                tenant=tenant,
                session=self._session(),  # type: ignore[arg-type]
            )

        assert exc.value.status_code == 422
        assert row.scheduling["scheduling_link_url"] == "https://old.example/agendar"

    @pytest.mark.asyncio
    async def test_url_change_persists_and_emits_structured_audit_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant = TenantContext(tenant_id=uuid4(), role="clinic_admin")
        row = self._row(scheduling_link_url="https://old.example/agendar")
        audit_info = Mock()

        async def keep_tenant(*args: Any, **kwargs: Any) -> TenantContext:
            return tenant

        async def get_row(*args: Any, **kwargs: Any) -> SimpleNamespace:
            return row

        async def no_cache(*args: Any, **kwargs: Any) -> None:
            pass

        monkeypatch.setattr("src.api.routes.config._resolve_config_tenant", keep_tenant)
        monkeypatch.setattr("src.api.routes.config._get_or_create_config", get_row)
        monkeypatch.setattr("src.api.routes.config._audit_logger.info", audit_info)
        monkeypatch.setattr(
            "src.services.event_service.invalidate_clinic_config_cache", no_cache
        )

        response = await patch_config(
            request=SimpleNamespace(query_params={}),
            body=ConfigPatchRequest(scheduling_link_url="https://new.example/agendar"),
            tenant=tenant,
            session=self._session(),  # type: ignore[arg-type]
        )

        assert row.scheduling["scheduling_link_url"] == "https://new.example/agendar"
        assert response.scheduling_link_url == "https://new.example/agendar"
        audit_info.assert_called_once_with(
            "scheduling_link_url_changed",
            tenant_id=str(tenant.tenant_id),
            previous_url="https://old.example/agendar",
            new_url="https://new.example/agendar",
        )
