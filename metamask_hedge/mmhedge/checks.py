"""One atomic condition, and the verdict it reached.

Borrowed wholesale from the 5-minute strategy next door, for the same reason it
exists there: a refused trade has to name the condition that refused it.  A
hedge that silently sits at zero because four different rules each nudged it
down is untunable, and a margin top-up that fires without saying which level
tripped is unauditable at exactly the moment you need the audit.

Deliberately duplicated rather than imported.  ``btc5m`` and ``mmhedge`` are
sibling strategies with no dependency between them, and a shared 30-line
dataclass is not worth coupling their release cycles.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    """``applicable`` marks a condition that could not be judged.

    An inapplicable check is shown in the trace but never counted as the reason
    something was refused -- there is no point reporting "funding flipped" on a
    hedge that was opened for risk reasons and never cared about funding.
    """

    label: str
    passed: bool
    detail: str
    applicable: bool = True

    def __str__(self) -> str:
        state = "PASS" if self.passed else "FAIL"
        if not self.applicable:
            state = "SKIP"
        return f"{state} {self.label}: {self.detail}"


def blockers(checks: tuple[Check, ...]) -> tuple[str, ...]:
    return tuple(c.label for c in checks if c.applicable and not c.passed)


def all_passed(checks: tuple[Check, ...]) -> bool:
    return all(c.passed for c in checks if c.applicable)


def render(checks: tuple[Check, ...], indent: str = "  ") -> str:
    return "\n".join(f"{indent}{c}" for c in checks)
