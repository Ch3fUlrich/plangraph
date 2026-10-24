"""Risk evaluation and intelligence threshold classification.

Evaluates risk signals (high-risk paths, missing tests, high fanout, large write set)
into 4 risk bands: low, normal, high, critical.

Maps risk bands to:
  - Minimum AA Intelligence threshold (RISK_MIN_INTELLIGENCE)
  - Required cross-family reviewer count (RISK_REVIEWER_COUNT)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

# Minimum AA Intelligence threshold per risk band
RISK_MIN_INTELLIGENCE: dict[str, float] = {
    "low": 37.5,
    "normal": 44.0,
    "high": 46.0,
    "critical": 50.0,
}

# Target AA Intelligence threshold per risk band (competency ramp decoupled from I_min)
RISK_TARGET_INTELLIGENCE: dict[str, float] = {
    "low": 41.0,
    "normal": 47.0,
    "high": 50.0,
    "critical": 54.0,
}

# Cost penalty weight per risk band (heavier on low/normal to prioritize cheap models)
RISK_GAMMA_COST: dict[str, float] = {
    "low": 15.0,
    "normal": 10.0,
    "high": 4.0,
    "critical": 4.0,
}

# Excess intelligence multiplier above I_target
RISK_BETA_EXCESS: dict[str, float] = {
    "low": 0.2,
    "normal": 0.4,
    "high": 0.6,
    "critical": 0.6,
}

# Required cross-family reviewer count per risk band, including documentation.
RISK_REVIEWER_COUNT: dict[str, int] = {
    "low": 1,
    "normal": 1,
    "high": 2,
    "critical": 2,
}

CRITICAL_KEYWORDS: tuple[str, ...] = ("auth", "crypto", "security", "policy.py")
HIGH_RISK_KEYWORDS: tuple[str, ...] = (
    "setup.ps1",
    "setup.sh",
    "auth",
    "crypto",
    "migrations",
    "policy.py",
    "systemd",
    "security",
)


def _normalize_path(path: str) -> str:
    return str(path).strip().replace("\\", "/").lower()


def is_critical_path(path: str) -> bool:
    """Whether a path touches critical security/policy/auth/crypto components."""
    return any(band == "critical" for band, _ in _path_rules(path))


def is_high_risk_path(path: str) -> bool:
    """Whether a path touches high-risk operational, installer, or security files."""
    return bool(_path_rules(path))


# Whole-token rules: (tokens, band, proposed planner tags). Public keyword
# constants above remain available for callers that import them.
_TOKEN_RULES = (
    ({"auth", "security"}, "critical", ("auth",)),
    ({"crypto"}, "critical", ("secrets",)),
    ({"policy"}, "critical", ("public-contract",)),
    ({"secret", "secrets", "token", "tokens", "credential", "credentials"},
     "high", ("secrets",)),
    ({"migration", "migrations"}, "high", ("migration",)),
    ({"live", "systemd", "deploy"}, "high", ("live-step",)),
)
_DOC_EXTENSIONS = {"md", "rst", "txt", "adoc"}
_UNIT_EXTENSIONS = {"service", "socket", "timer", "target", "mount", "automount",
                    "path", "slice", "scope", "swap"}


def _path_rules(path: str, *, include_non_code: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    norm = _normalize_path(path)
    segments = tuple(part for part in norm.split("/") if part)
    if not segments:
        return []
    base = segments[-1]
    stem, dot, extension = base.rpartition(".")
    stem = stem if dot else base
    tokens = set(segments)
    for segment in (*segments[:-1], stem):
        tokens.update(filter(None, re.split(r"[^a-z0-9]+", segment)))
    non_code = (
        extension in _DOC_EXTENSIONS
        or bool(set(segments[:-1]) & {"docs", "doc", "tests", "test"})
        or stem.startswith("test_") or stem.endswith("_test")
    )
    if non_code and not include_non_code:
        return []
    matches = [(band, tags) for keywords, band, tags in _TOKEN_RULES if keywords & tokens]
    # Structural rules use exact directory segments. The two explicit filename
    # families (engine*, install*) are the only intentional prefix matches.
    structural_rules = (
        ("keeper" in segments[:-1] and stem.startswith("engine"), ("live-step",)),
        ("schema" in segments[:-1] and extension == "sql", ("migration",)),
        (any(segments[i:i + 2] == (".github", "workflows")
             for i in range(len(segments) - 2)), ("live-step",)),
        (stem in {"setup", "bootstrap"}, ("live-step",)),
        (stem.startswith("install") and extension in {"ps1", "sh"}, ("live-step",)),
        (extension in _UNIT_EXTENSIONS, ("live-step",)),
    )
    matches.extend(("high", tags) for matched, tags in structural_rules if matched)
    return matches


def propose_risk_tags(paths: Sequence[str]) -> list[str]:
    """Propose sorted, unique planner risk tags from paths; card tags decide.

    These advisory hints do not assign or override a planner card's risk_tags.
    Documentation and tests can propose tags without escalating the path band.
    """
    return sorted({tag for path in paths
                   for _, tags in _path_rules(path, include_non_code=True)
                   for tag in tags})


@dataclass
class RiskAssessment:
    """Result of risk evaluation."""
    risk: str  # "low" | "normal" | "high" | "critical"
    min_intelligence: float
    reviewer_count: int
    review_exempt: bool = False
    target_intelligence: float = 44.0
    gamma_cost: float = 10.0
    beta_excess: float = 0.4
    reasons: list[str] = field(default_factory=list)
    critical_paths: list[str] = field(default_factory=list)
    high_risk_paths: list[str] = field(default_factory=list)


def evaluate_risk(
    paths: Sequence[str] | None = None,
    bucket: str = "S1",
    has_tests: bool = True,
    fanout: int = 0,
    is_docs_only: bool = False,
    is_pure_doc: bool = False,
    risk_tags: Sequence[str] = (),
) -> RiskAssessment:
    """Evaluate risk band, required intelligence threshold, and reviewer count.

    Risk bands:
      - 'critical': critical paths (auth, crypto, security, policy.py) or high-risk path with missing tests / S4
      - 'high': high-risk paths (setup.ps1, setup.sh, migrations, systemd), S4 complexity, or missing tests on S2+
      - 'normal': standard code changes with test coverage
      - 'low': pure documentation or trivial S0 changes
    """
    reasons: list[str] = []
    crit_paths: list[str] = []
    hr_paths: list[str] = []

    normalized_paths = [_normalize_path(p) for p in (paths or [])]
    proposed_tags = propose_risk_tags(normalized_paths)
    if proposed_tags:
        reasons.append(f"Proposed risk tags: {', '.join(proposed_tags)} (card tags decide)")
    for p in normalized_paths:
        if is_critical_path(p):
            crit_paths.append(p)
        elif is_high_risk_path(p):
            hr_paths.append(p)

    risk_band = "normal"

    if risk_tags:
        risk_band = 'high'
        reasons.append('Explicit risk tags require independent review')
    if crit_paths:
        risk_band = "critical"
        reasons.append(f"Touches critical path(s): {', '.join(crit_paths)}")
    elif risk_tags:
        risk_band = 'high'
    elif hr_paths and (not has_tests or bucket == "S4"):
        risk_band = "critical"
        reasons.append(f"High-risk path(s) {', '.join(hr_paths)} combined with unverified/S4 state")
    elif hr_paths:
        risk_band = "high"
        reasons.append(f"Touches high-risk path(s): {', '.join(hr_paths)}")
    elif bucket == "S4":
        risk_band = "high"
        reasons.append("S4 complexity: large cross-cutting architectural change")
    elif not has_tests and bucket in ("S2", "S3"):
        risk_band = "high"
        reasons.append(f"Missing tests for non-trivial {bucket} change")
    elif fanout > 10:
        risk_band = "high"
        reasons.append(f"High fanout reference count: {fanout}")
    elif is_docs_only or is_pure_doc or (bucket == "S0" and not hr_paths and fanout == 0):
        risk_band = "low"
        reasons.append("Docs-only or trivial S0 change")
    else:
        risk_band = "normal"
        reasons.append(f"Standard {bucket} change with test coverage")

    min_intel = RISK_MIN_INTELLIGENCE[risk_band]
    target_intel = RISK_TARGET_INTELLIGENCE[risk_band]
    gamma_cost = RISK_GAMMA_COST[risk_band]
    beta_excess = RISK_BETA_EXCESS[risk_band]

    # Documentation flags must never bypass review of critical or high paths.
    reviewer_count = max(2 if risk_band in ("high", "critical") else 1,
                         RISK_REVIEWER_COUNT[risk_band])

    if risk_band in ("low", "normal") and is_pure_doc and bucket == "S0" and not (crit_paths or hr_paths):
        reviewer_count = 0

    return RiskAssessment(
        risk=risk_band,
        min_intelligence=min_intel,
        reviewer_count=reviewer_count,
        review_exempt=reviewer_count == 0,
        target_intelligence=target_intel,
        gamma_cost=gamma_cost,
        beta_excess=beta_excess,
        reasons=reasons,
        critical_paths=crit_paths,
        high_risk_paths=hr_paths,
    )
