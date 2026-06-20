# AFK Composable Pipeline

Small Python scaffold for composable AFK pipeline steps. The first public
interface runs a no-op step and records evidence under a ledger directory.

## Usage

Run through the installed console script:

```sh
afk run-step noop --input '{"message":"hello"}' --ledger ledger
```

Run through the module fallback from a checkout:

```sh
PYTHONPATH=src python3 -m afk run-step noop --input '{"message":"hello"}' --ledger ledger
```

The CLI prints a JSON summary containing the `run_id`, step, status, and result
path.

Project contracts can be resolved by slug. The contract records stable project
facts and its resolved path/hash are written to the ledger:

```sh
PYTHONPATH=src python3 -m afk run-step noop \
  --project bump-eqemu \
  --contracts-dir project-contracts \
  --input '{"message":"hello"}' \
  --ledger ledger
```

Step names are dispatched through the fixed Python registry. Unknown steps fail
before ledger preparation with a clear list of known steps.

Select fixture, GitHub Issues, and Beads work sources with the same step
interface:

```sh
PYTHONPATH=src python3 -m afk run-step select-work \
  --input '{"required_labels":["afk:ready"],"sources":[{"type":"fixture","id":"fixture","items":[{"external_id":"demo-1","title":"Demo","status":"open","labels":["afk:ready"],"acceptance_criteria":["selected"],"afk":{"ready":true}}]}]}' \
  --ledger ledger
```

The selector attempts every configured source. Unreachable or unauthenticated
GitHub/Beads sources are skipped with explicit source status evidence instead
of failing the whole run.

Beads sources must point at an explicit absolute workspace mount and declare
`"workspace_kind": "central"` or `"workspace_kind": "mounted"` so a target
checkout is not accidentally treated as the issue tracker. Credential path
overrides are not accepted; the mounted workspace must provide
`secrets/dolt_beads_password.txt`.

Prepare a real clone for implementation or validation:

```sh
PYTHONPATH=src python3 -m afk run-step prepare-checkout \
  --input '{"repo_url":"git@github.com:thunderbump/bump-EQEmu.git","base_ref":"master","checkout_root":"/work","checkout_path":"/work/bump-EQEmu","review_branch":"afk/example"}' \
  --ledger ledger
```

`prepare-checkout` creates or reuses a full clone, checks out the requested ref
onto the review branch, initializes submodules, and records repo URL, base/ref,
start commit, checkout path, dirty-tree state, and submodule SHAs. Existing dirty
checkouts are refused with actionable status evidence. Existing clean checkouts
must have a matching `origin`, and checkout paths must stay inside an explicit
absolute `checkout_root` mount. Branch publication is off by default and is
recorded in `publication-result.json`; passing
`"publish":{"enabled":true,"branch":"afk/example"}` pushes the prepared `afk/*`
branch to `origin` and records the fetchable ref.

Run a fake/local Pi implementation adapter against a prepared checkout:

```sh
PYTHONPATH=src python3 -m afk run-step implement \
  --input '{"work_selection":{"selected_work":[{"source_id":"fixture","source_type":"fixture","external_id":"demo-1","title":"Demo","status":"open","labels":["afk:ready"],"acceptance_criteria":["implemented"],"dependencies":[],"blockers":[],"dependency_status":"clear","afk":{"ready":true}}]},"checkout":{"status":"prepared","checkout_path":"/work/bump-EQEmu","review_branch":"afk/example","requested_ref":"master","start_commit":"<sha>"},"guardrails":["stay within checkout"],"validation":{"profile":"tier1","commands":[["python3","-m","unittest","discover","-s","tests"]]},"agent":{"type":"fake-pi-command","command":["python3","-c","from pathlib import Path; Path(\"agent-result.json\").write_text(\"{\\\"status\\\":\\\"completed\\\",\\\"summary\\\":\\\"done\\\"}\", encoding=\"utf-8\")"],"result_path":"agent-result.json"}}' \
  --ledger ledger
```

`implement` consumes the normalized `WorkSelection` item and checkout metadata,
builds a `job-capsule.json` with work context, guardrails, checkout ref, and
validation hints, invokes the configured fake/local Pi command, normalizes the
adapter result into `agent-result.json`, and records post-run git metadata.
Adapter runtime failures, adapter protocol failures, and target-code failures
are classified separately. Adapter stdout/stderr are redacted and written to
the normal ledger logs.

## Ledger Artifacts

Each invocation writes a new run directory:

```text
ledger/
  runs/
    <run-id>/
      command.json
      ledger.jsonl
      stdout.log
      stderr.log
      step-result.json
```

`ledger.jsonl` is append-only within the run and records the public event stream:

- `run.started`
- `step.started`
- `step.completed`
- `run.completed`

`step-result.json` contains the step output. For `noop`, this is the original
JSON input. For `select-work`, this is a normalized `WorkSelection` with source
statuses, selected work, and skipped candidates. For `prepare-checkout`, this is
the checkout provenance and a pointer to `publication-result.json`, which stores
the publication result separately. For `implement`, it contains normalized work,
checkout, agent, and git metadata plus pointers to `job-capsule.json` and
`agent-result.json`.

## Development

Run the fixture-backed CLI tests:

```sh
python3 -m unittest discover -s tests
```

Run the container smoke test:

```sh
./scripts/container-smoke.sh
```

The smoke script builds the image and runs `afk run-step noop`, a fixture-backed
`afk run-step select-work`, `afk run-step prepare-checkout` against a mounted
local repo with a real submodule, and `afk run-step implement` with a fake/local
Pi command when Docker or Podman is available. If neither runtime exists, it
exits successfully with a clear skip message.
