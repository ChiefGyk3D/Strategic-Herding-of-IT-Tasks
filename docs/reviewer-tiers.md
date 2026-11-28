# Reviewer Providers and Tiers

**Status:** design note for issue #88. Nothing in this document is implemented.

## Contents

- [Goals](#goals)
- [Non-goals](#non-goals)
- [Design](#design)
- [Configuration](#configuration)
- [Availability detection](#availability-detection)
- [Degraded modes](#degraded-modes)
- [Report format](#report-format)
- [Failure behavior](#failure-behavior)
- [Differences from the sibling project](#differences-from-the-sibling-project)
- [Test plan](#test-plan)
- [Staged implementation plan](#staged-implementation-plan)
- [Open questions](#open-questions)

## Goals

- Run adversarial review only when its configured provider quorum is available. The default quorum requires both `claude` and `openai`, with at least two distinct provider kinds.
- Make an incomplete review unmistakable. Every report lists every requested reviewer, whether it ran, and why it did not run.
- Use the cyber-program tier only when the team configured it and a cheap, read-only probe proves it available to the current user. Otherwise, run the provider at its configured default tier and label that tier.
- Assign named providers to implementer, reviewer, and adversarial roles through TOML. Adding a provider requires an adapter and configuration, not a new branch in the core.
- Preserve the existing rules for bounded input, argument arrays, minimal environments, read-only review permissions, fixed messages, human approval, and untrusted model output.
- Fail the merge gate when a required review did not meet its policy. Absence is evidence, not success.

## Non-goals

- Implementing the configuration, probes, runner, report record, or merge gate.
- Proving that a provider, model, or cyber-program tier is secure or correct.
- Logging in, changing an account, buying access, or discovering credentials.
- Sending a prompt as an availability probe.
- Letting a model approve work. The lifecycle rule that only a human may enter `approved` remains unchanged.
- Making the cyber-program tier available to a user who has not received it from their model vendor.

## Design

The core works with provider instance names. It asks an adapter for a bounded availability result, chooses a tier under policy, runs the requested role through the existing plan boundary, and records the result. It does not know provider-specific commands, model identifiers, authentication files, or tier flags.

An adapter adds these provider-neutral operations to the current `prepare`, `parse_output`, and `info` interface:

1. `probe_default()` reports whether the executable, authentication, and no-cost readiness checks pass.
2. `probe_cyber_program()` reports whether the optional tier is available to the current user without an inference call.
3. `prepare_role(role, tier, brief)` returns an argument array, standard input, permissions, allowed environment names, and a timeout. Review roles remain read-only.

Probe results use a closed set: `available`, `not_configured`, `not_installed`, `not_authenticated`, `rate_limited`, and `probe_failed`. A result contains no raw subprocess output, account identifier, credential, or provider response. The detailed diagnostic stays in the confined log. Unknown results become `probe_failed`.

The core first expands the role's ordered provider names, probes each once for that review attempt, and selects the tier:

- If `cyber_program = true` and the cyber-program probe returns `available`, use `cyber-program`.
- Otherwise, if the default probe returns `available`, use `default` and report why the optional tier was not selected.
- Otherwise, the provider is absent and its fixed reason appears in the report.

For adversarial review, the core then checks both `required_provider_kinds` and `minimum_distinct_provider_kinds`. It starts no adversarial reviewer unless the complete quorum is available. This all-or-nothing start avoids presenting one provider's output as partial completion of a two-provider control. Regular review does not use the adversarial quorum.

Provider output is untrusted input under `docs/threat-model.md`. It can supply findings, but it cannot change policy, select another provider, waive a missing reviewer, mark the review complete, or approve the task.

## Configuration

The proposed schema extends the existing strict TOML configuration. Unknown tables and keys, duplicate keys, wrong types, invalid values, and unfilled placeholders remain refused.

```toml
schema_version = 1

[review_providers]
# Named instances used by all three role lists. Order is dispatch order.
implementer = ["openai-main"]
reviewer = ["claude-main"]
adversarial = ["claude-main", "openai-main"]

[adversarial_review]
required_provider_kinds = ["claude", "openai"]
minimum_distinct_provider_kinds = 2
required_for_merge = true
allow_local = false

[providers.claude-main]
kind = "claude"
adapter = "claude"
local = false
default_model = "<team-selected model>"
cyber_program = false
# Required only when cyber_program is true.
# cyber_program_model = "<team-selected model>"

[providers.openai-main]
kind = "openai"
adapter = "codex"
local = false
default_model = "<team-selected model>"
cyber_program = false
# cyber_program_model = "<team-selected model>"
```

Defaults when the new tables are absent:

| Setting | Default |
|---|---|
| `review_providers.implementer` | `[]` |
| `review_providers.reviewer` | `[]` |
| `review_providers.adversarial` | `[]` |
| `required_provider_kinds` | `["claude", "openai"]` |
| `minimum_distinct_provider_kinds` | `2` |
| `required_for_merge` | `true` |
| `allow_local` | `false` |
| provider `local` | `false` |
| provider `cyber_program` | `false` |

The empty role lists mean that no provider-backed work is requested by default. A request for adversarial review with the default empty list produces an incomplete report and cannot pass the gate. This does not change the existing paste-in, hand-operated workflow.

Provider names and kinds are lowercase slugs. Role lists contain at most 16 unique provider names. `minimum_distinct_provider_kinds` is an integer from 1 through 16. `required_provider_kinds` contains at most 16 unique kinds. Every listed name must have one matching `[providers.NAME]` table. An adapter must exist for each configured adapter name. A provider instance may appear in more than one role.

The adversarial list must be capable of satisfying both quorum rules by configuration alone. Two instances of one kind count as one distinct provider kind. Every required kind must occur in the adversarial list. A provider with `local = true` may serve implementer or reviewer. It may serve adversarial only when `allow_local = true`. This opt-in is separate because processing hostile input and returning model-controlled text can expose weaknesses in the local model or its serving stack. Issue #25 tracks the related local-model resilience work.

`cyber_program_model` is forbidden when `cyber_program = false` and required when it is true. The model values are bounded text passed only through an adapter's model field. Configuration cannot supply a probe command, executable path, arbitrary flag, environment variable, or timeout.

### Invalid configuration and exit codes

| Invalid configuration | Exit code |
|---|---:|
| Unknown or duplicate key or table, wrong type, invalid slug, unfilled placeholder | 3, `record_rejected` |
| Role list is empty when the command explicitly requires that role, too long, duplicated, or names an undefined provider | 3, `record_rejected` |
| Quorum is outside 1 through 16 or cannot be met by the adversarial list | 3, `record_rejected` |
| A required provider kind is absent from the adversarial list | 3, `record_rejected` |
| A local provider is assigned to adversarial while `allow_local = false` | 3, `record_rejected` |
| Cyber-program fields are missing or contradictory | 3, `record_rejected` |
| Adapter name is not registered | 3, `record_rejected` |
| Configuration exceeds the bounded input size | 7, `input_limit` |

Missing executables, missing authentication, unavailable tiers, rate limits, and failed probes are runtime availability results, not invalid configuration. They are recorded in the report. When they prevent a required review, the merge hook fails closed with exit code 8, `hook_failed_closed`. A configured tool that cannot start after a successful probe ends the attempt with exit code 11, `tool_unavailable`, and also leaves the review incomplete.

## Availability detection

All probes are read-only, have no prompt, create no work item, and make no inference request. Each subprocess receives the adapter's minimal environment and an empty standard input. The default deadline is 5 seconds per probe, with a hard maximum of 15 seconds in code. The team cannot enlarge it in TOML. Output is read to a small fixed bound, parsed into a fixed result, and never copied to the report.

Availability is scoped to the current operating-system user, current provider instance, and current review attempt. A cached result may be reused only within that attempt. No positive result is written as lasting account state.

| Provider kind | Default-tier probe | Cyber-program probe | `available` means |
|---|---|---|---|
| `claude` | Find the adapter's executable, then use its documented, non-interactive authentication and readiness metadata operation. | Use a documented account-capability or model-catalog metadata read that identifies the configured tier without generating tokens. If no such no-cost operation exists, return `probe_failed`. | Executable present, current user authenticated, metadata readable, and configured model visible without an inference call. |
| `openai` | Find the adapter's executable, then use its documented, non-interactive authentication and readiness metadata operation. | Use a documented account-capability or model-catalog metadata read that identifies the configured tier without generating tokens. If no such no-cost operation exists, return `probe_failed`. | Executable present, current user authenticated, metadata readable, and configured model visible without an inference call. |
| local | Find the configured adapter executable and use its bounded local health and model-list read. No network outside the local service is allowed. | Not applicable. A local provider never claims the cyber-program tier. | Executable or local service present and the configured model is already listed as ready. |
| future adapter | Implement the same fixed-result contract and document its non-billable metadata source. | Optional. Unsupported tier detection returns `probe_failed`, never assumed access. | The adapter proves executable, authentication where applicable, and configured model readiness under the same bounds. |

A probe never starts an interactive login. A login prompt, consent prompt, browser launch request, timeout, malformed answer, unsupported metadata operation, or ambiguous tier becomes `not_authenticated` or `probe_failed`. A provider's documented rate-limit response becomes `rate_limited`. Failure to distinguish a condition safely becomes `probe_failed`.

The exact command and response parser belong to the adapter and must be pinned to provider documentation during implementation. A help or version command can prove installation, but cannot prove authentication or tier access.

## Degraded modes

In this table, C is an available configured Claude provider and O is an available configured OpenAI provider. A provider is available at either its cyber-program tier or its default tier. The tier affects labeling, not whether it counts toward the default quorum.

| Available configured providers | Implementer run | Regular review run | Adversarial review run | Required report text | May the adversarial merge gate pass? |
|---|---|---|---|---|---|
| C and O, both quorum rules met | Assigned available provider | Assigned available reviewer | Both run | Each reviewer and selected tier; `adversarial review: complete` only if both finish validly | Yes, if both valid reviews pass policy |
| C and O, cyber-program available to both | Assigned available provider | Assigned available reviewer | Both run at cyber-program tier when configured | Each row says `cyber-program` | Yes, if both valid reviews pass policy |
| C and O, cyber-program unavailable to one or both | Assigned available provider | Assigned available reviewer | Both run at default tier where needed | Each fallback row says `default` and gives the tier absence reason | Yes, if both valid reviews pass policy |
| C only | C if assigned | C if assigned | None | O row gives its absence reason; `adversarial review: incomplete, quorum not met` | No |
| O only | O if assigned | O if assigned | None | C row gives its absence reason; `adversarial review: incomplete, quorum not met` | No |
| Other kinds meet the configured replacement quorum | Assigned available provider | Assigned available reviewer | Configured quorum runs | Rows name every provider and tier; report names the non-default quorum policy | Yes, if every configured rule is met |
| One kind has two available instances, minimum is two kinds | Assigned instance | Assigned instance | None | Both instances listed; `only one distinct provider kind available` | No |
| Local plus one remote, local opt-in off | Either for non-adversarial roles | Either for regular review | None unless remote providers alone meet quorum | Local row says `not eligible for adversarial review` | No unless the remaining eligible set meets quorum |
| Local plus one remote, local opt-in on and configured quorum permits both kinds | Either | Either | Both run | Report calls out local opt-in and its risk | Yes, if every configured rule is met |
| No assigned provider available | None | None | None | Every requested row gives its absence reason; all requested reviews are incomplete | No |
| Adversarial review not requested | As assigned | As assigned | None | `adversarial review: not requested` | Not applicable; it is not claimed as done |

Regular review has its own gate status. Its unavailable assigned reviewer is reported and cannot satisfy a required regular-review gate. The adversarial gate is evaluated only when adversarial review is requested or policy requires it. `required_for_merge = false` can make an incomplete adversarial review non-blocking, but the report still says incomplete and never says done.

## Report format

Every review report gains a machine-readable review summary followed by the human findings. The summary is produced by the wrapper from configuration and observed results, not copied from provider output.

```text
REVIEW TYPE: adversarial
REVIEW STATUS: complete | incomplete | failed | not-requested
QUORUM: required kinds=claude,openai; minimum distinct=2; observed distinct=2
MERGE GATE: pass | fail | not-applicable

| Requested reviewer | Kind | Role | Requested tier | Selected tier | Ran | Result or absence reason |
| claude-main | claude | adversarial | cyber-program | default | yes | pass; probe failed for optional tier |
| openai-main | openai | adversarial | cyber-program | cyber-program | yes | pass |
```

Allowed absence reasons are `not configured`, `not installed`, `not authenticated`, `rate-limited`, and `probe failed`. `not eligible` is added for a local provider refused by policy. A provider that starts but does not return a valid review uses `started, review failed`, with the detailed reason confined to diagnostics.

The report records the configured policy, selected tier, adapter name, exact model identifier reported by the adapter, start and finish times, bounded result, and gate decision. It does not record account data, raw probe output, credentials, full subprocess output, or rejected hostile text. Provider findings remain separate sections so disagreement is visible and is not averaged into one verdict.

`complete` means every requested reviewer required by policy ran, returned a valid bounded report, and the quorum held through completion. It does not mean the findings passed, the change is secure, or the task is approved.

## Failure behavior

| Failure | Behavior | Gate result |
|---|---|---|
| Provider dies or times out before returning a valid review | Stop that provider, mark `started, review failed`, retain other finished findings, do not replace it with an unrequested provider, and do not retry automatically. | Fail when the review is required. |
| Provider returns controls, bidirectional text, an oversized answer, instructions to waive policy, or malformed report data | Treat it as hostile untrusted output, reject it under the existing text and size contracts, record only the fixed failure reason, and keep raw diagnostics confined. | Fail when the review is required. |
| Probe says available but launch fails | Record the probe result and `started, review failed`; return tool-unavailable behavior for the attempt. | Fail when the review is required. |
| Probe says tier available but the provider rejects that tier | Do not silently rerun at default tier. Mark the requested reviewer failed. A new attempt may probe again and select default if the tier then reports unavailable. | Fail when the review is required. |
| Probe times out, is ambiguous, or its parser breaks | Mark `probe failed`. The optional cyber-program tier falls back to a proven default tier. If default availability is also unproven, the provider is absent. | Depends on whether the remaining eligible providers meet the complete quorum. |
| Provider becomes rate-limited after the probe | Mark `started, review failed` rather than silently substituting another provider. | Fail when the review is required. |
| Two reviewers disagree | Preserve both findings and mark the review complete but not passed. A human resolves the disagreement. | Fail until policy and the human decision allow progress. |
| Report writer or merge hook cannot verify the summary | Make no success claim and fail closed with exit code 8. | Fail. |

## Differences from the sibling project

The sibling owner-specific project requires the cyber-program tier for security review. This portable edition keeps its fail-closed and evidence properties but deliberately changes deployment assumptions.

| Property | Sibling behavior | This edition's behavior | Configuration switch | Default | Risk of changing it |
|---|---|---|---|---|---|
| Cyber-program tier | Mandatory for security review | Optional; selected only when configured and proven available, otherwise the default tier is labeled and allowed | Provider `cyber_program` | `false` | Default-tier models may have weaker security-research capability or stricter content limits. |
| Provider quorum | Requires its fixed approved security-review providers | Provider kinds and minimum distinct count are configurable; defaults still require Claude and OpenAI | `required_provider_kinds`, `minimum_distinct_provider_kinds` | Claude and OpenAI; 2 | A smaller or correlated quorum reduces independent review. |
| Local adversarial model | Not accepted for security review | Refused by default, with an explicit team opt-in | `allow_local` | `false` | Hostile input or output can attack a less isolated local serving stack, and model quality may be unknown. |
| Merge enforcement | Missing required security review blocks progress | Same by default, but a team can make adversarial review advisory | `required_for_merge` | `true` | Advisory review can be ignored while findings or missing reviewers remain. |
| Provider implementation | Owner-specific commands and access assumptions are acceptable | Core is provider-neutral; provider behavior is isolated in inspected adapters | Provider `adapter` | No implicit adapter | A faulty adapter can misclassify access or construct an unsafe launch plan. |
| Availability evidence | Approved access is a standing prerequisite | Access is re-probed for the current user and attempt; no lasting positive claim | No switch | Probe every attempt | A false positive can select an unusable tier; a false negative reduces availability. Neither may create a success claim. |
| Missing reviewer visibility | Security review cannot proceed without its required reviewers | Same: every requested reviewer has a report row and absence reason | No switch | Always on | Turning this off would create silent skips, so it is not configurable. |
| Human approval and untrusted output | Models cannot approve; output is untrusted and bounded | Same | No switch | Always on | Turning this off would let model text cross an authority boundary, so it is not configurable. |
| Read-only reviewer permissions | Security reviewers use read-only access | Same for regular and adversarial reviewers | No switch | Always on | Write access would let hostile or mistaken review output alter the subject under review. |

## Test plan

The implementation starts with these tests. Requirement numbers refer to issue #88.

| Test name | Assertion | Requirement |
|---|---|---:|
| `test_adversarial_default_requires_claude_and_openai` | One provider or two instances of one kind starts no adversarial reviewer and cannot pass. | 1 |
| `test_adversarial_configurable_distinct_provider_quorum` | A configured replacement set runs without a core change when its adapters exist. | 1, 4 |
| `test_adversarial_starts_only_after_complete_probe_quorum` | No adversarial subprocess starts until all quorum probes succeed. | 1 |
| `test_cyber_program_requires_config_and_positive_probe` | Tier is selected only when both conditions hold. | 2 |
| `test_cyber_probe_is_read_only_bounded_and_non_billable` | Probe has no prompt, inference request, login, write, or deadline above the hard bound. | 2 |
| `test_cyber_probe_failure_falls_back_to_labeled_default` | A proven default tier runs and the report labels the fallback. | 2 |
| `test_default_tier_adversarial_review_can_complete` | Teams without optional access still receive a labeled two-provider review. | 2 |
| `test_report_has_every_requested_reviewer_and_absence_reason` | All requested names appear for each fixed absence result. | 3 |
| `test_no_silent_substitution_or_retry` | Mid-review death does not invoke an unrequested provider or default-tier retry. | 3 |
| `test_role_assignments_use_named_provider_instances` | Implementer, reviewer, and adversarial roles resolve through the same provider registry. | 4 |
| `test_local_adversarial_requires_explicit_opt_in` | Local is allowed for other roles and refused for adversarial by default. | 4 |
| `test_hostile_reviewer_output_cannot_change_policy_or_approval` | Hostile text is rejected and cannot alter quorum, gate, or lifecycle. | 4 |
| `test_variation_defaults_match_portable_policy` | Optional tier, two-provider quorum, merge gate, and local refusal have the documented defaults. | 5 |
| `test_invalid_reviewer_configuration_exit_codes` | Every schema error maps to 3 and oversize maps to 7. | 1, 2, 4 |
| `test_required_incomplete_review_fails_hook_closed` | Missing, failed, or unverifiable required review maps to exit 8. | 1, 3 |
| `test_probe_lie_becomes_incomplete_review` | Successful probe followed by launch failure never yields completion. | 2, 3 |
| `test_review_summary_contains_no_raw_probe_or_hostile_output` | Report exposes fixed states only. | 3, 4 |

## Staged implementation plan

| Stage | Size | Work |
|---|---:|---|
| 1. Contracts and fixtures | M | Add configuration validation, fixed availability states, report schema, and hostile fixtures. No subprocesses. |
| 2. Adapter probe interface | M | Extend adapter metadata and plan objects; implement fake adapters first, then documented Claude, OpenAI, and local probes. |
| 3. Policy and orchestration | L | Resolve role assignments, select tiers, enforce all-or-nothing adversarial quorum, and handle mid-review failure. |
| 4. Reporting and gate | M | Produce the reviewer table, preserve separate findings, and fail the required merge hook closed. |
| 5. Security review and portability | M | Add hostile-output tests, platform tests, documentation verification, and an independent security review before enabling the feature. |

Each stage is a separate issue and pull request. Stage 2 must pin each real probe to current provider documentation and prove that it performs no inference or account mutation. Stage 5 includes a configurable-port review for the sibling project. This design belongs there only where it improves provider-neutral reporting or detection; its optional-tier policy does not replace that project's mandatory tier.

## Open questions

1. **Should `required_for_merge` be configurable?** Recommended: keep the switch for enterprise policy flexibility, default it to `true`, and require the report to show when it is disabled.
2. **Should a failed adversarial attempt retry automatically?** Recommended: no. A new explicit attempt should re-probe, preventing duplicate paid work and silent tier changes.
3. **Should regular review require a distinct provider from the implementer?** Recommended: yes in the implementation design, with a validation rule when regular review is required. Independence is weakened when the same provider implements and reviews.
4. **What exact no-cost metadata operation should each first-party adapter use?** Recommended: accept an adapter only after its operation is verified against dated provider documentation and a fixture proves it sends no prompt. If no such operation exists, report the optional tier as unavailable.
