"""Deployment-scoped model metadata and shared text-session budgeting."""

import hashlib
import json
import threading
import time
from functools import lru_cache
from urllib.parse import quote
from urllib.request import Request, urlopen

from filelock import FileLock

from .artifacts import atomic_write_json, read_json_object
from .project_logging import log

_cache = {}
_lock = threading.RLock()
_public_cache = (0, [])
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def public_models():
    """Fetch the anonymous catalogue once for internal metadata discovery."""
    global _public_cache
    with _lock:
        now = time.monotonic()
        expires, rows = _public_cache
        if now >= expires:
            rows = []
            try:
                request = Request(OPENROUTER_MODELS_URL, headers={"Accept": "application/json"})
                with urlopen(request, timeout=5) as response:
                    content = response.read(16 * 1024 * 1024 + 1)
                if len(content) > 16 * 1024 * 1024:
                    raise ValueError("model catalogue exceeds size budget")
                payload = json.loads(content)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError("invalid public model catalogue")
                rows = [r for r in payload["data"] if isinstance(r, dict) and isinstance(r.get("id"), str)]
            except Exception as error:
                log("WARNING", f"Public model catalogue unavailable: {type(error).__name__}")
            _public_cache = (now + (3600 if rows else 60), rows)
        return [dict(row) for row in rows]


def public_model_limit(model):
    """Use the anonymous catalogue as a nominal, not deployment-specific, limit."""
    rows = public_models()
    model_id = model.rsplit("/", 1)[-1]
    if not model_id:
        return None
    limits = {positive(row.get("context_length")) for row in rows
              if row["id"].rsplit("/", 1)[-1] == model_id}
    if len(limits) != 1:
        return None
    value = limits.pop()
    if value:
        log("WARNING", f"Model context budget: OpenRouter public nominal tokens={value}; deployment limit is unverified")
    return value


def positive(value):
    return value if type(value) is int and value > 0 else None


def _endpoint_key(config):
    return hashlib.sha256(config.get("url", "").rstrip("/").encode()).hexdigest()


@lru_cache(maxsize=128)
def _warn_cached_public(provider, model, value):
    log("WARNING", f"Cached OpenRouter public nominal context: provider={provider!r} model={model!r} "
                   f"tokens={value}; deployment limit is unverified")


def configured_limit(config, model):
    entries = config.get("model_info", [])
    if not isinstance(entries, list):
        raise ValueError("provider model_info must be a list of model objects")
    seen, selected = set(), None
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
            raise ValueError("each model_info entry requires a nonempty model id")
        if entry["id"] in seen:
            raise ValueError("duplicate model_info id")
        seen.add(entry["id"])
        limit = entry.get("context_length")
        if limit is not None and positive(limit) is None:
            raise ValueError("model_info context_length must be a positive integer or null")
        endpoint = entry.get("context_length_endpoint")
        if entry["id"] == model and (endpoint is None or endpoint == _endpoint_key(config)):
            selected = limit
    return selected


def log_cached_public_limit(config, provider, model, value):
    for entry in config.get("model_info", []):
        if entry["id"] == model and entry.get("context_length_source") == "openrouter":
            _warn_cached_public(provider, model, value)
            break


def persist_limit(path, provider, config, builtin, model, value, source):
    """Merge one discovered limit without overwriting an explicit override."""
    try:
        with FileLock(str(path) + ".lock", timeout=2):
            try:
                providers = read_json_object(path)
            except FileNotFoundError:
                providers = {}
            current = providers.get(provider, builtin)
            if not isinstance(current, dict) or _endpoint_key(current) != _endpoint_key(config):
                return value
            existing = configured_limit(current, model)
            if existing is not None:
                return existing
            entries = [dict(entry) for entry in current.get("model_info", [])]
            entry = next((item for item in entries if item["id"] == model), None)
            if entry is None:
                entry = {"id": model}
                entries.append(entry)
            entry.update(context_length=value, context_length_source=source,
                         context_length_endpoint=_endpoint_key(current))
            providers[provider] = {**current, "model_info": entries}
            atomic_write_json(path, providers)
    except (OSError, ValueError, TimeoutError) as error:
        log("WARNING", f"Could not persist model context: {type(error).__name__}; keeping in-memory result")
    return value


def metadata_limit(payload, model):
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if isinstance(rows, list):
        payload = next((r for r in rows if isinstance(r, dict) and r.get("id") == model), {})
    if payload.get("id", model) != model:
        return None
    for key in ("context_window", "context_length", "inputTokenLimit", "max_input_tokens"):
        value = positive(payload.get(key))
        if value:
            return value
    return None


def discover(llm):
    config = llm.cfg()
    model = (config.get("request_kwargs") or {}).get("model") or llm.model_name()
    key = hashlib.sha256(json.dumps([config, model, llm.resolve_key()], sort_keys=True).encode()).hexdigest()
    persist = getattr(llm, "persist_context_window", None)
    # Serializing cache misses avoids one discovery per simultaneous batch stage.
    with _lock:
        now = time.monotonic()
        if key in _cache and _cache[key][0] > now:
            _, value, source = _cache[key]
            if value is not None and callable(persist):
                return persist(config, model, value, source)
            return value
        value, payload = None, None
        try:
            with llm._client() as original:
                client = original.with_options(timeout=5.0, max_retries=0)
                endpoint = config.get("url", "").rstrip("/")
                if endpoint == "https://generativelanguage.googleapis.com/v1beta/openai":
                    client = client.with_options(base_url="https://generativelanguage.googleapis.com/v1beta/")
                    payload = client.get("models/" + quote(model.removeprefix("models/"), safe=""), cast_to=dict,
                                         options={"headers": {"x-goog-api-key": llm.resolve_key()}})
                else:
                    payload = client.get("models", cast_to=dict)
                value = metadata_limit(payload, model)
        except Exception as error:
            log("WARNING", f"Model metadata unavailable: {type(error).__name__}; using fallback budget")
        deployment_value = value
        if value is None:
            value = public_model_limit(model)
        source = "provider" if deployment_value is not None else "openrouter"
        if len(_cache) >= 128:
            _cache.clear()
        _cache[key] = (now + (3600 if value else 60), value, source)
        if deployment_value:
            log("INFO", f"Model context budget: metadata tokens={value}")
        elif value is None:
            log("WARNING", "Model metadata has no context limit for the selected model")
        if value is not None and callable(persist):
            return persist(config, model, value, source)
        return value


def estimate(content):
    # Approximate mixed English/JSON and CJK costs without downloading a tokenizer.
    ascii_chars = len(content) if content.isascii() else sum(c.isascii() for c in content)
    return 5 + (ascii_chars + 2) // 3 + 2 * (len(content) - ascii_chars)


def message_cost(message):
    return estimate(json.dumps(message, ensure_ascii=False, separators=(",", ":")))


def compact(session, kwargs, force=False):
    """Remove whole completed turns only; never orphan an active tool exchange."""
    method = getattr(session.llm, "context_window", None)
    if not callable(method):
        return False
    window = method()
    messages = kwargs["messages"]
    if messages is not session.messages:
        log("WARNING", "Custom messages override: context compaction skipped")
        return False
    reserve = max((positive(kwargs.get(k)) or 0 for k in ("max_tokens", "max_completion_tokens")), default=0)
    threshold = min(window * 4 // 5, window - reserve)
    tools_cost = message_cost({k: kwargs[k] for k in ("tools", "functions", "extra_body") if k in kwargs})
    projected = sum(message_cost(m) for m in messages) + tools_cost
    if session.last_prompt_tokens is not None and session.usage_messages is not None:
        previous = session.usage_messages
        if messages[:len(previous)] == previous:
            projected = max(projected, session.last_prompt_tokens
                            + sum(message_cost(m) for m in messages[len(previous):]) + tools_cost)
    if not force and projected < threshold:
        return False
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    # Feedback messages belong to their original request, as do tool responses.
    last = session.request_start if session.request_start is not None else (users[-1] if users else 1)
    if last <= 1:
        log("WARNING", f"LLM input budget pressure: estimated_tokens={projected} window={window}; no removable history")
        return False
    removable = [i for i in users[1:] if i <= last]
    if not removable:
        return False
    cut = removable[-1] if force else removable[0]
    while not force and cut < last and (message_cost(messages[0])
            + sum(message_cost(m) for m in messages[cut:]) + tools_cost) > threshold * 3 // 4:
        cut = next((i for i in removable if i > cut), last)
    del messages[1:cut]
    if session.request_start is not None:
        session.request_start -= cut - 1
    session.last_prompt_tokens = session.usage_messages = None
    log("INFO", f"LLM history compacted: estimated_tokens={projected} window={window} removed_messages={cut - 1}")
    return True
