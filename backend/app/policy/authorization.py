"""Pure authorization guards required by the Phase 1A contract tests."""

from __future__ import annotations

from datetime import datetime

from backend.app.schemas.actions import (
    ActionAuthorization,
    ActionProposal,
    AuthorizationStatus,
)
from backend.app.schemas.base import require_aware_datetime


def ensure_authorization_transition(
    previous: ActionAuthorization,
    candidate: ActionAuthorization,
) -> ActionAuthorization:
    """Accept one decision from PENDING and reject duplicates or identity changes."""

    if previous.action_id != candidate.action_id or previous.action_type is not candidate.action_type:
        raise ValueError("authorization identity does not match")
    if previous.authorization_status is not AuthorizationStatus.PENDING:
        raise ValueError("authorization has already been decided")
    if candidate.authorization_status not in {AuthorizationStatus.APPROVED, AuthorizationStatus.REJECTED}:
        raise ValueError("candidate must be an approval or rejection")
    return candidate


def ensure_action_executable(
    proposal: ActionProposal,
    authorization: ActionAuthorization,
    now: datetime,
) -> ActionProposal:
    """Validate authorization without executing or mutating an action."""

    require_aware_datetime(now, "now")
    if proposal.action_id != authorization.action_id:
        raise ValueError("authorization action_id does not match proposal")
    if proposal.action_type is not authorization.action_type:
        raise ValueError("authorization action_type does not match proposal")
    if authorization.authorization_status is not AuthorizationStatus.APPROVED:
        raise ValueError("action is not approved")
    if authorization.expires_at <= now or proposal.expires_at <= now:
        raise ValueError("authorization or proposal has expired")
    return proposal
