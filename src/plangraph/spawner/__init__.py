"""Plangraph Spawner Package.

Provides model catalog, quota-pool tracking, risk-aware routing, and host agent
bootstrapping for multi-agent autonomous workloads.
"""

from typing import Any

__all__ = ["CatalogDB", "TIER_RUNG"]


def __getattr__(name: str) -> Any:
    if name in ("CatalogDB", "TIER_RUNG"):
        from .catalog_db import CatalogDB as _CatalogDB, TIER_RUNG as _TIER_RUNG

        if name == "CatalogDB":
            return _CatalogDB
        return _TIER_RUNG
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
