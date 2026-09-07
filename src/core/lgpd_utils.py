"""Shared LGPD utilities used by pipeline and webhook."""

from collections.abc import Mapping


def resolve_privacy_link(lgpd: Mapping[str, object], tenant_id: str) -> str:
    """Return the resolved privacy link for a clinic.

    Uses lgpd_custom_link when set; otherwise builds the NeuroStack
    default page URL from public_base_url + tenant_id.

    settings is imported lazily to avoid circular imports
    (settings → core.exceptions → core.__init__ → pipeline → here).
    """
    configured_link = lgpd.get("lgpd_custom_link")
    custom = configured_link.strip() if isinstance(configured_link, str) else ""
    if custom:
        return custom
    from src.config.settings import settings  # lazy — breaks circular dep

    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/privacidade/{tenant_id}" if base else f"/privacidade/{tenant_id}"
