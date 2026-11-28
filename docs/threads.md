# Review threads

`shit threads` and `scripts/threads_run.py` list the open review threads of a pull request and answer them from a file: a reply by REST, a resolve by one GraphQL mutation per thread. They work on any repository and pull request; both are arguments. All traffic goes through the GitHub CLI (`gh`), which holds the credential: nothing here reads, passes on or prints a token, and the output of `gh` is never shown.

This is the configurable port of the SAFO tool `safo-threads` (SAFO issue 587, SAFO commit `b499b33`; SHIT issue 55). Nothing in it is specific to one maintainer.

## Two parts, and why

1. **`shit threads` (inside the package) is a plan builder.** It checks its inputs and prints the exact `gh api` requests as one line of JSON. It never starts a process, opens a socket or reads the environment.
2. **`scripts/threads_run.py` (outside the package) is the launcher.** It reads a plan and runs it with `gh`.

The launcher is outside the `shit` package because SHIT forbids any module of the package from importing a process or network module (`docs/hostile-fixtures.md`, H-29d, enforced by `tests/test_hostile.py`). That rule keeps the wrapper itself from launching anything. The one place that does is a short script you can read in full, and **running it is your own step**: `shit` never calls it. Nothing in the package imports or starts it, and the plan is data you can read before you run anything.

## Use

```
shit threads [--config PATH] list  OWNER/NAME PR                      > plan.json
shit threads [--config PATH] apply OWNER/NAME PR DISPOSITIONS.json    > plan.json
python3 scripts/threads_run.py [--config PATH] plan.json
```

Run the launcher from the directory that holds `plan.json`; the plan file, the dispositions file and the configuration file must be regular files below the working directory (a path outside it, a symlink, a FIFO and a hard-linked file are refused).

`--config` names a project configuration file (`templates/config.toml`). Its optional `[tracker] host` is the host name of the code host, for a self-hosted server; it is carried in the plan for reading, and it is the only host the launcher will talk to (see below). Without `--config`, or without a `host` key, the host is `github.com`. Pass the same `--config` to `shit threads` and to the launcher.

### The plan

One JSON object: `schema`, `schema_version`, `kind` (`list` or `apply`), `host`, `repo`, `pr`, and `requests`, in this order: the thread read, then every reply, then every resolve (in the order of the dispositions file). Each request has `op`, `method`, `endpoint`, `query` (graphql only), `comment_id` and `fields`. A plan holds no token. The launcher does not trust the file: it accepts a plan only if it equals, key for key, the plan `shit threads` would build for the same repository, pull request and bodies, so a hand-edited endpoint, query or extra key is refused with exit code 3 before any call.

### list

The launcher reads the unresolved, current threads (50 a page, at most 20 pages; more is refused, not cut) and prints them grouped by file, one line each: the first comment's id, `file:line`, the author and the first 200 characters of the body. Resolved and outdated threads are dropped. Whitespace becomes one space and every control, bidi, zero-width or separator character is written as a `\u` escape, so a comment cannot start a workflow command or move the cursor.

### apply

The dispositions file is a JSON list; each row is exactly `comment_id` (the id `list` printed), `action` and, unless the action is `skip`, `body` (a `skip` row must not have one):

```json
[
  {"comment_id": 1001, "action": "reply_resolve", "body": "Fixed in the next commit."},
  {"comment_id": 1002, "action": "reply", "body": "Keeping this on purpose; see the note in the docs."},
  {"comment_id": 1003, "action": "skip"}
]
```

- `reply` posts the body as a reply (`POST repos/OWNER/NAME/pulls/PR/comments/ID/replies`).
- `reply_resolve` posts the reply, then resolves that thread with one `resolveReviewThread` mutation.
- `skip` does nothing and is not in the plan.

**Refused when the plan is built** (`shit threads apply`, nothing printed): the file is read as strict JSON (at most 256 KiB, nesting at most 4 levels; duplicate keys, `NaN`, a byte order mark, invalid UTF-8 and lone surrogates refused). It must be a non-empty list of at most 200 rows; a row with another key, a `comment_id` that is not a positive integer or is repeated, an unknown `action`, an empty body, a body over 2000 characters or one holding a control, bidi or zero-width character (a line feed is allowed), or a body on a `skip` row is refused.

**Refused by the launcher before the first write:** a plan that does not validate, and any replied `comment_id` that is not an unresolved, current thread of that pull request (checked against the live read). A `skip` row is not checked against the live list.

**After the first write:** one call at a time. A reply is never retried (a POST of unknown outcome could double-post). Resolves run only after every reply was accepted, and a resolve counts only when the answer says `isResolved` is true and carries no errors. The first failure stops the run and prints how many writes were done; if replies were posted but a resolve failed, resolve that thread by hand or put it in a new file as `skip`.

### What the launcher passes to `gh`

**Host and credential.** A plan file is data anyone may have edited, and the credential goes to whatever host a call names, so the plan never chooses the host. The launcher takes the trusted host only from an explicit `--config PATH` (its `[tracker] host`), otherwise `github.com`. It never reads a configuration file by default, so a file left in the working directory cannot choose the host that receives your token. **A GitHub Enterprise or other self-hosted host needs `--config`**; without it only `github.com` is used. A plan whose `host` is null uses the trusted host; a plan naming a different host is refused before any call with exit code 3 (the message names no host). A `--config` file that is unreadable is refused too (3, 5 or 9). Every call carries `--hostname HOST` for the trusted host.

**Call rules.** No shell (an argument list), stdin closed, a 60 second timeout per call, standard output and standard error captured and never shown. The environment is an explicit allowlist (`ENV_ALLOW` in the script): `PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`, `TZ`, `USER`, `TMPDIR`, `GH_CONFIG_DIR`, the `XDG_*` directories, and the proxy and certificate variables `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` (and lowercase forms), `SSL_CERT_FILE`, `SSL_CERT_DIR`. It adds `GH_PROMPT_DISABLED=1` and `NO_COLOR=1`. Only the token variables that apply to the trusted host are passed, never both pairs: `GH_TOKEN` and `GITHUB_TOKEN` for `github.com`; `GH_ENTERPRISE_TOKEN` and `GITHUB_ENTERPRISE_TOKEN` for any other host. `GH_HOST` is not passed. Every other variable is dropped, and a value with a control character is dropped. The tokens are the user's own credential, passed untouched so `gh` can authenticate; the launcher never reads, prints or logs their values. If you sign in with `gh auth login`, no token variable is needed.

## Exit codes

The codes are the shared table in [exit-codes.md](exit-codes.md); no new code is added. The same codes apply to both parts.

| Code | Meaning for `threads` |
|---:|---|
| 0 | Done. |
| 2 | Arguments are invalid: bad repository name or number, or an unknown option. |
| 3 | The dispositions file, the configuration or the plan was rejected, including a plan naming a host other than the configured one. |
| 5 | A dispositions, configuration or plan path violated confinement. |
| 7 | An input or an answer from `gh` exceeded a size or depth limit. |
| 8 | GitHub did not answer as required. Before any write this is a failed read (nothing was written). After the first write the printed lines show what was done and where it stopped. |
| 9 | A file or the configuration was not found, or a replied or resolved `comment_id` is not an unresolved thread. Nothing was written. |
| 11 | `gh` is missing, cannot start, or a call timed out (60 seconds each). |

Tests: `tests/test_threads.py` (plan builder, validation, helpers) and `tests/test_threads_run.py` (the launcher, against a fake `gh` executable on a temporary `PATH`). No test touches the network or a real credential.
