from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
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
    with tempfile.TemporaryDirectory(prefix="afk-candidate-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        _materialize_candidate_snapshot(candidate, request["candidate_sha"], snapshot)
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
                str(snapshot),
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
                "--",
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
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise CandidateBrokerError("candidate broker request is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema_version", "candidate_sha", "candidate_path", "command"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != SCHEMA_VERSION
        or not _is_sha(value.get("candidate_sha"))
        or not isinstance(value.get("candidate_path"), str)
        or "\0" in value["candidate_path"]
        or not Path(value["candidate_path"]).is_absolute()
        or not isinstance(value.get("command"), list)
        or not value["command"]
        or not all(
            isinstance(item, str) and item and "\0" not in item
            for item in value["command"]
        )
    ):
        raise CandidateBrokerError("candidate broker request is invalid")
    return value


def _require_exact_candidate(candidate: Path, candidate_sha: str) -> None:
    if not is_exact_clean_commit(candidate, candidate_sha):
        raise CandidateBrokerError("Candidate does not match its exact clean commit")


def _materialize_candidate_snapshot(
    candidate: Path, candidate_sha: str, destination: Path
) -> None:
    listing = subprocess.run(
        ["git", "ls-tree", "-rz", "--full-tree", candidate_sha],
        cwd=candidate,
        capture_output=True,
        check=False,
    )
    if listing.returncode != 0:
        raise CandidateBrokerError("exact Candidate snapshot is unavailable")
    destination.mkdir(mode=0o700)
    try:
        records = listing.stdout.rstrip(b"\0").split(b"\0") if listing.stdout else []
        for record in records:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split()
            if (mode, object_type) not in {
                (b"100644", b"blob"),
                (b"100755", b"blob"),
                (b"120000", b"blob"),
            }:
                raise CandidateBrokerError(
                    "exact Candidate snapshot contains an unsupported entry"
                )
            relative = PurePosixPath(os.fsdecode(path_bytes))
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise CandidateBrokerError("exact Candidate snapshot path is invalid")
            blob = subprocess.run(
                ["git", "cat-file", "blob", object_id],
                cwd=candidate,
                capture_output=True,
                check=False,
            )
            if blob.returncode != 0:
                raise CandidateBrokerError("exact Candidate snapshot is unavailable")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == b"120000":
                os.symlink(os.fsdecode(blob.stdout), target)
            else:
                target.write_bytes(blob.stdout)
                target.chmod(0o755 if mode == b"100755" else 0o644)
    except CandidateBrokerError:
        raise
    except (OSError, ValueError) as exc:
        raise CandidateBrokerError("exact Candidate snapshot is invalid") from exc


def _is_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


if __name__ == "__main__":
    raise SystemExit(main())
