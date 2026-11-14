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

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import src.api.routes.config as config_routes
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
        monkeypatch.setattr("src.api.routes.config._load_config", get_row)

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
        monkeypatch.setattr("src.api.routes.config._load_config", get_row)
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


_PROMISE_PT = "Seu agendamento está confirmado! Acesse o link:"
_PROMISE_EN = "Your appointment is confirmed, open the link:"
_NEUTRAL_PT = "Clique no link para escolher o melhor horário para você:"
_DEFAULT_PT = "Para concluir seu agendamento, acesse o link abaixo:"
# UI placeholder of the "Mensagem do Link de Agendamento" field
# (frontend/artifacts/neurostack/src/pages/clinic-config.tsx).
_PLACEHOLDER_PT = (
    "Ex: Perfeito! Para concluir seu agendamento, acesse o link abaixo e "
    "escolha o horário desejado. Depois me avise se conseguiu confirmar. 😊"
)
_PLACEHOLDER_EN = (
    "e.g. Great! To finish scheduling, use the link below and pick a time. "
    "Let me know once you've confirmed. 😊"
)
_LINK_URL = "https://agenda.example/marcar"


class TestSchedulingLinkMessagePromiseOnSave:
    """PATCH rejects a link message that promises a booking, persisting nothing.

    The agent sends ``<message>\\n\\n<url>`` verbatim on a deterministic path
    that bypasses the output guard, so the save is the only checkpoint.
    """

    @staticmethod
    def _session() -> SimpleNamespace:
        calls: list[str] = []

        async def flush() -> None:
            calls.append("flush")

        async def commit() -> None:
            calls.append("commit")

        return SimpleNamespace(flush=flush, commit=commit, calls=calls)

    @staticmethod
    async def _patch(
        monkeypatch: pytest.MonkeyPatch,
        row: SimpleNamespace,
        session: SimpleNamespace,
        **fields: Any,
    ) -> Any:
        tenant = TenantContext(tenant_id=uuid4(), role="clinic_admin")

        async def keep_tenant(*args: Any, **kwargs: Any) -> TenantContext:
            return tenant

        async def get_row(*args: Any, **kwargs: Any) -> SimpleNamespace:
            return row

        async def no_cache(*args: Any, **kwargs: Any) -> None:
            pass

        monkeypatch.setattr("src.api.routes.config._resolve_config_tenant", keep_tenant)
        monkeypatch.setattr("src.api.routes.config._load_config", get_row)
        monkeypatch.setattr(
            "src.services.event_service.invalidate_clinic_config_cache", no_cache
        )
        return await patch_config(
            request=SimpleNamespace(query_params={}),
            body=ConfigPatchRequest(**fields),
            tenant=tenant,
            session=session,  # type: ignore[arg-type]
        )

    async def _assert_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stored: dict[str, Any],
        **fields: Any,
    ) -> None:
        row = _empty_config_row(scheduling=deepcopy(stored))
        before = flatten_config(row)
        session = self._session()

        with pytest.raises(HTTPException) as exc:
            await self._patch(monkeypatch, row, session, **fields)

        assert exc.value.status_code == 422
        detail = exc.value.detail
        assert isinstance(detail, list) and len(detail) == 1
        assert detail[0]["loc"][-1] == "scheduling_link_message"
        assert detail[0]["loc"] == ["body", "scheduling_link_message"]
        assert detail[0]["type"] == "scheduling_link_message_promise"
        assert isinstance(detail[0]["msg"], str) and detail[0]["msg"]
        for text in fields.values():
            if isinstance(text, str) and text:
                assert text not in detail[0]["msg"]
        stored_message = stored.get("scheduling_link_message")
        if stored_message:
            assert stored_message not in detail[0]["msg"]
        # Nothing persisted: no flush/commit and the stored config is unchanged.
        assert session.calls == []
        assert row.scheduling == stored
        assert flatten_config(row) == before

    async def _assert_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stored: dict[str, Any],
        **fields: Any,
    ) -> dict[str, Any]:
        row = _empty_config_row(scheduling=deepcopy(stored))
        session = self._session()

        await self._patch(monkeypatch, row, session, **fields)

        assert session.calls == ["flush", "commit"]
        flat: dict[str, Any] = flatten_config(row)
        return flat

    # -- 422: a new message with a promise --------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [_PROMISE_PT, _PROMISE_EN])
    async def test_new_promise_message_returns_422_without_persisting(
        self, monkeypatch: pytest.MonkeyPatch, message: str
    ) -> None:
        await self._assert_rejected(
            monkeypatch,
            {
                "scheduling_link_enabled": True,
                "scheduling_link_url": _LINK_URL,
                "scheduling_link_message": _NEUTRAL_PT,
            },
            scheduling_link_message=message,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [_PROMISE_PT, _PROMISE_EN])
    async def test_new_promise_message_on_empty_row_returns_422(
        self, monkeypatch: pytest.MonkeyPatch, message: str
    ) -> None:
        await self._assert_rejected(
            monkeypatch,
            {},
            scheduling_link_enabled=True,
            scheduling_link_url=_LINK_URL,
            scheduling_link_message=message,
        )

    @pytest.mark.asyncio
    async def test_rejection_log_names_category_but_never_the_message(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("DEBUG", logger="src.api.routes.config")

        await self._assert_rejected(
            monkeypatch,
            {},
            scheduling_link_enabled=True,
            scheduling_link_url=_LINK_URL,
            scheduling_link_message=_PROMISE_PT,
        )

        assert "confirmed_appointment" in caplog.text
        assert _PROMISE_PT not in caplog.text
        assert "confirmado" not in caplog.text

    # -- 422: the link switches on over a stored promise message ----------

    @pytest.mark.asyncio
    async def test_switch_on_with_stored_promise_and_equal_message_returns_422(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._assert_rejected(
            monkeypatch,
            {
                "scheduling_link_enabled": False,
                "scheduling_link_url": _LINK_URL,
                "scheduling_link_message": _PROMISE_PT,
            },
            scheduling_link_enabled=True,
            scheduling_link_message=_PROMISE_PT,
        )

    @pytest.mark.asyncio
    async def test_switch_on_with_stored_promise_and_absent_message_returns_422(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._assert_rejected(
            monkeypatch,
            {
                "scheduling_link_enabled": False,
                "scheduling_link_url": _LINK_URL,
                "scheduling_link_message": _PROMISE_EN,
            },
            scheduling_link_enabled=True,
        )

    @pytest.mark.asyncio
    async def test_switch_on_with_stored_toggle_key_missing_returns_422(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._assert_rejected(
            monkeypatch,
            {
                "scheduling_link_url": _LINK_URL,
                "scheduling_link_message": _PROMISE_PT,
            },
            scheduling_link_enabled=True,
        )

    @pytest.mark.asyncio
    async def test_switch_on_with_stored_toggle_as_string_returns_422(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self._assert_rejected(
            monkeypatch,
            {
                "scheduling_link_enabled": "no",
                "scheduling_link_url": _LINK_URL,
                "scheduling_link_message": _PROMISE_PT,
            },
            scheduling_link_enabled=True,
            scheduling_link_message=_PROMISE_PT,
        )

    @pytest.mark.asyncio
    async def test_url_added_to_enabled_row_without_url_returns_422(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cloned clinics and old rows keep the message with no URL; the link
        # becomes effectively on only when the URL arrives.
        await self._assert_rejected(
            monkeypatch,
            {
                "scheduling_link_enabled": True,
                "scheduling_link_url": None,
                "scheduling_link_message": _PROMISE_PT,
            },
            scheduling_link_url=_LINK_URL,
        )

    @pytest.mark.asyncio
    async def test_switch_on_reads_validated_toggle_coerced_from_stored_yes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stored "yes" is not `is True` (off before); the validated bucket
        # coerces it to True, so supplying the URL switches the link on.
        await self._assert_rejected(
            monkeypatch,
            {
                "scheduling_link_enabled": "yes",
                "scheduling_link_url": None,
                "scheduling_link_message": _PROMISE_PT,
            },
            scheduling_link_url="https://agenda.exemplo.com/marcar",
        )

    @pytest.mark.asyncio
    async def test_switch_on_reads_validated_toggle_coerced_from_string_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The flat PATCH schema passes an undeclared field through unchanged,
        # so the merged bucket carries the string "true"; only the validated
        # bucket has the boolean True that makes the link effectively on.
        assert (
            ConfigPatchRequest(scheduling_link_enabled="true").model_dump(
                exclude_unset=True
            )["scheduling_link_enabled"]
            == "true"
        )
        await self._assert_rejected(
            monkeypatch,
            {
                "scheduling_link_enabled": False,
                "scheduling_link_url": _LINK_URL,
                "scheduling_link_message": _PROMISE_PT,
            },
            scheduling_link_enabled="true",
        )

    @pytest.mark.asyncio
    async def test_mixed_bucket_patch_with_promise_never_flushes_or_commits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Other buckets may already be set on the unsaved row; what matters
        # is that the session is never flushed or committed.
        row = _empty_config_row(scheduling={})
        session = self._session()

        with pytest.raises(HTTPException) as exc:
            await self._patch(
                monkeypatch,
                row,
                session,
                timezone="America/New_York",
                scheduling_link_enabled=True,
                scheduling_link_url=_LINK_URL,
                scheduling_link_message=_PROMISE_PT,
            )

        assert exc.value.status_code == 422
        detail = exc.value.detail
        assert isinstance(detail, list)
        assert detail[0]["type"] == "scheduling_link_message_promise"
        assert session.calls == []

    # -- 200 ---------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_switch_on_with_stored_neutral_message_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flat = await self._assert_accepted(
            monkeypatch,
            {
                "scheduling_link_enabled": False,
                "scheduling_link_url": _LINK_URL,
                "scheduling_link_message": _NEUTRAL_PT,
            },
            scheduling_link_enabled=True,
        )
        assert flat["scheduling_link_enabled"] is True
        assert flat["scheduling_link_message"] == _NEUTRAL_PT

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "message", [_DEFAULT_PT, _NEUTRAL_PT, _PLACEHOLDER_PT, _PLACEHOLDER_EN]
    )
    async def test_safe_messages_are_accepted(
        self, monkeypatch: pytest.MonkeyPatch, message: str
    ) -> None:
        flat = await self._assert_accepted(
            monkeypatch,
            {},
            scheduling_link_enabled=True,
            scheduling_link_url=_LINK_URL,
            scheduling_link_message=message,
        )
        assert flat["scheduling_link_message"] == message
        assert flat["scheduling_link_enabled"] is True

    @pytest.mark.asyncio
    async def test_unchanged_old_promise_does_not_block_other_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        flat = await self._assert_accepted(
            monkeypatch,
            {
                "scheduling_link_enabled": True,
                "scheduling_link_url": _LINK_URL,
                "scheduling_link_message": _PROMISE_PT,
            },
            scheduling_link_enabled=True,
            scheduling_link_url=_LINK_URL,
            scheduling_link_message=_PROMISE_PT,
            timezone="America/New_York",
        )
        assert flat["timezone"] == "America/New_York"
        assert flat["scheduling_link_message"] == _PROMISE_PT
        assert flat["scheduling_link_enabled"] is True

    # -- pure helper truth table ------------------------------------------

    @pytest.mark.parametrize(
        ("prev", "new", "expected"),
        [
            # (a) message changed, link already on
            pytest.param(
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _NEUTRAL_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="on-on-message-changed",
            ),
            # (a) message changed, link stays off: still checked
            pytest.param(
                {"scheduling_link_message": _NEUTRAL_PT},
                {
                    "scheduling_link_enabled": False,
                    "scheduling_link_url": None,
                    "scheduling_link_message": _PROMISE_EN,
                },
                "confirmed_appointment",
                id="off-off-message-changed",
            ),
            # neither (a) nor (b): stored on, equal message
            pytest.param(
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                None,
                id="on-on-message-equal",
            ),
            # neither: link stays off, equal message
            pytest.param(
                {
                    "scheduling_link_enabled": False,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": False,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                None,
                id="off-off-message-equal",
            ),
            # neither: toggle on but URL stays empty, equal message
            pytest.param(
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": "",
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": None,
                    "scheduling_link_message": _PROMISE_PT,
                },
                None,
                id="on-without-url-stays-off",
            ),
            # (b) toggle False -> True with URL, equal message
            pytest.param(
                {
                    "scheduling_link_enabled": False,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="switch-on-toggle",
            ),
            # (b) stored URL empty string -> URL supplied
            pytest.param(
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": "",
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="switch-on-url-from-empty",
            ),
            # (b) stored URL missing -> URL supplied
            pytest.param(
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="switch-on-url-from-missing",
            ),
            # (b) off-pattern stored toggles count as off before
            pytest.param(
                {
                    "scheduling_link_enabled": "no",
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="stored-toggle-string-no",
            ),
            pytest.param(
                {
                    "scheduling_link_enabled": "yes",
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="stored-toggle-string-yes",
            ),
            pytest.param(
                {
                    "scheduling_link_enabled": None,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="stored-toggle-none",
            ),
            pytest.param(
                {
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="stored-toggle-missing",
            ),
            # (b) on a neutral message: checked, detector accepts
            pytest.param(
                {
                    "scheduling_link_enabled": False,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _NEUTRAL_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _NEUTRAL_PT,
                },
                None,
                id="switch-on-neutral",
            ),
            # switching off is never a reason to check an equal message
            pytest.param(
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": False,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                None,
                id="switch-off-message-equal",
            ),
            # empty / None message is never checked
            pytest.param(
                {"scheduling_link_message": _PROMISE_PT},
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": None,
                },
                None,
                id="message-none-switch-on",
            ),
            pytest.param(
                {"scheduling_link_message": _PROMISE_PT},
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": "",
                },
                None,
                id="message-empty-switch-on",
            ),
            # exact comparison: whitespace-only difference counts as changed
            pytest.param(
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT + " ",
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="message-exact-comparison",
            ),
            # (b) a whitespace-only stored URL is empty, so the link was off
            pytest.param(
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": "   ",
                    "scheduling_link_message": _PROMISE_PT,
                },
                {
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": _LINK_URL,
                    "scheduling_link_message": _PROMISE_PT,
                },
                "confirmed_appointment",
                id="switch-on-url-from-whitespace",
            ),
        ],
    )
    def test_link_message_promise_truth_table(
        self,
        prev: dict[str, Any],
        new: dict[str, Any],
        expected: Any,
    ) -> None:
        assert config_routes._link_message_promise(prev, new) == expected

    @pytest.mark.asyncio
    async def test_detector_error_is_fail_open_and_the_save_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.observability.scheduling_gate as scheduling_gate

        class _ExplodingPattern:
            def finditer(self, text: str) -> Any:
                raise RuntimeError("synthetic detector failure")

        original = scheduling_gate._CATEGORY_ORDER
        assert scheduling_gate.detect_scheduling_violation(_PROMISE_PT) is not None
        monkeypatch.setattr(
            scheduling_gate,
            "_CATEGORY_ORDER",
            ((original[0][0], _ExplodingPattern()), *original[1:]),
        )

        flat = await self._assert_accepted(
            monkeypatch,
            {},
            scheduling_link_enabled=True,
            scheduling_link_url=_LINK_URL,
            scheduling_link_message=_PROMISE_PT,
        )

        assert flat["scheduling_link_message"] == _PROMISE_PT
        assert flat["scheduling_link_enabled"] is True
