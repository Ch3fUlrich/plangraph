# OpenCode gateway provider URLs

The per-run overlay and renderer route both `mimo` and `omniroute` through
the configured gateway. `OMNIROUTE_BASE_URL` takes precedence over
`PLANGRAPH_GATEWAY_URL`; the transitional `AUTOOS_OMNIROUTE_URL` remains
supported by the renderer. When none of these variables supplies a valid URL,
the renderer also considers the child OmniRoute provider URL, including a
`gateway_url` supplied to `build_child_env` in the spawner/keeper config.
The overlay preserves that resolved child URL instead of replacing it with a
root config's environment placeholder. Concrete root URLs still take precedence
over generic generated values; only an explicit per-run gateway override
displaces them. A generated URL alone is not evidence of such an override.

Both gateway providers receive the same validated URL through the renderer's
shared provider loop. A path-less origin gains `/v1`; a configured path is
preserved, including a gateway's MiMo path. This does not require
`PLANGRAPH_REVIEW_URL`, and setting that review-only variable does not override
the configured per-run gateway. The tool-less review API still requires its
own explicit URL or review URL variable.

If neither deployment config nor a concrete provider URL resolves the active
gateway provider, rendering returns the same named refusal for MiMo and
OmniRoute: `not_served_on_host: gateway_base_url_unresolved`. Unresolved
environment placeholders do not count as URLs; there is no localhost default.
Credential handling is unchanged: config contains environment references, not
credential values, and rendering does not read or log keys.
