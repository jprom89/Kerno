"""Database layer: connection management and Row-Level Security helpers.

The most security-critical helper in the whole codebase lives here:
``set_tenant_context`` (in ``rls.py``), which must be called before any tenant
data is read or written. See CLAUDE.md Section 3.

How:   pytest tests/security/test_tenant_isolation.py -v
"""
