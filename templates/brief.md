<!--
Template. Copy to a scratch file, replace every <placeholder>, and check it with
`python3 -c "from shit import brief; print(brief.read('FILE'))"`. A person can fill it by hand; no AI
tool is needed. Adapted from the department-of-internal-arguments brief template (see PROVENANCE.md).
Keep it free of hostnames, addresses, serials, callsigns and personal identifiers.

Format: one `key: value` line per field, key in lowercase at the start of the line. scope, acceptance and
constraints repeat the key, one item per line. Headings, bullets, blank lines, indented lines, code
fences and comments are ignored. A `key: value` line with an unknown key is rejected.
-->

# Work brief

issue: <owner/repo#N>
role: <one role name, for example implementer, reviewer, researcher, planner, or human>
model: <the exact model id used, or n/a - done by hand; this line may be left out>
packs: <comma-separated pack names, or none>
objective: <the outcome and who uses it, in one line>
scope: <one permitted file, directory or area per line; repeat the line>
constraints: <one non-goal or rule per line; repeat the line, or leave the line out>
acceptance: <one observable acceptance check per line; repeat the line>
data_class: <public | internal | restricted, and what must not appear: hostnames, addresses, serials, callsigns, employer, secrets>
cleanup_duty: <what the worker removes when done, by exact path, and what it keeps>
evidence: <what the report must carry: gate exit code, red evidence, revert check, other gates>
review_destination: <who or what reviews the result, and where the report goes>

## Notes (ignored by the validator)

- Base commit, worktree (absolute path) and permitted files.
- Fix round number and prior findings: round 1 (none), or round N addressing findings <numbers>. A third round on the same finding class is not dispatched; the lead escalates instead.
- Gate command: the full gate, exactly as CI runs it.
- Stop conditions: ambiguous scope, a check that would have to be weakened, security-sensitive code the plan did not review. The worker returns its report instead of guessing.
- Required approvals: the worker never pushes, merges, tags or releases. The human's approval is recorded in the human's own message, not in this file.
- Local model step for text-only drafts, or `n/a - <reason>`.
- Spend: `none - free-only`, or `paid - approval in the human message at <time>, cap <n> USD`.
