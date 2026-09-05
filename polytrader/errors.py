"""Exception hierarchy for polytrader.

Golden rule: venue rejections are NOT exceptions — order and treasury calls
return structured results (`ok` / `error_msg`). Exceptions are
reserved for programmer error (bad arguments) and unrecoverable configuration.
Transport failures are retried internally and, if they still fail, surface as a
structured result too (they do not raise past the client boundary).
"""
from __future__ import annotations


class PolyTraderError(Exception):
    """Base class for every error this package raises."""


class ConfigError(PolyTraderError):
    """Invalid or missing configuration (unrecoverable at construct time)."""


class ValidationError(PolyTraderError, ValueError):
    """A caller passed a bad argument (programmer error).

    Subclasses ``ValueError`` so existing ``except ValueError`` blocks keep
    working, while remaining catchable as a ``PolyTraderError``.
    """


class TransportError(PolyTraderError):
    """A network/transport failure that survived the retry budget.

    Most public methods convert this into a structured ``ok=False`` result
    rather than letting it propagate; it exists for the few internal call sites
    that need to distinguish transport failure from a venue rejection.
    """
