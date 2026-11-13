# OpenCode gateway provider URLs

The spawner reads its provider block from plangraph's shipped
`configuration/opencode/providers.json`, not an external checkout. A non-empty
`PLANGRAPH_PROVIDERS_FILE` overrides that file with an operator-selected file
(JSON or JSONC). To refresh it, copy the `providers` block of the operator's
gateway config into a `{"providers": {...}}` file, omitting unrelated providers;
credentials must remain environment-variable references, never secret values.

The per-run overlay and renderer route both `mimo` and `omniroute` through
the configured gateway. The renderer first honours a valid child OmniRoute
provider URL already present in `OPENCODE_CONFIG_CONTENT`, including a
`gateway_url` supplied to `build_child_env` in the spawner/keeper config.
Only when no valid child URL is present does it consider `OMNIROUTE_BASE_URL`,
`PLANGRAPH_GATEWAY_URL`, then the transitional `AUTOOS_OMNIROUTE_URL`.
When building a new child config, `OMNIROUTE_BASE_URL` takes precedence over
`PLANGRAPH_GATEWAY_URL`, then the config's `gateway_url`.
The overlay preserves that resolved child URL instead of replacing it with the
root config's exact `{env:PLANGRAPH_REVIEW_URL}` placeholder. Other environment
placeholders retain their root precedence. Concrete root URLs still take precedence
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
credential values, and rendering does not read or log keys. The shared validator
refuses URL userinfo, queries, fragments (including bare delimiters), and
non-HTTP(S) schemes; a refusal leaves the caller's config unchanged.
