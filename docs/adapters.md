# Adapters

Adapters are optional. A brief and a report header work by hand; an adapter only saves typing for a worker whose tool it knows. One adapter is chosen per worker by configuration. Code lives in `shit/adapters/`.

| Adapter | Output | Default permissions |
|---|---|---|
| `claude` | argument array, brief on standard input | plan mode (read and propose only) |
| `codex` | argument array, brief on standard input | read-only sandbox |
| `copilot` | argument array, brief as one prompt argument | no allow flag passed; tool use is denied or prompts, and unattended runs deny |
| `prompt` | plain text to copy; no command | none (the worker pastes the answer back) |

For `copilot`, standard input stays empty because the tool ignores it when a prompt argument is given. `--no-ask-user` is on its safe extra-argument list, so a configured unattended run cannot stall on a question.

Vendor flags follow each tool's command-line reference at the time of writing and have not been run in this repository; check them with the tool's `--help` before relying on them.

## What an adapter does

- `prepare(brief_text, config=None, override=False)` validates the brief with the brief contract, then returns a plan: prompt, argument array (or none), standard input, permissions, files, allowed environment names, timeout and any override. `Plan.describe()` is the text for `run --dry-run`.
- `parse_output(text)` finds the report header in an answer (a pasted one included), bounds it and validates it with the report-header contract.
- `adapters.doctor()` describes every adapter: name, what it needs, what it never does.

## What an adapter never does

It never starts a process, opens a network connection, writes a file, or reads the environment. The brief text reaches the tool as data (standard input or one array element), never as part of a shell string. Running the plan is the worker's act, or the task command's, which must use the argument array without a shell, pass only `clean_env(os.environ)`, and enforce `plan.timeout`.

## Limits and refusals

| Case | Exit code |
|---|---:|
| Unknown adapter, unknown configuration key, malformed argument, timeout out of range | 2 |
| Brief or header rejected by its contract, no header in the answer | 3 |
| Brief over 64 KiB, answer over 256 KiB | 7 |
| Extra argument that is not on the adapter's safe list, without an explicit per-run override | 10 |

Hostile characters (control, bidirectional override, zero-width) are refused in the brief and the answer. An override allows only a printable-ASCII argument of at most 200 characters, at most eight of them, and is listed in the plan so the caller can log it. Errors never echo the rejected value.
