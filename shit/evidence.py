"""Validate the header at the top of a report (templates/report.md).

The header is the first block of lines, ended by the first blank line; everything after it is free text
and is not read. Each field is `LABEL: value`. ROLE and MODEL share a line, and so do HEAD and BASE,
separated by two or more spaces. A person can fill it by hand: ROLE may be `human`, MODEL and
LOCAL-LLM may say `n/a - <reason>`. Nothing here is specific to any tool or vendor.

Reads are bounded (16 KiB). A field longer than 500 characters, an unknown or duplicate label, a
malformed value, an unfilled `<placeholder>` or a missing field is rejected with exit code 3; a header
over the byte limit is rejected with exit code 7 (docs/exit-codes.md). Errors never echo the value.
"""

import re

from shit._text import check_text, decode, has_placeholder, read_bounded
from shit.errors import INPUT_LIMIT, ContractError, rejected

MAX_BYTES = 16384
MAX_LINES = 64
MAX_VALUE = 500

LABELS = ["ROLE", "MODEL", "HEAD", "BASE", "GATE", "OTHER GATES", "PACKS LOADED", "TREE", "NOT VERIFIED",
          "LOCAL-LLM"]
REPEATABLE = {"OTHER GATES", "NOT VERIFIED"}

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_REASON = re.compile(r"(not-run|n-a) - \S.*")
_GATE_RAN = re.compile(r"(\S.*?)\s{2,}exit=(\d{1,3})")
_OTHER_GATE = re.compile(r"[^:\n]{1,100}: (?:(?:pass|fail)(?: - \S.*)?|(?:not-run|n-a) - \S.*)")
_TREE = re.compile(r"(?:clean|NOT CLEAN) \(\S.*\)|n-a - \S.*")
_LLM = re.compile(r"n/a - \S.*|used - \S.*; VERIFIED-AGAINST: \S.*; DISCREPANCIES: \d+")
_PAIR_LINES = {
    "ROLE": re.compile(r"ROLE: (.*?)\s{2,}MODEL: (.*)"),
    "HEAD": re.compile(r"HEAD: (.*?)\s{2,}BASE: (.*)"),
}
_PAIR_LABELS = {"ROLE": ("ROLE", "MODEL"), "HEAD": ("HEAD", "BASE")}
_LINE = re.compile(r"([A-Z][A-Z -]*[A-Z]): (.*)")
_SINGLE_LINE = set(LABELS) - {"ROLE", "MODEL", "HEAD", "BASE"}


def _split(header):
    """Return (label, value) pairs from the header lines, in order."""
    lines = header.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or len(lines) > MAX_LINES:
        raise rejected("header has no lines or too many lines")
    pairs = []
    for line in lines:
        first = line.split(":", 1)[0]
        if first in _PAIR_LINES:
            m = _PAIR_LINES[first].fullmatch(line)
            if not m:
                raise rejected(f"{first}: malformed line")
            pairs.extend(zip(_PAIR_LABELS[first], m.groups(), strict=True))
            continue
        m = _LINE.fullmatch(line)
        if not m or m.group(1) not in _SINGLE_LINE:
            raise rejected("malformed or unknown header line")
        pairs.append((m.group(1), m.group(2)))
    return pairs


def _check_pairs(pairs):
    seen = {}
    for label, value in pairs:
        if not value or value != value.strip():
            raise rejected(f"{label}: empty or padded")
        if len(value) > MAX_VALUE:
            raise rejected(f"{label}: too long")
        if has_placeholder(value):
            raise rejected(f"{label}: unfilled placeholder")
        if label in seen and label not in REPEATABLE:
            raise rejected(f"{label}: duplicate")
        seen.setdefault(label, []).append(value)
    for label in LABELS:
        if label not in seen:
            raise rejected(f"{label}: required")
    return seen


def validate_header(text):
    """Validate a report header (the text may continue past it) and return its fields."""
    if not isinstance(text, str):
        raise rejected("input must be text")
    end = text.find("\n\n")
    header = text if end < 0 else text[: end + 1]
    if len(header.encode("utf-8", "surrogatepass")) > MAX_BYTES:
        raise ContractError(INPUT_LIMIT, "input exceeds limit")
    check_text(header)
    seen = _check_pairs(_split(header))

    role, model = seen["ROLE"][0], seen["MODEL"][0]
    if not _SLUG.fullmatch(role):
        raise rejected("ROLE: malformed")
    if "|" in model:
        raise rejected("MODEL: malformed")
    head, base = seen["HEAD"][0], seen["BASE"][0]
    if not _SHA.fullmatch(head) or not _SHA.fullmatch(base):
        raise rejected("HEAD or BASE: malformed")

    gate = seen["GATE"][0]
    gate_exit = None
    m = _GATE_RAN.fullmatch(gate)
    if m:
        gate_exit = int(m.group(2))
        if gate_exit > 255:
            raise rejected("GATE: exit code out of range")
    elif not _REASON.fullmatch(gate):
        raise rejected("GATE: malformed")

    other = seen["OTHER GATES"]
    for v in other:
        if v != "none" and not _OTHER_GATE.fullmatch(v):
            raise rejected("OTHER GATES: malformed")

    packs_value = seen["PACKS LOADED"][0]
    packs = []
    if packs_value != "none":
        packs = [p.strip() for p in packs_value.split(",")]
        if not all(_SLUG.fullmatch(p) for p in packs):
            raise rejected("PACKS LOADED: malformed")
    tree = seen["TREE"][0]
    if not _TREE.fullmatch(tree):
        raise rejected("TREE: malformed")
    llm = seen["LOCAL-LLM"][0]
    if not _LLM.fullmatch(llm):
        raise rejected("LOCAL-LLM: malformed")

    return {
        "role": role,
        "model": model,
        "head": head,
        "base": base,
        "gate": gate,
        "gate_exit": gate_exit,
        "other_gates": other,
        "packs": packs,
        "tree": tree,
        "not_verified": seen["NOT VERIFIED"],
        "local_llm": llm,
    }


def read_header(path):
    """Read at most 16 KiB from the start of a report file (regular file only) and validate its header."""
    data = read_bounded(path, MAX_BYTES)
    end = data.find(b"\n\n")
    if end >= 0:
        data = data[: end + 1]
    elif len(data) > MAX_BYTES:
        raise ContractError(INPUT_LIMIT, "input exceeds limit")
    return validate_header(decode(data))
