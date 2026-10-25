"""Compatibility alias for shared routing support."""
import sys
from plangraph.routing_support import catalog_db as _implementation
sys.modules[__name__] = _implementation
