ROLE: <role name>   MODEL: <the exact model id used, or n/a - done by hand>
HEAD: <short sha>   BASE: <short sha>
GATE: <command>  exit=<code> | not-run - reason | n-a - reason
OTHER GATES: <name>: pass | fail | not-run - reason | n-a - reason (one line per gate, or none)
PACKS LOADED: <comma-separated names, or none>
TREE: clean (<evidence>) | NOT CLEAN (<what>) | n-a - reason
NOT VERIFIED: <what you did not run or could not check (one line each, or none)>
LOCAL-LLM: used - <exact model tag and size>; VERIFIED-AGAINST: <source>; DISCREPANCIES: <n> | n/a - reason

Template for the header of a work report. Copy it to a report file and replace every <placeholder>. The
block above is the header; it ends at the first blank line and is checked by `shit.evidence.read_header`.
Everything after the blank line is free text and is not read by the validator. Keep GATE's command and
`exit=` apart by two or more spaces, and keep ROLE/MODEL and HEAD/BASE on one line each, also apart by
two or more spaces. A person may fill it by hand: ROLE may be `human`, and MODEL and LOCAL-LLM may say
`n/a - <reason>`. Keep the whole report free of hostnames, addresses, serials, callsigns, employer names
and secrets, and paste no raw logs: name the log by path instead.

Body, results first, in this order: commits (sha and subject, one per line), red evidence (test ->
failed before, failed on revert), changed files, deviations from the brief (`none` is a claim; check
first), escalations (anything that tempted you to weaken a check).
