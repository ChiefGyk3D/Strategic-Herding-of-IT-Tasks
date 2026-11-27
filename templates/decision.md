<!--
Template for a decision record. Its fields are the decision record in docs/schemas.md, one line each, so
a filled copy validates with shit/decision.py and can be written as the record. Adapted from the
department-of-internal-arguments decision template (see PROVENANCE.md). Keep it free of hostnames,
addresses and personal identifiers. Add supersedes_id only when status is superseded. A model or wrapper
may record a proposed decision; only a human may record an accepted one.
-->

# Decision record

schema: decision
schema_version: 1
id: <lowercase slug, for example schema-versioning-decision>
task_id: <lowercase slug of the task this belongs to>
created_at: <RFC 3339 UTC, for example 2026-11-26T15:30:00Z>
actor: <human | wrapper | model>
status: <proposed | accepted | rejected | superseded>
question: <one line, at most 2,000 characters>
choice: <one line, at most 2,000 characters>
rationale: <one line, at most 4,000 characters>
