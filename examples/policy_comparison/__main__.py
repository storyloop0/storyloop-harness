"""Compare selected context policies; fixture is the default."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from .runner import run_local
from .pricing import load_pricing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Also save the JSON report locally')
    parser.add_argument('--case', action='append', dest='cases', help='Case ID; repeat to select several offline')
    parser.add_argument('--policy', action='append', dest='policies', help='Policy ID; repeat to select several offline')
    parser.add_argument('--live', action='store_true', help='Use configured provider; incurs usage charges')
    parser.add_argument('--pricing', type=Path, help='Local JSON price snapshot for cost estimates')
    args = parser.parse_args(argv)
    if args.live and (args.cases is None or args.policies is None
                      or len(args.cases) != 1 or len(args.policies) != 1):
        parser.error('--live requires exactly one --case and one --policy')
    try:
        pricing = load_pricing(args.pricing) if args.pricing is not None else None
        report = run_local(case_ids=args.cases, policy_ids=args.policies,
                           model_mode='live' if args.live else 'fixture', output_path=args.output,
                           pricing=pricing)
    except (asyncio.CancelledError, KeyboardInterrupt):
        return 130
    except ValueError as error:
        parser.error(str(error))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
