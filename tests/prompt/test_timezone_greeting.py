"""
NeuroStack - Timezone-driven greeting tests.

Locks the behavior that the assistant greeting ("Bom dia" / "Boa tarde" /
"Boa noite") and the date/time line in `NormalTemplate.get_context_instructions`
are computed from the clinic's configured IANA timezone rather than a fixed
UTC offset or the server clock.

If `packages/neurostack-core/neurostack_core/prompt/templates/normal.py` were reverted to a hardcoded
`timezone(timedelta(hours=-3))` (or any offset that ignores the clinic
config), these tests would fail:
  * `test_config_drives_result_not_server_clock` asserts that two clinics
    at the SAME UTC instant get DIFFERENT greetings solely because their
    configured timezones differ — impossible with a hardcoded offset.
  * The other tests assert on the rendered hour (`09:00` / `20:00`) which
    only match when the tz is actually applied to a controlled UTC instant.
"""

from datetime import datetime, timezone

from neurostack_core.prompt.models import PromptVariables
from neurostack_core.prompt.templates.normal import NormalTemplate

# ============================================
# Helpers
# ============================================


def _fixed_datetime_class(fixed_utc: datetime):
    """Return a stand-in for the `datetime` class used inside
    `neurostack_core.prompt.timeutils`. Its `.now(tz)` returns the fixed instant
    projected into `tz` (or the raw fixed instant when `tz` is None).

    Monkeypatching `neurostack_core.prompt.timeutils.datetime` with this class means
    `clinic_now(tz_str)` still exercises the real `resolve_clinic_tz`
    branch (real ZoneInfo lookup + fallback) — only the wall-clock is
    frozen. That way the tests validate that the tz actually flows
    through, instead of stubbing out the whole clinic_now function.
    """

    class _FixedDatetime:
        @staticmethod
        def now(tz=None):
            if tz is None:
                return fixed_utc
            return fixed_utc.astimezone(tz)

    return _FixedDatetime


def _saudacao_variables(tz_value):
    """Build a `PromptVariables` targeted at the `saudacao` intent branch,
    so the greeting text renders inside `get_context_instructions`."""
    return PromptVariables(
        custom={"timezone": tz_value},
        last_intent="saudacao",
    )


# ============================================
# Tests
# ============================================


class TestTimezoneGreeting:
    """`get_context_instructions` must derive its greeting from the
    clinic-configured timezone, not the server clock or a hardcoded offset."""

    def test_sao_paulo_morning_produces_bom_dia(self, monkeypatch):
        # 12:00 UTC on a non-DST day -> 09:00 in America/Sao_Paulo (UTC-3).
        fixed = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "neurostack_core.prompt.timeutils.datetime", _fixed_datetime_class(fixed)
        )

        out = NormalTemplate().get_context_instructions(
            _saudacao_variables("America/Sao_Paulo")
        )

        assert 'A saudação correta é: **"Bom dia"**' in out
        # Hour projected into the configured tz is rendered on the datetime line
        # AND in the saudacao intent block -> proves the tz reached both callsites.
        assert "09:00" in out
        assert "horário local da clínica" in out
        # Old hardcoded label must not resurface.
        assert "horário de Brasília" not in out

    def test_new_york_evening_produces_boa_noite(self, monkeypatch):
        # 01:00 UTC on 2026-01-16 -> 20:00 on 2026-01-15 in America/New_York
        # (EST, UTC-5 in January) -> Boa noite (hour >= 18).
        fixed = datetime(2026, 1, 16, 1, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "neurostack_core.prompt.timeutils.datetime", _fixed_datetime_class(fixed)
        )

        out = NormalTemplate().get_context_instructions(
            _saudacao_variables("America/New_York")
        )

        assert 'A saudação correta é: **"Boa noite"**' in out
        assert "20:00" in out
        assert "horário local da clínica" in out

    def test_config_drives_result_not_server_clock(self, monkeypatch):
        """At the SAME UTC instant, two clinics with different tz configs
        must produce different greetings. This is the true regression guard:
        a hardcoded offset (or `datetime.now()` without the clinic tz) would
        yield the SAME greeting for both clinics, failing this test."""
        # 01:00 UTC on 2026-01-16 ->
        #   America/New_York (EST, UTC-5): 20:00 on 2026-01-15 -> Boa noite
        #   Asia/Tokyo       (JST, UTC+9): 10:00 on 2026-01-16 -> Bom dia
        fixed = datetime(2026, 1, 16, 1, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "neurostack_core.prompt.timeutils.datetime", _fixed_datetime_class(fixed)
        )
        template = NormalTemplate()

        ny = template.get_context_instructions(_saudacao_variables("America/New_York"))
        tokyo = template.get_context_instructions(_saudacao_variables("Asia/Tokyo"))

        assert 'A saudação correta é: **"Boa noite"**' in ny
        assert 'A saudação correta é: **"Bom dia"**' in tokyo
        # Hour rendered on the datetime line reflects the configured tz.
        assert "20:00" in ny
        assert "10:00" in tokyo

    def test_invalid_timezone_does_not_raise_and_falls_back(self, monkeypatch):
        """Invalid IANA strings must NOT raise; the greeting still renders
        via the São Paulo fallback path in `resolve_clinic_tz`."""
        # 12:00 UTC -> 09:00 in São Paulo (fallback) -> Bom dia.
        fixed = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "neurostack_core.prompt.timeutils.datetime", _fixed_datetime_class(fixed)
        )

        # Must not raise.
        out = NormalTemplate().get_context_instructions(
            _saudacao_variables("Not/AZone")
        )

        assert 'A saudação correta é: **"Bom dia"**' in out
        assert "09:00" in out
        assert "horário local da clínica" in out
