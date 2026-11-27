"""Render and validate a work brief (templates/brief.md).

A brief is plain text. Each field is one line at the start of a line, `key: value`, lowercase key. A
person can fill it by hand; no tool or model is needed. Fields with a list (scope, acceptance,
constraints) repeat the key, one item per line. Everything else about the text is free prose and is
ignored: headings, blank lines, bullets, indented lines, fenced code and HTML comments never count.

A line that looks like `lowercase_key: value` but names no known field is rejected, so a typo cannot
silently drop a field. Limits match docs/schemas.md (the brief record). Errors carry a stable exit
code (docs/exit-codes.md) and never echo the rejected value.

    python3 -c "from shit import brief; print(brief.read('brief.md'))"
"""

import re

from shit._text import check_size, check_text, decode, read_bounded
from shit._text import check_value as _check_value
from shit.errors import INPUT_LIMIT, ContractError, rejected

MAX_BYTES = 65536

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_ISSUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}#[1-9][0-9]{0,8}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+ -]{0,198}")
_LINE = re.compile(r"([a-z][a-z_]*): ?(.*)")


def _one(pattern):
    return lambda v: pattern.fullmatch(v) is not None


def _any(_v):
    return True


# key -> (required, maximum characters, extra check). Single-valued fields.
SINGLE = {
    "issue": (True, 200, _one(_ISSUE)),
    "role": (True, 64, _one(_SLUG)),
    "model": (False, 200, _one(_MODEL)),
    "packs": (True, 1000, _any),
    "objective": (True, 4000, _any),
    "data_class": (True, 500, _any),
    "cleanup_duty": (True, 1000, _any),
    "evidence": (True, 1000, _any),
    "review_destination": (True, 500, _any),
}
# key -> (minimum items, maximum characters per item). Repeated fields.
MULTI = {
    "scope": (1, 500),
    "acceptance": (1, 500),
    "constraints": (0, 500),
}
ORDER = ["issue", "role", "model", "packs", "objective", "scope", "constraints", "acceptance", "data_class",
         "cleanup_duty", "evidence", "review_destination"]


def _parse_packs(value):
    if value == "none":
        return []
    names = [p.strip() for p in value.split(",")]
    for n in names:
        if not _SLUG.fullmatch(n):
            raise rejected("packs: names must be lowercase slugs, comma separated, or none")
    if len(set(names)) != len(names):
        raise rejected("packs: duplicate name")
    return names


def _validate(fields):
    """Check a dict of field values (lists for packs and the multi fields) and return a clean copy."""
    if not isinstance(fields, dict):
        raise rejected("brief must be a mapping")
    for key in fields:
        if key not in SINGLE and key not in MULTI:
            raise rejected("unknown field")
    out = {}
    for key, (required, limit, check) in SINGLE.items():
        if key not in fields:
            if required:
                raise rejected(f"{key}: required")
            continue
        value = fields[key]
        if key == "packs":
            if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
                raise rejected("packs: must be a list of names")
            for name in value:
                if not _SLUG.fullmatch(name):
                    raise rejected("packs: names must be lowercase slugs")
            line = ", ".join(value) if value else "none"
            _check_value(key, line, limit)
            out[key] = _parse_packs(line)
            continue
        _check_value(key, value, limit)
        if not check(value):
            raise rejected(f"{key}: malformed")
        out[key] = value
    for key, (minimum, limit) in MULTI.items():
        items = fields.get(key, [])
        if not isinstance(items, list):
            raise rejected(f"{key}: must be a list")
        if len(items) < minimum:
            raise rejected(f"{key}: required")
        for item in items:
            _check_value(key, item, limit)
        out[key] = list(items)
    return out


def parse(text):
    """Validate brief text and return its fields. Raises ContractError."""
    if not isinstance(text, str):
        raise rejected("input must be text")
    check_size(text, MAX_BYTES)
    check_text(text)
    if "<!--" in re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL):
        raise rejected("unterminated comment")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    raw = {}
    fenced = False
    for line in text.split("\n"):
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _LINE.fullmatch(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key not in SINGLE and key not in MULTI:
            raise rejected("unknown field")
        if key in MULTI:
            raw.setdefault(key, []).append(value)
        elif key in raw:
            raise rejected(f"{key}: duplicate")
        else:
            raw[key] = value
    if "packs" in raw:
        _check_value("packs", raw["packs"], SINGLE["packs"][1])
        raw["packs"] = _parse_packs(raw["packs"])
    return _validate(raw)


def render(fields):
    """Validate fields and return brief text that parse() accepts. Raises ContractError."""
    clean = _validate(fields)
    lines = ["# Work brief", ""]
    for key in ORDER:
        if key in MULTI:
            lines.extend(f"{key}: {item}" for item in clean[key])
        elif key == "packs":
            lines.append("packs: " + (", ".join(clean[key]) if clean[key] else "none"))
        elif key in clean:
            lines.append(f"{key}: {clean[key]}")
    text = "\n".join(lines) + "\n"
    parse(text)  # the output must satisfy the reader, or nothing is returned
    return text


def read(path):
    """Read a brief file (at most 64 KiB, regular file only) and validate it."""
    data = read_bounded(path, MAX_BYTES)
    if len(data) > MAX_BYTES:
        raise ContractError(INPUT_LIMIT, "input exceeds limit")
    return parse(decode(data))
