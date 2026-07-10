"""Pydantic schemas — the request and response contracts of the API, one module
per surface. Kept apart from the routers so the externally visible shapes are
reviewable in one place and never accidentally coupled to internals.

How:   pytest tests/unit/api/ -v
"""
