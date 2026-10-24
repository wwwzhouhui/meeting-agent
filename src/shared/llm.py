"""OpenAI-compatible LLM client (httpx, no hard SDK dependency).

Protocol: ``POST {LLM_BASE_URL}/chat/completions`` — the common
denominator of OpenAI, DeepSeek, 通义千问 DashScope compatible mode,
火山方舟, Moonshot and local Ollama (``/v1``).

- No key / ``LLM_MODE=mock``: deterministic mock, never raises, so the
  whole pipeline runs with zero configuration (see config.Settings).
- Real mode: httpx directly. litellm / openai SDKs are NOT required;
  if httpx were unavailable and litellm happens to be installed it is
  used as a fallback transport, but a missing litellm never crashes.
- ``chat_json`` extracts JSON from ```json fences / surrounding prose.
- Retries: one retry on transport / HTTP failure.
- Tests can inject a fake transport or http client:
  ``LLMClient(settings, transport=httpx.MockTransport(handler))``
  or ``LLMClient(settings, http_client=httpx.Client(transport=...))``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Sequence, Union

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

Message = dict  # {"role": ..., "content": ...}
MessagesLike = Union[str, Sequence[Message]]


class LLMError(Exception):
    """LLM call or JSON-extraction failure.

    ``raw`` keeps the raw model output (when any) so callers can feed it
    back to the model in a repair attempt.
    """

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def extract_json(text: str) -> dict:
    """Extract a JSON object from model output.

    Handles: bare JSON, ```json fences, prose before/after the object.
    Raises LLMError when nothing parseable is found.
    """
    if text is None:
        raise LLMError("empty LLM output")
    raw = str(text).strip()
    if not raw:
        raise LLMError("empty LLM output")
    candidates: list[str] = []
    # fenced blocks first
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE):
        candidates.append(m.group(1).strip())
    candidates.append(raw)
    # outermost {...} span
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    last_err: Exception | None = None
    for cand in candidates:
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except Exception as exc:  # try next candidate
            last_err = exc
            continue
        if isinstance(data, dict):
            return data
        last_err = ValueError(f"JSON is not an object: {type(data).__name__}")
    raise LLMError(f"could not extract JSON object from LLM output: {last_err}", raw=raw[:4000])


def _normalize_messages(messages: MessagesLike, system: str = "") -> list[Message]:
    if isinstance(messages, str):
        out: list[Message] = []
        if system:
            out.append({"role": "system", "content": system})
        out.append({"role": "user", "content": messages})
        return out
    out = [dict(m) for m in messages]
    if system and not any(m.get("role") == "system" for m in out):
        out.insert(0, {"role": "system", "content": system})
    return out


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _proxy_kwargs(url: str) -> dict[str, Any]:
    """Proxy handling that works in BOTH direct and proxied environments.

    Why not trust_env=True: httpx's env parsing chokes on NO_PROXY values
    containing bracketed IPv6 like [::1] (Invalid port error). Why not a
    bare trust_env=False either: on machines whose egress REQUIRES an
    http proxy, direct TLS then fails (SSL WRONG_VERSION_NUMBER).

    So: localhost endpoints always go direct; remote endpoints use the
    proxy URL taken manually from the conventional env vars, if set.
    """
    import os
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return {}
    if parsed.scheme == "https":
        names = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    else:
        names = ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    for name in names:
        value = os.getenv(name)
        if value:
            return {"proxy": value}
    return {}


class LLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        model: str | None = None,
        client: Any = None,
        http_client: Any = None,
        transport: Any = None,
        fake_transport: Any = None,
    ):
        self.settings = settings or get_settings()
        self.model = model or self.settings.llm_model
        # Injectable fakes (tests / fake server): an httpx.Client, or a
        # transport passed to httpx.Client. ``client`` is an alias.
        self._injected = http_client or client
        self._transport = transport or fake_transport

    # -- availability -------------------------------------------------------
    @property
    def is_mock(self) -> bool:
        if self._injected is not None or self._transport is not None:
            return False
        return self.settings.mock_mode

    @property
    def available(self) -> bool:
        """True when a real LLM call should be attempted."""
        return not self.is_mock

    # -- core chat ------------------------------------------------------------
    def chat(
        self,
        messages: MessagesLike,
        system: str = "",
        *,
        model: str | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> str:
        msgs = _normalize_messages(messages, system)
        if self.is_mock:
            return self._mock_chat(msgs)
        last_exc: Exception | None = None
        for attempt in (1, 2):  # 1 try + 1 retry
            try:
                return self._chat_once(msgs, model=model, temperature=temperature, timeout=timeout)
            except Exception as exc:
                last_exc = exc
                logger.warning("LLM call attempt %d failed: %s", attempt, exc)
        if isinstance(last_exc, LLMError):
            raise last_exc
        raise LLMError(f"LLM call failed after retry: {last_exc}")

    def _chat_once(self, msgs, *, model=None, temperature=None, timeout=None) -> str:
        use_model = model or self.model
        use_temp = self.settings.llm_temperature if temperature is None else temperature
        use_timeout = self.settings.llm_timeout if timeout is None else timeout
        payload = {"model": use_model, "messages": msgs, "temperature": use_temp}
        # Prefer the httpx (OpenAI-compatible HTTP) path.
        try:
            import httpx
        except Exception:
            return self._chat_via_litellm(payload, msgs)
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        if self._injected is not None:
            http = self._injected
            close = False
        else:
            kwargs: dict[str, Any] = {"timeout": use_timeout, "trust_env": False}
            if self._transport is not None:
                # Injected transport (tests/fakes): it IS the network.
                # Adding a proxy would make httpx bypass it entirely.
                kwargs["transport"] = self._transport
            else:
                kwargs.update(_proxy_kwargs(url))
            http = httpx.Client(**kwargs)
            close = True
        try:
            resp = http.post(url, json=payload, headers=headers, timeout=use_timeout)
        finally:
            if close:
                http.close()
        if getattr(resp, "status_code", 200) >= 400:
            raise LLMError(f"LLM HTTP {resp.status_code}: {str(getattr(resp, 'text', ''))[:500]}")
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            raise LLMError(f"unexpected LLM response shape: {exc}") from exc

    def _chat_via_litellm(self, payload, msgs) -> str:
        """Fallback transport only when httpx is unavailable."""
        try:
            import litellm  # optional, may not be installed
        except Exception as exc:
            raise LLMError(f"neither httpx nor litellm available: {exc}") from exc
        resp = litellm.completion(
            model=payload["model"],
            api_key=self.settings.llm_api_key or None,
            api_base=self.settings.llm_base_url,
            messages=msgs,
            temperature=payload["temperature"],
        )
        return resp.choices[0].message.content or ""

    # -- JSON chat --------------------------------------------------------------
    def chat_json(
        self,
        messages: MessagesLike,
        system: str = "",
        *,
        model: str | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Call chat and extract a JSON object. Raises LLMError on failure."""
        if self.is_mock:
            raise LLMError("LLM is in mock mode; no real JSON available")
        text = self.chat(messages, system, model=model, temperature=temperature, timeout=timeout)
        return extract_json(text)

    def structured_chat(self, prompt: str, system: str = "") -> dict:
        """Backwards-compatible helper: {} on any failure (old contract)."""
        if self.is_mock:
            return {}
        try:
            return self.chat_json(prompt, system)
        except Exception as exc:
            logger.warning("structured_chat failed: %s", exc)
            return {}

    # -- mock ---------------------------------------------------------------------
    def _mock_chat(self, msgs: Iterable[Message]) -> str:
        prompt = "\n".join(str(m.get("content", "")) for m in msgs)
        logger.info("[mock-llm] model=%s prompt_len=%d", self.model, len(prompt))
        for line in prompt.splitlines():
            line = line.strip()
            if line:
                return f"[mock] {line[:200]}"
        return "[mock] (empty prompt)"
