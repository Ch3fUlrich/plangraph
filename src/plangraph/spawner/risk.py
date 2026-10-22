"""Risk evaluation and intelligence threshold classification.

Evaluates risk signals (high-risk paths, missing tests, high fanout, large write set)
into 4 risk bands: low, normal, high, critical.

Maps risk bands to:
  - Minimum AA Intelligence threshold (RISK_MIN_INTELLIGENCE)
  - Required cross-family reviewer count (RISK_REVIEWER_COUNT)
"""
from __future__ import annotations

import os
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
    "critical": 1.5,
}

# Excess intelligence multiplier above I_target
RISK_BETA_EXCESS: dict[str, float] = {
    "low": 0.2,
    "normal": 0.4,
    "high": 2.0,
    "critical": 2.5,
}

# Required cross-family reviewer count per risk band (0 for pure doc S0)
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
    norm = _normalize_path(path)
    base = norm.rsplit("/", 1)[-1]
    if base in ("policy.py", "auth.py", "crypto.py", "security.py"):
        return True
    return any(kw in norm.split("/") or kw in base for kw in CRITICAL_KEYWORDS)


def is_high_risk_path(path: str) -> bool:
    """Whether a path touches high-risk operational, installer, or security files."""
    norm = _normalize_path(path)
    base = norm.rsplit("/", 1)[-1]
    for kw in HIGH_RISK_KEYWORDS:
        if kw == base or kw in norm.split("/") or f"/{kw}/" in norm or norm.endswith(kw):
            return True
        if kw in norm:
            return True
    return False


@dataclass
class RiskAssessment:
    """Result of risk evaluation."""
    risk: str  # "low" | "normal" | "high" | "critical"
    min_intelligence: float
    reviewer_count: int
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
    for p in normalized_paths:
        if is_critical_path(p):
            crit_paths.append(p)
        elif is_high_risk_path(p):
            hr_paths.append(p)

    risk_band = "normal"

    if crit_paths:
        risk_band = "critical"
        reasons.append(f"Touches critical path(s): {', '.join(crit_paths)}")
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

    # Reviewer count: 0 for pure doc S0, otherwise mapped from band
    if bucket == "S0" and (is_docs_only or is_pure_doc):
        reviewer_count = 0
        reasons.append("Reviewer count 0: pure documentation S0 change requires no cross-family code review")
    else:
        reviewer_count = RISK_REVIEWER_COUNT[risk_band]

    return RiskAssessment(
        risk=risk_band,
        min_intelligence=min_intel,
        reviewer_count=reviewer_count,
        target_intelligence=target_intel,
        gamma_cost=gamma_cost,
        beta_excess=beta_excess,
        reasons=reasons,
        critical_paths=crit_paths,
        high_risk_paths=hr_paths,
    )
