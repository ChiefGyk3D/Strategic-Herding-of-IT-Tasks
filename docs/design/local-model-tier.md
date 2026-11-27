# Design: local model tier

Issue: ChiefGyk3D/Strategic-Herding-of-IT-Tasks#25 (epic #1)
Status: DRAFT for owner review. No measurements have been run for this document.

## 1. Purpose and scope

The local model tier is a self-hosted model that handles text-in, text-out work
(summaries, triage, drafts, skeletons) before any external model is used. It is a
built-in differentiator of the portable edition, and it must be used far more than
SAFO uses its local tier today.

This document covers:

- which tasks route to the local tier first;
- how the context window is configured and measured;
- the truncation guard and input caps;
- the hardware-class choice (owner decision);
- the self-hosted setup and the fallback tiers;
- the local-share metric per project;
- the measurement protocol and acceptance criteria.

Out of scope: model benchmarks from vendors, and any claim about hardware that has
not been measured on the owner's machines.

Evidence labels used below:

- MEASURED: observed in a run, with the run recorded. None exist for this document yet.
- SOURCE: taken from the issue text or from SAFO issues 484, 488 and 498 as quoted
  in issue #25. Those issues were not re-read for this draft.
- INFERRED: a design judgment, not checked against a vendor document.

No vendor behavior is stated as fact here. Any vendor behavior (Ollama context
options, model limits) must be read from the vendor docs, with the read date, before
it is used in a brief or in public text.

## 2. Owner requirement and status

- Owner requirement (2026-11-26, issue #25): the local tier is a differentiator and
  must be used far more than SAFO uses it (SOURCE).
- Direction: self-hosted Ollama is the likely runtime; the same gemma4 model family,
  run at a smaller context window of 32K or 64K instead of 128K (SOURCE).
- Status: design only. The hardware class and the measurements depend on owner
  hardware and are not started (SOURCE, issue #25 and SHIT plan).

## 3. Task routing

Routing rule: a task goes to the local tier first only when its output is checked
before use. The local model drafts; a deterministic check or a human accepts. Routes
that skip the check are not allowed.

| Task | Route | Basis |
|---|---|---|
| PR summary | local draft, then check | SOURCE (issue #25, item 1) |
| Issue triage | local draft, then check | SOURCE |
| Issue draft | local draft, then check | SOURCE |
| Doc skeleton | local draft, then check | SOURCE |
| Commit message | external or human only | SOURCE: not routed |
| CI digest | external or human only | SOURCE: not routed |

INFERRED: commit messages and CI digests are excluded because they are short,
machine-consumed, and easy to get subtly wrong, so a local draft saves little and
the check costs as much as the draft. Owner to confirm.

Each routed task names its check in the routing table (for example: schema check on
triage labels, link check on PR summary references). A task without a named check
cannot be routed.

## 4. Context size as a configuration value

Rules:

- The context window is a config value per deployment. It is never hard-coded in the
  code, the prompts, or the docs.
- A deployment declares the window it loaded. The tool reads that value and never
  assumes a default size.
- The reference deployment in SAFO uses a configured 128K (131072) window (SOURCE).
  The portable edition's default profile is 32K and 64K is an opt-in profile
  (section 6, DECIDED 2026-11-27). Neither is a recall promise until its matrix row
  is measured.

Measurement matrix (protocol only; NOT RUN):

| Window | Input sizes | Recall at end of input | Recall at start of input | Speed | Memory |
|---|---|---|---|---|---|
| 16K | nominal sweep | to measure | to measure | to measure | to measure |
| 32K | nominal sweep | to measure | to measure | to measure | to measure |
| 64K | nominal sweep | to measure | to measure | to measure | to measure |
| near 100K | nominal sweep | to measure | to measure | to measure | to measure |

Prior evidence (SOURCE, per issue #25): intact recall was measured only to about
8k nominal tokens in a prior run, plus one pass at 28.9k. No size above that is
promised until the matrix above is run. The portable edition promises only what the
matrix shows, per window.

Recall probe design (INFERRED): plant a unique fact at the start, middle and end of
a filler document of the target size, ask for each fact, and score exact matches.
Repeat with at least three seeds. The probe must not include owner identifiers or
real private data.

## 5. Truncation guard and input caps (from day one)

- Every call to the local tier computes the input token count before sending.
- If the count is above the declared window, the call fails closed. The tool must
  not send the prompt, because the server can keep only the tail of a prompt that
  exceeds its loaded window and discard the start without an error (SOURCE, issue
  #25, item 3).
- Per-model input caps are declared in the same config as the window. The cap is the
  lower of the model limit and the loaded window minus an output reserve (INFERRED).
- After each call, the tool compares the server's reported prompt token count with
  its own count. A mismatch is logged as a guard failure and the output is not used.

Test requirement: a hostile-input fixture with an over-window prompt must be refused,
with the refusal visible in the log.

## 6. Hardware class: DECIDED (32K default, 64K opt-in)

Status: DECIDED, 2026-11-27. Owner answer, multiple-choice, to the question on the
window for the portable edition: "32K default, 64K opt-in (Recommended)".

Decision:
- Default profile for the shared-machine profile: 32K tokens.
- Opt-in profile for capable hardware: 64K tokens. Documented, not the default.
- The window stays a config value per deployment (section 4). Neither profile is
  promised for recall until its matrix row in section 4 is measured. The 32K default
  is a configuration choice; it is not a recall claim.

The options below are kept as the record of what was weighed.

Option A: 32K window.
- For: smaller memory footprint; more machines qualify; lower latency per call
  (INFERRED).
- Against: fewer routed tasks fit whole (for example, long PR diffs); more chunking
  logic is needed; recall at this size is not yet measured (MEASURED: none).

Option B: 64K window.
- For: more of a PR or an issue thread fits in one call; less chunking; closer to the
  reference deployment (SOURCE: reference is 128K).
- Against: higher memory need, so fewer self-hosted machines qualify; slower per call
  (INFERRED); recall not yet measured (MEASURED: none).

Recommendation (INFERRED, adopted by the owner on 2026-11-27, see the decision above):
plan the code for both, with the window as config. Make the portable edition's
default profile 32K, and make 64K a documented opt-in profile, because the
self-hosted audience is small teams on ordinary hardware. The recall evidence for
the routed tasks at 32K still comes from the matrix in section 4; until that row is
measured, no recall level is promised at 32K.

## 7. Self-hosted setup for a small team, and honest tiers

Tiers, in the order a task falls through them:

1. Primary local model at the owner-chosen window.
2. Smaller fallback model, only when the primary is not present. Its window is
   declared in config and is subject to the same guard in section 5 (INFERRED).
3. No local model. The task goes to the external route if the task is routable
   externally, or waits for a human. The tool reports the tier it actually used.

Honest-tier rule: the output records which tier produced it. A fallback draft is
labeled as such. The tool never reports a smaller model's output as the primary's.

Local-model absence is reported to the owner at once, not worked around silently.
This follows the owner's runner and local-LLM rules (CLAUDE.md, the "Local LLM not
seen" and "Runner broken" rules).

Setup scope for a small team without a GPU server: a single machine, documented
steps, no shared server required (INFERRED). Actual install steps must be read from
the runtime's docs with the read date before they are written for users.

## 8. Local-share metric per project

- Each routed call records: project, task type, tier used, and whether the check
  accepted the output.
- Local share = routed calls accepted at tier 1 or 2, divided by all calls for the
  task types in section 3, per project per period.
- Stored per project, not as a global aggregate, so the savings are measured per
  repo (INFERRED).
- The metric is shown in the project's status output. No content of prompts or
  outputs is stored in the metric, only counts (INFERRED).

## 9. Measurement plan and acceptance criteria

Protocol:

1. Run the matrix in section 4 at 16K, 32K, 64K and near 100K on the owner's hardware.
2. Record every run with its window, model tag, seed, input size, scores and timing.
   Raw runs stay outside the repo (see the repo's `.gitignore` rules for local
   evidence).
3. Run the routed-task set from section 3 at the chosen window. Score each output
   against its named check.

Acceptance (INFERRED, owner to confirm the numbers):

- Intact recall at the chosen window, for the routed-task inputs, meets the bar the
  owner sets (a threshold is not chosen in this draft).
- The truncation guard refuses every over-window hostile fixture (section 5).
- The local-share metric is produced for at least one real project run.

## 10. Owner decisions needed

1. DECIDED (2026-11-27): 32K default, 64K opt-in. Owner words: "32K default, 64K
   opt-in (Recommended)". Record in section 6.
2. Model family: confirm gemma4 as the primary family, or name another. OWNER DECISION.
3. Routing exclusions: confirm that commit messages and CI digests stay external or
   human only (section 3). OWNER DECISION.
4. Recall acceptance bar: the threshold for intact recall that a window must meet.
   OWNER DECISION.
5. Measurement hardware: which machine runs the matrix, and whether the run is allowed
   to use the owner's local server. OWNER DECISION.
6. Metric visibility: whether the local-share metric is shown only in private status
   output or also in public project pages. OWNER DECISION.

## 11. Risks

- Silent tail truncation past the loaded window (section 5). Mitigation: fail closed
  and compare counts.
- A window chosen before measurement overpromises recall. Mitigation: no size is
  promised until its matrix row is measured (section 4).
- Near-100K windows may not fit consumer memory (INFERRED). Mitigation: the matrix
  records memory, and near-100K is not offered as a profile until measured.
- Fallback model output mistaken for primary output. Mitigation: honest-tier record
  (section 7).
- Check cost equals draft cost for short tasks. Mitigation: the routing table names
  the check per task, and exclusions follow from it (section 3).
