# Getting Started with Strategic Herding of IT Tasks

## What this is

Strategic Herding of IT Tasks, or SHIT, is a small kit for working more clearly and safely with Claude, Codex, Copilot, or a local model. It provides roles, which are working modes such as implementer or reviewer, and packs, which are short topic-specific reference documents the assistant can load. This is a pilot, so its expected benefits are predictions that should be tested and measured rather than treated as proven results.

## Install in five minutes

There is no installer. Copy the generated files for your assistant into each project where you want to use them.

### Claude

Copy the generated Claude files into `[project path]`. Place the main instructions in `[Claude instruction file path]` and any roles or packs in the generated folder structure. Check Claude’s own documentation for the current instruction file location.

### Codex

Copy the generated Codex files into `[project path]`. Place the main instructions in `[Codex instruction file path]` and keep the generated roles and packs in their provided folders. Check Codex’s own documentation for the current instruction file location.

### Local model

Copy the generated local-model files into `[project path]`. Put the main instructions in `[local model instruction file path]`, or include them in the model’s initial prompt if it does not support project instruction files. Check your model runner’s documentation for its instruction file location and loading behavior.

## Your first brief

Give the assistant a five-line task brief:

1. **Goal:** Say exactly what should change.
2. **Files:** Name the files it may inspect or edit.
3. **Done:** Describe the result and the checks that must pass.
4. **Do not touch:** Set clear boundaries.
5. **Report:** Say what evidence the assistant must return.

For example:

```text
Goal: Rename the variable `tmp` to `normalized_name` without changing behavior.
Files: src/names.py and tests/test_names.py.
Done: Every use is renamed, existing tests pass, and no public function names change.
Do not touch: Dependencies, formatting outside these files, or unrelated code.
Report: List changed files, the test command, its exit code, and anything not checked.
```

Keep the brief narrow enough that you can verify the result. If the task grows, stop and write a new brief instead of silently expanding the original one.

## Your first review

Ask for a second look before anything is merged. Give the reviewer the original brief, the changed files, and the test results.

A human peer is better than having the same assistant review its own work. If you only have one assistant, start a fresh session and ask it to review the completed change without editing it. That report must be labeled **SELF-REVIEW** so nobody mistakes it for an independent check.

A good review report contains:

- What was checked
- What was not checked
- Any problems found, with file locations
- Whether the work matches the original brief
- The exact verification command that was run
- The command’s exit code
- Any remaining risk or uncertainty

A review is evidence, not permission to stop thinking. Read the report and inspect important changes yourself.

## What the guards do and do not do

The kit’s rules are habits and written instructions. Some tools can enforce some rules automatically, while others can only remind the assistant to follow them.

These rules are guardrails, not a security boundary. An assistant may misunderstand an instruction, miss a problem, or claim success without checking the real result. Nothing in this kit replaces backups, careful review, or proper handling of secrets and company data.

Never paste passwords, access tokens, private keys, or company-confidential material into a tool your employer has not approved.

## When something goes wrong

Stop the task. Tell the human what happened, what changed, and what remains uncertain. Do not work around a blocked action, disabled safeguard, denied permission, or failed check.
