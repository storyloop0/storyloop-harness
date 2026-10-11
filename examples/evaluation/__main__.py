"""Run synthetic contracts; opt into hosted Langfuse and live models separately."""
import argparse
import json
import os
from pathlib import Path
import sys
from .runner import run_langfuse, run_local


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--langfuse', action='store_true', help='Write traces/scores to configured Langfuse')
    parser.add_argument('--live', action='store_true', help='Call configured real model; incurs charges')
    parser.add_argument('--output', type=Path, help='Also save the JSON report locally')
    args = parser.parse_args(argv)
    if args.langfuse:
        client = None
        try:
            required = ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY')
            if any(not os.environ.get(name, '').strip() for name in required):
                raise ValueError('Hosted mode requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY')
            if os.environ.get('LANGFUSE_TRACING_ENABLED', 'true').lower() == 'false':
                raise ValueError('Hosted mode requires LANGFUSE_TRACING_ENABLED to be enabled')
            from langfuse import Langfuse
            client = Langfuse()
            report = run_langfuse(client, live=args.live)
            report['export_status'] = 'unverified: SDK results do not confirm server ingestion'
        except Exception as error:
            report = {'passed': False, 'items': [], 'error': f'{type(error).__name__}: {error}'}
        finally:
            if client is not None:
                client.shutdown()
    else:
        report = run_local(live=args.live)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
