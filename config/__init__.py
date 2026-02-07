"""
NeuroStack API - Configuration Module (Legacy Compatibility)

This module provides backward compatibility for imports from `config`.
All configuration has been moved to `src.config.settings`.

DEPRECATED: Use `from src.config.settings import settings` instead.
"""

from src.config.settings import Settings, get_settings, settings

__all__ = ["Settings", "get_settings", "settings"]

