# Threat Model: SELF-REVIEW

**SELF-REVIEW:** This document is an initial design self-review. A human peer review is still required before implementation or reliance on these controls.

## Scope

This threat model covers the proposed provider-neutral `shit` wrapper's versioned records, lifecycle enforcement, local confined storage boundary, event and diagnostic logging, subprocess launch boundary, privacy scanning, and fail-closed hooks.

It evaluates the design inputs for later implementation issues. It does not test an implementation because no implementation exists in this issue.

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

## Residual risks

- Actor strings provide provenance labels, not strong human authentication.
- A compromised local account or administrator can alter records and logs.
- Stage one records are not cryptographically signed, encrypted, or append-only at the filesystem level.
- A compromised allowlisted executable can act within its operating-system permissions.
- Fail-closed hooks protect integrity but may create denial-of-service conditions.
- Privacy scanning can miss unknown patterns and can produce false positives.
- Confined storage cannot prevent disclosure by another process that already has permission to read the root.
- A human can accept incorrect model output or unsafe evidence.
- Disk exhaustion can interrupt atomic operations or prevent logging.
- Platform-specific filesystem behavior on macOS, Linux, and WSL2 requires separate implementation verification.

## Review still required

A human peer must review the lifecycle policy, schema contracts, executable and environment allowlists, path-confinement design, test plan, and residual-risk acceptance. Later implementation issues require code review and platform-specific tests. This self-review does not replace either review.
