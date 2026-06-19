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
