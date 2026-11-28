"""Validate the project configuration (templates/config.toml).

The file is TOML, parsed with the standard library (tomllib). Every key is named here; an unknown key,
an unknown table, a wrong type, a duplicate key, a placeholder left unfilled or a missing required key is
rejected with exit code 3. A file over the byte limit is rejected with exit code 7 before it is parsed
(docs/exit-codes.md). Missing `schema_version` or any other `schema_version` than 1 is rejected.

Secure defaults: privacy scanning is on when the key is absent, and the storage root and log file must
stay under the fixed `.shit` root. A context window other than 32768 (32K default) or 65536 (64K opt-in)
is rejected. Errors are fixed strings and never echo the rejected value.

    python3 -c "from shit import config; print(config.read('config.toml'))"
"""

import re
import tomllib

from shit._text import check_size, check_text, decode, has_placeholder, read_bounded
from shit.errors import rejected

MAX_BYTES = 8192

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
# Host name of the code host (for example a self-hosted GitHub Enterprise), no scheme, no port, no path.
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?){0,8}")
_LOG = re.compile(r"\.shit/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){0,3}")
_CONTEXT_WINDOWS = (32768, 65536)
_MAX_TEXT = 500
_MAX_ROLES = 16
_MAX_LIMIT = 10_000_000
_MODES = ("warn", "fail")

# Event kinds the yardstick report (shit/yardstick.py) can weight, in docs/schemas.md event order.
WEIGHT_KEYS = ("task_created", "state_transition", "brief_recorded", "report_recorded", "decision_recorded",
               "evidence_recorded", "review_recorded", "command_failed")
_MAX_WEIGHT = 1000

# table -> {key: kind}. Kinds: "int1" exact schema version, "text" bounded string, "slug" lowercase slug,
# "repo" owner/repo reference, "host" code host name, "log" path under .shit, "root" the fixed storage root, "bool", "window"
# context size in tokens, "slugs" list of slugs, "weight" integer 0 to 1000, "limit" positive int up to 10 million,
# "mode" warn or fail. The top level has only schema_version.
SCHEMA = {
    "": {"schema_version": "int1"},
    "project": {"name": "slug"},
    "tracker": {"repository": "repo", "host": "host"},
    "roles": {"names": "slugs"},
    "review": {"destination": "text"},
    "storage": {"root": "root"},
    "log": {"file": "log"},
    "privacy": {"scan": "bool"},
    "local_model": {"context_tokens": "window"},
    "limits": {"instruction_max_bytes": "limit", "listed_roles_max": "limit", "on_exceed": "mode"},
    "yardstick": {name: "weight" for name in WEIGHT_KEYS},
}
REQUIRED = {
    "": ["schema_version"],
    "project": ["name"],
    "tracker": ["repository"],
    "review": ["destination"],
    "storage": ["root"],
    "log": ["file"],
    "local_model": ["context_tokens"],
}
# Defaults applied when an optional key is absent. Privacy scanning fails closed: absent means on.
DEFAULTS = {
    "roles": {"names": ["implementer", "reviewer", "researcher", "planner"]},
    "privacy": {"scan": True},
    "limits": {"on_exceed": "warn"},
    "yardstick": {name: 1 for name in WEIGHT_KEYS},
}


def _check_str(value):
    if not isinstance(value, str):
        raise rejected("value must be text")
    check_text(value)
    if not value or value != value.strip():
        raise rejected("value empty or padded")
    if len(value) > _MAX_TEXT:
        raise rejected("value too long")
    if has_placeholder(value):
        raise rejected("unfilled placeholder")


def _check_value(kind, value):
    if kind == "int1":
        if type(value) is not int or value != 1:
            raise rejected("schema_version: must be the integer 1")
        return value
    if kind == "bool":
        if type(value) is not bool:
            raise rejected("value must be true or false")
        return value
    if kind == "window":
        if type(value) is not int or value not in _CONTEXT_WINDOWS:
            raise rejected("context_tokens: must be 32768 or 65536")
        return value
    if kind == "weight":
        if type(value) is not int or not 0 <= value <= _MAX_WEIGHT:
            raise rejected("yardstick: weight must be an integer from 0 to 1000")
        return value
    if kind == "limit":
        if type(value) is not int or not 1 <= value <= _MAX_LIMIT:
            raise rejected("limits: must be an integer from 1 to 10000000")
        return value
    if kind == "mode":
        if value not in _MODES:
            raise rejected("limits: on_exceed must be warn or fail")
        return value
    if kind == "slugs":
        if not isinstance(value, list) or not value or len(value) > _MAX_ROLES:
            raise rejected("roles: must be a non-empty list of names")
        for item in value:
            if not isinstance(item, str) or not _SLUG.fullmatch(item):
                raise rejected("roles: names must be lowercase slugs")
        if len(set(value)) != len(value):
            raise rejected("roles: duplicate name")
        return list(value)
    _check_str(value)
    if kind == "slug" and not _SLUG.fullmatch(value):
        raise rejected("name must be a lowercase slug")
    if kind == "repo" and not _REPO.fullmatch(value):
        raise rejected("tracker: repository must be owner/repo")
    if kind == "host" and (not _HOST.fullmatch(value) or len(value) > 253):
        raise rejected("tracker: host must be a host name")
    if kind == "root" and value != ".shit":
        raise rejected("storage: root must be .shit")
    if kind == "log" and (not _LOG.fullmatch(value) or ".." in value.split("/")):
        raise rejected("log: file must stay under .shit")
    return value


def parse(text):
    """Validate configuration text and return a dict of tables. Raises ContractError."""
    if not isinstance(text, str):
        raise rejected("input must be text")
    check_size(text, MAX_BYTES)
    check_text(text)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise rejected("configuration is not valid TOML") from None

    for name, value in raw.items():
        if name in SCHEMA:
            if not isinstance(value, dict):
                raise rejected(f"{name}: must be a table")
        elif name not in SCHEMA[""]:
            raise rejected("unknown key or table")
    for table, keys in SCHEMA.items():
        section = raw if table == "" else raw.get(table, {})
        for key in section:
            if table == "" and key in SCHEMA:
                continue
            if key not in keys:
                raise rejected("unknown key")

    out = {}
    for table, keys in SCHEMA.items():
        section = raw if table == "" else raw.get(table, {})
        clean = {}
        for key, kind in keys.items():
            if key not in section:
                if key in REQUIRED.get(table, []):
                    raise rejected(f"{key}: required")
                if key in DEFAULTS.get(table, {}):
                    clean[key] = DEFAULTS[table][key]
                continue
            clean[key] = _check_value(kind, section[key])
        if table == "":
            out.update(clean)
        else:
            out[table] = clean
    for table, defaults in DEFAULTS.items():
        if table not in out:
            out[table] = dict(defaults)
    return out


def read(path, base=None):
    """Read at most MAX_BYTES + 1 bytes from a regular file (no symlink, no FIFO) and validate it."""
    data = read_bounded(path, MAX_BYTES, base)
    return parse(decode(data))
