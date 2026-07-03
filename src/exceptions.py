"""Single home for all custom exception classes used across the application.
Always import from here — never from individual service modules — to prevent circular dependencies."""


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
