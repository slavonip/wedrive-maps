"""One vocabulary for every gate, and one predicate that decides what counts as a pass.

    PASS                     the gate ran and was satisfied
    FAIL                     the gate ran and refused
    UNCHECKED: <reason>      the gate could not run, and says why
    ABSENT                   the input was not there to check

**`ABSENT` and `UNCHECKED` are never a pass.** That sentence is the whole file, and it is written
as code rather than as a convention because the convention has already failed once: the timezone
field grew from a boolean into an object, and the promotion check `not meta.get("timezones", True)`
went on reading it — a non-empty dict is truthy, so a graph whose gate said `ABSENT` promoted
while the check looked exactly like a check.

That bug is not about timezones. It is about a predicate that accepts anything shaped roughly
right, and it can be written again in any gate that grows a field. So `is_pass` compares against
one literal string and nothing else:

    is_pass("PASS")                  True
    is_pass("ABSENT")                False
    is_pass("UNCHECKED: no osmium")  False
    is_pass(True)                    False   <- the shape that caused the bug
    is_pass({"...": "..."})          False   <- and the shape that hid it
    is_pass(None)                    False

**A verdict that is not a pass is not automatically a blocker.** What to do about one belongs to
the consumer and should be stated where the decision is made, not inferred here: an unreadable
PBF header makes a graph *unverified*, not *wrong*, while a missing timezone makes it quietly
wrong forever. Both are "not PASS"; only one should stop a release. The predicate refuses to
guess, which is what lets the policy be explicit.
"""

PASS = "PASS"
FAIL = "FAIL"
ABSENT = "ABSENT"
UNCHECKED = "UNCHECKED"


def unchecked(reason: str) -> str:
    """An UNCHECKED verdict always carries its reason. A gate that quietly checks nothing is the
    failure this vocabulary exists to make visible, and "UNCHECKED" alone is indistinguishable
    from it."""
    return f"{UNCHECKED}: {reason}"


def is_pass(verdict) -> bool:
    """True for the literal string PASS and for nothing else — no truthiness, no coercion."""
    return verdict == PASS


def is_unchecked(verdict) -> bool:
    return isinstance(verdict, str) and verdict.startswith(UNCHECKED)


def describe(verdict) -> str:
    """How to print a verdict, including the shapes that should never appear."""
    if verdict is None:
        return "(no verdict recorded)"
    if isinstance(verdict, bool):
        return f"(a legacy boolean: {verdict})"
    return str(verdict)
