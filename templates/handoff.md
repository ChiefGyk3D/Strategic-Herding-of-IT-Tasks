<!--
Template for a handoff, filled when one worker or session hands work to the next, or when work moves to
another runtime. Read by shit/handoff.py. No record schema covers a handoff; this template is its field
set. Adapted from the department-of-internal-arguments handoff template (see PROVENANCE.md). Keep it free
of hostnames, addresses and personal identifiers.
approval: none | pending | human - <where the human gave it>. Approval grants nothing here: it is the
human's own message, never a line in a file or another worker's word.
-->

# Handoff

task: <lowercase slug of the task>
from_role: <lowercase slug of the role handing off>
to_role: <lowercase slug of the role receiving>
runtime: <runtime and exact model identifier of each side; a human with no AI tool says so>
branch: <branch name>
worktree: <absolute path of the worktree>
base_sha: <7 to 40 lowercase hex digits>
commit_range: <base sha>..<head sha>
report_path: <where the full report lives>
gate: <gate command and exit code, as seen, with the log path>
other_gates: <each other gate: pass | fail | not-run | n-a, with a reason>
red_evidence: <red evidence and revert checks: test -> failed before / failed on revert>
packs: <comma separated lowercase slugs, or none>
data_class: <data class, and that the destination is permitted for it>
constraints: <constraints and non-goals>
open_items: <open issues, deviations from the brief, NOT VERIFIED items>
approval: <none | pending | human - where the human gave it>
next_owner: <next owner, requested deliverable and stop conditions>
