#!/usr/bin/env python3
import json
import os
import signal
import sys
from pathlib import Path


args = sys.argv[1:]
fixture_root = Path(__file__).resolve().parent
if "--skip-git-repo-check" not in args:
    delegate = fixture_root / "codex-candidate-review"
    os.execv(delegate, [str(delegate), *args])

prompt = sys.stdin.read()
control = fixture_root / ".fake-retrospective-mode"
mode = control.read_text(encoding="utf-8").strip() if control.exists() else "empty"
observer_path = fixture_root / ".fake-retrospective-observer.json"
if observer_path.exists():
    observer = json.loads(observer_path.read_text(encoding="utf-8"))
    with Path(observer["invocation_log"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"mode": mode}, sort_keys=True) + "\n")

if mode == "unavailable":
    raise SystemExit(7)
if mode == "interrupted":
    os.kill(os.getpid(), signal.SIGKILL)
if mode == "invalid":
    print("{not-json")
    raise SystemExit(0)

summary = json.loads(prompt)
result = {
    "schema_version": 1,
    "run_id": summary["run"]["run_id"],
    "terminal_outcome": summary["episode"]["state"],
    "summary": "No actionable findings.",
    "process_findings": [],
    "improvement_proposals": [],
}
if mode == "populated":
    result["summary"] = (
        "RAW_RETROSPECTIVE_ANALYSIS_SENTINEL "
        "token=sensitive-retrospective-token " + ("x" * 512)
    )[:512]
    result["process_findings"] = [
        {
            "id": "finding-1",
            "category": "orchestration",
            "title": "The Run required attention",
            "evidence": [
                {
                    "artifact": "episode-checkpoint.txt",
                    "line_start": 1,
                    "line_end": 1,
                }
            ],
            "impact": "The Run stopped for an operator.",
            "confidence": "high",
        }
    ]
    result["improvement_proposals"] = [
        {
            "id": "proposal-1",
            "addresses": ["finding-1"],
            "scope": "afk",
            "priority": "P1",
            "title": "Reduce attention interruptions",
            "rationale": "Fewer stops shorten the Run.",
            "suggested_change": "Improve interruption recovery.",
            "requires_human_decision": True,
        }
    ]
print(json.dumps(result))
