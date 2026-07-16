from __future__ import annotations

import argparse

from afk.run_store import RunStoreError
from afk.start import StartError, approve_bootstrap_validation


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m afk.bootstrap_approval",
        description="Approve an exact Candidate bootstrap validation harness",
    )
    parser.add_argument("harness")
    parser.add_argument("--run-id")
    parser.add_argument("--timeout-seconds", type=int, default=2700)
    args = parser.parse_args()
    try:
        run_id = approve_bootstrap_validation(
            args.harness,
            timeout_seconds=args.timeout_seconds,
            run_id=args.run_id,
        )
    except (StartError, RunStoreError) as exc:
        parser.error(str(exc))
    print(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
