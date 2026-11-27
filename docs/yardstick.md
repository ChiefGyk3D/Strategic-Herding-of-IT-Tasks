# Yardstick report

`python3 -m shit.yardstick [--config config.toml]` prints a JSON report computed from this project's own verified task log and recorded reports. It never reads figures from any other project.

- Baseline first. Task counts by state, event counts by kind, report outcomes and review verdicts (each group carries a `label` key) are counted from the log and labeled `measured`.
- `effort_points` is each event count times a weight, summed, and labeled `estimate`. Weights come from the optional `[yardstick]` table in the config file (one integer from 0 to 1000 per event kind, default 1). The config path must lie inside the project root.
- `tokens` is `{"value": null, "label": "not supplied"}`. A token number appears only if an adapter supplies one; no adapter does yet.
- `per_task` lists at most 100 tasks; `per_task_truncated` says when more exist. Totals always cover every task.
- A log that fails chain verification is refused with exit code 3. Errors print one fixed line and never echo input.
