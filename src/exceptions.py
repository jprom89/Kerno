"""Single home for all custom exception classes used across the application.
Always import from here — never from individual service modules — to prevent circular dependencies."""


class TenantContextMissingError(Exception):
    """Raised when a database query is attempted without a known tenant identifier.
    Never catch this and proceed — it signals a potential cross-tenant data leak."""


class EntryNotFoundError(Exception):
    """Raised when a requested register entry does not exist for the current tenant."""
