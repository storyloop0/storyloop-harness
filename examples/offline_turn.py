"""Run the same deterministic story as storyloop-harness-demo."""
import sys
from storyloop_harness.demo import main


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
