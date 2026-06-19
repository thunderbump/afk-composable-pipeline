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

`step-result.json` contains the no-op output, which is the original JSON input.

## Development

Run the fixture-backed CLI tests:

```sh
python3 -m unittest discover -s tests
```

Run the container smoke test:

```sh
./scripts/container-smoke.sh
```

The smoke script builds the image and runs `afk run-step noop` when Docker or
Podman is available. If neither runtime exists, it exits successfully with a
clear skip message.
