"""Run the fixed offline policy comparison without provider or Langfuse calls."""
import argparse
import json
from pathlib import Path
import sys

from .runner import run_local


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Also save the JSON report locally')
    args = parser.parse_args(argv)
    report = run_local()
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
