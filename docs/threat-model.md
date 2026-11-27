# Threat Model: SELF-REVIEW

**SELF-REVIEW:** This document is an initial design self-review. A human peer review is still required before implementation or reliance on these controls.

## Scope

This threat model covers the proposed provider-neutral `shit` wrapper's versioned records, lifecycle enforcement, local confined storage boundary, event and diagnostic logging, subprocess launch boundary, privacy scanning, and fail-closed hooks.

It began as the design input for the implementation issues. Those are now implemented in this repository; `docs/queue.md` maps each queue and lease row below to its tests. This document is still a self-review and does not itself test or audit the code.

This is not an audit. It does not establish that the wrapper, host, repository, providers, models, operating system, or future implementation is secure.

## Not covered

The following are outside this review:

- Active probing of any host, service, account, or provider.
- Provider API security and provider-side retention.
- Authentication or authorization beyond the local actor labels.
- Secret storage or secret transport. The first stage must not handle secrets.
- Cryptographic signing, encryption, or remote attestation.
- Network isolation.
- Operating-system compromise, malicious administrators, or physical attacks.
- Availability guarantees under disk exhaustion or total host failure.
- Detailed implementation review for later issues.
- Security claims for third-party tools invoked outside the fixed allowlist.
- Correctness of content supplied by a human or model.

## Assets

| Asset | Data class | Security objective |
|---|---|---|
| Task, brief, report, decision, and event records | Internal work-management data | Preserve confidentiality, integrity, provenance, and availability. |
| Evidence files and references | Potentially sensitive internal data | Prevent unintended disclosure, substitution, and path escape. |
| Confined diagnostic log | Sensitive local operational data | Keep raw logs local and prevent disclosure to the model. |
| Lifecycle state and approval history | Integrity-critical control data | Prevent forged approval, illegal transitions, and history rewriting. |
| Hook state | Integrity-critical control data | Prevent work from continuing when required checks are unknown or failed. |
| Wrapper configuration and fixed root | Security configuration | Prevent redirection to unconfined storage or symlinked settings. |
| Subprocess arguments and environment | Execution-control data | Prevent command injection and unintended secret disclosure. |
| Privacy scan results | Sensitive finding metadata | Reveal only path and line number, never matched content. |

## Actors

| Actor | Capability and trust |
|---|---|
| Human operator | Trusted to make approval decisions, but may make mistakes or supply unsafe paths. |
| Wrapper | Trusted only when executing validated version 1 contracts and fixed policies. |
| Model | Untrusted for authority; may propose content but cannot grant human approval. |
| Repository contributor | May control repository files and text consumed by the wrapper. |
| Evidence supplier | May provide malformed, oversized, linked, or specially typed files. |
| Pack author | May supply text containing instructions that conflict with wrapper policy. |
| Local unprivileged process | May race filesystem checks or attempt to replace confined files. |
| Compromised allowlisted executable | May misuse granted arguments, environment, or filesystem access. |

## Trust boundaries

1. Untrusted text crosses from repository content, issue text, model output, pack text, and evidence files into record parsing and command decisions.
2. Parsed JSON crosses from raw bytes into trusted record objects.
3. Model-generated proposals cross into lifecycle and approval enforcement.
4. User-supplied paths cross into the fixed local storage root.
5. Wrapper control crosses into an operating-system subprocess.
6. Subprocess output crosses into the confined diagnostic log and must not cross into model-visible output.
7. Hook observations cross into a decision to allow or stop work.
8. Privacy scan matches cross into user-visible metadata and must not disclose matched text.
9. Existing files cross through checks for symlinks, hard links, FIFOs, sockets, and device files before reading or writing.

## Untrusted inputs

The following inputs are explicitly untrusted:

- Repository content.
- Issue text.
- Model output.
- Pack text.
- Evidence files.

Untrusted text is data, never instructions. Text inside these inputs cannot change policy, grant approval, expand scope, alter the executable allowlist, request secrets, disable validation, or override confinement.

## Subprocess launch requirements

- Launch subprocesses with an argument vector array only.
- Never construct or execute a shell command string.
- Do not invoke a shell for interpolation, pipelines, redirection, globbing, or command substitution.
- Permit only executables in a fixed code-defined allowlist.
- Do not accept an executable name or executable path from record content.
- Pass environment variables through a fixed allowlist.
- Construct a minimal environment rather than copying the complete parent environment.
- Put no secrets in argv.
- The first stage must not read, accept, store, or forward secrets.
- Record only a sanitized fixed result for the model.
- Write full subprocess diagnostics only to the confined local log.
- If an allowed executable is unavailable or cannot start, return exit code 11.

## File confinement requirements

- Use one fixed storage root selected by trusted local configuration.
- Do not permit symlinked settings.
- Resolve every candidate path with `realpath` and verify that the resolved path is under the fixed root.
- Use path-component-aware containment checks, not string-prefix checks.
- Reject symlinks for reads and writes.
- Reject files with a hard-link count greater than one.
- Reject FIFOs, sockets, block devices, and character devices.
- Permit only expected regular files and directories.
- Reject writes outside the fixed root.
- Recheck the opened file descriptor before use so a path check does not stand alone.
- Apply restrictive creation permissions.
- Use atomic replacement within the same confined directory for complete record writes.
- Return exit code 5 for any confinement or prohibited-file-type violation.

## Hook behavior

Hooks fail closed. If a hook cannot verify its required state, configuration, input, dependency, or result, it stops the operation with exit code 8. Unknown state is not success. A hook failure must not partially update a task, history, decision, or event log.

## Risk register

| ID | Abuse case | Impact | Prerequisites | Rank | Mitigation | Verification | Residual risk |
|---|---|---|---|---|---|---|---|
| TM-01 | Repository or issue text tells the model to ignore policy and approve work. | Forged authority or unsafe actions. | Attacker can place text in a consumed repository or issue. | high | Treat repository content, issue text, model output, pack text, and evidence files as data only; require a human actor to enter `approved`. | `test_untrusted_text_cannot_approve` goes red if untrusted text can authorize approval. | A human may still approve a misleading proposal. |
| TM-02 | A record repeats a key so a permissive parser sees a different value than validation logic. | State, actor, or identifier confusion. | Attacker can supply JSON. | high | Parse with duplicate-pair detection and reject every duplicate key before schema validation. | `test_duplicate_key_rejected` uses `fixtures/invalid/duplicate-key.json` and expects exit code 3. | Parser defects may remain until independently reviewed. |
| TM-03 | An unknown schema version is interpreted as version 1. | Semantic confusion and unsafe acceptance. | Attacker or newer writer supplies an unsupported version. | high | Require exact supported versions and reject unknown versions without guessing. | `test_unknown_version_rejected` uses `fixtures/invalid/unknown-version.json` and expects exit code 3. | Coordinated upgrades still require explicit migration planning. |
| TM-04 | A crafted relative path, absolute path, or replaced path escapes the storage root. | Unauthorized read or write outside confined storage. | Attacker controls a path or can race filesystem operations. | critical | Resolve with `realpath`, perform component-aware root containment, reject writes outside root, and verify the opened descriptor. | `test_path_escape_rejected` goes red if `../outside` or a replacement race reaches a file outside the test root. | Some race resistance depends on operating-system primitives and implementation quality. |
| TM-05 | A symlink, hard link, FIFO, socket, or device file is accepted as ordinary evidence or a record target. | Data disclosure, overwrite, blocking, or interaction with devices. | Attacker can create filesystem entries under the root. | critical | Reject symlinks, link counts greater than one, FIFOs, sockets, block devices, and character devices before use. | `test_special_files_rejected` creates each supported test type and expects exit code 5. | Filesystem metadata can change concurrently, requiring descriptor-level rechecks. |
| TM-06 | Model output injects shell syntax into a subprocess command. | Arbitrary command execution. | Attacker influences a model-produced argument. | critical | Use argv arrays, never shell strings, and select executables only from a fixed code-defined allowlist. | `test_subprocess_uses_argv_without_shell` goes red if a metacharacter creates a sentinel file or if `shell=True` is used. | An allowlisted executable may interpret its own arguments unsafely. |
| TM-07 | The complete parent environment or a secret-bearing argument is passed to a subprocess. | Secret or configuration disclosure. | Sensitive values exist in the parent process environment or arguments. | high | Build a minimal environment from an allowlist, prohibit secrets in argv, and handle no secrets in stage one. | `test_subprocess_environment_allowlist` goes red if an unlisted sentinel variable reaches the child; `test_no_secret_in_argv` rejects secret-class arguments. | An allowed environment value may itself be sensitive if the allowlist is poorly reviewed. |
| TM-08 | Raw logs, exception text, subprocess output, or privacy matches are returned to the model. | Disclosure of sensitive local data. | A command fails, logs sensitive content, or finds a privacy hit. | high | Return only stable exit codes and short fixed messages; keep full logs confined; privacy hits reveal only path and line number. | `test_model_output_is_fixed_and_sanitized` and `test_privacy_hit_hides_match` go red if sentinel content appears in model-visible output. | File paths and line numbers can still reveal limited metadata. |
| TM-09 | A hook treats missing state, parse failure, or unavailable tooling as success. | Required controls are bypassed. | Hook verification encounters an error or unknown state. | high | Fail closed with exit code 8 and make record updates transactional. | `test_hook_unknown_state_fails_closed` goes red if any unverifiable hook state returns 0 or changes a record. | Repeated fail-closed behavior can reduce availability. |
| TM-10 | A model or wrapper directly changes a task into `approved`. | Human approval is forged. | Attacker can submit a transition or alter actor data. | critical | Enforce the lifecycle matrix and require actor `human` for every transition into `approved`. | `test_only_human_can_approve` goes red if a model or wrapper transition into `approved` succeeds. | Actor labels do not authenticate the physical human in stage one. |
| TM-11 | Oversized or deeply nested JSON exhausts memory or parser recursion. | Local denial of service. | Attacker can supply record bytes. | medium | Enforce a 65,536-byte per-record limit before parsing and a maximum nesting depth of 8 during validation. | `test_size_and_depth_limits` expects exit code 7 for both boundary violations. | Many individually valid records may still consume disk space. |
| TM-12 | A task history contains an illegal transition while the current state looks valid. | Lifecycle controls are bypassed or audit history becomes misleading. | Attacker can edit a task record. | high | Validate every ordered transition, actor constraint, chronological timestamp, and final-state consistency. | `test_bad_transition_rejected` uses `fixtures/invalid/bad-transition.json` and expects exit code 4. | Stage one does not cryptographically prevent deletion or rewriting of the whole history. |
| TM-13 | Unknown fields smuggle data or future semantics past a version 1 reader. | Confused-deputy behavior or hidden policy changes. | Attacker can add JSON fields. | medium | Reject unknown fields at every object level in version 1. | `test_unknown_field_rejected` uses `fixtures/invalid/unknown-field.json` and expects exit code 3. | New legitimate fields require a coordinated version bump. |
| TM-14 | A partial write leaves a task updated without its event, or an event without the task update. | Inconsistent state and unreliable evidence. | Process interruption or storage failure occurs during mutation. | high | Validate first, write atomically within the root, and commit related record and event changes as one recoverable operation. | `test_interrupted_update_recovers_consistently` injects a failure between stages and goes red if only one side is durable. | Full crash consistency varies by filesystem and requires implementation testing. |

## Queue and lease risks (issue 16)

**Scope of the queue protections (issue 17).** The queue, leases and recovery in issue 17 protect against crashes, races and mistakes of cooperating workers that run as the same operating-system user. They do not protect against a hostile process of that user (it can read every lease id, plant or rewrite any file under `.shit`, and ignore the advisory lock), nor against a rewrite or restore of the whole store. Multi-tenant or adversarial use needs an operating-system identity boundary, a credential service that holds the secrets outside the workers' reach (issue #48). Every "proves", "refuses" and "fails closed" below is meant inside that scope.

These rows cover the dependency graph, queue, leases and apply serialization specified in `docs/queue-spec.md`. They are design inputs for issues 17 and 18. Issue 17 implements the dependency graph, queue and lease rows; `docs/queue.md` maps each to its test. Rows and tests that belong to apply serialization and metrics (issue 18) do not exist yet; each must be written first and must go red when its control is removed. Network isolation and multi-host operation remain outside this review. Rule IDs refer to `docs/queue-spec.md`.

| ID | Abuse case | Impact | Prerequisites | Rank | Mitigation | Verification | Residual risk |
|---|---|---|---|---|---|---|---|
| TM-15 | A worker, or two workers at once, claims the same task twice. | Duplicate work and double application of a change. | Two dispatchers or workers run at the same moment, or one retries a claim. | high | Claim under the exclusive store lock with a re-read of state, exclusive create of the lease file, one live lease per task (Q-CLAIM-01 to Q-CLAIM-04). | `test_simultaneous_claims_single_winner` runs 200 racing claims and goes red if any task has two leases; `test_precreated_lease_refuses_claim` goes red if exclusive create is removed. | Advisory locks are unreliable on network filesystems (OQ-10). |
| TM-16 | The system clock moves backward or forward, or a worker supplies a time. | Leases never expire, expire early, or history is reordered. | Local clock change, or a caller who can supply timestamps. | high | Wrapper clock only, no worker-supplied times, claim and expiry refused when the clock is earlier than the newest recorded timestamp, fencing and apply slots keep a forward jump from causing a double apply (Q-LEASE-04, Q-LEASE-08, Q-LEASE-09). | `test_clock_backward_fails_closed` expects exit code 8; `test_clock_forward_no_double_apply` goes red if two applies occur after a jump; `test_worker_time_field_rejected` expects exit code 2. | A large forward jump can cause duplicate drafting work. A local administrator can still set any clock. |
| TM-17 | A forged, guessed, copied or hand-written lease is presented to move a task. | A task is advanced or reported by a party that does not own it. | Attacker can write the queue directory, or read a lease file as the same user. | high | Unguessable 128-bit wrapper-generated `lease_id`, constant-time comparison, a lease trusted only if it matches the latest queue entry, confined files (Q-LEASE-01, Q-LEASE-10, Q-LEASE-11). | `test_forged_lease_refused` (wrong id, wrong generation, hand-written file, no lease) expects the proposed exit code 12 and no change. | A process of the same operating-system user can read a valid lease and use it (OQ-8). A lease is not authentication. |
| TM-18 | A dependency cycle, self edge or dangling edge is introduced by an edit or a model proposal. | Tasks wait forever, or the dispatcher loops. | Attacker or model can change graph input or edit a dependency shard under `deps/`. | medium | Cycle check over all edge types on add and on load, missing-node checks, only `human` or `wrapper` may change the graph, every shard digest is recorded in the hash chain, and a missing, edited or unchained shard fails closed (Q-DEP-03 to Q-DEP-07). | `test_cycles_unknown_and_duplicate`, `test_chain_proven_cycle_fails_closed`, `test_shard_deleted_fails_closed`, `test_shard_edited_fails_closed` and `test_shard_added_without_a_chain_entry` expect exit codes 3, 8, 8, 8 and 8. | A valid but wrong edge can still stall work; a human reviews the graph. |
| TM-19 | A finished task is brought back: a `done` id is reused, an older state is restored, or a task is moved out of `done`. | Closed work is reopened without approval, or history is rewritten. | Attacker can edit or restore files under `.shit`. | high | `done` is terminal, ids are never reused, a stale chain tip or older restored copy fails closed (Q-ST-05, Q-RES-01, Q-RES-02). | `test_done_is_terminal`, `test_closed_id_not_reusable` and `test_restored_older_queue_fails_closed` go red if the control is removed. | Restoring a consistent older copy of every log cannot be detected without signing (already a stated residual risk). |
| TM-20 | A worker whose lease expired or was reclaimed keeps working and submits (a stale or stolen claim). | Stale output overwrites a newer attempt. | Slow or paused worker, lease expiry, a second claim. | high | Fencing generation on every operation, expiry releases apply slots, stale generation refused (Q-LEASE-02, Q-LEASE-07, Q-SER-04). | `test_stale_generation_submit_refused` goes red if the old worker's submit is accepted after a second claim. | Side effects a worker already made outside the wrapper are not undone. |
| TM-21 | Two apply steps change the same target at once, or an apply starts on a changed base. | Lost updates or corrupted shared files. | Parallel dispatch with overlapping targets. | high | Target slots taken all or nothing in sorted order, `apply` edges order steps, `base_sha256` precondition, target path confinement (Q-SER-01 to Q-SER-07). | `test_overlapping_apply_never_concurrent`, `test_apply_refused_on_changed_base` and `test_target_path_escape_rejected` expect exit codes 4, 9 and 5. | Targets are declared by the task; an undeclared change by a worker is not prevented by the queue. |
| TM-22 | `QUEUE.md`, a brief or a report is edited so it appears to change queue state or carries instructions to the dispatcher. | Forged readiness or injected instructions. | Attacker can edit generated or text files. | medium | `QUEUE.md` is output only and never read, text from briefs and reports is data (TM-01), state comes only from the verified chain (Q-DEP-08, Q-REC-04, Q-REC-06). | `test_queue_md_edit_changes_nothing` and `test_queue_md_deleted_changes_nothing`. | None beyond the general residual risks. |
| TM-23 | A worker renews a lease forever or hoards many leases. | Starvation of other workers and denial of service. | Any worker able to ask for leases. | medium | Maximum lease length and renewals, capacity counted from files, a claim past capacity recorded as `dispatched` and never `ready` (Q-LEASE-03, Q-CLAIM-04). | `test_renewal_bounds_enforced` and `test_capacity_counted_from_files`. | A worker can hold work for the bounded time; a human can force a reclaim (Q-REC-05). |
| TM-24 | A crash leaves orphan leases, orphan apply slots or a half-written queue update. | Tasks stuck, or two sources of truth. | Process interruption or storage failure. | high | Commit-point pattern, idempotent apply, reconciliation of orphans, fail closed on a broken chain (Q-REC-01 to Q-REC-04). | `test_queue_crash_matrix_recovers_consistently` kills after each stage; `test_orphan_lease_removed`. | Filesystem crash consistency varies and needs per-platform testing (already stated). |

## Residual risks

- Actor strings provide provenance labels, not strong human authentication.
- A compromised local account or administrator can alter records and logs.
- Stage one records are not cryptographically signed, encrypted, or append-only at the filesystem level.
- The queue hash chain detects edits, truncation and reordering by someone who does not also rewrite the chain. Anyone who can rewrite `queue.jsonl`, `queue.chain` and the shards together, including deleting them all, cannot be detected without signing (docs/queue-spec.md, Q-RES-02).
- A lease id is a bearer token readable by any process of the same operating-system user (TM-17). Per-worker credentials, as designed in `docs/design/lease-credentials.md` and not yet implemented, do not stop a hostile process of the same OS user: that needs a broker running under another OS identity with authenticated IPC, or distinct OS identities per worker. The design narrows exposure to passive disclosure of `.shit` only.
- A same-user process with write access to the repository can still move or replace the `.shit` directory or its files. The store lock now rests on a directory descriptor and the queue tip is anchored in `queue-anchor.json`, which closes accidental and partial rollback, but a process that rewrites the queue files, the chain, the shards and the anchor together, or replaces the whole store, is the unsigned-history case above: an authenticated monotonic anchor outside the writable repository is required to close it. Until then a generation can be reused after such a coordinated rollback.
- The hash chain is unkeyed: an entry appended with a recomputed chain is accepted if it is a valid next step. Actor labels (`human`, `wrapper`) are validated but not authenticated.
- A same-user hostile or buggy process can plant `queue-pending.json`. Every entry point then refuses on sight with exit code 8 until an operator follows `docs/queue-spec.md`, Q-REC-02. If the planted record is internally consistent, `queue.recover` applies it, because nothing authenticates who wrote it; an operator inspects it first. Validation cannot prevent this; only an operating-system identity boundary can (a credential service, issue #48). Nothing discards a record automatically and there is no quarantine feature.
- Work done while holding the store lock is not time-bounded, and privacy-scan traversal does not bound directory width or cumulative bytes (follow-up).
- A compromised allowlisted executable can act within its operating-system permissions.
- Fail-closed hooks protect integrity but may create denial-of-service conditions.
- Privacy scanning can miss unknown patterns and can produce false positives.
- Confined storage cannot prevent disclosure by another process that already has permission to read the root.
- A human can accept incorrect model output or unsafe evidence.
- Disk exhaustion can interrupt atomic operations or prevent logging.
- Platform-specific filesystem behavior on macOS, Linux, and WSL2 requires separate implementation verification.

## Review still required

A human peer must review the lifecycle policy, schema contracts, executable and environment allowlists, path-confinement design, test plan, and residual-risk acceptance. Later implementation issues require code review and platform-specific tests. This self-review does not replace either review.
