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
statuses, selected work, and skipped candidates.

## Development

Run the fixture-backed CLI tests:

```sh
python3 -m unittest discover -s tests
```

Run the container smoke test:

```sh
./scripts/container-smoke.sh
```

The smoke script builds the image and runs `afk run-step noop` and a
fixture-backed `afk run-step select-work` when Docker or Podman is available. If
neither runtime exists, it exits successfully with a clear skip message.
