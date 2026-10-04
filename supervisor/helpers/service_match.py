"""Exact service identity.

Matching ignores case and surrounding whitespace only. A prefix, a
substring, or a fuzzy comparison is not a match.
"""


def service_names_match(left: str, right: str) -> bool:
    a = str(left or "").strip().casefold()
    b = str(right or "").strip().casefold()
    if not a or not b:
        return False
    return a == b
