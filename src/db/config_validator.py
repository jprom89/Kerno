# config_validator.py
import os
import json
from datetime import datetime, timezone

REQUIRED_ENV_VARS = [
    "DATABASE_URL",
    "EMBEDDING_API_URL",
    "EMBEDDING_API_KEY",
    "KERNO_ENV",
]

OPTIONAL_ENV_VARS = {
    "LOG_LEVEL": "INFO",
}

VALID_KERNO_ENVS = {"development", "staging", "production"}


class ConfigValidationError(Exception):
    """Raised when required environment variables are missing or invalid."""


def validate_config() -> dict:
    """
    Validate all required environment variables are present and well-formed.

    Call once at application startup, before any database pool is initialised.
    If validation fails the application must not start. See CLAUDE.md Rule 7.

    Returns:
        dict: Resolved config containing all required and optional values.

    Raises:
        ConfigValidationError: If any required variable is absent or invalid.
    """
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        _log_startup_failure(missing)
        raise ConfigValidationError(
            "Application cannot start. Missing required environment variables: "
            f"{', '.join(missing)}. "
            "Copy .env.example to .env and fill in the missing values."
        )
    _validate_kerno_env(os.getenv("KERNO_ENV"))
    _validate_database_url(os.getenv("DATABASE_URL"))
    config = {var: os.getenv(var) for var in REQUIRED_ENV_VARS}
    for var, default in OPTIONAL_ENV_VARS.items():
        config[var] = os.getenv(var, default)
    _log_startup_success(config)
    return config


def _validate_kerno_env(value: str) -> None:
    """
    Raise ConfigValidationError if KERNO_ENV is not a recognised value.

    Returns:
        None

    Raises:
        ConfigValidationError: If value is not in VALID_KERNO_ENVS.
    """
    if value not in VALID_KERNO_ENVS:
        valid = ", ".join(sorted(VALID_KERNO_ENVS))
        raise ConfigValidationError(
            f"KERNO_ENV must be one of: {valid}. "
            # CLAUDE.md Rule 8: do not log the actual value — it may contain
            # a typo with sensitive info if misconfigured.
        )


def _validate_database_url(value: str) -> None:
    """
    Raise ConfigValidationError if DATABASE_URL does not begin with postgresql://.

    Returns:
        None

    Raises:
        ConfigValidationError: If value does not begin with the expected scheme.
    """
    if not value.startswith("postgresql://"):
        raise ConfigValidationError(
            "DATABASE_URL must begin with postgresql://. Check your .env file."
            # CLAUDE.md Rule 8: never log the DATABASE_URL value — it contains credentials.
        )


def _log_startup_failure(missing_vars: list) -> None:
    """Emit a structured JSON log entry for a failed startup validation."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "log_level": "ERROR",
        "layer": "infrastructure",
        "function_name": "validate_config",
        "message": "Startup validation failed. Application will not start.",
        "missing_variables": missing_vars,
        # CLAUDE.md Rule 8: log variable names only, never their values.
    }
    print(json.dumps(entry))


def _log_startup_success(config: dict) -> None:
    """Emit a structured JSON log entry for a successful startup validation."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "log_level": "INFO",
        "layer": "infrastructure",
        "function_name": "validate_config",
        "message": "All required environment variables present. Startup proceeding.",
        "kerno_env": config.get("KERNO_ENV"),
        # CLAUDE.md Rule 8: log environment name only, never credentials or URLs.
    }
    print(json.dumps(entry))
