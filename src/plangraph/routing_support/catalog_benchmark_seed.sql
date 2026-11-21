-- External observations are priors for the named serving harness. source_date
-- records when the publisher's result was checked. No task run date is inferred.
WITH priors(model_id, effort, harness, score, cost, source, record) AS (
    VALUES
    ('omniroute/cx-gpt-6.2-sol-low', 'low', 'opencode', 41, 0.18, 'sol', 'GPT-6.2 Sol (Low)'),
    ('omniroute/cx-gpt-6.2-sol-medium', 'medium', 'opencode', 53, 0.41, 'sol', 'GPT-6.2 Sol (Medium)'),
    ('omniroute/cx-gpt-6.2-sol-high', 'high', 'opencode', 55, 0.56, 'sol', 'GPT-6.2 Sol (High)'),
    ('omniroute/cx-gpt-6.2-sol-xhigh', 'xhigh', 'opencode', 56, 0.65, 'sol', 'GPT-6.2 Sol (Xhigh)'),
    ('omniroute/cx-gpt-6.2-sol-max', 'max', 'opencode', 57, 1.01, 'sol', 'GPT-6.2 Sol (Max)'),
    ('codex/gpt-6.2-sol-medium', 'medium', 'codex', 53, 0.41, 'sol', 'GPT-6.2 Sol (Medium)'),
    ('claude/claude-fable-5-2', 'low', 'claude', 54, 3.10, 'fable', 'Claude Fable 5.2 (Low)'),
    ('claude/claude-fable-5-2', 'medium', 'claude', 56, 3.80, 'fable', 'Claude Fable 5.2 (Medium)'),
    ('claude/claude-fable-5-2', 'high', 'claude', 62, 9.08, 'fable', 'Claude Fable 5.2 (High)'),
    ('claude/claude-fable-5-2', 'xhigh', 'claude', 62, 14.83, 'fable', 'Claude Fable 5.2 (Xhigh)'),
    ('claude/claude-fable-5-2', 'max', 'claude', 65, 26.54, 'fable', 'Claude Fable 5.2 (Max)'),
    ('agy/claude-opus-5-5-low', 'low', 'agy', 35, 0.98, 'anthropic', 'Claude Opus 5.5 (Low)'),
    ('agy/claude-opus-5-5-medium', 'medium', 'agy', 48, 2.45, 'anthropic', 'Claude Opus 5.5 (Medium)'),
    ('agy/claude-opus-5-5-high', 'high', 'agy', 52, 3.46, 'anthropic', 'Claude Opus 5.5 (High)'),
    ('agy/claude-sonnet-5-5-low', 'low', 'agy', 26, 0.51, 'anthropic', 'Claude Sonnet 5.5 (Low)'),
    ('agy/claude-sonnet-5-5-medium', 'medium', 'agy', 31, 0.78, 'anthropic', 'Claude Sonnet 5.5 (Medium)'),
    ('agy/claude-sonnet-5-5-high', 'high', 'agy', 42, 1.64, 'anthropic', 'Claude Sonnet 5.5 (High)'),
    ('claude/claude-sonnet-5-5-high', 'high', 'claude', 42, 1.64, 'anthropic', 'Claude Sonnet 5.5 (High)'),
    ('omniroute/cx-gpt-6.1-terra-medium', 'medium', 'opencode', 38, 0.17, 'terra-luna', 'GPT-6.1 Terra (Medium)'),
    ('omniroute/cx-gpt-6.1-terra-xhigh', 'xhigh', 'opencode', 45, 0.4, 'terra-luna', 'GPT-6.1 Terra (Xhigh)'),
    ('omniroute/cx-gpt-6-luna-low', 'low', 'opencode', 10, 0.0051, 'terra-luna', 'GPT-6 Luna (Low)'),
    ('omniroute/cx-gpt-6-luna-medium', 'medium', 'opencode', 18, 0.0422, 'terra-luna', 'GPT-6 Luna (Medium)'),
    ('omniroute/cx-gpt-6-luna-high', 'high', 'opencode', 21, 0.0729, 'terra-luna', 'GPT-6 Luna (High)'),
    ('omniroute/cx-gpt-6-luna-xhigh', 'xhigh', 'opencode', 23, 0.1, 'terra-luna', 'GPT-6 Luna (Xhigh)'),
    ('omniroute/cx-gpt-6-luna-max', 'max', 'opencode', 28, 0.15, 'terra-luna', 'GPT-6 Luna (Max)'),
    ('agy/gemini-4-hydrogen-high', 'high', 'agy', 51, 1.87, 'hydrogen-neon', 'Gemini 4 Hydrogen (High)'),
    ('agy/gemini-4-hydrogen-medium', 'medium', 'agy', 41, 0.8, 'hydrogen-neon', 'Gemini 4 Hydrogen (Medium)'),
    ('agy/gemini-4-neon-medium', 'medium', 'agy', 55, 1.9, 'hydrogen-neon', 'Gemini 4 Neon (Medium)'),
    ('mimo/mimo-v2.6-pro', '', 'opencode', 30, 0.26, 'mimo-pro', 'MiMo-V2.6-Pro'),
    ('mimo/mimo-v2.6-flash', '', 'opencode', 27, 0.13, 'mimo-flash', 'MiMo-V2.6-Flash'),
    ('agy/gemini-4-argon-low', 'low', 'agy', 47.9, 7.96, 'argon', 'Gemini 4 Argon Low'),
    ('agy/gemini-4-argon-medium', 'medium', 'agy', 55.8, 7.64, 'argon', 'Gemini 4 Argon Medium'),
    ('opencode-go/deepseek-v4.1-flash', '', 'opencode', 74.2, NULL, 'deepseek', 'DeepSeek-V4.1-Flash')
), sources(source, url, origin, version) AS (
    VALUES
    ('sol', 'https://artificialanalysis.ai/models/comparisons/gpt-6-2-sol-vs-claude-fable-5-2', 'aa-index', '5.0.1'),
    ('fable', 'https://artificialanalysis.ai/models/comparisons/gpt-6-2-sol-vs-claude-fable-5-2', 'aa-index', '5.0.1'),
    ('anthropic', 'https://artificialanalysis.ai/providers/anthropic', 'aa-index', '5.0.1'),
    ('terra-luna', 'https://artificialanalysis.ai/models/comparisons/gpt-6-1-terra-vs-gpt-6-luna', 'aa-index', '5.0.1'),
    ('hydrogen-neon', 'https://artificialanalysis.ai/models/comparisons/gemini-4-neon-vs-gemini-4-hydrogen', 'aa-index', '5.0.1'),
    ('mimo-pro', 'https://artificialanalysis.ai/models/mimo-v2-6-pro', 'aa-index', '5.0.1'),
    ('mimo-flash', 'https://artificialanalysis.ai/models/mimo-v2-6-flash', 'aa-index', '5.0.1'),
    ('argon', 'https://prod.cursor.com/evals', 'cursorbench', '4.1'),
    ('deepseek', 'https://api-docs.deepseek.com/updates/', 'deepswe', '1.1')
)
INSERT OR IGNORE INTO catalog_benchmark_measurements (
    model_id, effort, harness, score, cost_per_task_usd, imputed, effort_channel,
    source_url, source_date, source_record, origin, index_version
)
SELECT model_id, effort, harness, score, cost, 1,
       CASE WHEN effort = '' THEN NULL ELSE 'publisher' END,
       url, '2026-11-21', record, origin, version
FROM priors JOIN sources USING (source);

-- Xiaomi's reference rates per token, as listed on the MiMo rows above.
-- Published index results lack a per-task split of billable token classes.
INSERT OR IGNORE INTO catalog_benchmark_price_inputs (
    model_id, effort, harness, openrouter_id, reference_prompt_price, reference_completion_price
) VALUES
    ('xiaomi/mimo-v2.6-pro', '', 'openrouter', 'xiaomi/mimo-v2.6-pro', 0.00000043, 0.00000087),
    ('xiaomi/mimo-v2.6-flash', '', 'openrouter', 'xiaomi/mimo-v2.6-flash', 0.00000014, 0.00000028);
