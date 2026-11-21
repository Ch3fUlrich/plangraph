"""Read-only benchmark coverage gate for each image release/swap database."""
import argparse
import json
from pathlib import Path
import sys

# Support direct execution from an uninstalled checkout.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import benchmark_rows_from_db


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    models = list(dict.fromkeys(l.model for l in DEFAULT_LEGS
                               if set(l.roles) & {"implement", "review"}))
    reports = []
    for path in args.db:
        rows, errors = benchmark_rows_from_db(path)
        counts = {m: rows.get(m, {}).get("benchmark_row_count", 0) for m in models}
        reports.append({"db": path, "row_counts": counts,
                        "total_rows": sum(r["benchmark_row_count"] for r in rows.values()),
                        "missing_models": [m for m, count in counts.items() if not count],
                        "errors": errors})
    code = 2 if any(r["errors"] for r in reports) else 1 if any(r["missing_models"] for r in reports) else 0
    if args.json:
        print(json.dumps({"databases": reports, "exit_code": code}, indent=2))
    else:
        for report in reports:
            print(f"{report['db']}: {report['total_rows']} benchmark rows")
            for model, count in report["row_counts"].items():
                print(f"  {model}: {count} rows" + (" MISSING" if not count else ""))
            for error in report["errors"]:
                print(f"  ERROR: {error}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
