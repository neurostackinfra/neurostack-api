"""
NeuroStack API - Configuration Module
"""

# Lazy imports to avoid circular dependencies
# When this module is imported (e.g., when importing src.config.logging),
# we don't want to immediately import settings which may cause circular imports


def __getattr__(name: str) -> object:
    """Lazy import for settings-related exports to avoid circular imports."""
    if name == "Settings":
        from src.config.settings import Settings

        return Settings
    elif name == "get_settings":
        from src.config.settings import get_settings

        return get_settings
    elif name == "settings":
        from src.config.settings import settings

        return settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["Settings", "get_settings", "settings"]
