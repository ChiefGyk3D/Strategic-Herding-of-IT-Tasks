# Yardstick report

`python3 -m shit.yardstick [--config config.toml]` prints a JSON report computed from this project's verified SQLite events and indexed report bodies. It never reads figures from any other project.

- Baseline first. Task counts by state, event counts by kind, report outcomes and review verdicts (each group carries a `label` key) are counted from one read-only store snapshot and labeled `measured`.
- `effort_points` is each event count times a weight, summed, and labeled `estimate`. Weights come from the optional `[yardstick]` table in the config file (one integer from 0 to 1000 per event kind, default 1). The config path must lie inside the project root.
- `tokens` is `{"value": null, "label": "not supplied"}`. A token number appears only if an adapter supplies one; no adapter does yet.
- `per_task` lists at most 100 tasks; `per_task_truncated` says when more exist. Totals always cover every task.
- A store whose event chain, task invariants, or indexed bodies do not verify is refused with exit code 8; malformed indexed record bodies retain their documented record error. Errors print one fixed line and never echo input.
