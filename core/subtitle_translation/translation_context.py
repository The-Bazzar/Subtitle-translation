"""Bound a translation session without summarization calls or transcript edits."""

import json
from collections import deque

from .model_context import configured_limit, discover, estimate, log_cached_public_limit
from .project_logging import log


def window_for(llm):
    config = llm.cfg()
    model = (config.get("request_kwargs") or {}).get("model") or llm.model_name()
    value = configured_limit(config, model)
    if value is not None:
        log_cached_public_limit(config, getattr(llm, "provider", "?"), model, value)
        return value
    if callable(getattr(llm, "resolve_key", None)):
        discovered = discover(llm)
        if discovered:
            return discovered
    endpoint = config.get("url", "").rstrip("/")
    if (endpoint in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
            and model in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"}):
        return 1_000_000
    if endpoint == "https://api.openai.com/v1" and model in {"gpt-4.1-mini", "gpt-4.1-mini-2025-04-14"}:
        return 1_047_576
    if endpoint == "https://generativelanguage.googleapis.com/v1beta/openai" and model == "gemini-3.5-flash":
        return 1_048_576
    if endpoint == "https://openrouter.ai/api/v1" and model == "anthropic/claude-sonnet-4.6":
        return 1_000_000
    log("INFO", f"Unknown model context length for provider={getattr(llm, 'provider', '?')!r} "
                f"model={model!r}; using a 32768-token planning budget")
    return 32768


class TranslationContext:
    def __init__(self, window, source_key, target_key, request_kwargs):
        self.window = window
        reserve = max((v for k, v in request_kwargs.items()
                       if k in {"max_tokens", "max_completion_tokens"} and type(v) is int), default=0)
        self.threshold = min(window * 4 // 5, window - reserve)
        if self.threshold <= 0:
            raise ValueError("configured output token limit leaves no translation input budget")
        self.source_key, self.target_key = source_key, target_key
        self.recent = deque(maxlen=128)

    def remember(self, item_id, source, translation):
        self.recent.append((item_id, source, translation))

    def prepare(self, session, request):
        messages = session.messages
        if len(messages) <= 1:
            return
        next_cost = estimate(request)
        if session.last_prompt_tokens is not None:
            projected = session.last_prompt_tokens + estimate(messages[-1]["content"]) + next_cost
        else:
            projected = sum(estimate(m["content"]) for m in messages) + next_cost
        if projected < self.threshold:
            return
        fixed = estimate(messages[0]["content"]) + next_cost
        # Leave headroom after compaction so subsequent batches can accumulate again.
        available = self.threshold * 3 // 4 - fixed - 32
        sources, targets = [], []
        for item_id, source, target in reversed(self.recent):
            src = json.dumps({"id": item_id, self.source_key: source}, ensure_ascii=False)
            dst = json.dumps({"id": item_id, self.target_key: target}, ensure_ascii=False)
            available -= estimate(src) + estimate(dst)
            if available < 0:
                break
            sources.append(src)
            targets.append(dst)
        del messages[1:]
        if sources:
            for role, items in (("user", sources), ("assistant", targets)):
                messages.append({"role": role, "content": '{"items":[' + ",".join(reversed(items)) + "]}"})
        session.last_prompt_tokens = None
        log("INFO", f"Translation history compacted: projected_tokens={projected} window={self.window} "
                    f"retained_sentence_pairs={len(sources)}")

    def recover_overflow(self, session):
        if len(session.messages) <= 1:
            return False
        del session.messages[1:]
        session.last_prompt_tokens = None
        log("WARNING", "Provider context limit: dropping compressed/prior history for the next translation attempt")
        return True
