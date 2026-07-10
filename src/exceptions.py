"""Single home for all custom exception classes used across the application.
Always import from here — never from individual service modules — to prevent circular dependencies.

Why:   one import location keeps exception identity stable across layers, so a
       caught TenantContextMissingError is always the same class everywhere.
How:   exercised by every service and router test; no standalone tests.
"""


class TenantContextMissingError(Exception):
    """Raised when a database query is attempted without a known tenant identifier.
    Never catch this and proceed — it signals a potential cross-tenant data leak."""


class EntryNotFoundError(Exception):
    """Raised when a requested register entry does not exist for the current tenant."""


class MappingError(Exception):
    """Raised when the AI control mapping step fails due to an LLM API error, malformed JSON
    response, invalid status enum, or an out-of-range confidence score. Never catch silently."""


class ConfigurationError(Exception):
    """Raised when a required runtime configuration value is missing or malformed — for example,
    an absent LLM API key. Signals a deployment fault, not a per-request error."""


class JiraClientError(Exception):
    """Raised when a Jira API call fails or the Jira connection is not configured.
    Callers map this to HTTP 503 — the remediation feature is unavailable, not broken."""


class WebhookAuthenticationError(Exception):
    """Raised when a webhook delivery fails authentication (KER-205) — unknown or
    malformed registration id, inactive registration, missing/malformed signature
    header, or HMAC mismatch. One error type for all four causes, so the HTTP layer
    maps every failure to the same 401 and a caller can never probe which part failed."""


class UnsupportedEventTypeError(ValueError):
    """Raised when an authenticated webhook delivery carries an event_type outside
    the supported set (KER-205). The router maps this to 422 — only ever AFTER the
    signature has verified, so event types cannot be probed without a valid secret."""
