# Exit Codes

## Stability rules

Exit codes are part of the public command contract. Once assigned, a code must not be reused for another meaning. New meanings receive new numbers.

The model receives only the exit code and the corresponding short fixed message. It never receives raw logs, exception text, matched privacy content, subprocess output, or environment values. Full diagnostic logs remain in the local confined log file.

For privacy scan findings, output contains only the file path and line number. It never contains the matched text.

## Shared exit-code table

| Code | Name | Short fixed message | Meaning |
|---:|---|---|---|
| 0 | ok | `ok` | The command completed successfully. |
| 1 | internal_error | `internal error` | An unexpected internal failure occurred. |
| 2 | usage_error | `usage error` | Arguments, command syntax, or option combinations are invalid. |
| 3 | record_rejected | `record rejected` | A record has an unknown version, missing or unknown field, duplicate key, bad type, invalid value, or malformed JSON. |
| 4 | illegal_transition | `illegal lifecycle transition` | The requested lifecycle transition is not legal. |
| 5 | confinement_violation | `confinement violation` | A path escapes its root or encounters a symlink, hard link, FIFO, socket, or device file. |
| 6 | privacy_hit | `privacy scan hit` | A privacy rule matched; only the file path and line number may be reported. |
| 7 | input_limit | `input exceeds limit` | A record exceeds the byte-size or nesting-depth limit. |
| 8 | hook_failed_closed | `hook verification failed` | A hook could not verify its required state and failed closed. For the queue this includes an interrupted queue commit that cannot be proven or a broken event log; the operator procedure is in docs/queue-spec.md, Q-REC-02. |
| 9 | precondition_missing | `precondition missing` | A required record, file, directory, or prior state was not found. |
| 10 | human_approval_required | `human approval required` | The operation requires an explicit human approval that is absent. |
| 11 | tool_unavailable | `subprocess or tool unavailable` | An allowed subprocess or required local tool is unavailable or cannot start. |
| 12 | lease_refused | `lease refused` | Approved by the owner on 2026-11-27 (docs/queue-spec.md, OQ-1). A queue operation presented a missing, wrong, stale, expired or over-renewed lease. |

## Per-command exit codes

| Command | Code | Meaning for this command |
|---|---:|---|
| `task` | 0 | The task record was created or displayed. |
| `task` | 1 | An unexpected internal failure occurred. |
| `task` | 2 | Task arguments are invalid. |
| `task` | 3 | The task record was rejected. |
| `task` | 5 | The task path violated confinement. |
| `task` | 7 | Task input exceeded a size or depth limit. |
| `task` | 9 | A requested task record was not found. |
| `brief` | 0 | The brief record was created or displayed. |
| `brief` | 1 | An unexpected internal failure occurred. |
| `brief` | 2 | Brief arguments are invalid. |
| `brief` | 3 | The brief record was rejected. |
| `brief` | 5 | The brief path violated confinement. |
| `brief` | 7 | Brief input exceeded a size or depth limit. |
| `brief` | 9 | The referenced task or brief was not found. |
| `evidence` | 0 | Evidence metadata was recorded or displayed. |
| `evidence` | 1 | An unexpected internal failure occurred. |
| `evidence` | 2 | Evidence arguments are invalid. |
| `evidence` | 3 | Evidence metadata was rejected. |
| `evidence` | 5 | The evidence path or file type violated confinement. |
| `evidence` | 7 | Evidence metadata exceeded a size or depth limit. |
| `evidence` | 9 | The task, evidence file, or evidence record was not found. |
| `evidence` | 11 | A required allowlisted evidence tool was unavailable. |
| `review` | 0 | The review was recorded or displayed. |
| `review` | 1 | An unexpected internal failure occurred. |
| `review` | 2 | Review arguments are invalid. |
| `review` | 3 | The review record was rejected. |
| `review` | 4 | The requested transition into or out of review was illegal. |
| `review` | 5 | The review path violated confinement. |
| `review` | 7 | Review input exceeded a size or depth limit. |
| `review` | 9 | The task, report, evidence, or review record was not found. |
| `review` | 10 | The requested approval requires a human actor. |
| `decision` | 0 | The decision was recorded or displayed. |
| `decision` | 1 | An unexpected internal failure occurred. |
| `decision` | 2 | Decision arguments are invalid. |
| `decision` | 3 | The decision record was rejected. |
| `decision` | 5 | The decision path violated confinement. |
| `decision` | 7 | Decision input exceeded a size or depth limit. |
| `decision` | 9 | The task or referenced decision was not found. |
| `decision` | 10 | An accepted approval decision requires a human actor. |
| `close` | 0 | The approved task was closed. |
| `close` | 1 | An unexpected internal failure occurred. |
| `close` | 2 | Close arguments are invalid. |
| `close` | 3 | A task, report, review, or decision record was rejected. |
| `close` | 4 | The transition to closed was illegal. |
| `close` | 5 | A required path violated confinement. |
| `close` | 7 | A required record exceeded a size or depth limit. |
| `close` | 9 | A required task, report, evidence, review, or decision was not found. |
| `close` | 10 | Human approval is missing. |
| `check` | 0 | All checked records and confined files passed. |
| `check` | 1 | An unexpected internal failure occurred. |
| `check` | 2 | Check arguments are invalid. |
| `check` | 3 | At least one record was rejected. |
| `check` | 4 | At least one lifecycle history contains an illegal transition. |
| `check` | 5 | At least one path or file type violated confinement. |
| `check` | 7 | At least one record exceeded a size or depth limit, or `on_exceed = "fail"` limits in `config.toml` were exceeded. |
| `check` | 9 | A requested check target was not found. |
| `doctor` | 0 | All local readiness checks passed. |
| `doctor` | 1 | An unexpected internal failure occurred. |
| `doctor` | 2 | Doctor arguments are invalid. |
| `doctor` | 5 | The configured root or log path violated confinement. |
| `doctor` | 8 | Hook state could not be verified. |
| `doctor` | 9 | A required local file or directory was not found. |
| `doctor` | 11 | A required allowlisted subprocess or tool was unavailable. |
| `privacy-scan` | 0 | No privacy rule matched. |
| `privacy-scan` | 1 | An unexpected internal failure occurred. |
| `privacy-scan` | 2 | Privacy-scan arguments are invalid. |
| `privacy-scan` | 5 | A scanned path or file type violated confinement. |
| `privacy-scan` | 6 | A rule matched; report only the file path and line number. |
| `privacy-scan` | 7 | Scan input exceeded an applicable size or depth limit. |
| `privacy-scan` | 9 | A requested scan target was not found. |
| `privacy-scan` | 11 | A required allowlisted scanning tool was unavailable. |
| `hooks` | 0 | Hook state was verified or the requested local hook operation succeeded. |
| `hooks` | 1 | An unexpected internal failure occurred. |
| `hooks` | 2 | Hook arguments are invalid. |
| `hooks` | 3 | Hook configuration data was rejected. |
| `hooks` | 5 | A hook path or file type violated confinement. |
| `hooks` | 7 | Hook configuration exceeded a size or depth limit. |
| `hooks` | 8 | Hook state could not be verified, so the hook failed closed. |
| `hooks` | 9 | A required hook file or configuration was not found. |
| `hooks` | 10 | The requested hook operation requires human approval. |
| `hooks` | 11 | A required allowlisted hook subprocess or tool was unavailable. |
