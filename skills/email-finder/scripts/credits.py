"""Credit accounting for email-finder (1 credit per email found or verified)."""

from __future__ import annotations

from typing import Any, Optional

CREDIT_PER_EMAIL_FOUND = 1
CREDIT_PER_EMAIL_VERIFIED = 1


def find_credits_used(*, found: bool) -> int:
    """Billable credits for a find attempt (charged only when an email is found)."""
    return CREDIT_PER_EMAIL_FOUND if found else 0


def verify_credits_used(*, count: int = 1) -> int:
    """Billable credits for verification (1 per email verified)."""
    return max(0, int(count)) * CREDIT_PER_EMAIL_VERIFIED


def icypeas_credits_for_status(status: str, *, email: str = "") -> int:
    """IcyPeas: 1 credit when an email is returned; 0 for not-found (incl. DEBITED_NOT_FOUND)."""
    if (email or "").strip():
        return CREDIT_PER_EMAIL_FOUND
    return 0


def trykitt_findable_from_balance(remaining_credits: float, job_credits: float) -> int:
    """Estimate how many emails can still be found from trykitt API balance."""
    if job_credits <= 0:
        return int(remaining_credits) if remaining_credits > 0 else 0
    return int(remaining_credits / job_credits)


def mv_credit_summary(
    *,
    email_count: int,
    credits_remaining: float,
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Plan MillionVerifier bulk verification using 1 credit per email."""
    required = verify_credits_used(count=email_count)
    remaining = int(credits_remaining) if credits_remaining else 0
    return {
        "unique_emails": email_count,
        "emails_to_verify": email_count,
        "credits_required": required,
        "credits_remaining": remaining,
        "credits_per_email": CREDIT_PER_EMAIL_VERIFIED,
        "sufficient_credits": error is None and remaining >= required,
        "error": error,
        "credit_model": "1 credit per email verified",
    }
