"""Fixed readiness protocol CLI; contains no poller, writer or metric publisher."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from readiness import CHECKS, collect_report, run_check  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', choices=CHECKS)
    args = parser.parse_args()
    if args.check:
        status, reason = run_check(args.check)
        print(json.dumps(dict(status=status, reason=reason)))
        return 0
    report = collect_report(ROOT.name)
    print(json.dumps(report, separators=(',', ':'), allow_nan=False))
    return 0 if all(item['status'] == 'pass' for item in report['checks'].values()) else 1


if __name__ == '__main__':
    sys.exit(main())
