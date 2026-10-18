"""Domain-level exceptions. The API layer maps these to HTTP responses."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code: str = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class InvalidTransitionError(ConflictError):
    code = "invalid_transition"


class StaleProposalError(ConflictError):
    code = "stale_proposal"


class ValidationFailedError(DomainError):
    code = "validation_failed"


class PermissionDeniedError(DomainError):
    code = "forbidden"


class AuthenticationError(DomainError):
    code = "unauthenticated"


class RetryableError(Exception):
    """Transient failure: the queue message should be retried with backoff."""


class PermanentError(Exception):
    """Non-retryable failure: the message should be dead-lettered immediately."""
