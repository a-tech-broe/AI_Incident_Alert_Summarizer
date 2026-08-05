"""Shared HTTP client for the third-party integrations.

urllib3 rather than `requests`: it is already a botocore dependency, so it adds
nothing to the deployment package, and it exposes the retry and timeout controls
these calls need.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
        # Scrub the exception text, not just our own message: urllib3 embeds the
        # full URL in "Max retries exceeded with url: ...", and for a Slack
        # webhook the path *is* the credential. Interpolating the raw exception
        # writes that credential to CloudWatch Logs.
        raise HttpError(f"{method} {_redact(url)} failed: {_scrub(str(exc), url)}") from exc

    return HttpResponse(status=response.status, body=response.data.decode("utf-8", errors="replace"))


# Matches absolute and protocol-relative URLs, which is the form urllib3 uses
# in its own error messages.
_URLISH = re.compile(r"(?:https?:)?//[^\s'\"<>\\)]+")


def _redact(url: str) -> str:
    """Reduce a URL to scheme and host. The path may be a bearer credential."""
    candidate = url.strip()
    try:
        parts = urlsplit(candidate if "//" in candidate else f"//{candidate}")
        host = parts.netloc
    except ValueError:
        return "<unparseable url>"

    if not host:
        return "<url>"

    scheme = f"{parts.scheme}://" if parts.scheme else "//"
    return f"{scheme}{host}/…" if (parts.path or parts.query) else f"{scheme}{host}"


def _scrub(text: str, url: str | None = None) -> str:
    """Redact every URL appearing anywhere in a message.

    The regex catches well-formed URLs, but a malformed one (a pasted secret
    with an embedded space, say) can survive it. So when the request URL is
    known, its path is removed by literal substring match first — that path is
    the credential, and this does not depend on the message's shape.
    """
    if url:
        path = urlsplit(url.strip() if "//" in url else f"//{url.strip()}").path
        # Guard against a single-slash path matching half the message.
        if path and len(path) > 1:
            text = text.replace(path, "/…")

    return _URLISH.sub(lambda m: _redact(m.group(0)), text)
