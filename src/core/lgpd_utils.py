"""Shared LGPD utilities used by pipeline and webhook."""

from src.config.settings import settings


def resolve_privacy_link(lgpd: dict, tenant_id: str) -> str:
    """Return the resolved privacy link for a clinic.

    Uses lgpd_custom_link when set; otherwise builds the NeuroStack
    default page URL from public_base_url + tenant_id.
    """
    custom = ((lgpd or {}).get("lgpd_custom_link") or "").strip()
    if custom:
        return custom
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/privacidade/{tenant_id}" if base else f"/privacidade/{tenant_id}"
