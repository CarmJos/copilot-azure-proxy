"""
copilot-azure-proxy -- standalone single-port proxy for JetBrains Copilot (Azure mode).

Accepts Azure-format    ->  translates to OpenAI-compatible backend
  /openai/deployments/{model}/chat/completions

Usage:
  python copilot_azure_proxy.py [--port 4000] [--config config.yaml]

Requires:  pip install aiohttp litellm pyyaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import yaml
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

import litellm


# ======================================================================
#  Logging helpers
# ======================================================================

_LOG_BUF: list[str] = []


def log(emoji: str, msg: str, *args: object) -> None:
    line = f" {emoji} | {msg.format(*args)}"
    print(line, flush=True)
    _LOG_BUF.append(line)
    if len(_LOG_BUF) > 5000:
        _LOG_BUF[:1000] = []


def log_request(method: str, path_qs: str, headers: dict) -> None:
    """Log ALL request details for debugging purposes."""
    global DEBUG
    # When debug is off, only log GET requests (skip POST request bodies)
    if not DEBUG and method.upper() == "POST":
        return
    ct = headers.get("Content-Type", headers.get("content-type", "-"))
    auth = "YES" if "authorization" in headers or "api-key" in headers else "NO"
    # Extract api-version from query string
    api_ver = "-"
    if "?" in path_qs:
        qp = path_qs.split("?", 1)[1]
        for pair in qp.split("&"):
            if pair.startswith("api-version="):
                api_ver = pair.split("=", 1)[1]
                break
    log("📡", "REQ  {}  {}  [api-ver: {}] [CT: {}]  [Auth: {}]",
        method, path_qs, api_ver, ct, auth)


# ======================================================================
#  Config loader
# ======================================================================

class ProxyConfig:
    """Represents a single backend model as defined in config.yaml."""

    __slots__ = (
        "name", "provider", "model", "api_base", "api_key",
        "supports_vision", "supports_function_calling",
        "supports_reasoning", "supports_tool_choice",
        "temperature", "max_tokens", "max_input_tokens", "max_output_tokens",
        "timeout", "extra_headers", "base_model",
    )

    def __init__(self, name: str, params: dict) -> None:
        self.name = name
        self.provider: str = params.get("provider", "openai")
        self.model: str = params.get("model", name)
        self.api_base: str = _resolve(params.get("api_base", ""))
        self.api_key: str = _resolve(params.get("api_key", ""))
        self.supports_vision: bool = params.get("supports_vision", False)
        self.supports_function_calling: bool = params.get("supports_function_calling", False)
        self.supports_reasoning: bool = params.get("supports_reasoning", False)
        self.supports_tool_choice: bool = params.get("supports_tool_choice", False)
        self.temperature: float | None = params.get("temperature")
        self.max_tokens: int | None = params.get("max_tokens")
        self.max_input_tokens: int | None = params.get("max_input_tokens")
        self.max_output_tokens: int | None = params.get("max_output_tokens")
        self.timeout: int = params.get("timeout", 120)
        self.extra_headers: dict = params.get("extra_headers", {}) or {}
        # base_model: the Azure model name to report in responses (e.g. "gpt-4o").
        # If set, JetBrains may recognise this model and use its known context window.
        # If unset, falls back to the deployment name.
        self.base_model: str | None = params.get("base_model")

    def display_model_name(self) -> str:
        """Return the model name to report in Azure responses.

        Uses ``base_model`` if configured, otherwise the deployment name.
        """
        return self.base_model or self.name

    def to_kwargs(self, extra: dict | None = None) -> dict:
        k: dict[str, Any] = {"model": self.model, "timeout": self.timeout}
        if self.api_base:
            k["api_base"] = self.api_base
        if self.api_key:
            k["api_key"] = self.api_key
        if self.temperature is not None:
            k.setdefault("temperature", self.temperature)
        if self.max_tokens is not None:
            k.setdefault("max_tokens", self.max_tokens)
        if self.max_input_tokens is not None:
            k.setdefault("max_input_tokens", self.max_input_tokens)
        if self.max_output_tokens is not None:
            k.setdefault("max_output_tokens", self.max_output_tokens)
        if self.extra_headers:
            k.setdefault("extra_headers", self.extra_headers)
        if extra:
            k.update(extra)
        return k


def _resolve(value: str) -> str:
    """Resolve ``os.environ/KEY`` references to real env values."""
    if isinstance(value, str) and value.startswith("os.environ/"):
        return os.environ.get(value.split("/", 1)[1], "")
    return str(value) if value else ""


def load_config(path: str | Path) -> tuple[dict[str, ProxyConfig], int, int, bool, str]:
    """Parse config.yaml -> {model_name: ProxyConfig} + port + timeout + debug + proxy_api_key."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    models: dict[str, ProxyConfig] = {}
    for entry in raw.get("models", raw.get("model_list", [])):
        name = entry.get("model_name", entry.get("name", ""))
        if not name:
            continue
        params = entry.get("litellm_params", entry.get("params", entry))
        models[name] = ProxyConfig(name, params if isinstance(params, dict) else {})

    settings = raw.get("general", raw.get("general_settings", raw.get("settings", {})))
    port = int(settings.get("port", raw.get("port", 4000)))
    timeout = int(settings.get("timeout", raw.get("request_timeout", 120)))
    debug = bool(settings.get("debug", False))
    proxy_api_key: str = str(settings.get("api-key", "")).strip()

    return models, port, timeout, debug, proxy_api_key


# ======================================================================
#  Helpers
# ======================================================================

def extract_deployment(path: str) -> str | None:
    """Azure URL -> deployment name.

    /openai/deployments/deepseek-v4-pro/chat/completions -> deepseek-v4-pro
    """
    parts = path.strip("/").split("/")
    try:
        idx = parts.index("deployments")
        if idx + 1 >= len(parts):
            return None
        return parts[idx + 1]
    except (ValueError, IndexError):
        return None


def strip_image_url(messages: list[dict]) -> list[dict]:
    """Drop every ``image_url`` content part; unwrap single text parts."""
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        text_parts = [
            p for p in content
            if not (isinstance(p, dict) and p.get("type") == "image_url")
        ]
        if not text_parts:
            continue
        if (len(text_parts) == 1
                and isinstance(text_parts[0], dict)
                and text_parts[0].get("type") == "text"):
            msg = {**msg, "content": text_parts[0]["text"]}
        else:
            msg = {**msg, "content": text_parts}
        out.append(msg)
    return out


def to_azure_error(message: str, code: str = "500") -> dict:
    """Return an Azure-format error dict (JetBrains expects this shape)."""
    return {
        "error": {
            "message": message,
            "type": code,
            "code": code,
            "param": None,
        }
    }


# ======================================================================
#  Azure Model / Deployment definitions
# ======================================================================

def build_deployment_entry(name: str, cfg: ProxyConfig) -> dict:
    """Build a single Azure-format deployment entry from a ProxyConfig.

    Follows the REAL Azure OpenAI API response structure (api-version >= 2024-06-01).
    Includes extra token-limit fields that JetBrains Copilot may read.
    """
    caps: dict[str, bool] = {"chat_completion": True}
    if cfg.supports_function_calling:
        caps["function_calling"] = True
    if cfg.supports_reasoning:
        caps["reasoning"] = True
    if cfg.supports_tool_choice:
        caps["tool_choice"] = True
    if cfg.supports_vision:
        caps["vision"] = True

    now = int(time.time())
    model_name = cfg.display_model_name()

    entry: dict[str, Any] = {
        # -- Real Azure fields --
        "id": name,
        "object": "deployment",
        "model": model_name,            # ← base_model or deployment name
        "owner": "organization-owner",
        "status": "succeeded",
        "created_at": now,
        "updated_at": now,
        "capabilities": caps,
        "scale_settings": {
            "scale_type": "standard",
            "capacity": None,
        },
        "version": "2025-01-01",
        "is_latest_version": True,
        "is_preview": False,
        "rate_limits": [
            {"key": "request", "renewal_period": "PT1M", "count": 100},
            {"key": "tokens",  "renewal_period": "PT1M", "count": cfg.max_input_tokens or 1000000},
        ],

        # -- Extra token-limit fields (not standard Azure, added for JetBrains) --
        "max_tokens": cfg.max_tokens,
        "max_input_tokens": cfg.max_input_tokens,
        "max_output_tokens": cfg.max_output_tokens,

        # -- Even more field name variants that JetBrains might look for --
        "max_context_tokens": cfg.max_input_tokens,
        "max_input_context_tokens": cfg.max_input_tokens,
        "context_window": cfg.max_input_tokens,
        "max_model_tokens": cfg.max_input_tokens,
    }

    return entry


def build_model_entry(name: str, cfg: ProxyConfig) -> dict:
    """Build an entry for the Azure /openai/models endpoint (model catalog)."""
    caps: dict[str, bool] = {"chat_completion": True}
    if cfg.supports_function_calling:
        caps["function_calling"] = True
    if cfg.supports_reasoning:
        caps["reasoning"] = True
    if cfg.supports_tool_choice:
        caps["tool_choice"] = True
    if cfg.supports_vision:
        caps["vision"] = True

    now = int(time.time())
    model_name = cfg.display_model_name()

    return {
        "id": model_name,
        "object": "models",
        "status": "succeeded",
        "created_at": now,
        "owned_by": "organization-owner",
        "capabilities": caps,
        "lifecycle_status": "ga",
        "deprecation": {
            "fine_tune": None,
            "inference": None,
        },
        # Extra token limit fields
        "max_tokens": cfg.max_tokens,
        "max_input_tokens": cfg.max_input_tokens,
        "max_output_tokens": cfg.max_output_tokens,
        "max_context_tokens": cfg.max_input_tokens,
        "max_input_context_tokens": cfg.max_input_tokens,
        "context_window": cfg.max_input_tokens,
        "max_model_tokens": cfg.max_input_tokens,
    }


# ======================================================================
#  Azure Endpoints
# ======================================================================

# -- Deployments -------------------------------------------------------

async def handle_deployments_list(request: web.Request) -> web.Response:
    """GET /openai/deployments — list all configured deployments."""
    data_list = [
        build_deployment_entry(name, cfg)
        for name, cfg in MODELS.items()
    ]
    return web.json_response({
        "object": "list",
        "data": data_list,
    })


async def handle_deployment_detail(request: web.Request) -> web.Response:
    """GET /openai/deployments/{name} — single deployment detail."""
    name = request.match_info.get("name", "")
    cfg = MODELS.get(name)
    if not cfg:
        return web.json_response(
            to_azure_error(f"Deployment '{name}' not found", code="404"),
            status=404)
    return web.json_response(build_deployment_entry(name, cfg))


async def handle_deployment_models(request: web.Request) -> web.Response:
    """GET /openai/deployments/{name}/models — model version info for a deployment."""
    name = request.match_info.get("name", "")
    cfg = MODELS.get(name)
    if not cfg:
        return web.json_response(
            to_azure_error(f"Deployment '{name}' not found", code="404"),
            status=404)

    entry = build_model_entry(name, cfg)
    return web.json_response({
        "object": "list",
        "data": [entry],
    })


# -- Models catalog (Azure) -------------------------------------------

async def handle_models_list(request: web.Request) -> web.Response:
    """GET /openai/models — list available models (model catalog, Azure format)."""
    data_list = [
        build_model_entry(name, cfg)
        for name, cfg in MODELS.items()
    ]
    return web.json_response({
        "object": "list",
        "data": data_list,
    })


# -- Models (OpenAI-compatible) ---------------------------------------

async def handle_v1_models_list(request: web.Request) -> web.Response:
    """GET /v1/models — list models in OpenAI format."""
    data_list = [
        {
            "id": name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "organization-owner",
            "root": name,
            "parent": None,
            # Extra token info
            "max_tokens": cfg.max_tokens,
            "max_input_tokens": cfg.max_input_tokens,
            "max_output_tokens": cfg.max_output_tokens,
            "max_context_tokens": cfg.max_input_tokens,
            "context_window": cfg.max_input_tokens,
        }
        for name, cfg in MODELS.items()
    ]
    return web.json_response({
        "object": "list",
        "data": data_list,
    })


async def handle_v1_model_detail(request: web.Request) -> web.Response:
    """GET /v1/models/{name} — single model detail in OpenAI format."""
    name = request.match_info.get("name", "")
    cfg = MODELS.get(name)
    if not cfg:
        return web.json_response(
            {"error": {"message": f"Model '{name}' not found", "type": "not_found"}},
            status=404)
    return web.json_response({
        "id": name,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "organization-owner",
        "root": name,
        "parent": None,
        # Extra token info
        "max_tokens": cfg.max_tokens,
        "max_input_tokens": cfg.max_input_tokens,
        "max_output_tokens": cfg.max_output_tokens,
        "max_context_tokens": cfg.max_input_tokens,
        "context_window": cfg.max_input_tokens,
    })


# -- Chat completions --------------------------------------------------

def override_model_field(payload: dict, display_model_name_val: str) -> dict:
    """Override the ``model`` field in a chat completion response dict
    so it matches the Azure model name (base_model if configured, else deployment name)."""
    if isinstance(payload, dict) and "model" in payload:
        payload["model"] = display_model_name_val
    return payload


async def handle_chat(request: web.Request) -> web.StreamResponse:
    """Handle /openai/deployments/{name}/chat/completions — the main proxy flow."""

    # ---- resolve model ----
    deployment = extract_deployment(request.path)
    if not deployment:
        return web.json_response(
            to_azure_error(
                "Unrecognized Azure deployment URL. "
                "Expected /openai/deployments/{model}/chat/completions",
                code="404"),
            status=404)

    cfg = MODELS.get(deployment)
    if not cfg:
        return web.json_response(
            to_azure_error(f"Unknown deployment: {deployment}", code="404"),
            status=404)

    log("⚡", "{:<6} {}  ->  {}", request.method, request.path_qs, deployment)

    display_model = cfg.display_model_name()

    # ---- parse body ----
    try:
        body = await request.read()
        data: dict = json.loads(body) if body else {}
    except json.JSONDecodeError as e:
        return web.json_response(
            to_azure_error(f"Invalid JSON: {e}", code="400"),
            status=400)

    # ---- strip image_url ----
    if "messages" in data:
        orig = len(data["messages"])
        data["messages"] = strip_image_url(data["messages"])
        if (dropped := orig - len(data["messages"])):
            log("🖼️", "dropped {} image-only message(s)", dropped)

    # ---- build litellm args ----
    kwargs = cfg.to_kwargs({"messages": data.get("messages", [])})
    for key in ("temperature", "max_tokens", "top_p",
                "frequency_penalty", "presence_penalty",
                "stop", "tools", "tool_choice"):
        if key in data and data[key] is not None:
            kwargs[key] = data[key]

    # ---- call ----
    try:
        if data.get("stream"):
            return await _stream_response(request, kwargs, display_model)
        else:
            return await _non_stream_response(kwargs, display_model)
    except litellm.exceptions.AuthenticationError as e:
        log("🔐", "auth error: {}", str(e)[:120])
        return web.json_response(
            to_azure_error(f"Backend auth error: {e}", code="401"),
            status=401)
    except litellm.exceptions.APIError as e:
        log("❌", "API error: {}", str(e)[:200])
        return web.json_response(
            to_azure_error(f"Backend API error: {e}", code="502"),
            status=502)
    except asyncio.TimeoutError:
        log("⏱️", "timeout for {}", deployment)
        return web.json_response(
            to_azure_error("Request timed out", code="504"),
            status=504)
    except Exception:
        log("💥", "unexpected error:\n{}", traceback.format_exc())
        return web.json_response(
            to_azure_error("Internal proxy error", code="500"),
            status=500)


async def _non_stream_response(kwargs: dict, display_model_name_val: str) -> web.Response:
    result = await litellm.acompletion(**kwargs)
    if hasattr(result, "model_dump"):
        payload: dict = result.model_dump()
    elif hasattr(result, "json"):
        payload = json.loads(result.json())
    else:
        payload = dict(result)
    override_model_field(payload, display_model_name_val)
    # Add Azure-like response headers
    resp = web.json_response(payload)
    resp.headers["x-request-id"] = f"proxy-req-{int(time.time())}"
    return resp


async def _stream_response(request: web.Request, kwargs: dict, display_model_name_val: str) -> web.StreamResponse:
    resp = web.StreamResponse(status=200)
    resp.headers["Content-Type"] = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["x-request-id"] = f"proxy-req-{int(time.time())}"
    await resp.prepare(request)

    interrupted = False
    try:
        completion = await litellm.acompletion(stream=True, **kwargs)
        async for chunk in completion:
            if hasattr(chunk, "model_dump"):
                payload = chunk.model_dump()
            elif hasattr(chunk, "json"):
                payload = json.loads(chunk.json())
            else:
                payload = chunk if isinstance(chunk, dict) else {"_raw": str(chunk)}
            override_model_field(payload, display_model_name_val)
            await resp.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
    except ClientConnectionResetError:
        interrupted = True
        log("🚫", "stream interrupted — client disconnected")
    except Exception:
        log("💥", "stream error:\n{}", traceback.format_exc())
        try:
            err_payload = json.dumps({"error": str(traceback.format_exc())})
            await resp.write(f"data: {err_payload}\n\n".encode("utf-8"))
        except (ClientConnectionResetError, ConnectionResetError):
            interrupted = True
            log("🚫", "stream interrupted while writing error")

    if not interrupted:
        try:
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        except (ClientConnectionResetError, ConnectionResetError):
            log("🚫", "stream interrupted — client disconnected before [DONE]")
    return resp


# ======================================================================
#  Health & Debug endpoints
# ======================================================================

async def handle_health(request: web.Request) -> web.Response:
    """GET /health — simple health check."""
    return web.json_response({
        "status": "ok",
        "proxy": PROXY_NAME,
        "models": list(MODELS.keys()),
    })


async def handle_logs(request: web.Request) -> web.Response:
    """GET /logs — return recent log buffer as JSON."""
    return web.json_response({
        "lines": _LOG_BUF[-200:],
    })


async def handle_catch_all(request: web.Request) -> web.Response:
    """Catch-all handler for any unmatched routes.

    Logs the request details so we can see what JetBrains is calling.
    """
    log("⚠️", "UNMATCHED  {}  {}  [UA: {}] [CT: {}] [Accept: {}]",
        request.method,
        request.path_qs,
        request.headers.get("User-Agent", "-"),
        request.headers.get("Content-Type", "-"),
        request.headers.get("Accept", "-"))

    return web.json_response(
        to_azure_error(f"Not found: {request.method} {request.path}", code="404"),
        status=404)


# ======================================================================
#  Middleware
# ======================================================================

@web.middleware
async def logging_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Log every request and enforce proxy-level api-key if configured."""
    log_request(request.method, request.path_qs, dict(request.headers))

    # -- proxy-level api-key check --
    if PROXY_API_KEY:
        client_key = request.headers.get("api-key", "")
        if client_key != PROXY_API_KEY:
            log("🔐", "REJECTED  {}  {}  —  bad or missing api-key", request.method, request.path_qs)
            return web.json_response(
                to_azure_error("Invalid or missing api-key", code="401"),
                status=401)

    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        log("💥", "middleware error:\n{}", traceback.format_exc())
        raise


# ======================================================================
#  Main application
# ======================================================================

PROXY_NAME = "copilot-azure-proxy"
MODELS: dict[str, ProxyConfig] = {}
DEFAULT_TIMEOUT = 120
DEBUG = False
PROXY_API_KEY: str = ""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=f"{PROXY_NAME} -- Azure-to-OpenAI proxy for JetBrains Copilot")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to YAML config (default: config.yaml)")
    parser.add_argument("--port", type=int, default=0,
                        help="Override port (default: from config)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0)")
    args = parser.parse_args(argv)

    global MODELS, DEFAULT_TIMEOUT, DEBUG, PROXY_API_KEY
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path(__file__).parent / config_path
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    MODELS, config_port, DEFAULT_TIMEOUT, DEBUG, PROXY_API_KEY = load_config(str(config_path))
    port = args.port or config_port or 4000

    if not MODELS:
        print("ERROR: No models defined in config.", file=sys.stderr)
        sys.exit(1)

    log("📋", "config  : {}", config_path)
    if PROXY_API_KEY:
        log("🔑", "apikey : *** ({} chars)", len(PROXY_API_KEY))
    else:
        log("🔓", "apikey : <none> —  accepting all keys")
    log("🚀", "models  : {}", ", ".join(MODELS.keys()))
    for n, c in MODELS.items():
        if c.base_model:
            log("🏷️", "   {}  →  \"{}\" (base_model)", n, c.base_model)
        else:
            log("🏷️", "   {}  →  \"{}\"", n, n)

    log("🌐", "listen  : http://{}:{}", args.host, port)

    app = web.Application(middlewares=[logging_middleware])

    # -- Azure deployments API --
    app.router.add_route("GET", "/openai/deployments", handle_deployments_list)
    app.router.add_route("GET", "/openai/deployments/", handle_deployments_list)
    app.router.add_route("GET", "/openai/deployments/{name}", handle_deployment_detail)
    app.router.add_route("GET", "/openai/deployments/{name}/", handle_deployment_detail)

    # -- Azure deployment models --
    app.router.add_route("GET", "/openai/deployments/{name}/models", handle_deployment_models)
    app.router.add_route("GET", "/openai/deployments/{name}/models/", handle_deployment_models)

    # -- Chat completions --
    app.router.add_route("*", "/openai/deployments/{name}/chat/completions", handle_chat)
    app.router.add_route("*", "/openai/deployments/{name}/chat/completions/", handle_chat)

    # -- Azure model catalog --
    app.router.add_route("GET", "/openai/models", handle_models_list)
    app.router.add_route("GET", "/openai/models/", handle_models_list)

    # -- OpenAI-compatible models endpoint --
    app.router.add_route("GET", "/v1/models", handle_v1_models_list)
    app.router.add_route("GET", "/v1/models/", handle_v1_models_list)
    app.router.add_route("GET", "/v1/models/{name}", handle_v1_model_detail)

    # -- Health / debug --
    app.router.add_route("GET", "/", handle_health)
    app.router.add_route("GET", "/health", handle_health)
    app.router.add_route("GET", "/logs", handle_logs)

    # -- Catch-all (must be last) --
    app.router.add_route("*", "/{tail:.*}", handle_catch_all)

    web.run_app(app, host=args.host, port=port, print=lambda *_: None)


if __name__ == "__main__":
    main()

