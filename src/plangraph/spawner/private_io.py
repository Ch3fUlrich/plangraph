"""Compatibility alias for shared routing support."""
import sys
from plangraph.routing_support import private_io as _implementation
sys.modules[__name__] = _implementation
