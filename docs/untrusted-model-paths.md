# Untrusted model paths

The model-path registry is a code-built map of every function in `shit/` that can send untrusted text to a
model. Untrusted text includes issue text, comments, logs, web pages, continuous-integration output, files
another person can write, and a previous model's output.

The registry is `MODEL_PATHS` in `shit/model_paths.py`. It is currently empty. This edition has adapters that
validate text and prepare command plans, but code under `shit/` does not start those commands, call a model
service, or open a network connection. Preparing an argument array is not a model call.

## Register a path

When code adds a model call, add its `ModelPath` row in the same change. Use the exact Python module and
function that contain the call. Give the row a stable ID and record:

- what triggers the call;
- every untrusted input that can reach it;
- the provider-neutral kind of model;
- the guard that enforces confinement;
- whether the path is `CLOSED` or `OPEN`, with the reason.

The scanner in `tests/test_model_paths.py` parses every Python file under `shit/` without importing or
executing it. It recognizes network and process primitives and call targets with common model-client names.
The test fails when a scanned module and function lacks a registry row or when a registry row has no scanned
call site. Fixture tests prove that calls are detected while comments and string literals are ignored.

## Privilege rules

Every registered model call must set `tools`, `write`, and `secrets` to `False`. The guard must be nonempty and
must name the code or policy that enforces those restrictions. A model call may receive only the bounded text
needed for its task. It may not inherit credentials, gain a write-capable sandbox, or receive tool access.

`CLOSED` means the guard is implemented and verified. `OPEN` records a known gap and must still keep all three
privilege fields false. An open path is not permission to run with broader privileges.

## Relationship to the sibling project

This edition is configurable and provider-neutral. The sibling project's registry describes owner-specific
paths and controls, so its table is not copied here. Portable behavior can be brought into this registry only
when its inputs, model kind, and guards are expressed without private environment details.
