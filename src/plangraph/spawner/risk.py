"""Compatibility alias for shared routing support."""
import sys
from plangraph.routing_support import risk as _implementation
sys.modules[__name__] = _implementation
