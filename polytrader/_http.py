"""Pooled httpx client factory + bounded retry helper.

One shared client per PolyTrader keeps connections warm. Retries cover only
transient failures (transport errors, 5xx/429), never 4xx venue rejections.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


def make_http_client(*, timeout: float = 10.0) -> httpx.AsyncClient:
    """The single shared async HTTP client for all outbound calls.

    Uses HTTP/2 when the optional ``h2`` package is present, else HTTP/1.1.
    """
    common = dict(
        headers={"user-agent": "polytrader/0.1"},
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        ),
    )
    try:
        return httpx.AsyncClient(http2=True, **common)
    except ImportError:
        logger.debug(
            "h2 not installed; HTTP/2 disabled, using HTTP/1.1 keep-alive"
        )
        return httpx.AsyncClient(**common)


def _is_retryable_status(status: int) -> bool:
    # Retry server-side failures + rate limiting; 4xx (except 429) are
    # deterministic rejections.
    return status >= 500 or status == 429


async def request_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int,
    backoff_s: float,
    retry_on_status: bool = True,
    description: str = "request",
) -> httpx.Response:
    """Invoke ``send`` with bounded exponential backoff on transient failures.

    ``send`` must issue exactly one request. Retries on transport errors and
    (when ``retry_on_status``) 5xx/429; raises the last transport error only
    when the budget is exhausted. Total attempts = ``max_retries + 1``.
    """
    attempts = max(0, max_retries) + 1
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            resp = await send()
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                logger.debug("%s: transport error, budget exhausted: %s",
                             description, exc)
                raise
            delay = backoff_s * (2 ** attempt)
            logger.debug("%s: transport error (%s); retry %d/%d in %.2fs",
                         description, exc, attempt + 1, attempts - 1, delay)
            await asyncio.sleep(delay)
            continue

        if retry_on_status and _is_retryable_status(resp.status_code) \
                and attempt + 1 < attempts:
            delay = backoff_s * (2 ** attempt)
            logger.debug("%s: HTTP %d; retry %d/%d in %.2fs",
                         description, resp.status_code, attempt + 1,
                         attempts - 1, delay)
            await asyncio.sleep(delay)
            continue
        return resp

    # Only reached if every attempt raised (returns happen inline above).
    assert last_exc is not None
    raise last_exc
