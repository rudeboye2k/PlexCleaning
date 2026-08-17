"""Shared HTTP plumbing: retries, timeouts, uniform error type."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class ClientError(Exception):
    """Any failure talking to an external service."""

    def __init__(self, service: str, message: str, status: int | None = None):
        self.service = service
        self.status = status
        super().__init__(f"[{service}] {message}")


@dataclass
class ServiceStatus:
    name: str
    ok: bool
    detail: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "version": self.version}


class HttpClient:
    """Small synchronous JSON client with bounded retries.

    Retries are deliberately conservative and only apply to GETs plus
    transient status codes — a DELETE that half-succeeded must not be replayed.
    """

    service = "http"

    def __init__(self, base_url: str, *, timeout: int = 60, verify_ssl: bool = False,
                 headers: dict[str, str] | None = None, retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.retries = max(retries, 1)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            verify=verify_ssl,
            headers={"Accept": "application/json", **(headers or {})},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        attempts = self.retries if method.upper() == "GET" else 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code in RETRY_STATUS and attempt < attempts - 1:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code >= 400:
                    raise ClientError(
                        self.service,
                        f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}",
                        resp.status_code,
                    )
                return resp
            except httpx.HTTPError as exc:
                last = exc
                if attempt < attempts - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise ClientError(self.service, f"{method} {path} failed: {exc}") from exc
        raise ClientError(self.service, f"{method} {path} failed: {last}")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.request("GET", path, params=params)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ClientError(self.service, f"GET {path} returned non-JSON: {resp.text[:200]}") from exc

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.request("DELETE", path, params=params)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:500]}

    def post_json(self, path: str, payload: Any = None, params: dict[str, Any] | None = None) -> Any:
        resp = self.request("POST", path, json=payload, params=params)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:500]}

    def put_json(self, path: str, payload: Any = None) -> Any:
        resp = self.request("PUT", path, json=payload)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:500]}


def as_bool_param(value: bool) -> str:
    """The *arr APIs want lowercase JSON-style booleans in the query string."""
    return "true" if value else "false"
