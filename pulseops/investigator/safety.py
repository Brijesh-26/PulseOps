"""Deterministic detection of instruction-like content in evidence.

Used by the backend validator independently of the model: evidence matching
these patterns can never support a hypothesis or an action, whether or not the
model flagged it. Pattern matching is a heuristic and is not a complete defence
against prompt injection; the main controls remain the action allowlist, scope
validation and mandatory human approval.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

INJECTION_PATTERN = re.compile(
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)|disregard (the )?"
    r"(system|previous)|you are now|system prompt|approve (this|the|all)|"
    r"execute (the following|this command)|rm -rf|curl [^\n]*\|\s*(ba)?sh|"
    r"</?(evidence|system|instructions)>",
    re.IGNORECASE,
)


class _Text(Protocol):
    code: str
    title: str
    content: str


def suspicious_codes(items: Iterable[_Text]) -> set[str]:
    return {e.code for e in items if INJECTION_PATTERN.search(f"{e.title}\n{e.content}")}
