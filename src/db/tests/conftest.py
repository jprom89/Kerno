# conftest.py
# src/db/tests/conftest.py
# Shared pytest fixtures for database infrastructure tests.
# Provides two pre-seeded tenant UUIDs for RLS bypass-attempt tests.

import uuid
import pytest

# Fixed UUIDs so tests are deterministic and readable in failure output.
TENANT_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TENANT_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(scope="session")
def db_connection():
    """
    Provide a database connection for the test session.

    Requires a live PostgreSQL instance with all migrations applied.
    Mark tests using this fixture with @pytest.mark.integration.

    Returns:
        connection: An active database connection from the pool.

    Raises:
        pytest.skip: Until src/db/connection.py (File 3) is implemented.
    """
    # Uncomment once File 3 (src/db/connection.py) is written:
    # from src.db.connection import initialise_connection_pool, get_connection
    # initialise_connection_pool()
    # connection = get_connection()
    # yield connection
    # connection.close()
    pytest.skip(
        "db_connection requires src/db/connection.py (File 3). "
        "Activate this fixture once File 3 is complete."
    )


@pytest.fixture
def tenant_a_id():
    """Return the fixed UUID for Tenant A used across isolation tests."""
    return TENANT_A_ID


@pytest.fixture
def tenant_b_id():
    """Return the fixed UUID for Tenant B used across isolation tests."""
    return TENANT_B_ID
