"""Shared slowapi rate limiter (SEC-05).

Defined in its own module so both the app factory and the individual routers can
import the same Limiter instance without a circular import (app.py imports the
routers, and the routers need the limiter to decorate their endpoints).

How:   exercised through the SEC-05 endpoint limits in the scheduler, export,
       and overrides router tests (tests/unit/api/).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

# Keyed on the caller's remote address; in-memory storage is sufficient for the
# single-process MVP. Swap the storage backend here if the app is horizontally scaled.
limiter = Limiter(key_func=get_remote_address)
