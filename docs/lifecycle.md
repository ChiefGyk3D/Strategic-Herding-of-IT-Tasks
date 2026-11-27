# Task Lifecycle

## States

- `open`: The task is ready for work but no subprocess is running.
- `running`: The wrapper has started a subprocess for the task.
- `review`: Work has been reported and awaits review.
- `approved`: A human has approved the reviewed work.
- `closed`: The task is complete and terminal.
- `blocked`: Work cannot continue until a stated blocking item is resolved.
- `failed`: Work stopped because an attempted operation failed.

## Transition rules

- Only a human may enter `approved`.
- `closed` is terminal. No transition out of `closed`, including `closed` to `closed`, is legal.
- `failed` may return to `open` only through a human transition.
- A task entering `blocked` must record both `blocked_reason` and `blocking_item`.
- `running` is entered only by the wrapper when it starts a subprocess.
- The actor recorded in history must be the actor that performed the transition.
- Repeating the current state is not a lifecycle transition and is illegal.
- An illegal transition returns exit code 4 and does not modify the task or event log.

## Complete transition table

| From | To | Verdict | Actor | Reason |
|---|---|---|---|---|
| open | open | illegal | none | Repeating the current state is not a transition. |
| open | running | legal | wrapper | The wrapper enters running when it starts a subprocess. |
| open | review | illegal | none | Work must run before it can enter review. |
| open | approved | illegal | none | Approval requires reviewed work. |
| open | closed | illegal | none | Open work must be run, reviewed, and approved first. |
| open | blocked | legal | human or wrapper | A known blocker may prevent work from starting. |
| open | failed | legal | human or wrapper | A pre-work operation may fail before execution begins. |
| running | open | illegal | none | Running work must reach review, blocked, or failed. |
| running | running | illegal | none | Repeating the current state is not a transition. |
| running | review | legal | wrapper | The wrapper submits completed subprocess work for review. |
| running | approved | illegal | none | Running work cannot bypass review. |
| running | closed | illegal | none | Running work cannot bypass review and approval. |
| running | blocked | legal | wrapper | The wrapper may stop running work when a blocking item is identified. |
| running | failed | legal | wrapper | The wrapper records a failed subprocess or operation. |
| review | open | legal | human | A human may request changes and reopen the task. |
| review | running | illegal | none | Changes must first move the task to open. |
| review | review | illegal | none | Repeating the current state is not a transition. |
| review | approved | legal | human | Only a human may approve reviewed work. |
| review | closed | illegal | none | Reviewed work must receive human approval before closure. |
| review | blocked | legal | human or wrapper | Review may identify a blocking item that prevents a decision. |
| review | failed | legal | human or wrapper | Review or its supporting verification may fail. |
| approved | open | illegal | none | Approved work is not reopened without a new task. |
| approved | running | illegal | none | Approved work cannot resume execution in the same lifecycle. |
| approved | review | illegal | none | Approved work has completed review. |
| approved | approved | illegal | none | Repeating the current state is not a transition. |
| approved | closed | legal | human or wrapper | Approved work may be finalized and closed. |
| approved | blocked | illegal | none | Approval resolves the review path and cannot become blocked. |
| approved | failed | illegal | none | Failures after approval require a new task rather than rewriting approval. |
| closed | open | illegal | none | Closed is terminal. |
| closed | running | illegal | none | Closed is terminal. |
| closed | review | illegal | none | Closed is terminal. |
| closed | approved | illegal | none | Closed is terminal. |
| closed | closed | illegal | none | Closed is terminal and cannot transition to itself. |
| closed | blocked | illegal | none | Closed is terminal. |
| closed | failed | illegal | none | Closed is terminal. |
| blocked | open | legal | human | A human confirms the blocker is resolved and reopens the task. |
| blocked | running | legal | wrapper | The wrapper may resume by starting a subprocess after the blocker is resolved. |
| blocked | review | illegal | none | Blocked work must resume or reopen before review. |
| blocked | approved | illegal | none | Blocked work cannot be approved. |
| blocked | closed | illegal | none | Blocked work cannot be closed. |
| blocked | blocked | illegal | none | Repeating the current state is not a transition. |
| blocked | failed | legal | human or wrapper | A blocker may become a terminal failure for the attempt. |
| failed | open | legal | human | Only a human may reopen a failed task for another attempt. |
| failed | running | illegal | none | A failed task must be reopened by a human before execution. |
| failed | review | illegal | none | Failed work is not ready for review. |
| failed | approved | illegal | none | Failed work cannot be approved. |
| failed | closed | illegal | none | A failed task must be reopened rather than closed as successful work. |
| failed | blocked | illegal | none | A failed task must be reopened before another condition is recorded. |
| failed | failed | illegal | none | Repeating the current state is not a transition. |

## Proposed amendment: bounded wrapper-only retry (draft, not in force)

**Status:** draft for review. Nothing in this section changes the table above, and no code implements it. The owner decided on 2026-11-27 that a bounded, wrapper-only automatic retry is allowed (docs/queue-spec.md, OQ-3). A human must review this text before it replaces any rule above.

Today `running` to `open` is illegal and `failed` to `open` is human-only, so a failed attempt always waits for a person. The amendment would add one legal transition and one counter:

1. New row: `running` to `open`, actor `wrapper`, allowed only when the attempt failed for a reason the wrapper classifies as retryable (a lease that expired, or a launch failure that is not a work failure). The history reason must be one of a small fixed set of strings.
2. A retry counter per task, kept in the queue log, not in the task record. After `max_auto_retries` (a configured integer, suggested default 1, allowed range 0 to 3; 0 turns the amendment off) consecutive wrapper retries the next failure moves the task to `failed` and only a human may reopen it.
3. A human reopen, an accepted human decision, or a successful report resets the counter.
4. The row `failed` to `open` stays human-only. A wrapper retry never passes through `failed`.
5. Every retry is an event in the log and a queue entry, so it is visible and counted by the metrics.
6. The threat model needs a new row for retry loops: an attacker or a flaky tool burning attempts. The control is the counter bound and the fixed reason set; the test must go red if the bound is removed.

Open points for the reviewer: whether lease expiry on a running task is always safe to retry (the worker may have changed files outside the store), and whether `max_auto_retries` should be per role. Issue 17 leaves `expired` on a running lease ending in `failed` (Q-LEASE-06, Q-RET-02) until this text is approved.
