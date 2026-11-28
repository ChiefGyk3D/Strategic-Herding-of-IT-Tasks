# Hostile fixtures

Issue 14. The corpus lives in `fixtures/hostile/` (static files) and in `tests/test_hostile.py` (fixtures a test builds on disk, because a symlink, a FIFO or a 16 MiB file does not belong in a repository). Every test runs with network access denied by `tests/_netguard.py`.

`HostileTable` in the test file fails if a test is missing from this table, a row names a test that does not exist, a file in `fixtures/hostile/` is not cited here, or a row cites a threat-model row that does not exist. Revert proofs are in `docs/hostile-revert-proofs.md`.

Rows marked `none` have no risk-register row. They are gaps in `docs/threat-model.md` that a reviewer should close: tampered evidence and the event log chain (history is covered only as TM-12 for task records), and network denial (the threat model lists network isolation as not covered).

| ID | Fixture | Row | Expected exit | Test |
|---|---|---|---|---|
| H-01 | `duplicate-key-nested.json`, `duplicate-key-event.jsonl`: repeated key in a history entry and in an event | TM-02 | 3 | `Hostile.test_duplicate_keys` |
| H-02 | `duplicate-key-nested.json` planted in the store and read through the CLI | TM-02 | 3 | `Hostile.test_duplicate_key_planted_on_disk` |
| H-03 | `version-true.json`, `version-float.json`: version true and 1.0 | TM-03 | 3 | `Hostile.test_version_confusion` |
| H-04 | `unknown-field-nested.json`: unknown key in a history entry | TM-13 | 3 | `Hostile.test_unknown_field_in_nested_object` |
| H-05 | `depth-9.json`, a bracket bomb, an oversize record, a record file over the limit | TM-11 | 7 | `Hostile.test_depth_and_size_limits` |
| H-05b | brackets inside a string must not count as nesting | TM-11 | 0 | `Hostile.test_brackets_inside_strings_do_not_count_as_depth` |
| H-06 | `bidi-title.json`, `escape-title.json`, `zero-width-summary.json` planted in the store | TM-01 | 3 | `Hostile.test_hostile_characters_in_planted_task_are_refused` |
| H-06b | the same characters (and CR, tab, NUL, isolates, BOM, separators, C1) on create | TM-01 | 3 | `Hostile.test_hostile_characters_refused_on_create` |
| H-06c | control, bidi and zero-width characters in a transition reason | TM-01 | 3 | `Hostile.test_hostile_characters_in_transition_reason` |
| H-07 | `forged-model-approval.json`, `reordered-history.json`, `forged-event-model-approval.jsonl` | TM-12, TM-10 | 4 or 3 | `Hostile.test_forged_history_and_events` |
| H-08 | `injection-text.json`: instruction text as a task summary | TM-01 | 4 | `Hostile.test_injected_text_is_data` |
| H-09 | a model or the wrapper moves a reviewed task to approved | TM-10 | 10 | `Hostile.test_only_a_human_can_approve` |
| H-09b | illegal moves, tested at the matrix and at the command | TM-12 | 4 | `Hostile.test_transition_matrix_alone` |
| H-10 | an accepted decision recorded by a model or the wrapper | TM-10 | 10 | `Hostile.test_decision_by_non_human_actor` |
| H-11 | a non-human accepted decision forged on disk with a chained event | TM-10 | 10 | `Hostile.test_forged_non_human_decision_cannot_close` |
| H-12 | a passing review by the wrapper | TM-10 | 9 | `Hostile.test_non_human_review_cannot_unlock_close` |
| H-13 | dot-dot, absolute, NUL, empty, dot and store-internal evidence paths | TM-04 | 5 | `Hostile.test_path_escape_in_evidence_path` |
| H-13b | dot-dot that resolves inside the store, and outside it, on every store call | TM-04 | 5 | `Hostile.test_path_escape_in_store_paths` |
| H-14 | symlinked evidence file, evidence directory, record, event log and store | TM-05 | 5 | `Hostile.test_symlinks` |
| H-14b | a store subdirectory swapped for a symlink | TM-05 | 5 | `Hostile.test_symlinked_subdirectory_of_store` |
| H-15 | hard-linked evidence, event log and record | TM-05 | 5 | `Hostile.test_hard_links` |
| H-16 | a FIFO as evidence, record, chain file and lock file, with a 5 second alarm | TM-05 | 5 | `Hostile.test_fifos` |
| H-17 | a socket, a directory, and block and character device modes | TM-05 | 5 | `Hostile.test_sockets_and_directories_and_devices` |
| H-18 | evidence over 16 MiB, a record over 64 KiB, an event line over 64 KiB, an over-long title | TM-11 | 7 or 3 | `Hostile.test_oversize_evidence_and_records` |
| H-19 | a deeply nested or malformed `pending.json` | TM-14, TM-11 | 3 | `Hostile.test_planted_pending_record_bomb` |
| H-19b | a `pending.json` whose chain entry does not follow the log | TM-14 | 3 | `Hostile.test_forged_pending_with_wrong_chain` |
| H-20 | the chain file against an independent recomputation | TM-12 | 0 | `Hostile.test_chain_matches_independent_recomputation` |
| H-21 | swapped, deleted, truncated, edited, duplicated and appended events; bad chain entries | TM-12 | 3 or 4 | `Hostile.test_reordered_deleted_edited_events` |
| H-22 | blank line, missing newline, CRLF, forged duplicate id with a recomputed chain | TM-12 | 3 | `Hostile.test_log_shape_attacks` |
| H-23 | a chained event whose record file does not exist | none | 9 | `Hostile.test_forged_event_without_record` |
| H-24 | evidence edited, deleted or replaced by a symlink after it was recorded | none | 3, 9 or 5 | `Hostile.test_tampered_evidence_file` |
| H-25 | a record edited, copied under another id, or dropped in without an event | none | 3 or 9 | `Hostile.test_tampered_record_files` |
| H-26 | evidence files named with bidi, newline, escape, zero-width or tab characters | TM-01 | 3 | `Hostile.test_hostile_file_names` |
| H-26b | identifiers with traversal, case, control or bidi characters | TM-01 | 3 | `Hostile.test_hostile_identifiers` |
| H-27 | failure output never echoes arguments, paths or a sentinel | TM-08 | non-zero | `Hostile.test_errors_never_echo_input` |
| H-28 | shell syntax and non-ASCII text is stored as data and printed ASCII-escaped | TM-06 | 0 | `Hostile.test_cli_whole_flow_with_hostile_text_stays_ascii` |
| H-29 | the whole CLI flow, success and failure paths, makes zero network calls | none | 0 | `NetworkDenial.test_every_command_makes_no_network_call` |
| H-29b | the guard fails and records every way to open a socket or resolve a name | none | n/a | `NetworkDenial.test_guard_bites_on_socket_creation_and_dns` |
| H-29c | a command that quietly opens a socket and swallows the error fails the flow | none | n/a | `NetworkDenial.test_a_hidden_connection_is_caught_by_the_flow` |
| H-29d | no module of the package imports a network or process module | none | n/a | `NetworkDenial.test_no_module_imports_network_or_process_code` |
| H-29e | the import scan reports a banned import in every spelling | none | n/a | `NetworkDenial.test_import_scan_bites` |
| H-29f | every file under `shit/` is Python and none launches a process (os calls, aliases, dynamic imports, eval); only named scripts outside it may | none | n/a | `NetworkDenial.test_nothing_under_the_package_can_launch_a_process` |
| H-29g | the process-launch scan reports every spelling and accepts ordinary `os` use | none | n/a | `NetworkDenial.test_process_launch_scan_bites` |

## Not testable on this branch

- TM-06 and TM-07 (subprocess argv and environment): no code on main launches a process. H-28 and H-29d show that shell syntax is data and that no module imports `subprocess`. The adapters issue owns the full tests.
- TM-09 (fail-closed hooks): the check command is not on main.
- Device nodes (TM-05): creating one needs privilege, so H-17 tests the type check against block and character device modes rather than a real node.
- A local attacker who can rewrite both `events.jsonl` and `events.chain` consistently is accepted by design (stage one is not signed; see the threat model residual risks). H-22 shows the recomputed-chain case is still stopped by the per-event rules (duplicate id).
