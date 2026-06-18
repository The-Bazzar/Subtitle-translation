#!/usr/bin/env python3
"""
glossary_builder.py — AI Agent: 联网搜索 + LLM → 自动生成 glossary.md

依赖: tavily-python (pip install tavily-python)
"""

import json
import os
import sys
from typing import Optional

# 复用 translate_srt 的 dataclass 和 LLM 基础设施
from translate_srt import (
    LLMConfig, SRTContext, load_env, load_providers, parse_srt,
    _read_text_file, get_api_key,
)

_KNOWLEDGE_PROMPT = """You are a terminology expert. Analyze the subtitles and search results to build a glossary.

Output format:
# 术语知识库 — <title>

## 背景
<2-3 sentences summarizing the video topic based on subtitles + search results>

## 核心术语
| 英文 | 推荐译法 | 说明 |
|------|---------|------|
| term | 标准译法 | why this translation, context from the video |

## 态度基调
- <attitude observation from subtitle tone>

## 关键论点
- <core argument repeated throughout>

Rules:
- Only include terms that actually appear in the subtitles
- Search results help verify standard translations — cite them in 说明
- If uncertain about a translation, mark it with (?)
- Keep under 100 lines"""


def tavily_search(query: str, api_key: str, max_results: int = 5) -> list[dict]:
    """Call Tavily Search API."""
    import urllib.request
    import urllib.error

    # Tavily uses POST with api_key in body
    body_with_key = json.dumps({
        'query': query,
        'max_results': max_results,
        'search_depth': 'basic',
        'api_key': api_key,
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.tavily.com/search',
        data=body_with_key,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data.get('results', [])
    except Exception as e:
        print(f"  Warning: Tavily search failed: {e}", file=sys.stderr)
        return []


def build_glossary(
    ctx: SRTContext,
    llm: LLMConfig,
    tavily_key: str = '',
    tavily_max_results: int = 10,
    quiet: bool = False,
) -> str:
    """
    一键生成 glossary.md:
    1. 读取字幕 + tags + description + info.json
    2. 联网搜索 (如有 tavily_key)
    3. LLM 综合分析 → glossary.md
    """
    # ── Step 1: 收集所有上下文 ──────────────────────────────────────────
    if not quiet:
        print(">>> Step 1: Gathering context...", file=sys.stderr)

    # 优先用分句后的 SRT
    srt_to_read = ctx.split_srt if os.path.isfile(ctx.split_srt) else ctx.path
    subs = parse_srt(srt_to_read) if os.path.isfile(srt_to_read) else []
    subs_text = '\n'.join(s['text'] for s in subs) if subs else ''
    if not quiet:
        print(f"  Subtitles: {len(subs)} lines from {srt_to_read}", file=sys.stderr)

    # 读取 .description
    desc_text = ''
    if os.path.isfile(ctx.desc):
        desc_text = _read_text_file(ctx.desc)
        if not quiet:
            print(f"  Description: {len(desc_text)} chars", file=sys.stderr)

    # 读取 .tags.txt
    tags = []
    tags_path = os.path.join(ctx.dir, f'{ctx.name}.tags.txt')
    if os.path.isfile(tags_path):
        try:
            raw = _read_text_file(tags_path)
            for line in raw.strip().split('\n'):
                line = line.strip()
                if line.startswith('[') and line.endswith(']'):
                    try:
                        parsed = __import__('ast').literal_eval(line)
                        if isinstance(parsed, list):
                            tags.extend(parsed)
                    except (ValueError, SyntaxError):
                        pass
            tags = list(dict.fromkeys(tags))  # dedup, keep order
        except Exception:
            pass
    if not quiet:
        print(f"  Tags: {', '.join(tags[:10])}{'...' if len(tags) > 10 else ''}",
              file=sys.stderr)

    # 读取 .info.json 获取标题
    title = ctx.name
    info_json = os.path.join(ctx.dir, f'{ctx.name}.info.json')
    if os.path.isfile(info_json):
        try:
            with open(info_json, 'r', encoding='utf-8') as f:
                info = json.load(f)
            title = info.get('title', ctx.name)
        except Exception:
            pass

    # ── Step 2: 联网搜索 ────────────────────────────────────────────────
    search_text = ''
    if tavily_key:
        if not quiet:
            print(f">>> Step 2: Web search ({len(tags) + 1} queries)...",
                  file=sys.stderr)

        all_results = []
        # 搜索标题 + 前 5 个标签
        queries = [title] + tags[:5]
        for q in queries:
            if not quiet:
                print(f"  Searching: {q[:60]}...", file=sys.stderr)
            results = tavily_search(q, tavily_key, max_results=tavily_max_results)
            all_results.extend(results)

        # 去重
        seen_urls = set()
        unique_results = []
        for r in all_results:
            if r['url'] not in seen_urls:
                seen_urls.add(r['url'])
                unique_results.append(r)

        search_text = '\n\n'.join(
            f"Source: {r['url']}\n{r.get('content', '')[:500]}"
            for r in unique_results[:10]
        )
        if not quiet:
            print(f"  Collected {len(unique_results)} unique results",
                  file=sys.stderr)
    else:
        if not quiet:
            print(">>> Step 2: Skipped (no TAVILY_API_KEY)",
                  file=sys.stderr)

    # ── Step 3: LLM 生成 glossary ──────────────────────────────────────
    if not quiet:
        print(f">>> Step 3: LLM generating glossary ({llm.provider})...",
              file=sys.stderr)

    user_prompt = f"""Title: {title}

Subtitles (excerpt):
{subs_text[:6000]}

Description:
{desc_text[:1000]}

Tags: {', '.join(tags[:20])}

Search results:
{search_text[:4000] if search_text else '(no web search)'}"""

    try:
        resp = llm._client().chat.completions.create(
            model=llm.model_name(),
            messages=[
                {'role': 'system', 'content': _KNOWLEDGE_PROMPT},
                {'role': 'user', 'content': user_prompt},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        glossary = resp.choices[0].message.content
    except Exception as e:
        print(f"Error: LLM call failed: {e}", file=sys.stderr)
        return ''

    # ── Step 4: 写入 glossary.md ──────────────────────────────────────
    with open(ctx.glossary, 'w', encoding='utf-8') as f:
        f.write(glossary)

    if not quiet:
        print(f">>> Done: {ctx.glossary}", file=sys.stderr)

    return glossary


# ─── CLI ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='AI Agent: 联网搜索 + LLM 生成 glossary.md')
    parser.add_argument('srt', help='SRT 字幕文件路径')
    parser.add_argument('--provider', help='LLM 后端 (默认: .env)')
    parser.add_argument('-q', '--quiet', action='store_true')
    args = parser.parse_args()

    if not os.path.isfile(args.srt):
        print(f"Error: SRT not found: {args.srt}", file=sys.stderr)
        sys.exit(1)

    srt_path = os.path.abspath(args.srt)
    ctx = SRTContext.from_path(srt_path)

    _env = load_env(os.path.dirname(os.path.abspath(__file__)))
    _provider = _env.get('TRANSLATE_PROVIDER', '')
    if not _provider:
        print("Error: TRANSLATE_PROVIDER not set in .env.", file=sys.stderr)
        sys.exit(1)
    _model = _env.get('TRANSLATE_MODEL', '') or None
    _tavily_key = _env.get('TAVILY_API_KEY', '')
    _tavily_max_results = int(_env.get('TAVILY_MAX_RESULTS', '10'))

    if not _tavily_key:
        print("Note: TAVILY_API_KEY not set in .env, skipping web search.",
              file=sys.stderr)

    llm = LLMConfig(provider=_provider, model=_model)
    build_glossary(ctx, llm, _tavily_key, _tavily_max_results, args.quiet)


if __name__ == '__main__':
    main()
