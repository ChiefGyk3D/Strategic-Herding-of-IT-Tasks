# Review threads

`shit threads` lists the open review threads of a pull request and answers them from a file: a reply by REST, a resolve by one GraphQL mutation per thread. It works on any repository and pull request; both are arguments. All traffic goes through the GitHub CLI (`gh`), which holds the credential: the tool never reads, passes or prints a token, and never shows the output of `gh`.

This is the configurable port of the SAFO tool `safo-threads` (SAFO issue 587, SAFO commit `b499b33`; SHIT issue 55). Nothing in it is specific to one maintainer.

## Configuration

```
shit threads [--config PATH] list OWNER/NAME PR
shit threads [--config PATH] apply OWNER/NAME PR DISPOSITIONS.json
```

`--config` names a project configuration file (`templates/config.toml`). Its optional `[tracker] host` is the host name of the code host, for a self-hosted server; it is passed to every call as `gh api --hostname HOST`. Without `--config`, or without a `host` key, `gh` uses its own default host. The host is validated by the configuration contract (a plain host name, no scheme, port or path). The configuration file and the dispositions file are read from below the working directory only: a path outside it, a symlink, a FIFO and a hard-linked file are refused.

## list

Prints the unresolved, current threads grouped by file, one line per thread: the first comment's id, `file:line`, the author and the first 200 characters of the body. Resolved and outdated threads are dropped. Whitespace becomes one space and every control, bidi, zero-width or separator character is written as a `\u` escape, so a comment cannot start a workflow command or move the cursor.

## apply

The file is a JSON list; each row is exactly `comment_id` (the id `list` printed), `action` and, unless the action is `skip`, `body` (a `skip` row must not have one):

```json
[
  {"comment_id": 1001, "action": "reply_resolve", "body": "Fixed in the next commit."},
  {"comment_id": 1002, "action": "reply", "body": "Keeping this on purpose; see the note in the docs."},
  {"comment_id": 1003, "action": "skip"}
]
```

- `reply` posts the body as a reply (`POST repos/OWNER/NAME/pulls/PR/comments/ID/replies`).
- `reply_resolve` posts the reply, then resolves that thread with one `resolveReviewThread` mutation.
- `skip` does nothing.

### Refused before any write

The file is read as strict JSON (at most 256 KiB, nesting at most 4 levels; duplicate keys, `NaN`, a byte order mark, invalid UTF-8 and lone surrogates refused). It is refused when it is not a non-empty list of at most 200 rows, a row has another key, a `comment_id` is not a positive integer or is repeated, an `action` is unknown, a body is empty, over 2000 characters, or holds a control, bidi or zero-width character (a line feed is allowed), or a body is given on a `skip` row. Every `comment_id` must be an unresolved, current thread of that pull request (the same set `list` shows, read with a query of 50 threads a page, at most 20 pages; more is refused, not cut). A bad row anywhere means nothing is written.

### After the first write

Writes run one at a time. The first failure stops the run and the output says how many rows were done. A reply is never retried (a POST of unknown outcome could double-post), and a thread is resolved only after its reply was accepted. If the reply posted but the answer to the resolve does not say `isResolved` is true, the output says so: resolve that thread by hand or put it in a new file as `skip` later.

## Exit codes

The codes are the shared table in [exit-codes.md](exit-codes.md); no new code is added.

| Code | Meaning for `threads` |
|---:|---|
| 0 | Done. |
| 2 | Arguments are invalid: bad repository name or number, or an unknown option. |
| 3 | The dispositions file or the configuration was rejected. Nothing was written. |
| 5 | The dispositions file or the configuration path violated confinement. Nothing was written. |
| 7 | The dispositions file or an answer from `gh` exceeded a size or depth limit. |
| 8 | GitHub did not answer as required. Before any write this is a failed read (nothing was written). After the first write the printed lines show what was done and where it stopped. |
| 9 | The dispositions file or the configuration was not found, or a `comment_id` is not an unresolved thread of the pull request. Nothing was written. |
| 11 | `gh` is missing, cannot start, or a call timed out (60 seconds each). |

Each `gh` call has stdin closed, no shell and a 60 second timeout. Tests: `tests/test_threads.py`, against a fake runner; no test touches the network.
