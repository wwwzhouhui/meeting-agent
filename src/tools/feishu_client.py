"""Shared Feishu OpenAPI client used by the calendar, task and notify tools.

The client deliberately mirrors the proxy policy in ``src.shared.llm``:
localhost traffic is direct, while remote traffic uses a proxy read
manually from the conventional environment variables.  ``trust_env`` is
not used because malformed ``NO_PROXY`` values can break httpx parsing on
some machines, and a blanket direct connection fails on machines whose
egress requires a proxy.

Only ``httpx`` is required.  Tests may inject either an
``httpx.MockTransport`` or a complete ``httpx.Client``.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

FEISHU_BASE_URL = "https://open.feishu.cn"
_TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_REFRESH_TOKEN_PATH = "/open-apis/authen/v1/oidc/refresh_access_token"
# Codes returned by Feishu for expired/invalid user access tokens.  They
# are used only for one refresh-and-retry attempt when a refresh token is
# available; all other API errors are surfaced immediately.
_USER_TOKEN_ERROR_CODES = {99991663, 99991664, 99991668, 99991671}


class FeishuAPIError(RuntimeError):
    """A Feishu API or transport failure with the provider error retained."""

    def __init__(self, code: int | str, msg: str, path: str = ""):
        self.code = code
        self.msg = str(msg)
        self.path = path
        suffix = f" ({path})" if path else ""
        super().__init__(f"Feishu API error {code}: {self.msg}{suffix}")


def map_user_id(user_id: str | None, user_map: dict[str, str] | None = None) -> str | None:
    """Map an internal user id to a Feishu ``open_id``.

    A mapped value wins.  Otherwise only an id that already is an
    ``ou_...`` open_id is accepted.  Names, email addresses and arbitrary
    internal ids return ``None`` so callers can skip them with a warning
    instead of sending an identifier Feishu will reject.
    """
    if not user_id:
        return None
    raw = str(user_id).strip()
    if not raw:
        return None
    mapped = (user_map or {}).get(raw)
    if mapped:
        candidate = str(mapped).strip()
        return candidate if candidate.startswith("ou_") else None
    return raw if raw.startswith("ou_") else None


def zoneinfo_or_utc(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown timezone %r; falling back to Asia/Shanghai", name)
        return ZoneInfo("Asia/Shanghai")


def parse_feishu_datetime(
    value: str | date | datetime,
    timezone: str = "Asia/Shanghai",
    *,
    end_of_day: bool = False,
) -> datetime:
    """Parse an ISO date/datetime into an aware datetime in ``timezone``.

    Date-only values mean the start of that local day, or 23:59:59 when
    ``end_of_day`` is true (the convention used for task due dates).
    Offset-aware datetimes are converted to the requested zone; naive
    datetimes are interpreted as already being in that zone.
    """
    tz = zoneinfo_or_utc(timezone)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime_time.max.replace(microsecond=0)
                                  if end_of_day else datetime_time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed_date = date.fromisoformat(raw)
            parsed = datetime.combine(
                parsed_date,
                datetime_time.max.replace(microsecond=0) if end_of_day else datetime_time.min,
            )
        else:
            # datetime.fromisoformat handles a trailing Z only in newer
            # Python versions in some builds; normalize it explicitly.
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        raise ValueError(f"unsupported datetime value: {value!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def timestamp_seconds(value: str | date | datetime, timezone: str = "Asia/Shanghai") -> str:
    """Feishu calendar timestamp: Unix **seconds**, as a string."""
    return str(int(parse_feishu_datetime(value, timezone).timestamp()))


def timestamp_millis(
    value: str | date | datetime,
    timezone: str = "Asia/Shanghai",
    *,
    end_of_day: bool = False,
) -> str:
    """Feishu task timestamp: Unix **milliseconds**, as a string."""
    parsed = parse_feishu_datetime(value, timezone, end_of_day=end_of_day)
    return str(int(parsed.timestamp()) * 1000 + parsed.microsecond // 1000)


class FeishuClient:
    """Small OpenAPI client with tenant-token caching and user-token refresh."""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        *,
        user_access_token: str = "",
        refresh_token: str = "",
        static_access_token: str = "",
        transport: Any = None,
        http_client: Any = None,
        client: Any = None,
        timeout: float = 30.0,
        time_fn: Callable[[], float] | None = None,
    ):
        self.app_id = app_id or ""
        self.app_secret = app_secret or ""
        self.user_access_token = user_access_token or ""
        self.refresh_token = refresh_token or ""
        # Backwards compatibility for callers that used to pass one token
        # directly to a tool.  It is treated as an already-obtained access
        # token and is not logged or returned by this client.
        self.static_access_token = static_access_token or ""
        self.timeout = timeout
        self._transport = transport
        self._injected = http_client or client
        self._time_fn = time_fn or time.time
        self._tenant_token = ""
        self._tenant_expires_at = 0.0
        self._user_expires_at = 0.0

    @property
    def has_app_credentials(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def configured(self) -> bool:
        return bool(
            self.has_app_credentials
            or self.user_access_token
            or self.refresh_token
            or self.static_access_token
        )

    # ------------------------------------------------------------------ HTTP
    def _http_client(self):
        import httpx

        if self._injected is not None:
            return self._injected, False
        kwargs: dict[str, Any] = {"timeout": self.timeout, "trust_env": False}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            # Imported lazily to avoid a module-level cycle and to reuse
            # exactly the LLM client's tested proxy behaviour.
            from src.shared.llm import _proxy_kwargs

            kwargs.update(_proxy_kwargs(FEISHU_BASE_URL))
        return httpx.Client(**kwargs), True

    def _send_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        path_for_error: str = "",
    ) -> dict:
        http, close = self._http_client()
        try:
            response = http.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=headers or {},
                timeout=self.timeout,
            )
        except Exception as exc:
            raise FeishuAPIError("transport_error", str(exc), path_for_error or url) from exc
        finally:
            if close:
                http.close()

        status = getattr(response, "status_code", 200)
        try:
            payload = response.json()
        except Exception as exc:
            text = str(getattr(response, "text", ""))[:500]
            raise FeishuAPIError(status, f"non-JSON response: {text}", path_for_error or url) from exc
        if not isinstance(payload, dict):
            raise FeishuAPIError(status, "response JSON is not an object", path_for_error or url)
        if status >= 400:
            code = payload.get("code", status)
            msg = payload.get("msg") or payload.get("message") or f"HTTP {status}"
            raise FeishuAPIError(code, msg, path_for_error or url)
        return payload

    @staticmethod
    def _check_payload(payload: dict, path: str) -> dict:
        code = payload.get("code", payload.get("StatusCode", 0))
        if code not in (0, "0", None):
            msg = payload.get("msg") or payload.get("message") or payload.get("StatusMessage") or "unknown error"
            raise FeishuAPIError(code, msg, path)
        return payload

    # ---------------------------------------------------------------- tokens
    def get_tenant_token(self, *, force_refresh: bool = False) -> str:
        now = self._time_fn()
        if (
            not force_refresh
            and self._tenant_token
            and self._tenant_expires_at - now > 300
        ):
            return self._tenant_token
        if not self.has_app_credentials:
            raise FeishuAPIError(
                "not_configured",
                "FEISHU_APP_ID and FEISHU_APP_SECRET are required to obtain a tenant_access_token",
                _TENANT_TOKEN_PATH,
            )
        payload = self._send_json(
            "POST",
            f"{FEISHU_BASE_URL}{_TENANT_TOKEN_PATH}",
            json_body={"app_id": self.app_id, "app_secret": self.app_secret},
            headers={"Content-Type": "application/json; charset=utf-8"},
            path_for_error=_TENANT_TOKEN_PATH,
        )
        self._check_payload(payload, _TENANT_TOKEN_PATH)
        token = payload.get("tenant_access_token") or (payload.get("data") or {}).get("tenant_access_token")
        if not token:
            raise FeishuAPIError("invalid_response", "tenant token response has no token", _TENANT_TOKEN_PATH)
        expire = payload.get("expire") or (payload.get("data") or {}).get("expire") or 7200
        try:
            expire_seconds = int(expire)
        except (TypeError, ValueError):
            expire_seconds = 7200
        self._tenant_token = str(token)
        self._tenant_expires_at = now + expire_seconds
        return self._tenant_token

    def refresh_user_token(self) -> str:
        """Exchange the configured refresh token for a fresh user token."""
        if not self.refresh_token:
            raise FeishuAPIError(
                "not_configured",
                "FEISHU_REFRESH_TOKEN is required to refresh a user_access_token",
                _REFRESH_TOKEN_PATH,
            )
        if not self.has_app_credentials:
            raise FeishuAPIError(
                "not_configured",
                "FEISHU_APP_ID and FEISHU_APP_SECRET are required for user-token refresh",
                _REFRESH_TOKEN_PATH,
            )
        basic = base64.b64encode(f"{self.app_id}:{self.app_secret}".encode("utf-8")).decode("ascii")
        payload = self._send_json(
            "POST",
            f"{FEISHU_BASE_URL}{_REFRESH_TOKEN_PATH}",
            json_body={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json; charset=utf-8",
            },
            path_for_error=_REFRESH_TOKEN_PATH,
        )
        self._check_payload(payload, _REFRESH_TOKEN_PATH)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        token = data.get("access_token") or data.get("user_access_token")
        if not token:
            raise FeishuAPIError("invalid_response", "refresh response has no access_token", _REFRESH_TOKEN_PATH)
        self.user_access_token = str(token)
        if data.get("refresh_token"):
            self.refresh_token = str(data["refresh_token"])
        try:
            self._user_expires_at = self._time_fn() + int(data.get("expires_in") or 7200)
        except (TypeError, ValueError):
            self._user_expires_at = self._time_fn() + 7200
        return self.user_access_token

    def _access_token(self, token_type: str) -> str:
        token_type = (token_type or "tenant").strip().lower()
        if token_type == "tenant":
            if self.static_access_token:
                return self.static_access_token
            return self.get_tenant_token()
        if token_type == "user":
            if self.user_access_token:
                # A manually supplied token has no trustworthy expiry
                # locally.  Use it first; request() refreshes and retries
                # once if Feishu rejects it as expired/invalid.
                return self.user_access_token
            if self.refresh_token:
                return self.refresh_user_token()
            if self.static_access_token:
                return self.static_access_token
            raise FeishuAPIError(
                "not_configured",
                "FEISHU_USER_ACCESS_TOKEN or FEISHU_REFRESH_TOKEN is required for user-token mode",
            )
        raise ValueError(f"unsupported Feishu token_type: {token_type!r}")

    # --------------------------------------------------------------- requests
    def request(
        self,
        method: str,
        path: str,
        *,
        token_type: str = "tenant",
        json: Any = None,
        params: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> dict:
        """Call an OpenAPI path and return its ``data`` object.

        The full provider envelope is validated here: HTTP errors and
        ``code != 0`` become :class:`FeishuAPIError`.  For endpoints whose
        useful response is not nested under ``data``, the whole envelope
        is returned.
        """
        if path.startswith("http://") or path.startswith("https://"):
            url = path
            error_path = path
        else:
            clean_path = path if path.startswith("/") else f"/{path}"
            url = f"{FEISHU_BASE_URL}{clean_path}"
            error_path = clean_path

        token = self._access_token(token_type)
        payload = self._send_json(
            method,
            url,
            json_body=json,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            path_for_error=error_path,
        )
        try:
            self._check_payload(payload, error_path)
        except FeishuAPIError as exc:
            # One refresh-and-retry is safe and useful for long-lived
            # deployments using user tokens; tenant errors are not retried.
            if (
                not _retried
                and (token_type or "").lower() == "user"
                and self.refresh_token
                and self._coerce_code(exc.code) in _USER_TOKEN_ERROR_CODES
            ):
                self.refresh_user_token()
                return self.request(
                    method, path, token_type=token_type, json=json, params=params, _retried=True
                )
            raise
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    @staticmethod
    def _coerce_code(code: int | str) -> int | None:
        try:
            return int(code)
        except (TypeError, ValueError):
            return None

    def post_webhook(self, url: str, payload_body: dict) -> dict:
        """POST to a custom-bot webhook and return the full response envelope."""
        payload = self._send_json(
            "POST",
            url,
            json_body=payload_body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            path_for_error="feishu webhook",
        )
        return self._check_payload(payload, "feishu webhook")


__all__ = [
    "FEISHU_BASE_URL",
    "FeishuAPIError",
    "FeishuClient",
    "map_user_id",
    "parse_feishu_datetime",
    "timestamp_millis",
    "timestamp_seconds",
]
