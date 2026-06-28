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

Run project validation through a Validation Worker adapter:

```sh
PYTHONPATH=src python3 -m afk run-step validate \
  --profile tier3-harness \
  --input '{"checkout":{"status":"prepared","repo_url":"git@github.com:thunderbump/bump-EQEmu.git","checkout_path":"/work/bump-EQEmu","review_branch":"afk/example","requested_ref":"master","start_commit":"<sha>"},"validation":{"dry_run":true,"timeout_seconds":30},"worker":{"type":"local-command","command":["python3","-c","import json, os; from pathlib import Path; request=json.loads(Path(os.environ[\"AFK_WORKER_REQUEST\"]).read_text(encoding=\"utf-8\")); Path(os.environ[\"AFK_WORKER_RESULT\"]).write_text(json.dumps({\"profile\":request[\"profile\"],\"status\":\"pass\",\"steps\":[]}), encoding=\"utf-8\")"]}}' \
  --ledger ledger
```

`validate` consumes prepared checkout metadata and a validation profile, writes
`worker-request.json`, invokes a configured local-command or remote-command
adapter, reads the worker evidence result, and records `worker-result.json`.
Local adapters receive `repo.path` plus `repo.commit`; remote-command adapters
receive `repo.url`, `repo.ref` when available, and pinned `repo.commit`.
Adapters receive `AFK_WORKER_REQUEST`, `AFK_WORKER_RESULT`, and
`AFK_WORKER_EVIDENCE_DIR`. The Bump EQEmu project contract maps AFK profiles
to the central-lve.6 worker request profiles and defaults to
`scripts/validation-worker.sh run --request <request>` when no explicit worker
adapter is supplied.

Validation failure categories distinguish missing worker results, adapter
timeouts/runtime failures, worker-reported validation failures, and skipped
profiles. Worker request/result payloads and stdout/stderr excerpts are
redacted before being stored in the ledger. Failed validations also record
`actionable_failures` with the failing command, exit code, and a log-path field
that is explicit about availability: when a worker provides a relative or
absolute log path, AFK records it as the exact resolved `log_path`; when no
path is provided, `log_path` is `null` and `log_path_status` is `unavailable`.
The first actionable excerpt is also preserved so follow-up work does not need
manual `rg`/`tail` inspection of worker logs.

Run final review against explicit implementation and validation evidence:

```sh
PYTHONPATH=src python3 -m afk run-step review \
  --input '{"work_item":{"source_id":"fixture","source_type":"fixture","external_id":"demo-1","title":"Demo","status":"open","labels":["afk:ready"],"acceptance_criteria":["reviewed"],"dependencies":[],"blockers":[],"dependency_status":"clear","afk":{"ready":true}},"checkout":{"status":"prepared","checkout_path":"/work/bump-EQEmu","review_branch":"afk/example","requested_ref":"master","start_commit":"<sha>"},"implementation":{"status":"implemented","summary":"done","git":{"before_commit":"<sha>","after_commit":"<sha2>","changed_files":["src/example.py"],"commits":[{"commit":"<sha2>","subject":"implement demo"}],"dirty":false,"dirty_status":[]}},"validation":{"required_artifacts":[{"name":"tier3-harness","step_result_path":"/ledger/runs/<validation-run>/step-result.json","worker_result_path":"/ledger/runs/<validation-run>/worker-result.json"}]},"guardrails":[{"name":"no secrets","status":"pass"}],"cleanup":{"status":"clean","resources":[]},"reviewer":{"type":"fake-reviewer-command","command":["python3","-c","import json, os; from pathlib import Path; request=json.loads(Path(os.environ[\"AFK_REVIEWER_REQUEST\"]).read_text(encoding=\"utf-8\")); assert request[\"evidence_pack\"][\"validation\"][\"required\"][0][\"status\"] == \"validated\"; Path(os.environ[\"AFK_REVIEWER_RESULT\"]).write_text(json.dumps({\"status\":\"pass\",\"summary\":\"ready\",\"findings\":[]}), encoding=\"utf-8\")"]}}' \
  --ledger ledger
```

`review` builds `evidence-pack.json` from the supplied work item acceptance
criteria, checkout metadata, changed files/commits, validation artifact
summaries, guardrails, cleanup status, and redaction metadata. It writes
`reviewer-request.json`, invokes a fake/local reviewer command with
`AFK_REVIEWER_REQUEST` and `AFK_REVIEWER_RESULT`, then stores the normalized
`reviewer-result.json` plus `review-summary.md`. Reviewer statuses are
normalized to `passed`, `failed`, or `request_revision`. The step refuses to
pass before invoking the reviewer when any required final validation artifact is
missing, skipped, failed, malformed, or otherwise not `validated`.

Run a shared-branch workstream recipe and terminal PR publisher:

```sh
PYTHONPATH=src python3 -m afk run-workstream \
  --workstream-id central-lve.9 \
  --parent central-lve \
  --input '{"workstream_id":"central-lve.9","review_branch":"afk/central-lve-9","steps":[{"name":"select-work","input":{"sources":[]}},{"name":"prepare-checkout","input":{"repo_url":"git@github.com:thunderbump/afk-composable-pipeline.git","base_ref":"afk/central-lve-8-final-review","checkout_root":"/work","checkout_path":"/work/afk-composable-pipeline"}},{"name":"implement","input":{"guardrails":[],"validation":{"profile":"tier1","commands":[]},"agent":{"type":"fake-pi-command","command":["python3","agent.py"]}}},{"name":"validate","profile":"tier1","input":{"validation":{"dry_run":false},"worker":{"type":"local-command","command":["python3","validate.py"]}}},{"name":"review","input":{"guardrails":[],"cleanup":{"status":"clean","resources":[]},"reviewer":{"type":"fake-reviewer-command","command":["python3","review.py"]}}}],"publisher":{"enabled":true,"mode":"create","repo":"thunderbump/afk-composable-pipeline","base":"afk/central-lve-8-final-review","head":"afk/central-lve-9","title":"central-lve.9: Workstream publisher","git":{"push":true,"remote":"origin"},"gh":{"auth":{"config_dir":"/work/mounts/gh-config"}}}}' \
  --ledger ledger
```

The recipe schema is intentionally small:

- `workstream_id`, `parent`, and `review_branch` identify the workstream and
  shared review branch. CLI `--workstream-id` and `--parent` override the recipe
  values.
- `retry_policy.max_retries` is optional and defaults to `0`. It bounds how many
  same-item retry checkout cycles may start after a failed validation.
- `review_cycles` is optional evidence for PR review passes after publication.
  Each cycle records reviewer roles such as `correctness` and `bug-risk`,
  review status, summary, optional PR comment URL, whether a response is
  required, and optional response evidence. Accepted cycle/review statuses are
  `passed`, `findings-open`, `request-changes`, and `findings-addressed`.
  Response objects must carry `status: "addressed"` or
  `status: "findings-addressed"`; a non-empty response string is also accepted
  as freeform addressed evidence. Repeated cycles are preserved so findings
  from earlier passes are not overwritten.
- `retrospective` is optional terminal evidence for a merged or explicit
  `no-merge` tracker decision. It carries a concise summary plus optional
  `changes`, `validation`, `review`, `unresolved_risks`, and
  `process_findings` string lists, optional `follow_up.recommended` /
  `follow_up.created` Beads references (`id`, `summary`, `labels`), and
  optional note-path lists under `notes.personal_work` and `notes.spikes`.
  Keep retrospective note paths free of secrets; AFK redacts sensitive-looking
  values before writing ledger outputs.
- `retrospective_follow_up` is optional top-level configuration for creating or
  recording Beads after the deterministic retrospective is built. By default
  AFK stays in recommendation-only mode. When enabled, it runs a local or fake
  command against a redacted request and records the normalized result under
  `pipeline_retrospective.follow_up.creation` without changing publication or
  tracker status.
- `tracker.terminal_decision` is optional. Leave it unset while a PR is open or
  under review. Set `{"status":"merged","merge_commit":"<sha>","pr_url":"<url>"}`
  only after the PR merges, or
  `{"status":"no-merge","reason":"<why>","pr_url":"<url>"}` when the branch is
  intentionally not going to merge and the source Beads item should close with
  that reason. `pr_url` is required for merged/no-merge terminal decisions. When
  unresolved `review_cycles` findings still require a response, add
  `review_feedback_status: "resolved"` or `"waived"` before AFK will emit
  close guidance for a terminal merge/no-merge decision.
- `steps` is an ordered list of existing step names: `select-work`,
  `prepare-checkout`, `implement`, `validate`, and `review`. Each step has an
  explicit `input` object, plus optional `profile` for `validate`.
- The runner injects prior step outputs when omitted: selected work and checkout
  into `implement`, checkout and profile into `validate`, and work item,
  checkout, implementation, final validation artifacts, and cleanup into
  `review`.
- `publisher` supports `mode: "create"` with `gh pr create` or `mode: "update"`
  with `gh pr edit`. If `gh pr edit` fails on the GitHub Projects classic
  GraphQL deprecation path, AFK falls back to `gh api --method PATCH
  repos/<owner>/<repo>/pulls/<number> --input <json>`. `git.path`/`gh.path` may
  point at fake command shims for offline tests. `git.push: true` pushes `HEAD`
  to the configured PR head before invoking `gh`.
- AFK always runs `gh auth status --hostname github.com` before any `git push`
  or `gh pr create/edit` attempt. Publisher auth stays on the minimal scrubbed
  environment by default, so missing GitHub auth blocks publication before
  push with terminal evidence and retry instructions. To publish a real GitHub
  PR deliberately, mount a GitHub CLI config directory outside the checkout and
  set `publisher.gh.auth.config_dir` to that absolute path. AFK passes it to
  `gh` through `GH_CONFIG_DIR` after validating that the directory exists and
  is outside the target checkout.
- Do not place raw token values in recipes. `publisher.gh.token`,
  `publisher.gh.github_token`, `publisher.gh.access_token`, `publisher.gh.api_key`,
  and similar ad hoc secret-bearing auth keys are rejected. Ambient `GH_TOKEN`,
  `GITHUB_TOKEN`, and similar variables are not inherited by publisher commands.

Actual PR publication is blocked unless at least one final `validate` step
produced `validated` evidence for the implemented HEAD and the final `review`
step produced `passed` for that same HEAD. A workstream can still finish as
`validated-unpublished` in two cases:

- the current HEAD already has final validation evidence and the next configured
  step would start a fresh `select-work` / `prepare-checkout` / `implement`
  cycle for the same item. AFK only allows a follow-up `select-work` to proceed
  when its configured input proves it excludes the current item; today that
  proof is limited to explicit `target_ids` or fully enumerated fixture
  candidates. Otherwise AFK stops conservatively and treats the follow-up as a
  same-item retry/fresh-cycle attempt.
- the current HEAD has final validation plus a passed final review, but
  `publisher.enabled` is `false`

Retry-hygiene terminal stops stay `blocked`, not `validated-unpublished`,
because they end on a failed-validation retry chain rather than a publishable
validated HEAD. Those blocked cases include:

- a failed validation followed by a same-item retry path that exceeds
  `retry_policy.max_retries`
- a failed validation followed by an attempt to start a fresh retry checkout
  while the previous retry checkout is still dirty
- a failed validation followed by an attempt to start a fresh retry checkout
  while the previous retry checkout is still awaiting validation evidence

Generated PR bodies include a `## Validation` section. Each validation bullet is
nonblank and has this contract:

```md
- <profile>: <status> - result: <worker-result-summary> - command: <worker-command> - summary: <validation-summary> - evidence: <step-result-path>; <worker-result-path>
```

`<profile>` falls back to `validation-N`, `<status>` falls back to `missing`,
and evidence fields are included only when available. Worker result summaries
come from worker evidence such as `steps[].name/status` (`unit=pass`) or the raw
worker status; ledger paths point to the step and worker result artifacts.

In both terminal outcomes `next_allowed_command` points at
`afk run-workstream ...`, not a
separate `afk publish` command. Treat it as the follow-up entrypoint: rerun the
workstream with an updated recipe that keeps the same review branch/current HEAD
and either adds the remaining final review/publisher path or enables the
publisher for the already-reviewed HEAD. The PR body is generated from ledger
facts: workstream identity, selected work, changed files, commits, validation
artifact refs/statuses, review result, cleanup, retry status, and artifact
paths.

### GitHub PR Smoke

Real smoke against a disposable/private repository with an intentional mounted
GitHub CLI config:

1. Prepare a disposable private repo and an absolute checkout-external GitHub
   CLI config directory such as `/work/mounts/gh-config`.
2. Run `afk run-workstream` with a publisher block like:

```json
{
  "publisher": {
    "enabled": true,
    "mode": "create",
    "repo": "OWNER/DISPOSABLE-REPO",
    "base": "main",
    "head": "afk/github-pr-smoke",
    "title": "central-afk-pr.2: GitHub publisher auth smoke",
    "git": {
      "push": true,
      "remote": "origin"
    },
    "gh": {
      "auth": {
        "config_dir": "/work/mounts/gh-config"
      }
    }
  }
}
```

3. Confirm `publication-result.json` reports `published`, then rerun with
   `mode: "update"` and `pr` set to the PR number or branch selector to smoke
   the edit path against the same disposable target.

When GitHub auth is not available, use the documented local substitute instead:

1. Point `publisher.git.path` and `publisher.gh.path` at local fake shims that
   record argv/env and return success.
2. Keep the same `publisher.gh.auth.config_dir` shape when you want to verify
   mounted-config handling locally; the fake shims can assert `GH_CONFIG_DIR`
   while proving that `GH_TOKEN`, `GITHUB_TOKEN`, and other ambient secrets stay
   absent.
3. Inspect `workstream-result.json`, `publication-result.json`, and `pr-body.md`
   in the ledger. The behavior tests in `tests/test_workstream_cli.py` are the
   reference substitute.

Generate the common single-item recipe from a Beads id and project contract:

```sh
PYTHONPATH=src python3 -m afk generate-recipe \
  --workstream-id central-afk-pr.1 \
  --project bump-eqemu \
  --contracts-dir project-contracts \
  --ledger ledger \
  --beads-workspace /home/bump/Projects/beads \
  --checkout-root /work \
  --checkout-path /work/bump-EQEmu \
  --validation-profile tier1 \
  --output recipes/central-afk-pr.1.json
```

The generated recipe is inspectable JSON for `afk run-workstream --input`.
It uses the contract repo URL/base branch, explicit Beads workspace and checkout
mounts, a `target_ids` selector for the requested Beads item, the named
validation profile, local fake implementation/validation/review adapters, and
`"publisher": {"enabled": false}`. Replace the local adapters or publisher only
when real worker/publisher credentials are intentionally available; the
generator does not invent credentials.

For `--validation-mode project-worker`, the generator embeds the worker host
contract into `steps[].input.validation`. By default that keeps
`validation.worker_home` under `checkout_root/.validation-worker/<checkout-name>`
and derives `validation.stack.path` as a sibling of the checkout path
(`checkout_path.parent / "bump-akk-stack-validation"`). If `checkout_root` is a
nested mount such as `/work/mounts/checkouts`, pass an explicit host stack path
with `--validation-stack-path /work/bump-akk-stack-validation` so the generated
recipe does not rely on a hidden sibling assumption.

Selection evidence is recorded by the normal `select-work` step. The generated
selector records the requested item in `selected_work`; non-target Beads
candidates are recorded in `skipped_candidates` with `target_id_mismatch`.
Unreachable or unauthenticated Beads workspaces block the workstream at
selection and leave actionable `source_statuses` such as `skipped_unreachable`
or `skipped_no_auth`.

Discover the next project item and emit an inspectable recipe in one step:

```sh
PYTHONPATH=src python3 -m afk run-next \
  --project bump-eqemu \
  --contracts-dir project-contracts \
  --checkout-root /work \
  --checkout-path /work/bump-EQEmu \
  --validation-profile tier1
```

`run-next` builds a project-scoped `select-work` request from the contract
labels plus the observed `ready-for-agent` tag, tries both Beads and GitHub
Issues sources when the contract can name a GitHub repo, and chooses a stable
default candidate from the valid results. The selection envelope records the
request, source statuses, chosen id/source, and the emitted recipe preview.
With `--selector-mode model`, the command invokes `codex exec` and accepts only
the lightweight model names `gpt-5.3-codex-spark` and `gpt-5.4-mini`; if the
model call fails or returns an invalid choice, it falls back to deterministic
selection.

For `bump-eqemu`, GitHub Issues are effectively disabled, so Beads are the
practical source there for now. The command still includes the GitHub source
when the contract repo points at GitHub, and it will skip cleanly when auth is
not available.

### Real Agent Container Contract

For container/remote execution with `agent.type: real-agent-command`, AFK validates
auth/config mounts up front and stores only their paths in the job capsule.

| mount key | recipe location | env exposed to adapter | required | requirement |
|---|---|---|---|---|
| Codex config dir | `agent.codex_home` | `CODEX_HOME` | required | absolute existing directory outside checkout; runner should provision `auth.json` when Codex auth is needed |
| shared config state | `agent.config_home` | `XDG_CONFIG_HOME` | required | absolute existing directory outside checkout |
| Pi config | `agent.env.PI_CONFIG_HOME` | `PI_CONFIG_HOME` | required | absolute existing directory outside checkout |
| Pi coding agent dir | `agent.env.PI_CODING_AGENT_DIR` | `PI_CODING_AGENT_DIR` | recommended | absolute path outside checkout |
| Pi session state dir | `agent.env.PI_CODING_AGENT_SESSION_DIR` | `PI_CODING_AGENT_SESSION_DIR` | recommended | absolute path outside checkout |
| wrapped secrets | `agent.wrapper_secret_files.<name>` | `AFK_JOB_CAPSULE -> capsule.agent_mounts.wrapper_secret_files` | optional | absolute existing files outside checkout, path-only values, non-secret logical keys |

If any required mount is missing, malformed, not absolute, inside checkout, or
non-existent, AFK fails early as `failed_invalid_payload` and does not execute
the adapter.

AFK currently validates the required mount directories and records
`codex_home`, `config_home`, `pi_config_home`, and wrapper secret file paths in
the job capsule. Additional Pi path env such as `PI_CODING_AGENT_DIR` and
`PI_CODING_AGENT_SESSION_DIR` is passed through to the adapter environment after
path validation, but is not yet copied into `capsule.agent_mounts`.

AFK forwards wrapper secret file paths only; it never forwards secret values in
`agent.env` or command args. Wrapper-side consumers read mounted files and export
real tokens before launching real Pi/Codex commands. Secret-bearing values are
not persisted in artifacts; they are only redacted in stdout/stderr and normalized
payloads.

Expected runtime auth evidence:

- Missing Codex auth should fail as `failed_runtime` with runtime evidence in
  `step-result.output.failures` and an adapter stderr/stdout excerpt that shows a
  missing-credential error (for example `missing credentials`).
- Expired Pi OAuth should fail as `failed_runtime` and can surface as
  `No API key for provider: openai-codex` in the adapter stderr excerpt.
- If an auth mount is invalid, AFK stops before execution and records
  `agent.env.*`/`agent.*` mount validation messages in the step result message.

Minimal recipe fragment for remote/container portability (for `central-afk-pr.7`):

```json
{
  "name": "implement",
  "input": {
    "guardrails": ["stay within the prepared checkout", "do not write secrets"],
    "validation": { "profile": "tier1", "commands": [] },
    "agent": {
      "type": "real-agent-command",
      "command": ["python3", "agent.py"],
      "result_path": "agent-result.json",
      "codex_home": "/work/mounts/codex-home",
      "config_home": "/work/mounts/xdg-config",
      "env": {
        "PI_CONFIG_HOME": "/work/mounts/pi-config",
        "PI_CODING_AGENT_DIR": "/work/mounts/pi-coding-agent",
        "PI_CODING_AGENT_SESSION_DIR": "/work/mounts/pi-session"
      }
    }
  }
}
```

Do not add token values to request JSON. `PI_TOKEN`, `OPENAI_API_KEY`, and other
secret variables are rejected by contract validation or redacted from all ledger
artifacts.
Ledger artifacts keep mount path evidence for `agent.codex_home` and
`agent.config_home` and `PI_CONFIG_HOME` while keeping secret-bearing values redacted.
`PI_CONFIG_HOME` is validated as an existing mount path before execution and
the sanitized mount evidence appears in `job-capsule.agent_mounts` as
`codex_home`, `config_home`, and `pi_config_home` paths.

Interim wrapper example for runner-local secret resolution:

```json
{
  "name": "implement",
  "input": {
    "guardrails": ["stay within the prepared checkout", "do not write secrets"],
    "validation": { "profile": "tier1", "commands": [] },
    "agent": {
      "type": "real-agent-command",
      "command": ["/runner/bin/pi-wrapper"],
      "result_path": "agent-result.json",
      "codex_home": "/work/mounts/codex-home",
      "config_home": "/work/mounts/xdg-config",
      "env": {
        "PI_CONFIG_HOME": "/work/mounts/pi-config",
        "PI_CODING_AGENT_DIR": "/work/mounts/pi-coding-agent",
        "PI_CODING_AGENT_SESSION_DIR": "/work/mounts/pi-session"
      },
      "wrapper_secret_files": {
        "primary": "/work/mounts/secrets/openai-api-key.txt",
        "secondary": "/work/mounts/secrets/pi-refresh-token.txt"
      }
    }
  }
}
```

The wrapper stays runner-local. AFK passes only mounted file paths in the job
capsule; the wrapper can read those files, export `OPENAI_API_KEY` or token files
at runtime, run the real CLI, and exit without echoing secrets. Direct
secret-bearing `agent.env` values such as `OPENAI_API_KEY=...` and flags like
`--token`, `--auth-file`, or `--api-key` remain rejected.

### Canonical Secret References

The canonical remote-runner secret reference shape now exists for recipe and
job-capsule metadata. AFK accepts references only; it does not resolve secret
values, call a provider, or persist secret material in ledger artifacts.

Recipe/job-capsule shape:

```json
{
  "agent": {
    "type": "real-agent-command",
    "command": ["/runner/bin/pi-wrapper"],
    "result_path": "agent-result.json",
    "codex_home": "/work/mounts/codex-home",
    "config_home": "/work/mounts/xdg-config",
    "env": {
      "PI_CONFIG_HOME": "/work/mounts/pi-config"
    },
    "secret_refs": {
      "primary": {
        "secretRef": {
          "provider": "runner-local-files",
          "name": "codex-auth",
          "key": "openai_api_key"
        }
      }
    }
  }
}
```

Contract rules:

- `agent.secret_refs` must be an object keyed by non-secret logical names such
  as `primary`, `secondary`, or `codex`.
- Each entry must be exactly `{ "secretRef": { "provider": "...", "name": "...",
  "key": "..." } }`.
- `secretRef.provider`, `secretRef.name`, and `secretRef.key` must be non-empty
  strings.
- `secretRef.provider`, `secretRef.name`, and `secretRef.key` must be reference
  identifiers, not token-shaped or otherwise secret-looking values.
- Plaintext fields such as `value`, `token`, `api_key`, or ad hoc inline secret
  payloads are rejected from this contract.
- AFK copies validated references into `job-capsule.json` under
  `capsule.agent_mounts.secret_refs` unchanged and does not add them to the
  adapter environment.

Minimal resolver-provider contract for a future runner integration:

Resolver input:

```json
{
  "provider": "runner-local-files",
  "name": "codex-auth",
  "key": "openai_api_key"
}
```

Resolver result:

```json
{
  "status": "resolved",
  "provider": "runner-local-files",
  "name": "codex-auth",
  "key": "openai_api_key",
  "materialization": {
    "kind": "file-path | env-var | opaque-handle",
    "locator": "<runner-local reference>"
  }
}
```

Failure shape:

```json
{
  "status": "error",
  "provider": "runner-local-files",
  "name": "codex-auth",
  "key": "openai_api_key",
  "code": "not_found | access_denied | invalid_reference | unavailable",
  "message": "<non-secret diagnostic>"
}
```

Resolver outputs are runner-internal. AFK should receive only the safe
reference metadata above or a later sanitized materialization contract, never a
raw token value in recipes, ledgers, PR bodies, or Beads comments.

Until resolver integration exists, the current runner auth inputs remain
path-only mounts:

- `agent.codex_home`
- `agent.config_home`
- `agent.env.PI_CONFIG_HOME`
- `publisher.gh.auth.config_dir`
- `agent.wrapper_secret_files`

Those fields continue to carry absolute runner-local paths only. They are not
aliases for `secret_refs`, and AFK still rejects direct secret-bearing
`agent.env` values and credential flags.

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
  workstreams/
    <workstream-run-id>/
      command.json
      pipeline-retrospective.json
      pr-body.md
      publication-result.json
      retrospective-follow-up-request.json
      retrospective-follow-up-result.json
      retrospective-follow-up-stderr.log
      retrospective-follow-up-stdout.log
      retrospective.json
      tracker-result.json
      workstream-result.json
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
`agent-result.json`. For `validate`, it contains normalized checkout,
validation, adapter, worker evidence, and derived `actionable_failures` plus
pointers to `worker-request.json` and `worker-result.json`. For `review`, it
contains the normalized final review status plus pointers to
`evidence-pack.json`, `reviewer-request.json`,
`reviewer-result.json`, and `review-summary.md`.

`run-workstream` records one workstream directory and one normal `runs/<run-id>/`
directory for each composed step. `workstream-result.json` lists every step run,
its ledger result path, the generated equivalent `afk run-step ...` command,
selected work result summaries, cleanup status, `retry_budget`,
`retry_attempts`, terminal reason, next allowed command, retry instructions,
terminal PR publication status, and tracker-close guidance. Dirty retry
checkouts are surfaced through `cleanup.resources` with path, branch, commit,
and status so failed retry attempts stay visible without spawning more sibling
checkouts.
`publication-result.json` records one of six explicit terminal states:
`blocked`, `validated-unpublished`, `failed-needs-human`, `published`,
`tracker-close-blocked`, or `tracker-closed`.
`tracker-result.json` records whether the source Beads item stays open, whether
it is ready to close, the PR URL when one was opened, any carried-forward review
findings, and the merge commit or explicit no-merge close reason when one is
recorded.
`pipeline-retrospective.json` records deterministic pipeline feedback for every
completed workstream run. It summarizes retrospective health, publication and
tracker status, derived signals, and recommended follow-up without changing the
functional publication or tracker outcome. `pipeline_retrospective.follow_up`
contains Beads-shaped `recommended` entries with `kind`, `summary`, `labels`,
and stable redacted `fingerprint` values, plus any `created` Beads and a
`creation` record describing recommendation-only mode or an optional creator
adapter run. The legacy `recommended_follow_up` list is preserved for
compatibility.
An optional top-level `retrospective_judge` recipe block can add a disabled-by-
default post-pass that runs a local command against a redacted evidence pack
built from the deterministic pipeline retrospective, tracker/publication
summary, selected work, cleanup state, and redacted terminal retrospective
evidence. Judge findings are recorded under `pipeline_retrospective.judge` and
may add retrospective signals, but they do not change the functional
publication or tracker status.
When `retrospective_follow_up.enabled` is true, AFK writes a redacted
`retrospective-follow-up-request.json`, runs the configured local or fake
command in a minimal environment, and records the normalized outcome in
`retrospective-follow-up-result.json` plus stdout/stderr logs. Command failures
are kept inside `pipeline_retrospective.follow_up.creation` and do not alter
the functional publication or tracker result.
`retrospective.json`, when present, stores the user-supplied terminal
retrospective evidence separately from the derived pipeline retrospective.
`review_cycles` evidence, when supplied, is included in both
`workstream-result.json` and `tracker-result.json`. Open or response-required
cycle findings keep the tracker state at `review-findings-open` until the
relevant review record carries addressed evidence. Once review cycle evidence is
present and all response-required findings are addressed, the tracker state
advances to `review-feedback-addressed` and still keeps the source item open
until merge or no-merge. AFK only treats a response object as addressed when
its `status` is `addressed` or `findings-addressed`; a non-empty response
string is the freeform addressed evidence path.
`retrospective` evidence, when supplied, is also included in both
`workstream-result.json` and `tracker-result.json`, while
`pipeline_retrospective` is always included in `workstream-result.json`. Use
`notes.personal_work` for concise daily work summaries kept under
`~/Documents/rmd/Ceremonies/Personal Work/work/YYYY-MM-DD-personal.md`. Use
`notes.spikes` for investigation or audit notes that should be preserved under
`~/Documents/rmd/Ceremonies/Personal Work/spikes/`. Do not record secrets in
either path or note content.
Step-level outputs may still use other status strings; specifically,
`prepare-checkout` uses `publication.status == "skipped_disabled"` when
the checkout publisher path is intentionally disabled.

Audit note (PR #17): searched downstream `run-workstream` terminal consumers for
publication status handling and found no dedicated mapping branch for legacy
terminal strings (`completed`, `failed_publication`, `needs_human`); unknown
publication states are mapped to `failed-needs-human` at the terminal boundary.
`prepare-checkout`'s `publication.status == "skipped_disabled"` remains a
step-level value and is intentionally not part of terminal-status branching in
workstream runs.
`validated-unpublished` means the current HEAD is terminally validated but AFK
did not publish a PR in that run; only the subset with a passed final review is
immediately eligible for PR publication on a follow-up rerun.
`published` means the PR exists but the source Beads item is still
`awaiting-review`; it stays open until the PR merges or
`tracker.terminal_decision.status == "no-merge"` is recorded.
`tracker-close-blocked` means a terminal merge/no-merge decision was recorded,
but unresolved review feedback still requires `review_feedback_status:
"resolved"` or `"waived"` before AFK can close the source item.
`tracker-closed` means a terminal merge or no-merge decision was recorded, so
AFK skipped publisher commands and emitted tracker close guidance instead.
`pr-body.md` is written before terminal PR commands run, so fake/offline
publisher tests can inspect the exact body.

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
local repo with a real submodule, `afk run-step validate` with a fake/local
Validation Worker, `afk run-step implement` with a fake/local Pi command, and
`afk run-step review` with a fake/local reviewer command when Docker or Podman
is available. If neither runtime exists, it exits
successfully with a clear skip message.

<!-- AFK_SUCCESS_DOGFOOD: central-afk-pr.16 --> Dogfood success marker for central-afk-pr.16 (docs-only, pipeline path).
