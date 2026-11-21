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
shared provider loop, including both `settings.baseURL` and any existing
`options.baseURL` (and under a singular `provider` block if present), and
receive `AUTOOS_OMNIROUTE_KEY` in `env` when routed through the configured
gateway so a root config's direct MiMo endpoint or direct key never bypasses
gateway call-log witnessing. A path-less origin gains `/v1`; a configured path is
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

Plain-shell CLI runs resolve a missing deployment endpoint from
`plangraph/omniroute-base-url` under XDG_CONFIG_HOME (otherwise HOME/.config).
The witness and child configuration share the same resolver; private call-log
credentials stay in the parent. Existing explicit child URLs and config-only
root-placeholder precedence remain unchanged.

The management token for the gateway management API (call logs, the
provider-state and quota-utilization readers in `quota_snapshots`) is read by `opencode_render.load_token`:
`PLANGRAPH_GATEWAY_LOG_TOKEN`, then the file named by
`PLANGRAPH_GATEWAY_LOG_TOKEN_FILE`, then `plangraph/omniroute-mgmt.key` under
HOME/.config; the file must be a regular owner-only (0600) file. The gateway
client key is never sent to a management endpoint, and neither the token nor its file path reaches a child process (harness children, `opencode models` discovery: `opencode_render.drop_parent_only_env`).

Shipped MiMo model cards in `configuration/opencode/providers.json` and the
fallback `_mimo_template()` in `opencode_render` carry a `body.reasoning.max_tokens`
cap strictly below their output limit (`32768` on the 65536 template, `65536`
on the 131072 shipped/runtime cards). The OmniRoute provider declares `cx-gpt-6.2-sol`
and `cx-gpt-6.2-astra` in place of retired `cx-gpt-6.1-sol`.
