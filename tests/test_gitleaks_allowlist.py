"""PG-REPUBLISH-FRESH: the PROMPT.md / orchestration-setup allowlist entries are value-scoped.

Proves that after the regexes+AND tightening only the reviewed synthetic prose strings
are waived in those two paths: a different key-shaped string (assembled at runtime, never
a literal in this file) planted in a copy of the same paths IS still reported.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks.toml"


PROMPT_REL = Path("PROMPT.md")
PLAN_REL = Path("docs/plans/2026-10-16-orchestration-setup.md")
# assembled at runtime so this test file is not itself a generic-api-key finding
# (it sits in the same scanner gate as the paths it exercises)
def _join(*parts: str) -> str:
    return "".join(parts)

WAIVED_PROMPT_SECRET = _join("bool/str/", "negative")
WAIVED_PLAN_SECRET = _join("RT-P2-", "DECISIONS")
# the exact prose that the generic-api-key rule mis-fires on in the two
# historically allow-listed files
PROMPT_PROSE = _join("It accepts tokens, ", WAIVED_PROMPT_SECRET, " values.\n")
PLAN_PROSE = _join("Phases: Q-API-PUSH, ", WAIVED_PLAN_SECRET, " are done.\n")


#: An open body: an any-content atom (`.`, `[\s\S]`, `[^\n]`, `[^\r\n]`) or a
#: non-space run (`\S`) under an UNBOUNDED quantifier (`*`, `+`, `{n,}`) swallows
#: everything between the `^` and `$` anchors, so `[\s\S]*`, `\S*` or `.{2,}` would
#: waive any value in the path while still looking anchored.  Whitespace runs
#: (`\s*`, `\s+`) are not open bodies -- they can only match spaces, which is what
#: the pinned PEM regexes legitimately use.
_OPEN_BODY = re.compile(
    r"\.\*|\.\+|\.\{\d*,\}"                 # .*  .+  .{n,}
    r"|\[\\s\\S\][*+]|\[\\s\\S\]\{\d*,\}"   # [\s\S]*  [\s\S]+  [\s\S]{n,}
    r"|\[\^\\r\\n\][*+]|\[\^\\r\\n\]\{\d*,\}"   # [^\r\n]* ...
    r"|\[\^\\n\][*+]|\[\^\\n\]\{\d*,\}"     # [^\n]* ...
    r"|\\S[*+]|\\S\{\d*,\}"                 # \S*  \S+  \S{n,}
)


def _assert_narrow_entry(entry):
    assert entry.get("targetRules"), "allowlist must name targetRules"
    assert entry.get("condition") == "AND"
    for value in entry["paths"] + entry["regexes"]:
        assert value.startswith("^") and value.endswith("$")
        assert ".*" not in value and ".+" not in value
        assert _OPEN_BODY.search(value) is None, f"open body: {value!r}"


def test_root_and_public_allowlists_match_except_rust_pem():
    """Name kept for history: the two allowlists now match EXACTLY, nothing is excepted."""
    root = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["allowlists"]
    public = tomllib.loads((ROOT / "tools/split/gitleaks.toml").read_text(
        encoding="utf-8"))["allowlists"]
    assert root == public
    for entry in root:
        _assert_narrow_entry(entry)


@pytest.mark.parametrize("entry", [
    {"condition": "AND", "paths": ["^tests/x$"], "regexes": ["^x$"]},
    {"condition": "AND", "targetRules": ["private-key"],
     "paths": ["^rust/.*$"], "regexes": ["^x$"]},
    {"condition": "AND", "targetRules": ["private-key"],
     "paths": ["rust/x"], "regexes": ["^x$"]},
    {"condition": "AND", "targetRules": ["generic-api-key"],
     "paths": ["^tests/x$"], "regexes": ["^.*$"]},
    # open bodies that keep every `^...$` anchor and evade a plain ".*"/".+" check
    {"condition": "AND", "targetRules": ["private-key"],
     "paths": [r"^tests/x[\s\S]*$"], "regexes": ["^x$"]},
    {"condition": "AND", "targetRules": ["generic-api-key"],
     "paths": ["^tests/x$"], "regexes": [r"^\S*$"]},
    {"condition": "AND", "targetRules": ["generic-api-key"],
     "paths": ["^tests/x$"], "regexes": [r"^v.{2,}$"]},
])
def test_allowlist_validation_rejects_broad_or_unscoped_entries(entry):
    with pytest.raises(AssertionError):
        _assert_narrow_entry(entry)


def _gitleaks() -> str:
    import os

    env = os.environ.get("GITLEAKS_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("gitleaks")
    if found:
        return found
    fallback = Path("C:/fleet/tools/gl/gitleaks.exe")
    if fallback.exists():
        return str(fallback)
    pytest.skip("gitleaks binary not installed (PATH, GITLEAKS_BIN, or C:/fleet/tools/gl)")


def _make_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / PLAN_REL.parent).mkdir(parents=True)
    (tree / PROMPT_REL).write_text(PROMPT_PROSE, encoding="utf-8")
    (tree / PLAN_REL).write_text(PLAN_PROSE, encoding="utf-8")
    return tree


def _scan(exe: str, tree: Path, config: Path | None) -> tuple[int, list[dict]]:
    report = tree.parent / "findings.json"
    if report.exists():
        report.unlink()
    cmd = [exe, "detect", "--no-git", "--source", ".", "--no-banner",
           "--report-format", "json", "--report-path", str(report)]
    if config is not None:
        cmd += ["--config", str(config)]
    proc = subprocess.run(cmd, cwd=tree, capture_output=True, text=True)
    findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    return proc.returncode, findings


def _fake_secret(fragments: tuple[str, ...]) -> str:
    return "".join(fragments)


def test_known_synthetic_strings_are_waived(tmp_path):
    exe = _gitleaks()
    tree = _make_tree(tmp_path)
    rc, findings = _scan(exe, tree, CONFIG)
    assert rc == 0, f"known prose must stay waived, findings: {findings}"


def test_waived_strings_are_real_findings_without_the_config(tmp_path):
    # guard against the prose silently stopping matching generic-api-key (which would
    # make the waived-path test above pass vacuously)
    exe = _gitleaks()
    tree = _make_tree(tmp_path)
    rc, findings = _scan(exe, tree, None)
    secrets = {f["Secret"] for f in findings if f["RuleID"] == "generic-api-key"}
    assert rc == 1 and secrets == {WAIVED_PROMPT_SECRET, WAIVED_PLAN_SECRET}, secrets


@pytest.mark.parametrize("rel", [PROMPT_REL, PLAN_REL])
def test_other_key_shaped_string_in_same_path_is_reported(tmp_path, rel):
    exe = _gitleaks()
    tree = _make_tree(tmp_path)
    planted = _fake_secret(("Vk7", "qP2", "mR9x", "Tl4b", "Zr")) + rel.name[:2]
    with (tree / rel).open("a", encoding="utf-8") as fh:
        fh.write(f"api key = {planted}\n")
    rc, findings = _scan(exe, tree, CONFIG)
    secrets = {f["Secret"] for f in findings if f["RuleID"] == "generic-api-key"}
    assert rc == 1, "tightened allowlist must not waive a new key-shaped value"
    assert planted in secrets
    waived = {WAIVED_PROMPT_SECRET, WAIVED_PLAN_SECRET}
    assert not secrets & waived, f"known strings leaked into findings: {secrets & waived}"


# --- PG-PUBLIC-FIX: credential-guard fixtures are plain literals, waived by value only ------

#: The files whose synthetic guard value is a plain literal in the source.  `.gitleaks.toml`
#: waives exactly that value in exactly that path (per-file, per-regex, reason recorded
#: there), so the fixtures may be readable literals without opening the gate up.
GUARD_FIXTURES = ("tests/test_keeper_estimate_src_edit.py",
                  "tests/test_keeper_guard_key.py")


@pytest.mark.parametrize("rel", GUARD_FIXTURES)
def test_guard_fixture_value_is_waived_but_nothing_else(tmp_path, rel):
    """The fixture's synthetic value is waived by value alone; new values still fail.

    The file is copied verbatim into a tree at the same relative path (the waiver is
    path-scoped) and given one freshly planted key-shaped line:

    * without the config both the fixture value and the planted one are findings --
      so the waiver below is live, not a no-op over a literal that stopped matching;
    * with the config ONLY the planted one survives -- so the entry is value-scoped and
      another credential-shaped string in the same file is never exempt.
    """
    exe = _gitleaks()
    tree = tmp_path / "tree"
    target = tree / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    planted = _fake_secret(("Vk7", "qP2", "mR9x", "Tl4b", "Zr")) + Path(rel).name[:2]
    target.write_text((ROOT / rel).read_text(encoding="utf-8")
                      + f"\napi key = {planted}\n", encoding="utf-8")

    rc0, bare = _scan(exe, tree, None)
    bare_secrets = {f["Secret"] for f in bare if f["RuleID"] == "generic-api-key"}
    assert rc0 == 1 and planted in bare_secrets, bare_secrets
    assert bare_secrets - {planted}, \
        f"{rel} must still carry a credential-shaped finding of its own: {bare_secrets}"

    rc, findings = _scan(exe, tree, CONFIG)
    secrets = {f["Secret"] for f in findings if f["RuleID"] == "generic-api-key"}
    assert rc == 1 and secrets == {planted}, \
        f"only the planted value may be reported in {rel}: {secrets}"


# --- PG-PUBLIC-FIX: the private-key waiver pins the full fake PEM block, not a header ------

#: Assembled at runtime: this file sits in the same scanner gate, so no fragment here may form
#: a matchable PEM header and footer, or a credential-shaped blob.
_PEM_HEADER = _join("-----BEGIN ", "TEST PRIVATE KEY-----")
_PEM_FOOTER = _join("-----END ", "TEST PRIVATE KEY-----")
_PEM_FILLER = "A" * 64  # letter-only, no digits: never a base64 key body
#: The fixture exactly as it sits in rust/worker-core/src/{check,redact}.rs: one literal block.
FAKE_PEM = _PEM_HEADER + "\n" + "\n".join([_PEM_FILLER] * 30) + "\n" + _PEM_FOOTER
#: A real (fake-content, real-shape) key: base64 body WITH digits under the very same header.
#: Every literal fragment is digit-free -- this file is scanned by the scrub high-entropy rule,
#: which only fires on mixed-case-and-digit tokens -- and the digits are spliced in at runtime,
#: so the source itself never holds a key-body-looking token while the written file does.
REAL_PEM_BODY = ("MIIEvQIBADANBgkqhkiG" + str(9) + "w" + str(0)
                 + "BAQEFAASCBKcwggSjAgEAAoIBAQC" + str(7)
                 + "VJTUt" + str(9) + "Us" + str(8) + "cKmPw" + str(7) + "nQ")
REAL_PEM = _PEM_HEADER + "\n" + REAL_PEM_BODY + "\n" + _PEM_FOOTER
#: The pre-fix fixture: the block was BUILT in code, so the private-key match starts at the
#: header string literal and runs through the builder to the footer literal.  This is what the
#: git-history gate still sees in the base commits; the config waives exactly this shape.
HISTORICAL_PEM = (
    '    let mut v = vec!["' + _PEM_HEADER + '".to_string()];\n'
    "        for _ in 0..body_lines {\n"
    '            v.push("A".repeat(64));\n'
    "        }\n"
    '        v.push("' + _PEM_FOOTER + '".to_string());\n'
)

#: The historical shape with REAL key material pushed before the footer: every token the
#: config waives (header literal, builder, footer literal) is still there in order, but a
#: base64 body sits inside the header..footer span.  A waiver that only pins the opening
#: prefix of the match would exempt this whole block, so it must stay a finding.
HISTORICAL_PEM_WITH_REAL_BODY = (
    '    let mut v = vec!["' + _PEM_HEADER + '".to_string()];\n'
    "        for _ in 0..body_lines {\n"
    '            v.push("' + REAL_PEM_BODY + '");\n'
    '            v.push("A".repeat(64));\n'
    "        }\n"
    '        v.push("' + _PEM_FOOTER + '".to_string());\n'
)

_PEM_REL = "rust/worker-core/src/check.rs"


def _make_pem_tree(tmp_path: Path, text: str) -> Path:
    """A scan tree holding one file at the config's allow-listed PEM path."""
    tree = tmp_path / "tree"
    target = tree / _PEM_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return tree


def test_full_fake_pem_block_is_waived(tmp_path):
    """The allowlist waives the fixture's whole fake block (header + 64-A filler + footer)."""
    exe = _gitleaks()
    tree = _make_pem_tree(tmp_path, 'const FAKE_PEM: &str = r#"' + FAKE_PEM + '"#;\n')
    rc, findings = _scan(exe, tree, CONFIG)
    assert rc == 0, f"the reviewed fake PEM block must stay waived: {findings}"


def test_real_key_under_the_test_header_is_still_reported(tmp_path):
    """A header-only waiver would hide a real key; the full-block pin must not.

    Same header, base64 body (digits included), same path: without the config the rule
    fires (so the check below is not vacuous), and with the config it must STILL fire.
    """
    exe = _gitleaks()
    tree = _make_pem_tree(tmp_path, REAL_PEM)
    rc0, bare = _scan(exe, tree, None)
    assert rc0 == 1 and any(f["RuleID"] == "private-key" for f in bare), \
        f"the private-key rule must fire on a real key: {bare}"
    rc, findings = _scan(exe, tree, CONFIG)
    assert rc == 1 and any(f["RuleID"] == "private-key" for f in findings), \
        f"a real key under the TEST header must never be waived: {findings}"


def test_pre_fix_history_shape_is_waived(tmp_path):
    """The git-history gate (`gitleaks git --log-opts=--all`) stays green.

    The pre-fix commits build the fake block programmatically, so their match is the header
    literal followed by builder code; the config's second private-key entry pins that exact
    shape.  Without the config the match is a finding, so the waiver is live, not vacuous.
    """
    exe = _gitleaks()
    tree = _make_pem_tree(tmp_path, HISTORICAL_PEM)
    rc0, bare = _scan(exe, tree, None)
    assert rc0 == 1 and any(f["RuleID"] == "private-key" for f in bare), \
        f"the historical fixture must be a finding without the config: {bare}"
    rc, findings = _scan(exe, tree, CONFIG)
    assert rc == 0, f"the pre-fix fixture must stay waived so history scans clean: {findings}"


def test_real_key_inside_the_historical_shape_is_still_reported(tmp_path):
    """Real key material spliced into the historical builder must NOT be waived.

    The waiver pins that shape; it must pin all of it.  A regex that only checks the
    opening prefix (header + builder prologue) would exempt a real base64 body dropped in
    before the footer, because gitleaks' private-key match spans header..first footer.

    Without the config the rule fires on the planted body (so the assertion below is not
    vacuous); with the config it must STILL fire, while the untouched historical shape
    stays waived (test_pre_fix_history_shape_is_waived).
    """
    exe = _gitleaks()
    tree = _make_pem_tree(tmp_path, HISTORICAL_PEM_WITH_REAL_BODY)
    rc0, bare = _scan(exe, tree, None)
    assert rc0 == 1 and any(f["RuleID"] == "private-key" for f in bare), \
        f"the planted body must be a private-key finding without the config: {bare}"
    assert any(REAL_PEM_BODY in f.get("Secret", "") for f in bare if f["RuleID"] == "private-key"), \
        f"the finding must cover the real body that was spliced in: {bare}"
    rc, findings = _scan(exe, tree, CONFIG)
    assert rc == 1 and any(f["RuleID"] == "private-key" for f in findings), \
        f"a real key inside the historical builder shape must never be waived: {findings}"


# --- PG-PUBLIC-READY: every replay id the two fixtures ever carried is pinned by value -----

#: The two fixture paths the replay-id waiver is scoped to, and to nothing else.
PHASE_CARD_PATHS = {
    r"^rust/worker/tests/fixtures/phase_defer\.json$",
    r"^rust/worker/tests/fixtures/phase_two_cards\.json$",
}

#: Synthetic Temporal replay ids carried by `phase_defer.json`: the three the CURRENT fixture
#: still holds and the six older generations that exist only in git history (the
#: RT-P1-PHASE-RUNNER S2 commits the history scan still walks).  Hand-listed from the reviewed
#: brief, never generated -- a config value may only be one of these literals.
DEFER_REPLAY_IDS = (
    "e2e-defer-a-33069540525861094644722115702876536833",
    "e2e-defer-b-33069540525861163949139600629661958146",
    "phase-33069540525861169132674685342045962243",
    "e2e-defer-a-33069397607394545167591213328121200644",
    "e2e-defer-a-33069465295839244738770422596414472193",
    "e2e-defer-b-33069397607394594328164169764076257285",
    "e2e-defer-b-33069465295839301296487752589899726850",
    "phase-33069397607394603385515509955466100742",
    "phase-33069465295839305354771448806001082371",
)

#: Same split for `phase_two_cards.json`: three current ids, six historical ones.
TWO_CARDS_REPLAY_IDS = (
    "e2e-phase-a-33069540670160061784166484599698882561",
    "e2e-phase-b-33069540670160144222665750007685054466",
    "phase-33069540670160152892635464651174313987",
    "e2e-phase-a-33069397622649438415115238476464783369",
    "e2e-phase-a-33069465440153862981330642214886309889",
    "e2e-phase-b-33069397622649511593348978882256044042",
    "e2e-phase-b-33069465440153917306991939289515819010",
    "phase-33069397622649516942904760258026012683",
    "phase-33069465440153924519668872109950500867",
)

#: The whole reviewed set: 6 ids the current fixtures hold + 12 that live only in history.
REPLAY_IDS = DEFER_REPLAY_IDS + TWO_CARDS_REPLAY_IDS


def _phase_card_entries(config: Path) -> list[dict]:
    lists = tomllib.loads(config.read_text(encoding="utf-8"))["allowlists"]
    return [al for al in lists if set(al.get("paths", ())) & PHASE_CARD_PATHS]


def test_both_configs_pin_all_18_replay_ids_by_value_and_nothing_broader():
    """The 12 historical replay ids join the 6 current ones, one literal each.

    `gitleaks git -c .gitleaks.toml --redact` over the real clone still reports the older
    id generations left in the RT-P1-PHASE-RUNNER S2 commits, so BOTH configs must waive
    exactly these 18 values -- and the waiver must stay value-scoped:

    * every regex is `^` + one literal from the list + `$`, so a pattern waiver that would
      also hide a NEW id (`phase-\\d+`, a class, `.*`) can never be pinned here by accident;
    * the paths stay the reviewed two-fixture set, so no other file is ever exempt;
    * condition = AND keeps a planted credential-shaped value in the same fixture reported
      (the generic-api-key findings the waiver does not name are never touched).
    """
    for config in (CONFIG, ROOT / "tools/split/gitleaks.toml"):
        entries = _phase_card_entries(config)
        assert len(entries) == 1, \
            f"{config}: exactly one shared phase/card allowlist entry expected: {entries}"
        entry = entries[0]
        assert set(entry["paths"]) == PHASE_CARD_PATHS, entry["paths"]
        assert entry.get("condition") == "AND"
        assert entry.get("targetRules") == ["generic-api-key"]

        pinned = entry["regexes"]
        assert len(pinned) == len(REPLAY_IDS) == 18, \
            f"{config}: {len(pinned)} replay ids pinned, 18 expected"

        literals: set[str] = set()
        for regex in pinned:
            assert regex[:1] == "^" and regex[-1:] == "$", f"unanchored: {regex!r}"
            assert "\\" not in regex and "[" not in regex and "*" not in regex \
                and "+" not in regex and "?" not in regex and "." not in regex, \
                f"{config}: not a plain literal: {regex!r}"
            literal = regex[1:-1]
            assert literal in REPLAY_IDS, f"{config}: unexpected waiver: {regex!r}"
            literals.add(literal)
        assert literals == set(REPLAY_IDS), \
            f"{config}: missing {sorted(set(REPLAY_IDS) - literals)}"
