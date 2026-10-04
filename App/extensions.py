"""Shared Flask extensions.

Kept in its own module so both ``app.py`` and the blueprints can import them
without a circular dependency.
"""

from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

#: Default to a generous global limit; individual routes tighten this further.
#: Storage is per-process memory, which is fine for this single-container app.
limiter = Limiter(
    get_remote_address,
    default_limits=["300 per minute"],
    storage_uri="memory://",
)
