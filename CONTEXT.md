# AFK Orchestration

AFK coordinates one tracked unit of work through implementation, verification, integration, and closure while preserving enough evidence to explain every outcome.

## Language

**Run**:
One attempt to carry a single Bead from claim through a terminal outcome.
_Avoid_: Workstream, pipeline job

**Run State**:
A durable checkpoint describing a fact the Run has already achieved.
_Avoid_: Step, current activity, in-progress action

**Action Attempt**:
Evidence that the Run started an operation which may succeed, fail, or be interrupted without advancing the Run State.
_Avoid_: State, step

**Completed**:
The terminal Run State indicating that the vetted change was merged, its Bead was closed, and a retrospective attempt was recorded.
_Avoid_: Passed, published, done

**Attention Required**:
The autonomously terminal Run State indicating that AFK cannot safely continue without a person; it retains the last achieved checkpoint and may leave only through an explicit, reconciled human resume.
_Avoid_: Failed, blocked, stopped

**Failure Classification**:
A stable category explaining why a Run requires attention without becoming a separate terminal state.
_Avoid_: Failure state, blocked state

**Created**:
The first Run State, meaning AFK has durably recorded the requested Bead but has not yet proven ownership of it.
_Avoid_: Queued, selected

**Claimed**:
The Run State meaning AFK has exclusive tracker ownership of the open Bead and may begin repository work.
_Avoid_: Selected, assigned

**Change Committed**:
The Run State meaning the dedicated Run branch contains at least one new commit beyond its pinned base and its worktree is clean.
_Avoid_: Implemented, agent completed

**Candidate**:
The exact clean commit currently pushed as the Run's open draft pull-request head and evaluated as one indivisible unit by validation and review.
_Avoid_: Change, branch, latest code

**Gate Cycle**:
The validation and review evidence associated with one Candidate.
_Avoid_: Retry, test run, review pass

**Validated**:
The Run State meaning the current Candidate passed its repository-owned Validation Contract.
_Avoid_: Tests passed, worker passed

**Reviewed**:
The Run State meaning the current validated Candidate passed both the Standards and Spec review axes.
_Avoid_: Approved, review complete

**Validation Contract**:
The repository-owned boundary through which AFK asks whether an exact Candidate satisfies that repository's deterministic verification requirements.
_Avoid_: Test command, worker adapter, validation profile

**Repair Brief**:
The immutable, Candidate-bound evidence AFK gives a Repair Attempt to explain every validation or review rejection it must address.
_Avoid_: Repair prompt, feedback list, retry context

**Repair Attempt**:
An Action Attempt bound to a rejected Candidate and its Repair Brief that consumes one of the Run's four repair slots while trying to produce a successor Candidate.
_Avoid_: Retry, Gate Cycle, new Run

**Event History**:
The authoritative ordered record of facts and Action Attempt boundaries durably observed during one Run.
_Avoid_: Log file, audit output, state cache

**Active Run**:
The single Run that still owns AFK-managed work or external state, including a Run awaiting explicit human resume from Attention Required.
_Avoid_: Running process, nonterminal Run, current job

**Effect Record**:
The durable intent and observed confirmation for one AFK-owned mutation of an external system.
_Avoid_: Transaction, command result, side-effect log

**Process Finding**:
An evidence-backed observation about how a Run's process helped or hindered its outcome.
_Avoid_: Signal, code defect, follow-up item

**Improvement Proposal**:
An advisory change that addresses one or more Process Findings and awaits explicit human disposition.
_Avoid_: Follow-up, automatic task, required fix
