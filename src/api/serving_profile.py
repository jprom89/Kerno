"""serving_profile.py — which surfaces this API process serves: the full product or the pilot workflow.

What:  Resolves and validates KERNO_SERVING_PROFILE, a server-side setting with
       exactly two values. "full" serves every router. "pilot" serves only the
       supported pilot workflow (docs/pilot_readiness_kickoff.md §4.1) and
       leaves the §4.3 exclusions unregistered, so they answer 404 without
       entering any business code. A blank value means "full" only when
       KERNO_ENV is development; anywhere else the profile must be stated, and
       any other value is refused rather than widened to "full".
Why:   PILOT-READY-003 (issue #20, gate G10). The pilot runs with a reduced
       runtime grant set; serving a route whose grants were removed would fail
       at request time, and serving an excluded surface at all is what the
       pilot must not do. The value is read once, by whoever builds the app or
       starts a job, never from a request, a header, a cookie or the browser.
How:   create_app() in src/api/app.py and the nightly job's main() call
       resolve_serving_profile(). Tests:
       pytest tests/unit/api/test_serving_profile.py -v
"""

from __future__ import annotations

SERVING_PROFILE_VARIABLE = "KERNO_SERVING_PROFILE"
FULL_PROFILE = "full"
PILOT_PROFILE = "pilot"
SERVING_PROFILES = (FULL_PROFILE, PILOT_PROFILE)


class ServingProfileError(RuntimeError):
    """The serving profile is missing outside development, or is not one of the recognised values."""


def resolve_serving_profile(value: str | None, development: bool) -> str:
    """Return "full" or "pilot" for a configured value, or raise ServingProfileError.

    Surrounding whitespace is ignored and a blank value counts as unset, which
    means "full" in development and is refused elsewhere. Values are
    case-sensitive: "Pilot" is refused, never guessed at.
    """
    stated = (value or "").strip()
    if not stated:
        if development:
            return FULL_PROFILE
        raise ServingProfileError(
            f"{SERVING_PROFILE_VARIABLE} must be set to 'full' or 'pilot' outside development."
        )
    if stated not in SERVING_PROFILES:
        raise ServingProfileError(
            f"{SERVING_PROFILE_VARIABLE} must be 'full' or 'pilot'; the configured value is not recognised."
        )
    return stated
