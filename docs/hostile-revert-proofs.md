# Revert proofs for the hostile fixtures

Issue 14. For each guard, one line of source was removed or neutralized in a working copy, `python3 -m unittest discover -s tests -p test_hostile.py` was run, and the file was restored with `git restore`. A guard is proven when at least one hostile test goes red. The tree was clean after the last mutation.

Commit under test: the parent of the commit that adds this file. Mutation text is given as the replacement made.

| ID | Guard (file) | Mutation | Tests that went red |
|---|---|---|---|
| M01 | lexical dot-dot check (`project._split`) | condition replaced by `False` | `test_path_escape_in_store_paths` |
| M03 | symlink refusal (`project._NOFOLLOW`) | constant set to 0 | `test_symlinks` |
| M04 | hard-link count (`project._check_regular`) | `st_nlink` term removed | `test_hard_links` |
| M05 | file type (`project._check_regular`) | `S_ISREG` term removed | `test_fifos`, `test_sockets_and_directories_and_devices` |
| M06 | evidence path components (`commands._rel_parts`) | condition replaced by `False` | `test_path_escape_in_evidence_path` |
| M07 | hard-link count (`commands.hash_file`) | `st_nlink` term removed | `test_hard_links` |
| M08 | symlink refusal on the evidence leaf (`commands.hash_file`) | `O_NOFOLLOW` removed | `test_symlinks`, `test_tampered_evidence_file`, `test_errors_never_echo_input` |
| M09 | file type (`commands.hash_file`) | `S_ISREG` term removed | `test_fifos` |
| M10 | 16 MiB evidence limit | condition replaced by `False` | `test_oversize_evidence_and_records` |
| M11 | nesting depth limit (`records.scan_depth`) | condition replaced by `False` | `test_depth_and_size_limits` |
| M12 | record size limit (`records.parse`) | condition replaced by `False` | `test_depth_and_size_limits` |
| M13 | duplicate keys (`records._pairs`) | condition replaced by `False` | `test_duplicate_keys`, `test_duplicate_key_planted_on_disk` |
| M14 | exact version type (`records._version`) | `type(version) is not int` term removed | `test_version_confusion` |
| M15 | unknown fields (`records._keys`) | upper-bound term removed | `test_unknown_field_in_nested_object` |
| M16 | hostile characters (`records._text`) | `check_text` call replaced by `pass` | `test_hostile_characters_in_planted_task_are_refused`, `test_hostile_characters_in_transition_reason` |
| M17 | newline in an evidence path (`artifacts._evidence`) | term removed | `test_hostile_file_names` |
| M18 | previous-hash term of the chain (`log.chain_hash`) | hash of the line alone | `test_chain_matches_independent_recomputation`, `test_reordered_deleted_edited_events` |
| M19 | chain comparison (`log.verify`) | condition replaced by `False` | `test_reordered_deleted_edited_events`, `test_log_shape_attacks` |
| M20 | chain and log have equal length (`log.verify`) | condition replaced by `False` | `test_reordered_deleted_edited_events` |
| M21 | duplicate event id (`log.verify`) | condition replaced by `False` | `test_log_shape_attacks` |
| M23 | human approval, exit 10 (`task.transition`) | condition replaced by `False` | `test_only_a_human_can_approve` |
| M24 | transition matrix in `task.transition` and in history validation | both calls removed | `test_forged_history_and_events` |
| M25 | accepted decision needs a human (`commands.add_decision`) | condition replaced by `False` | `test_decision_by_non_human_actor` |
| M26 | close needs a human decision actor | term removed | `test_forged_non_human_decision_cannot_close` |
| M27 | close needs a human review actor | term removed | `test_non_human_review_cannot_unlock_close` |
| M28 | close re-checks the evidence digest | condition replaced by `False` | `test_tampered_evidence_file` |
| M29 | record digest bound to its event (`commands._load`) | term removed | `test_tampered_record_files` |
| M30 | a record needs an event (`commands._load`) | condition replaced by `False` | `test_tampered_record_files` |
| M31 | pending chain check (`task._recover`) | condition replaced by `False` | `test_forged_pending_with_wrong_chain` |
| M32 | pending parse catches `RecursionError` | exception removed from the tuple | `test_planted_pending_record_bomb` |
| M33 | parser never echoes input (`cli_tasks._Parser.error`) | calls argparse's own error | `test_errors_never_echo_input` |
| M34 | the net guard patches sockets (`tests/_netguard.py`) | `patcher.start()` replaced by `pass` | `test_guard_bites_on_socket_creation_and_dns`, `test_a_hidden_connection_is_caught_by_the_flow` |
| M35 | no network import in the package | `import socket` added to `log.py` | `test_no_module_imports_network_or_process_code` |
| M36 | a command that opens a connection (`commands.add_evidence`) | `socket.create_connection` added | `test_every_command_makes_no_network_call` and 15 others |

## Guards the hostile tests cannot isolate

These mutations left every hostile test green. Each is a second layer behind a guard above, so the behavior is still protected, but no test proves this line on its own.

| ID | Guard | Why it stays green |
|---|---|---|
| M02 | realpath containment in `project._split` | `O_NOFOLLOW` descent and the lexical dot-dot check (M01, M03) already refuse every path that realpath would catch. |
| M22 | final newline required in `log._lines` | Without it the last character of the last line is dropped, which fails JSON or chain checks anyway. |

In M24 the transition matrix is enforced three times (command, history validation, event validation). Only removing the first two together, with the event layer also exercised by the forged fixtures, was shown red; removing a single layer is covered by the other two.

## Red before the fix

Before the two source fixes below, two new tests were red on the unmodified code: `test_hostile_characters_in_transition_reason` (a reason with `ESC`, bidi or zero-width characters was accepted by `task.transition`) and `test_hostile_file_names` (an evidence path containing a line feed was accepted). `M16` and `M17` re-prove them after the fix.
