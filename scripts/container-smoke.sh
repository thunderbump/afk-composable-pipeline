#!/usr/bin/env sh
set -eu

if command -v podman >/dev/null 2>&1; then
  runtime=podman
elif command -v docker >/dev/null 2>&1; then
  runtime=docker
else
  echo "container-smoke: SKIP no Docker or Podman runtime found"
  exit 0
fi

tag=afk-composable-pipeline:smoke
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

ledger="$tmpdir/ledger"
mkdir -p "$ledger"

"$runtime" build -t "$tag" -f Containerfile .
"$runtime" run --rm \
  -v "$ledger:/ledger" \
  "$tag" \
  run-step noop --input '{"smoke":true}' --ledger /ledger > "$tmpdir/out.json"

"$runtime" run --rm \
  -v "$ledger:/ledger" \
  "$tag" \
  run-step select-work \
  --input '{"required_labels":["afk:ready"],"sources":[{"type":"fixture","id":"fixture","items":[{"external_id":"smoke-1","title":"Smoke work","status":"open","labels":["afk:ready"],"acceptance_criteria":["select-work smoke passes"],"afk":{"ready":true}}]}]}' \
  --ledger /ledger > "$tmpdir/select-work-out.json"

python3 - "$tmpdir/out.json" "$ledger" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ledger = Path(sys.argv[2])
run_dir = ledger / "runs" / summary["run_id"]

result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
events = [
    json.loads(line)
    for line in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
]

assert summary["status"] == "succeeded", summary
assert result["output"] == {"smoke": True}, result
assert [event["event"] for event in events] == [
    "run.started",
    "step.started",
    "step.completed",
    "run.completed",
], events
assert (run_dir / "stdout.log").read_text(encoding="utf-8") == ""
assert (run_dir / "stderr.log").read_text(encoding="utf-8") == ""
print(f"container-smoke: PASS {summary['run_id']}")
PY

python3 - "$tmpdir/select-work-out.json" "$ledger" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ledger = Path(sys.argv[2])
run_dir = ledger / "runs" / summary["run_id"]

result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
selection = result["output"]

assert summary["step"] == "select-work", summary
assert summary["status"] == "succeeded", summary
assert selection["source_statuses"][0]["status"] == "selected", selection
assert selection["selected_work"][0]["external_id"] == "smoke-1", selection
assert selection["skipped_candidates"] == [], selection
print(f"container-smoke select-work: PASS {summary['run_id']}")
PY
