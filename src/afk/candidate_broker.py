from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from afk.checkouts import is_exact_clean_commit
from afk.jsonutil import canonical_json


SCHEMA_VERSION = 1


class CandidateBrokerError(ValueError):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated Candidate command")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    try:
        request = _read_request(Path(args.request))
        result = run_candidate(request)
        Path(args.result).write_text(canonical_json(result) + "\n", encoding="utf-8")
    except (CandidateBrokerError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def run_candidate(request: dict[str, Any]) -> dict[str, Any]:
    candidate = Path(request["candidate_path"])
    _require_exact_candidate(candidate, request["candidate_sha"])
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise CandidateBrokerError("bubblewrap is unavailable")
    completed = subprocess.run(
        [
            bwrap,
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/work",
            "--ro-bind",
            str(candidate),
            "/candidate",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/work",
            "--chdir",
            "/work",
            *request["command"],
        ],
        capture_output=True,
        check=False,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": request["candidate_sha"],
        "status": "completed",
        "exit_code": completed.returncode,
        "stdout": completed.stdout.decode("utf-8", errors="replace"),
        "stderr": completed.stderr.decode("utf-8", errors="replace"),
    }


def _read_request(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema_version", "candidate_sha", "candidate_path", "command"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or not _is_sha(value.get("candidate_sha"))
        or not isinstance(value.get("candidate_path"), str)
        or not Path(value["candidate_path"]).is_absolute()
        or not isinstance(value.get("command"), list)
        or not value["command"]
        or not all(isinstance(item, str) and item for item in value["command"])
    ):
        raise CandidateBrokerError("candidate broker request is invalid")
    return value


def _require_exact_candidate(candidate: Path, candidate_sha: str) -> None:
    if not is_exact_clean_commit(candidate, candidate_sha):
        raise CandidateBrokerError("Candidate does not match its exact clean commit")


def _is_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


if __name__ == "__main__":
    raise SystemExit(main())
