"""Compatibility alias for shared routing support."""
import sys
from plangraph.routing_support import leg_state as _implementation
sys.modules[__name__] = _implementation
