# AFK

AFK carries one explicit Bead through implementation, validation, independent
Standards and Spec review, repair, exact-head merge, tracker closure, and a
terminal retrospective. It runs one durable Run at a time and records the facts
needed to resume safely after interruption.

AFK v1 is intentionally fixed-purpose. It has no recipe engine, step registry,
automatic work selection, generic worker adapters, or compatibility workstream.

## Requirements

- Python 3.11 or newer
- Linux with readable `/proc` and pidfd support
- Git, GitHub CLI, systemd user units, Bubblewrap, and Codex
- A central Beads workspace (default: `/home/bump/Projects/beads`)
- GitHub and Beads credentials configured outside the repository

Install into a virtual environment:

```sh
python -m venv .venv
.venv/bin/pip install -e .
```

## Run lifecycle

Start one Run by exact Bead ID from the target repository checkout:

```sh
afk start central-example.1
```

AFK snapshots the Bead, pins the repository base, creates an isolated worktree,
launches implementation, validates the exact Candidate, performs Standards and
Spec review, applies at most four automated repairs, and reconciles publication
and merge effects. A successful Run closes its Bead and clears the Active Run.

Inspect the Active Run or a retained Run:

```sh
afk status
afk status <run-id>
afk status <run-id> --json
afk report <run-id>
```

`status` returns 2 for `attention_required` and includes the safe resume
precondition. Resume only after addressing the reported condition:

```sh
afk resume
afk resume <run-id> --note "operator context"
```

If an attention-retained Run must be retired without rewriting its evidence:

```sh
afk supersede --reason "why this Run must not continue"
```

Supersession is restricted to the Active Run in `attention_required`. It
appends an audit event and clears the Active pointer; it does not delete the Run
or its evidence.

## Repository-owned validation

A normal target repository pins its validation control flow in `afk.toml`:

```toml
schema_version = 1

[validation]
command = ["./scripts/validation-worker.sh"]
trusted_files = [
  "scripts/validation-worker.sh",
  "scripts/validate.sh",
]
timeout_seconds = 2700
```

Every trusted file must be a tracked regular file with the same blob and mode in
the Candidate and pinned base. AFK materializes those base-owned files outside
the Candidate checkout and supplies an ephemeral Candidate broker capability.
The validator therefore tests the exact Candidate without giving Candidate code
access to the validation request, broker token, host checkout, or Gate evidence.

A repository without an accepted `afk.toml` may be started explicitly in
bootstrap mode:

```sh
afk start central-example.1 --bootstrap-contract
python -m afk.bootstrap_approval scripts/validate.sh --timeout-seconds 300
afk resume
```

Bootstrap approval is bound to one Candidate SHA and one tracked executable
identity. Any Candidate change requires a new approval.

## Durability and evidence

State defaults to `$XDG_STATE_HOME/afk` or
`~/.local/state/afk`. The append-only Event History is authoritative;
projections are rebuildable. Run evidence is redacted before persistence,
manifested, byte-bounded, and stored separately from disposable worktrees.

Only one Active Run may own local or external state. External mutations are
recorded as prepared and confirmed effects so resume reconciles ambiguous
outcomes instead of repeating them.

## Configuration

`AFK_BEADS_WORKSPACE` selects the central Beads workspace. Beads database
authentication is read from the password file configured in
`$XDG_CONFIG_HOME/afk/config.json` (or `~/.config/afk/config.json`):

```json
{
  "schema_version": 1,
  "beads": {
    "password_file": "/absolute/path/outside/git/dolt_beads_password.txt"
  }
}
```

Never place passwords, tokens, or private keys in this repository, Run prompts,
Bead comments, or shell arguments.

## Development validation

Run focused tests while changing a seam, then the complete suite and repository
hooks:

```sh
.venv/bin/python -m unittest tests.test_cli_surface
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
pre-commit run --all-files
```

Implementation changes require a final two-axis review against
`CODING_STANDARDS.md` and the active Bead before publication or merge.
