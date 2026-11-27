# Record Schemas

## Common rules

All record readers and writers must apply these rules:

- Each record is encoded as UTF-8 JSON.
- A byte order mark is rejected.
- Each record is one JSON object. The `event-log` format is JSON Lines, with exactly one JSON object per nonempty line.
- Every record carries `schema`, whose value is the record type name, and `schema_version`, whose integer value starts at `1`.
- A reader must reject an unknown `schema_version` with exit code 3. It must not guess, coerce, or fall back to another version.
- Keys use lowercase snake_case.
- Duplicate keys in any JSON object are rejected. Last-wins parsing is prohibited.
- Unknown extra keys are rejected in version 1, including keys in nested objects.
- A single record, or a single JSON Lines object, must not exceed 65,536 bytes after UTF-8 encoding.
- Maximum JSON nesting depth is 8. The top-level object is depth 1.
- Size or depth violations return exit code 7.
- Timestamps use RFC 3339 UTC form with a trailing `Z`, such as `2026-11-26T15:30:00Z`. Offsets and fractional seconds are not accepted in version 1.
- Record identifiers and referenced record identifiers are lowercase slugs matching `^[a-z0-9][a-z0-9-]{0,63}$`.
- Strings must contain valid Unicode and must not contain unescaped control characters.
- Required strings must not be empty unless a field explicitly says otherwise.
- Actors are `human`, `wrapper`, or `model`. Only `human` may enter the `approved` lifecycle state.
- Lifecycle states are `open`, `running`, `review`, `approved`, `closed`, `blocked`, and `failed`.

## Nested transition object

A transition object may appear in a task's `history` array.

| Field | Type | Requirement | Allowed values or rules |
|---|---|---|---|
| `from` | string | required | Any lifecycle state |
| `to` | string | required | Any lifecycle state |
| `actor` | string | required | `human`, `wrapper`, or `model` |
| `at` | string | required | RFC 3339 UTC timestamp with trailing `Z` |
| `reason` | string | required | Nonempty plain-text explanation |

The transition must also be legal under `docs/lifecycle.md`.

## Task record

### Purpose

A task records the durable identity, current lifecycle state, summary, and transition history of one unit of work.

### File format

One JSON object per task file.

| Field | Type | Requirement | Allowed values or rules |
|---|---|---|---|
| `schema` | string | required | Exactly `task` |
| `schema_version` | integer | required | Exactly `1` |
| `id` | string | required | Lowercase slug |
| `title` | string | required | Nonempty, maximum 200 characters |
| `summary` | string | required | Nonempty, maximum 4,000 characters |
| `state` | string | required | `open`, `running`, `review`, `approved`, `closed`, `blocked`, or `failed` |
| `owner` | string | required | `human`, `wrapper`, or `model` |
| `created_at` | string | required | RFC 3339 UTC timestamp |
| `updated_at` | string | required | RFC 3339 UTC timestamp, not earlier than `created_at` |
| `blocked_reason` | string | optional | Required and nonempty when `state` is `blocked`; otherwise prohibited |
| `blocking_item` | string | optional | Required and nonempty when `state` is `blocked`; otherwise prohibited |
| `history` | array of transition objects | required | Chronological; each entry must be legal; the final `to` must equal `state` when nonempty |

### Complete example

```json
{
  "schema": "task",
  "schema_version": 1,
  "id": "schema-design",
  "title": "Define wrapper contracts",
  "summary": "Define versioned records and lifecycle behavior for the wrapper.",
  "state": "open",
  "owner": "human",
  "created_at": "2026-11-26T15:30:00Z",
  "updated_at": "2026-11-26T15:30:00Z",
  "history": []
}
```

## Brief record

### Purpose

A brief records the bounded objective, scope, constraints, and cleanup duty supplied before work begins.

### File format

One JSON object per brief file.

| Field | Type | Requirement | Allowed values or rules |
|---|---|---|---|
| `schema` | string | required | Exactly `brief` |
| `schema_version` | integer | required | Exactly `1` |
| `id` | string | required | Lowercase slug |
| `task_id` | string | required | Lowercase slug identifying a task |
| `created_at` | string | required | RFC 3339 UTC timestamp |
| `actor` | string | required | `human`, `wrapper`, or `model` |
| `objective` | string | required | Nonempty, maximum 4,000 characters |
| `scope` | array of strings | required | At least one nonempty item; each item maximum 500 characters |
| `constraints` | array of strings | required | May be empty; each item maximum 500 characters |
| `cleanup_duty` | string | required | Nonempty, maximum 1,000 characters |
| `local_llm` | string | required | `used` or `not_applicable` |

### Complete example

```json
{
  "schema": "brief",
  "schema_version": 1,
  "id": "schema-design-brief",
  "task_id": "schema-design",
  "created_at": "2026-11-26T15:31:00Z",
  "actor": "human",
  "objective": "Specify stable version 1 record contracts.",
  "scope": [
    "Document task, brief, report, decision, and event-log records."
  ],
  "constraints": [
    "Design only.",
    "Use the Python standard library for later implementation."
  ],
  "cleanup_duty": "Remove only temporary artifacts created for this task.",
  "local_llm": "used"
}
```

## Report record

### Purpose

A report records an outcome communicated by a human, model, or wrapper without embedding raw tool logs.

### File format

One JSON object per report file.

| Field | Type | Requirement | Allowed values or rules |
|---|---|---|---|
| `schema` | string | required | Exactly `report` |
| `schema_version` | integer | required | Exactly `1` |
| `id` | string | required | Lowercase slug |
| `task_id` | string | required | Lowercase slug identifying a task |
| `created_at` | string | required | RFC 3339 UTC timestamp |
| `actor` | string | required | `human`, `wrapper`, or `model` |
| `outcome` | string | required | `informational`, `success`, `blocked`, or `failed` |
| `summary` | string | required | Nonempty, maximum 4,000 characters |
| `evidence_ids` | array of strings | required | May be empty; each value is a lowercase slug |
| `log_reference` | string | optional | Lowercase slug naming a confined local log; never raw log content |

### Complete example

```json
{
  "schema": "report",
  "schema_version": 1,
  "id": "schema-design-report",
  "task_id": "schema-design",
  "created_at": "2026-11-26T16:00:00Z",
  "actor": "model",
  "outcome": "success",
  "summary": "The version 1 design documents and fixtures were drafted.",
  "evidence_ids": [
    "schema-fixture-check"
  ]
}
```

## Decision record

### Purpose

A decision records a question, the selected choice, its status, and the rationale. It does not itself perform a lifecycle transition.

### File format

One JSON object per decision file.

| Field | Type | Requirement | Allowed values or rules |
|---|---|---|---|
| `schema` | string | required | Exactly `decision` |
| `schema_version` | integer | required | Exactly `1` |
| `id` | string | required | Lowercase slug |
| `task_id` | string | required | Lowercase slug identifying a task |
| `created_at` | string | required | RFC 3339 UTC timestamp |
| `actor` | string | required | `human`, `wrapper`, or `model` |
| `status` | string | required | `proposed`, `accepted`, `rejected`, or `superseded` |
| `question` | string | required | Nonempty, maximum 2,000 characters |
| `choice` | string | required | Nonempty, maximum 2,000 characters |
| `rationale` | string | required | Nonempty, maximum 4,000 characters |
| `supersedes_id` | string | optional | Lowercase slug; allowed only when `status` is `superseded` |

An `accepted` decision that grants approval must have `actor` equal to `human`. A model or wrapper may record a proposal, but cannot create human approval.

### Complete example

```json
{
  "schema": "decision",
  "schema_version": 1,
  "id": "schema-versioning-decision",
  "task_id": "schema-design",
  "created_at": "2026-11-26T15:40:00Z",
  "actor": "human",
  "status": "accepted",
  "question": "How should readers handle unknown schema versions?",
  "choice": "Reject the record with exit code 3.",
  "rationale": "Failing explicitly prevents readers from guessing incompatible semantics."
}
```

## Evidence record

An evidence record names one regular file under the repository and pins it by digest. Common fields are as in the other records (`schema` `evidence`, `schema_version` 1, `id`, `task_id`, `created_at`, `actor`). Added fields, all required: `description` (nonempty, at most 1,000 characters), `path` (repository-relative, at most 500 characters, no empty, `.` or `..` component, never under `.shit`), `sha256` (64 lowercase hex digits) and `size` (integer, bytes). The file must be a regular, single-link file reached without a symlink and at most 16 MiB. `close` re-reads the file and rejects the task with exit code 3 if the digest or size changed.

## Review record

A review records a verdict on one report. Common fields as above (`schema` `review`), plus `report_id` (slug of a report of the same task), `verdict` (`pass`, `changes` or `fail`) and `summary` (nonempty, at most 4,000 characters). A review is accepted only while the task is in `review`. A review is not an approval: only an accepted decision by a human, followed by the human transition into `approved`, grants approval.

## Recording and closing rules

Report, evidence, review and decision files live under `.shit/reports`, `.shit/evidence`, `.shit/reviews` and `.shit/decisions`. Each is recorded by an event whose `message` carries the sha256 of the file bytes (`<Kind> record added; sha256 <hex>.`). A reader trusts a file only when that event exists and the digest matches, so an edited, dropped or crash-orphaned file is never treated as recorded. Every string in these records is refused if it holds a control, bidirectional, zero-width or separator character. A decision with status `accepted` recorded by a non-human actor is refused with exit code 10. `close` requires, in one locked step: a task in `approved`, a report with outcome `success`, a latest review with verdict `pass` recorded by a human actor on a successful report (a later model or wrapper review never clears an earlier human non-pass; only a newer human pass does), that report's evidence unchanged, and a latest effective decision that is `accepted` by a human (a decision named by a later `supersedes_id` is not effective). A missing record is exit code 9; a missing human decision is exit code 10.

## Event-log record

### Purpose

An event-log record provides an append-only, chronological account of significant wrapper activity. It contains summaries and references, not raw subprocess logs.

### File format

JSON Lines. Each nonempty line contains one complete JSON object. Blank lines are rejected.

| Field | Type | Requirement | Allowed values or rules |
|---|---|---|---|
| `schema` | string | required | Exactly `event-log` |
| `schema_version` | integer | required | Exactly `1` |
| `id` | string | required | Lowercase slug |
| `task_id` | string | required | Lowercase slug identifying a task |
| `timestamp` | string | required | RFC 3339 UTC timestamp |
| `actor` | string | required | `human`, `wrapper`, or `model` |
| `event` | string | required | `task_created`, `state_transition`, `brief_recorded`, `report_recorded`, `decision_recorded`, `evidence_recorded`, `review_recorded`, or `command_failed` |
| `message` | string | required | Short fixed or sanitized summary, maximum 500 characters |
| `record_id` | string | optional | Lowercase slug identifying the related record |
| `from_state` | string | optional | Any lifecycle state; required only for `state_transition` |
| `to_state` | string | optional | Any lifecycle state; required only for `state_transition` |
| `exit_code` | integer | optional | One stable code from `docs/exit-codes.md`; required only for `command_failed` |

For `state_transition`, `from_state` and `to_state` are required and must form a legal transition. For all other event values, those fields are prohibited. For `command_failed`, `exit_code` is required and nonzero. For all other event values, `exit_code` is prohibited.

### Complete example

```jsonl
{"schema":"event-log","schema_version":1,"id":"event-0001","task_id":"schema-design","timestamp":"2026-11-26T15:30:00Z","actor":"human","event":"task_created","message":"Task record created.","record_id":"schema-design"}
{"schema":"event-log","schema_version":1,"id":"event-0002","task_id":"schema-design","timestamp":"2026-11-26T15:31:00Z","actor":"human","event":"brief_recorded","message":"Brief record added.","record_id":"schema-design-brief"}
```

## Schema version changes

A schema change that alters fields, types, allowed values, validation, or semantics requires a new integer `schema_version`. The version number is incremented rather than editing version 1 behavior in place.

Each new version must include:

1. A migration note describing compatibility, field transformations, information loss, and rollback limits.
2. At least one new valid fixture for that version.
3. Invalid fixtures for new rejection behavior.
4. Updated examples and validation tests.
5. An explicit reader policy for every supported older version.

Readers must continue to reject versions they do not implement with exit code 3. Migration is an explicit operation and must never happen implicitly while reading a record.
