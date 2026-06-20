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
export GIT_AUTHOR_NAME="AFK Smoke"
export GIT_AUTHOR_EMAIL="afk-smoke@example.test"
export GIT_COMMITTER_NAME="AFK Smoke"
export GIT_COMMITTER_EMAIL="afk-smoke@example.test"
export GIT_ALLOW_PROTOCOL="file"

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

submodule_repo="$tmpdir/submodule-src"
repo="$tmpdir/repo-src"
checkout="$tmpdir/checkout"
mkdir -p "$submodule_repo" "$repo"
git -C "$submodule_repo" init --initial-branch main
git -C "$submodule_repo" config user.name "AFK Smoke"
git -C "$submodule_repo" config user.email "afk-smoke@example.test"
printf 'submodule smoke\n' > "$submodule_repo/submodule.txt"
git -C "$submodule_repo" add submodule.txt
git -C "$submodule_repo" commit -m "seed submodule"

git -C "$repo" init --initial-branch main
git -C "$repo" config user.name "AFK Smoke"
git -C "$repo" config user.email "afk-smoke@example.test"
printf 'root smoke\n' > "$repo/README.md"
git -C "$repo" add README.md
git -C "$repo" commit -m "seed root"
git -C "$repo" -c protocol.file.allow=always submodule add ../submodule-src deps/submodule
git -C "$repo" commit -m "add submodule"
start_commit="$(git -C "$repo" rev-parse HEAD)"
submodule_sha="$(git -C "$submodule_repo" rev-parse HEAD)"

"$runtime" run --rm \
  -e GIT_ALLOW_PROTOCOL=file \
  -v "$ledger:/ledger" \
  -v "$tmpdir:/work" \
  "$tag" \
  run-step prepare-checkout \
  --input '{"repo_url":"/work/repo-src","base_ref":"main","checkout_root":"/work","checkout_path":"/work/checkout","review_branch":"afk/smoke-review"}' \
  --ledger /ledger > "$tmpdir/prepare-checkout-out.json"

python3 - "$tmpdir/prepare-checkout-out.json" "$ledger" "$checkout" "$start_commit" "$submodule_sha" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ledger = Path(sys.argv[2])
checkout = Path(sys.argv[3])
start_commit = sys.argv[4]
submodule_sha = sys.argv[5]
run_dir = ledger / "runs" / summary["run_id"]

result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
publication = json.loads((run_dir / "publication-result.json").read_text(encoding="utf-8"))
prepared = result["output"]
submodule_git_file = checkout / "deps/submodule/.git"
gitdir_prefix = "gitdir: "
gitdir_text = submodule_git_file.read_text(encoding="utf-8").strip()
submodule_gitdir = (submodule_git_file.parent / gitdir_text[len(gitdir_prefix):]).resolve()

assert summary["step"] == "prepare-checkout", summary
assert summary["status"] == "succeeded", summary
assert prepared["status"] == "prepared", prepared
assert prepared["start_commit"] == start_commit, prepared
assert prepared["dirty"] is False, prepared
assert prepared["publication"]["status"] == "skipped_disabled", prepared
assert prepared["artifacts"]["publication"] == "publication-result.json", prepared
assert publication["artifact_type"] == "checkout-publication", publication
assert publication["output"] == prepared["publication"], publication
assert prepared["submodules"] == [
    {
        "path": "deps/submodule",
        "sha": submodule_sha,
        "gitdir": ".git/modules/deps/submodule",
    }
], prepared
assert (checkout / ".git").is_dir(), checkout
assert str(submodule_gitdir).startswith(str((checkout / ".git/modules").resolve())), submodule_gitdir
print(f"container-smoke prepare-checkout: PASS {summary['run_id']}")
PY

python3 - "$tmpdir/validate-input.json" "$start_commit" <<'PY'
import json
import sys
from pathlib import Path

worker_code = """
import json
import os
from pathlib import Path

request = json.loads(Path(os.environ["AFK_WORKER_REQUEST"]).read_text(encoding="utf-8"))
Path(os.environ["AFK_WORKER_RESULT"]).write_text(
    json.dumps(
        {
            "profile": request["profile"],
            "status": "pass",
            "failureCount": 0,
            "repo": request["repo"]["path"],
            "steps": [
                {
                    "name": "smoke_validate",
                    "status": "pass",
                    "category": "ok",
                    "reason": "container smoke validation passed",
                }
            ],
        }
    ),
    encoding="utf-8",
)
print("validate smoke complete")
""".strip()

payload = {
    "checkout": {
        "status": "prepared",
        "repo_url": "/work/repo-src",
        "checkout_path": "/work/checkout",
        "review_branch": "afk/smoke-review",
        "requested_ref": "main",
        "start_commit": sys.argv[2],
    },
    "validation": {"dry_run": True, "timeout_seconds": 30},
    "worker": {
        "type": "local-command",
        "command": ["python3", "-c", worker_code],
        "timeout_seconds": 30,
    },
}
Path(sys.argv[1]).write_text(json.dumps(payload), encoding="utf-8")
PY

"$runtime" run --rm \
  -v "$ledger:/ledger" \
  -v "$tmpdir:/work" \
  "$tag" \
  run-step validate \
  --profile tier3-harness \
  --input "$(cat "$tmpdir/validate-input.json")" \
  --ledger /ledger > "$tmpdir/validate-out.json"

python3 - "$tmpdir/validate-out.json" "$ledger" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ledger = Path(sys.argv[2])
run_dir = ledger / "runs" / summary["run_id"]

result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
worker_request = json.loads((run_dir / "worker-request.json").read_text(encoding="utf-8"))
worker_result = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")

validated = result["output"]
assert summary["step"] == "validate", summary
assert summary["status"] == "succeeded", summary
assert validated["status"] == "validated", validated
assert validated["classification"] == "success", validated
assert validated["artifacts"] == {
    "worker_request": "worker-request.json",
    "worker_result": "worker-result.json",
}, validated
assert worker_request["profile"] == "tier3-harness", worker_request
assert worker_request["repo"]["path"] == "/work/checkout", worker_request
assert worker_result["artifact_type"] == "worker-result", worker_result
assert worker_result["result"]["raw"]["status"] == "pass", worker_result
assert "validate smoke complete" in stdout_log, stdout_log
print(f"container-smoke validate: PASS {summary['run_id']}")
PY

python3 - "$tmpdir/implement-input.json" "$start_commit" <<'PY'
import json
import sys
from pathlib import Path

agent_code = """
import json
import os
import subprocess
from pathlib import Path

capsule = json.loads(Path(os.environ["AFK_JOB_CAPSULE"]).read_text(encoding="utf-8"))
Path("implemented-smoke.txt").write_text(capsule["work_item"]["external_id"] + "\\n", encoding="utf-8")
subprocess.run(["git", "add", "implemented-smoke.txt"], check=True)
subprocess.run(["git", "commit", "-m", "implement smoke"], check=True)
Path("agent-result.json").write_text(
    json.dumps({"status": "completed", "summary": "smoke implemented"}),
    encoding="utf-8",
)
print("implement smoke complete")
""".strip()

payload = {
    "work_selection": {
        "schema_version": 1,
        "selected_work": [
            {
                "source_id": "fixture",
                "source_type": "fixture",
                "external_id": "smoke-implement",
                "url": "",
                "title": "Smoke implement",
                "status": "open",
                "labels": ["afk:ready"],
                "parent": None,
                "workstream": "smoke",
                "acceptance_criteria": ["implement smoke passes"],
                "dependencies": [],
                "blockers": [],
                "dependency_status": "clear",
                "afk": {"ready": True},
            }
        ],
    },
    "checkout": {
        "status": "prepared",
        "checkout_path": "/work/checkout",
        "review_branch": "afk/smoke-review",
        "requested_ref": "main",
        "start_commit": sys.argv[2],
    },
    "guardrails": ["stay within checkout"],
    "validation": {"profile": "smoke", "commands": [["python3", "-m", "unittest", "discover", "-s", "tests"]]},
    "agent": {
        "type": "fake-pi-command",
        "command": ["python3", "-c", agent_code],
        "result_path": "agent-result.json",
    },
}
Path(sys.argv[1]).write_text(json.dumps(payload), encoding="utf-8")
PY

"$runtime" run --rm \
  -e GIT_AUTHOR_NAME="AFK Smoke" \
  -e GIT_AUTHOR_EMAIL="afk-smoke@example.test" \
  -e GIT_COMMITTER_NAME="AFK Smoke" \
  -e GIT_COMMITTER_EMAIL="afk-smoke@example.test" \
  -v "$ledger:/ledger" \
  -v "$tmpdir:/work" \
  "$tag" \
  run-step implement \
  --input "$(cat "$tmpdir/implement-input.json")" \
  --ledger /ledger > "$tmpdir/implement-out.json"

python3 - "$tmpdir/implement-out.json" "$ledger" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ledger = Path(sys.argv[2])
run_dir = ledger / "runs" / summary["run_id"]

result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
capsule = json.loads((run_dir / "job-capsule.json").read_text(encoding="utf-8"))
agent_result = json.loads((run_dir / "agent-result.json").read_text(encoding="utf-8"))
stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")

implemented = result["output"]
assert summary["step"] == "implement", summary
assert summary["status"] == "succeeded", summary
assert implemented["status"] == "implemented", implemented
assert implemented["classification"] == "success", implemented
assert implemented["git"]["changed_files"] == ["implemented-smoke.txt"], implemented
assert implemented["artifacts"] == {
    "job_capsule": "job-capsule.json",
    "agent_result": "agent-result.json",
}, implemented
assert capsule["artifact_type"] == "job-capsule", capsule
assert capsule["capsule"]["work_item"]["external_id"] == "smoke-implement", capsule
assert agent_result["artifact_type"] == "agent-result", agent_result
assert agent_result["result"]["summary"] == "smoke implemented", agent_result
assert "implement smoke complete" in stdout_log, stdout_log
print(f"container-smoke implement: PASS {summary['run_id']}")
PY

python3 - "$tmpdir/review-input.json" "$tmpdir/implement-out.json" "$tmpdir/validate-out.json" "$ledger" <<'PY'
import json
import sys
from pathlib import Path

implement_summary = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
validate_summary = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
host_ledger = Path(sys.argv[4])
implement_run = host_ledger / "runs" / implement_summary["run_id"]
implemented = json.loads((implement_run / "step-result.json").read_text(encoding="utf-8"))["output"]

reviewer_code = """
import json
import os
from pathlib import Path

request = json.loads(Path(os.environ["AFK_REVIEWER_REQUEST"]).read_text(encoding="utf-8"))
pack = request["evidence_pack"]
assert pack["work_item"]["external_id"] == "smoke-implement"
assert pack["implementation"]["git"]["changed_files"] == ["implemented-smoke.txt"]
assert pack["validation"]["required"][0]["status"] == "validated"
Path(os.environ["AFK_REVIEWER_RESULT"]).write_text(
    json.dumps(
        {
            "status": "pass",
            "summary": "smoke review passed",
            "findings": [{"status": "pass", "title": "Smoke evidence complete"}],
        }
    ),
    encoding="utf-8",
)
print("review smoke complete")
""".strip()

payload = {
    "work_item": implemented["work_item"],
    "checkout": {
        "status": "prepared",
        "checkout_path": "/work/checkout",
        "review_branch": "afk/smoke-review",
        "requested_ref": "main",
        "start_commit": implemented["git"]["before_commit"],
    },
    "implementation": {
        "status": implemented["status"],
        "summary": implemented["summary"],
        "git": implemented["git"],
    },
    "validation": {
        "required_artifacts": [
            {
                "name": "tier3-harness",
                "step_result_path": f"/ledger/runs/{validate_summary['run_id']}/step-result.json",
                "worker_result_path": f"/ledger/runs/{validate_summary['run_id']}/worker-result.json",
            }
        ]
    },
    "guardrails": [{"name": "stay within checkout", "status": "pass"}],
    "cleanup": {"status": "clean", "resources": []},
    "reviewer": {
        "type": "fake-reviewer-command",
        "command": ["python3", "-c", reviewer_code],
        "timeout_seconds": 30,
    },
}
Path(sys.argv[1]).write_text(json.dumps(payload), encoding="utf-8")
PY

"$runtime" run --rm \
  -v "$ledger:/ledger" \
  -v "$tmpdir:/work" \
  "$tag" \
  run-step review \
  --input "$(cat "$tmpdir/review-input.json")" \
  --ledger /ledger > "$tmpdir/review-out.json"

python3 - "$tmpdir/review-out.json" "$ledger" <<'PY'
import json
import sys
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ledger = Path(sys.argv[2])
run_dir = ledger / "runs" / summary["run_id"]

result = json.loads((run_dir / "step-result.json").read_text(encoding="utf-8"))
evidence_pack = json.loads((run_dir / "evidence-pack.json").read_text(encoding="utf-8"))
reviewer_result = json.loads((run_dir / "reviewer-result.json").read_text(encoding="utf-8"))
review_summary = (run_dir / "review-summary.md").read_text(encoding="utf-8")
stdout_log = (run_dir / "stdout.log").read_text(encoding="utf-8")

reviewed = result["output"]
assert summary["step"] == "review", summary
assert summary["status"] == "succeeded", summary
assert reviewed["status"] == "passed", reviewed
assert reviewed["classification"] == "success", reviewed
assert reviewed["artifacts"] == {
    "evidence_pack": "evidence-pack.json",
    "reviewer_request": "reviewer-request.json",
    "reviewer_result": "reviewer-result.json",
    "review_summary": "review-summary.md",
}, reviewed
assert evidence_pack["artifact_type"] == "evidence-pack", evidence_pack
assert evidence_pack["evidence_pack"]["validation"]["required"][0]["status"] == "validated", evidence_pack
assert reviewer_result["artifact_type"] == "reviewer-result", reviewer_result
assert reviewer_result["result"]["summary"] == "smoke review passed", reviewer_result
assert "Smoke evidence complete" in review_summary, review_summary
assert "review smoke complete" in stdout_log, stdout_log
print(f"container-smoke review: PASS {summary['run_id']}")
PY
