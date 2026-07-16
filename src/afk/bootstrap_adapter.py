from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

if __package__:
    from afk.validation_contract import VALIDATION_STATUS_EXIT_CODES
else:
    from validation_contract import VALIDATION_STATUS_EXIT_CODES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    request = _request(Path(args.request))
    completed = subprocess.run(
        [args.harness, request["candidate_sha"]],
        check=False,
    )
    statuses_by_exit_code = {
        exit_code: status for status, exit_code in VALIDATION_STATUS_EXIT_CODES.items()
    }
    status = statuses_by_exit_code.get(completed.returncode, "inconclusive")
    evidence = Path(request["evidence_dir"])
    (evidence / "bootstrap.log").write_text(
        f"approved bootstrap harness exited {completed.returncode}\n",
        encoding="utf-8",
    )
    (evidence / "result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_sha": request["candidate_sha"],
                "status": status,
                "summary": f"approved bootstrap validation {status}",
                "checks": [
                    {
                        "name": "bootstrap",
                        "status": status,
                        "log_path": "bootstrap.log",
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return VALIDATION_STATUS_EXIT_CODES[status]


def _request(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "run_id", "candidate_sha", "evidence_dir"}
        or value.get("schema_version") != 1
        or not all(
            isinstance(value.get(key), str)
            for key in ("run_id", "candidate_sha", "evidence_dir")
        )
    ):
        raise ValueError("invalid bootstrap validation request")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
