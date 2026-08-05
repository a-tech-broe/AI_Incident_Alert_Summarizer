"""Shared HTTP client for the third-party integrations.

urllib3 rather than `requests`: it is already a botocore dependency, so it adds
nothing to the deployment package, and it exposes the retry and timeout controls
these calls need.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import urllib3
from urllib3.util.retry import Retry


@dataclass
class HttpResponse:
    status: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, TypeError):
            return None


class HttpError(RuntimeError):
    """Raised for transport-level failures. HTTP error statuses are returned,
    not raised, so callers can distinguish 'service said no' from 'unreachable'."""


def _build_pool(total_retries: int, backoff: float) -> urllib3.PoolManager:
    retries = Retry(
        total=total_retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    return urllib3.PoolManager(retries=retries, maxsize=4)


# Enrichment tolerates failure, so it retries less and gives up sooner. Slack is
# the delivery path — a dropped summary is a lost incident notification — so it
# gets a longer budget.
_ENRICHMENT_POOL = _build_pool(total_retries=1, backoff=0.3)
_DELIVERY_POOL = _build_pool(total_retries=3, backoff=0.5)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    form: dict[str, str] | None = None,
    timeout: float = 8.0,
    delivery: bool = False,
) -> HttpResponse:
    """Issue an HTTP request, returning the response rather than raising on 4xx/5xx."""
    pool = _DELIVERY_POOL if delivery else _ENRICHMENT_POOL
    request_headers = dict(headers or {})

    kwargs: dict[str, Any] = {
        "timeout": urllib3.Timeout(connect=min(3.0, timeout), read=timeout),
        "headers": request_headers,
    }

    if form is not None:
        kwargs["fields"] = form
        kwargs["encode_multipart"] = False
    elif body is not None:
        request_headers.setdefault("Content-Type", "application/json")
        kwargs["body"] = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body

    try:
        response = pool.request(method, url, **kwargs)
    except urllib3.exceptions.HTTPError as exc:
        raise HttpError(f"{method} {_redact(url)} failed: {exc}") from exc

    return HttpResponse(status=response.status, body=response.data.decode("utf-8", errors="replace"))


def _redact(url: str) -> str:
    """Strip the path from webhook URLs before they reach a log line."""
    parts = url.split("/", 3)
    return "/".join(parts[:3]) + "/…" if len(parts) > 3 else url
