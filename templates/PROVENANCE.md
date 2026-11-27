# Template provenance

These templates are adapted from generic content in the department-of-internal-arguments repository
(https://github.com/ChiefGyk3D/department-of-internal-arguments), read from a local checkout at commit
`9a5826bc011f5d476d9f92b8fbf571b9ab30413c`.

| File here | Source in that repository at that commit | Changes |
|---|---|---|
| `brief.md` | `templates/brief.md`, `docs/writing-a-brief.md` | Fields renamed to match `docs/schemas.md` (objective, scope, constraints, cleanup_duty) plus issue, packs, acceptance, data_class, evidence, review_destination; `model` optional; role is any lowercase slug; vendor-specific lines removed; read by `shit/brief.py`. |
| `evidence-header.md` | `fragments/report-header.md` (the report header) | Header kept field for field; MODEL and LOCAL-LLM may say `n/a` so a person with no AI tool can fill it; read by `shit/evidence.py`. |
| `handoff.md` | `templates/handoff.md` | Wording made tool-neutral; fields unchanged. |
| `decision.md` | `templates/adr.md` | Fields aligned to the decision record in `docs/schemas.md`. |

Not imported: `config.toml`. The source repository has no generic config template at that commit, so none
is invented here; a configuration file belongs with the first command that reads one.

To refresh a template, read the source file at a newer commit, update this table with the new commit, and
re-run `python3 -m unittest discover -s tests -v`. Any update is a reviewed diff, never an automatic copy.
