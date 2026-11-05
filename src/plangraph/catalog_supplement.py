"""Supplemental catalog seed rows, kept separate to bound table module size."""

MIMO_FLASH = {
    "model_id": "mimo/mimo-v2.6-flash",
    "provider_id": "mimo",
    "upstream_id": "mimo-v2.6-flash",
    "family": "mimo",
    "parameter_effort": None,
    "max_safe_effort": None,
    "effort_warning": None,
    "context_tokens": 1000000,
    "max_output_tokens": 65536,
    "price_cliff_tokens": None,
    "quota_pool_id": "mimo-sub",
    "pool_burn_weight": 1.0,
    "status": "active",
    "available": 1,
    "unavailable_reason": None,
    "supports_reasoning": 1,
    "supports_tool_calling": 1,
    "supports_vision": 0,
    "trains_on_prompts": 0,
}
