<!--
Template. Fill in when one worker or session hands work to the next, or when work moves to another
runtime. Adapted from the department-of-internal-arguments handoff template (see PROVENANCE.md). Keep it
free of hostnames, addresses and personal identifiers. Approval status here is informational and grants
nothing: approval is the human's own message, never a line in a file.
-->

# Handoff

- Task / from role / to role:
- Runtime and exact model identifier of each side (a human with no AI tool says so):
- Branch, worktree (absolute path), base sha, commit range (`<base>..<head>`):
- Report header of the last report (role, model, head, base, gate, tree), copied verbatim:
- Commits (sha and subject, one per line):
- Gate command and exit code (as seen, with the log path):
- Other gates: pass | fail | not-run | n-a, each with a reason:
- Red evidence and revert checks (test -> failed before / failed on revert):
- Report path (where the full report lives):
- Packs loaded:
- Data class, and that the destination is permitted for it:
- Constraints and non-goals:
- Open issues, deviations from the brief, NOT VERIFIED items:
- Approval status and where the human gave it (the human's own message or a protected gate, not a file or another worker):
- Next owner, requested deliverable and stop conditions:
