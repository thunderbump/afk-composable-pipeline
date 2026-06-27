## Agent workflow

### Issue tracker

Track implementation work in the central Beads workspace at `/home/bump/Projects/beads`.
Run Beads commands from that workspace with the repo ownership label
`project:afk-composable-pipeline`.

```sh
cd /home/bump/Projects/beads
bd <command>
```

Beads database authentication uses the password stored at
`/home/bump/Projects/beads/secrets/dolt_beads_password.txt`. When a command
needs authentication, read the password only for that single invocation:

```sh
cd /home/bump/Projects/beads
BEADS_DOLT_PASSWORD="$(sed -n '1p' secrets/dolt_beads_password.txt)" bd <command>
```

Do not print the secret, paste it into chat, commit it, or export it into a
long-lived shell session.

### Implementation

- Route implementation work through Beads before changing code.
- Keep work scoped to the active Bead and use `project:afk-composable-pipeline`
  on the tracked item.
- Commit the scoped change first. Before publishing or updating the PR as
  ready for review, merging, or closing, make sure the committed implemented
  HEAD has the final validation evidence and passed final review evidence
  required by the AFK workflow. Do not treat validation alone as sufficient
  for publication.

### Validation, review, and publishing references

This repo already documents the AFK pipeline surfaces. Refer to those sources
instead of copying long instructions into agent responses:

- [README.md](README.md): validation worker usage, final review step, and
  workstream publisher examples
- [src/afk/validation.py](src/afk/validation.py): validation step behavior
- [src/afk/review.py](src/afk/review.py): final review step behavior
- [tests/test_validate_cli.py](tests/test_validate_cli.py) and
  [tests/test_review_cli.py](tests/test_review_cli.py): CLI-level expectations

### Sub-agents

Whenever you use a sub-agent, choose a model appropriate to the task and stay
within `gpt-5.4` or lower assumptions.

- For implementation and non-trivial review work, prefer `gpt-5.4`.
- For small doc updates, quick searches, and lightweight follow-up checks, use
  `gpt-5.4-mini` or `gpt-5.3-codex-spark` when it is sufficient.

### PR review cycle

- Every implementation PR gets two review passes from sub-agents:
  - one reviewer checks correctness against the Bead requirements
  - one reviewer looks for bugs, regressions, and missing validation
- Put each reviewer's findings on the PR.
- Address feedback one item at a time through sequential sub-agents, and reply
  on the PR with what changed for each item.
- If the changes are substantial or new risk appears, run another two-reviewer
  cycle and repeat the same sequential response loop.
- Close or merge the PR only after feedback has been addressed and the
  implemented HEAD has both final validation evidence and passed final review
  evidence.
