"""Bring-Your-Own-Key generation backend (`server/byok_backend.py`).

Optional alternative to dispatching turns through the Antigravity host model:
the assembled `ST_RUNTIME_FRAME` is sent to any OpenAI-compatible
`/chat/completions` endpoint with the user's own API key (for example a
Google AI Studio key against Gemini's OpenAI-compatible endpoint).

Design rules:
- Standard library only (`urllib`), matching the rest of the control plane.
- The API key is NEVER persisted by the plugin and NEVER returned to the UI:
  it is read from the `TAVERN_BYOK_API_KEY` environment variable, or from a
  user-created `<state_dir>/byok_api_key` file (first line).
- Loopback endpoints bypass `HTTP(S)_PROXY`; remote endpoints honour it, since
  many users need a system proxy to reach hosted APIs.
- Error messages never echo the key or the request body.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .net_utils import is_loopback_host, open_url

DEFAULT_BYOK_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_BYOK_TIMEOUT_S = 150
KEY_ENV_VAR = "TAVERN_BYOK_API_KEY"
BASE_URL_ENV_VAR = "TAVERN_BYOK_BASE_URL"
MODEL_ENV_VAR = "TAVERN_BYOK_MODEL"
KEY_FILE_NAME = "byok_api_key"
BYOK_CACHE_MODES = ("off", "prefix_pinned")


def _is_loopback_host(host: str) -> bool:
    return is_loopback_host(host)


def _validate_base_url(base_url: str) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("BYOK base_url 必须是 http(s):// 开头的完整地址。")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname or ""):
        raise ValueError("BYOK 远程端点必须使用 https://（仅本机回环地址允许 http://）。")
    if parsed.username or parsed.password:
        raise ValueError("BYOK base_url 不能内嵌账号或密钥，请改用 TAVERN_BYOK_API_KEY。")
    return cleaned


def read_byok_api_key(state_dir: str | Path | None) -> str:
    key = os.environ.get(KEY_ENV_VAR, "").strip()
    if key:
        return key
    if state_dir:
        key_file = Path(state_dir) / KEY_FILE_NAME
        if key_file.is_file():
            try:
                first = key_file.read_text(encoding="utf-8").splitlines()
            except OSError:
                return ""
            return first[0].strip() if first else ""
    return ""


def resolve_byok_config(plugin_settings: dict[str, Any], state_dir: str | Path | None) -> dict[str, Any]:
    """Merges env vars over plugin settings. Returns a config dict without the key itself."""
    base_url = (
        os.environ.get(BASE_URL_ENV_VAR, "").strip()
        or str(plugin_settings.get("byok_base_url") or "").strip()
        or DEFAULT_BYOK_BASE_URL
    )
    model = os.environ.get(MODEL_ENV_VAR, "").strip() or str(plugin_settings.get("byok_model") or "").strip()
    key_present = bool(read_byok_api_key(state_dir))
    cache_mode = str(plugin_settings.get("byok_cache_mode") or "off")
    if cache_mode not in BYOK_CACHE_MODES:
        cache_mode = "off"
    return {
        "base_url": base_url,
        "model": model,
        "cache_mode": cache_mode,
        "cache_control": bool(plugin_settings.get("byok_cache_control", False)),
        "key_present": key_present,
        "key_source": "env" if os.environ.get(KEY_ENV_VAR, "").strip() else ("file" if key_present else "none"),
        "ready": key_present and bool(model),
    }


def build_byok_messages(system_frame: str, user_prompt: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": system_frame}]
    messages.append({"role": "user", "content": user_prompt or "(continue the scene)"})
    return messages


def _with_cache_breakpoint(message: dict[str, Any]) -> dict[str, Any]:
    """Anthropic-style `cache_control` breakpoint on a message (passed through by some
    OpenAI-compatible gateways; strict ones may reject it, hence opt-in)."""
    return {
        "role": message["role"],
        "content": [{"type": "text", "text": str(message["content"]), "cache_control": {"type": "ephemeral"}}],
    }


def build_byok_cached_messages(
    frame_parts: dict[str, Any],
    user_prompt: str,
    cache_control: bool = False,
) -> list[dict[str, Any]]:
    """Prefix-cache friendly request: [system: static prefix] + history turns + [user: tail + prompt].

    Providers with automatic prefix caching (DeepSeek, OpenAI-style, Gemini implicit caching) match
    on the leading tokens, so the static prefix and the append-only history are reused turn to turn.
    Mid-conversation system items are sent as user turns, because some OpenAI-compatible layers hoist
    every system message into the system instruction, which would change the prefix.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": str(frame_parts.get("static_prefix_block", ""))}]
    for h in frame_parts.get("history_messages") or []:
        content = str(h.get("content", ""))
        if not content.strip():
            continue
        role = h.get("role")
        if role == "system":
            messages.append({"role": "user", "content": f"[{h.get('name') or 'System'}] {content}"})
        else:
            messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    if cache_control:
        messages[0] = _with_cache_breakpoint(messages[0])
        if len(messages) > 1:
            messages[-1] = _with_cache_breakpoint(messages[-1])
    tail = str(frame_parts.get("dynamic_tail_block", "")).strip()
    final = (user_prompt or "").strip() or "(continue the scene)"
    history = frame_parts.get("history_messages") or []
    if history and history[-1].get("role") == "user" and str(history[-1].get("content", "")).strip() == final:
        # The user's line is already the last history turn; don't repeat it after the tail.
        final = "[Respond in character to the latest user message above.]"
    messages.append({"role": "user", "content": f"{tail}\n\n{final}" if tail else final})
    return messages


def extract_cache_usage(data: Any) -> dict[str, int | None]:
    """Normalises prompt / cached token counts across OpenAI-compatible providers."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "cached_tokens": None, "completion_tokens": None}

    def _int(v: Any) -> int | None:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    cached = None
    for candidate in (
        details.get("cached_tokens"),            # OpenAI-style (also Gemini OpenAI-compat, many gateways)
        usage.get("prompt_cache_hit_tokens"),    # DeepSeek
        usage.get("cache_read_input_tokens"),    # Anthropic-style gateways
        usage.get("cached_tokens"),
    ):
        cached = _int(candidate)
        if cached is not None:
            break
    return {
        "prompt_tokens": _int(usage.get("prompt_tokens")),
        "cached_tokens": cached,
        "completion_tokens": _int(usage.get("completion_tokens")),
    }


def byok_chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = DEFAULT_BYOK_TIMEOUT_S,
) -> str:
    """Calls `<base_url>/chat/completions` (non-streaming) and returns the first choice's text."""
    text, _usage = byok_chat_completion_with_usage(
        base_url, api_key, model, messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout
    )
    return text


def byok_chat_completion_with_usage(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = DEFAULT_BYOK_TIMEOUT_S,
) -> tuple[str, dict[str, int | None]]:
    """Same as `byok_chat_completion`, also returning normalised token usage (incl. cache hits)."""
    if not api_key:
        raise ValueError(f"未找到 BYOK API Key：请设置环境变量 {KEY_ENV_VAR}，或在插件状态目录创建 {KEY_FILE_NAME} 文件。")
    if not model:
        raise ValueError("未设置 BYOK 模型名：请在舞台配置中填写，或设置环境变量 TAVERN_BYOK_MODEL。")
    endpoint = _validate_base_url(base_url) + "/chat/completions"
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with open_url(req, timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            body = json.loads(err.read().decode("utf-8", errors="replace"))
            if isinstance(body, list) and body:
                body = body[0]
            e_obj = body.get("error") if isinstance(body, dict) else None
            if isinstance(e_obj, dict):
                detail = str(e_obj.get("message") or "")[:300]
            elif isinstance(e_obj, str):
                detail = e_obj[:300]
        except Exception:
            detail = ""
        if api_key and api_key in detail:
            detail = detail.replace(api_key, "***")
        raise ValueError(f"BYOK 接口返回 HTTP {err.code}{('：' + detail) if detail else ''}") from None
    except urllib.error.URLError as err:
        raise ValueError(f"BYOK 接口连接失败：{err.reason}") from None
    except TimeoutError:
        raise ValueError(f"BYOK 接口超时（>{int(timeout)}s）") from None

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError("BYOK 接口返回格式无法解析（不是 OpenAI 兼容的 chat/completions 响应）。") from None
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    text = str(content or "").strip()
    if not text:
        raise ValueError("BYOK 接口返回了空回复（可能被服务端安全策略拦截）。")
    return text, extract_cache_usage(data)
