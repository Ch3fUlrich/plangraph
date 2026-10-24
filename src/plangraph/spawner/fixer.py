"""Pure file-ownership partitioning and writer report contracts."""
import math
import re


def split_findings(findings, limit=10):
    """Balance connected file components; indivisible components may exceed limit."""
    if limit < 1:
        raise ValueError('limit must be positive')
    components = []
    unassigned = []
    for finding in findings:
        files = set(finding.get('files') or ())
        if not files:
            unassigned.append(finding)
            continue
        merged = [finding]
        rest = []
        for paths, items in components:
            if paths & files:
                files |= paths
                merged.extend(items)
            else:
                rest.append((paths, items))
        components = rest + [(files, merged)]
    if not findings:
        return ()
    bins = [[] for _ in range(min(len(components) or 1, math.ceil(len(findings) / limit)))]
    for _, items in sorted(components, key=lambda c: -len(c[1])):
        min(bins, key=len).extend(items)
    min(bins, key=len).extend(unassigned)
    return tuple(tuple(group) for group in bins if group)


def render_fixer_brief(findings):
    checklist = '\n'.join(f"{i}. [ ] {f.get('text', f.get('description', str(f)))}"
                          for i, f in enumerate(findings, 1))
    return (checklist + '\n\nThe run ends only when each item is fixed or refuted by a named test\n'
            'Run affected tests only; follow TEST-SPEED-AFFECTED.\nCOMMIT before finishing\n')


def remaining_gaps(output):
    """Read Markdown Remaining gaps sections and unchecked checklist entries."""
    gaps = []
    section = False
    for line in output.splitlines():
        heading = re.match(r'^\s*(?:#{1,6}\s*)?(?:\*\*)?Remaining gaps(?:\*\*)?\s*:?(.*)$', line, re.I)
        if heading:
            section = True
            line = heading.group(1).strip()
        elif re.match(r'^\s*#{1,6}\s', line):
            section = False
        unchecked = re.match(r'^\s*(?:[-*]|\d+[.)])\s+\[ \]\s+(.+)', line)
        if unchecked:
            value = unchecked.group(1)
        elif section:
            value = re.sub(r'^\s*(?:[-*]|\d+[.)])\s+', '', line).strip()
        else:
            continue
        if value and value.lower().rstrip('.') not in ('none', 'none remaining', 'no remaining gaps', 'n/a'):
            if value not in gaps:
                gaps.append(value)
    return gaps
