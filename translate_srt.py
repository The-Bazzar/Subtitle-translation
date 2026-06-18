#!/usr/bin/env python3
"""
translate_srt.py — 将英文 SRT 字幕翻译为中文

用法:
  python3 translate_srt.py <video.srt> [选项]

输出:
  .zh.srt     — 中文 SRT 翻译缓存 (同目录存在则跳过 LLM)
  .zh.ass     — 仅中文 ASS (style=zh)
  .zh-en.ass  — 双语 ASS (英文 bi-en + 中文 bi-zh, 用于硬压)

特性:
  - 解析 SRT 字幕，忽略时间码只送文本给大模型翻译 (节省 token)
  - 支持 OpenRouter / DeepSeek / Gemini 三种后端
  - 自动检测 .zh.srt 缓存: 已存在则跳过 LLM 直接合成 ASS
  - CJK 中文自动插入 \\N 软换行 (解决中文无空格无法自动换行问题)
  - 自动分块批量翻译，适配长视频字幕
"""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class LLMConfig:
    """LLM 调用配置 — 翻译/校对模型可独立设置, 校对回退翻译."""
    provider: str
    model: str = ''
    proofread_provider: str = ''
    proofread_model: str = ''
    api_key: Optional[str] = None
    batch_size: int = 50

    def pr_provider(self) -> str:
        return self.proofread_provider or self.provider

    def pr_model(self) -> str:
        return self.proofread_model or self.model

    def resolve_key(self) -> str:
        if self.api_key is None:
            self.api_key = get_api_key(self.provider, load_env(os.path.dirname(os.path.abspath(__file__))))
        return self.api_key

    def cfg(self) -> dict:
        return load_providers()[self.provider]

    def model_name(self) -> str:
        return self.model or self.cfg().get('default_model', '')

    def _client(self):
        """懒加载 OpenAI 兼容客户端."""
        from openai import OpenAI
        provider_cfg = self.cfg()
        return OpenAI(
            base_url=provider_cfg['url'],
            api_key=self.resolve_key(),
            default_headers=provider_cfg.get('extra_headers', {}),
        )


@dataclass
class SplitConfig:
    """长句拆分配置."""
    enabled: bool = True
    max_chars: int = 60
    max_duration: float = 3.0


@dataclass
class SRTContext:
    """SRT 文件上下文 — 一个路径进来, 所有派生路径出来."""
    path: str
    dir: str
    name: str
    json: str
    zh_srt: str
    split_srt: str
    zh_en_ass: str
    zh_ass: str
    desc: str
    zh_desc: str
    glossary: str

    @staticmethod
    def from_path(srt_path: str, output_ass: str = '') -> 'SRTContext':
        srt_dir = os.path.dirname(os.path.abspath(srt_path))
        srt_name = (os.path.basename(srt_path)).split(".")[0]
        json_path = os.path.join(srt_dir, f'{srt_name}.json')
        return SRTContext(
            path=srt_path,
            dir=srt_dir,
            name=srt_name,
            json=json_path,
            zh_srt=os.path.join(srt_dir, f'{srt_name}.zh.srt'),
            split_srt=os.path.join(srt_dir, f'{srt_name}.split.srt'),
            zh_en_ass=output_ass or os.path.join(srt_dir, f'{srt_name}.zh-en.ass'),
            zh_ass=os.path.join(srt_dir, f'{srt_name}.zh.ass'),
            desc=os.path.join(srt_dir, f'{srt_name}.description'),
            zh_desc=os.path.join(srt_dir, f'{srt_name}.zh.description'),
            glossary=os.path.join(srt_dir, 'glossary.md'),
        )


# ─── SRT 解析 ──────────────────────────────────────────────────────────────────

_SRT_TIME_RE = re.compile(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})')


def parse_srt_time(time_str: str) -> float:
    """SRT 时间戳 → 秒."""
    m = _SRT_TIME_RE.match(time_str.strip())
    if not m:
        raise ValueError(f"Invalid SRT timestamp: {time_str!r}")
    h, mi, s, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def ass_time(seconds: float) -> str:
    """秒 → ASS 时间戳 (H:MM:SS.ms)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _read_text_file(filepath: str) -> str:
    """读取文本文件，自动检测编码 (utf-8-sig → utf-8 → gbk → latin-1)."""
    for enc in ('utf-8-sig', 'utf-8', 'gbk', 'latin-1'):
        try:
            with open(filepath, 'r', encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    # last resort
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def parse_srt(filepath: str) -> list[dict]:
    """
    解析 SRT 文件，返回 [{index, start, end, start_ass, end_ass, text}, ...].
    start/end 是秒 (float)，start_ass/end_ass 是 ASS 格式字符串.
    """
    content = _read_text_file(filepath)

    blocks = re.split(r'\n\s*\n', content.strip())
    subtitles = []

    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(lines) < 2:
            continue
        try:
            index = int(lines[0])
        except ValueError:
            continue

        m = re.match(
            r'(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})',
            lines[1]
        )
        if not m:
            continue

        start = parse_srt_time(m.group(1))
        end = parse_srt_time(m.group(2))
        text = '\n'.join(lines[2:])

        subtitles.append({
            'index': index,
            'start': start,
            'end': end,
            'start_ass': ass_time(start),
            'end_ass': ass_time(end),
            'text': text,
        })

    return subtitles


# ─── LLM 翻译 ──────────────────────────────────────────────────────────────────

# 内置默认提供商 (providers.json 缺失时的回退)
_BUILTIN_PROVIDERS = {
    'openrouter': {
        'url': 'https://openrouter.ai/api/v1',
        'default_model': 'anthropic/claude-sonnet-4-6',
        'env_key': 'OPENROUTER_API_KEY',
        'auth_header': 'Bearer {api_key}',
        'extra_headers': {
            'HTTP-Referer': 'https://github.com/oculr/Subtitle-translation',
            'X-Title': 'Subtitle Translation',
        },
    },
    'deepseek': {
        'url': 'https://api.deepseek.com/v1',
        'default_model': 'deepseek-v4-pro',
        'env_key': 'DEEPSEEK_API_KEY',
        'auth_header': 'Bearer {api_key}',
        'extra_headers': {},
    },
    'gemini': {
        'url': 'https://generativelanguage.googleapis.com/v1beta/openai',
        'default_model': 'gemini-2.5-pro',
        'env_key': 'GEMINI_API_KEY',
        'auth_header': 'Bearer {api_key}',
        'extra_headers': {},
    },
}

_providers_cache = None

def load_providers() -> dict:
    """从 providers.json 加载提供商配置 (不存在则用内置默认)."""
    global _providers_cache
    if _providers_cache is not None:
        return _providers_cache
    import json as _json
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'providers.json')
    if os.path.isfile(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as _f:
                _providers_cache = _json.load(_f)
            if _providers_cache:
                return _providers_cache
        except (_json.JSONDecodeError, OSError):
            pass
    _providers_cache = dict(_BUILTIN_PROVIDERS)
    return _providers_cache


def load_description(desc_path: str) -> str:
    """
    Read .description file and format as context for the translation prompt.
    Returns empty string if file doesn't exist or is empty.
    """
    if not desc_path or not os.path.isfile(desc_path):
        return ""
    try:
        with open(desc_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            return ""
        # Truncate if too long
        if len(content) > 2000:
            content = content[:2000].rsplit('\n', 1)[0]
        return (
            "\n\nThe following is the description of the video being translated. "
            "Use this context to understand domain-specific vocabulary, "
            "proper names, and the overall topic of the video:\n\n"
            + content
        )
    except OSError:
        return ""


def load_glossary(glossary_path: str) -> str:
    """
    读取 glossary.md 术语知识库, 转化为可注入 prompt 的文本.
    文件不存在或为空则返回空字符串.
    """
    if not glossary_path or not os.path.isfile(glossary_path):
        return ""
    try:
        with open(glossary_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            return ""
        return (
            "\n\n以下是本视频的术语知识库, "
            "请在翻译和校对时严格遵循其中的术语理解、推荐译法、语气判断和一致性要求:\n\n"
            + content
        )
    except OSError:
        return ""


# 内置回退提示词 (translate_prompt.md / proofread_prompt.md 缺失时使用)
_TRANSLATE_PROMPT_FALLBACK = """You are a professional subtitle translator specializing in English to Simplified Chinese.

Rules:
- Translate each numbered line 1:1 to natural, fluent Chinese
- Match the tone of the original: casual stays casual, formal stays formal
- Keep proper nouns, brand names, and technical terms in original form unless a standard Chinese translation exists
- Do not skip, merge, split, or add items — exactly N input lines -> N output lines
- Each input is already a short, self-contained segment; translate it as-is

Netflix Chinese formatting: omit all punctuation marks (。，！？；：) except 、and 《》.
Use a single space for natural pauses where punctuation was removed."""

_PROOFREAD_PROMPT_FALLBACK = """You are a bilingual (EN+ZH) subtitle proofreader. Review each pair and fix both languages.

Step 1 — Check English for common ASR errors:
- Homophone confusion (their/there, its/it's, to/too, your/you're)
- Garbled proper names, brand names, or technical terms
- Missing or extra negation (not, never, don't)
- Obvious grammar breaks that distort meaning
Fix any errors found.

Step 2 — Check Chinese against the (corrected) English:
- Fix mistranslations, omissions, or added content
- Improve awkward phrasing — read fluently as spoken subtitles
- Fix tone mismatches — register must match the original
- Netflix formatting: no 。，！？；：, keep 、and 《》, use spaces for pauses

Do not merge, split, or reorder — exactly N input pairs -> N output lines.
  ..."""


def load_prompt(filename: str, fallback: str) -> str:
    """
    从文件加载提示词.
    - 先尝试 <filename>.md (用户自定义, gitignored)
    - 再尝试 <filename>.example.md (仓库模板)
    - 都不存在则返回 fallback 字符串
    """
    base = os.path.dirname(os.path.abspath(__file__))
    for suffix in ('.md', '.example.md'):
        path = os.path.join(base, filename + suffix)
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if content:
                    return content
            except OSError:
                pass
    return fallback


def load_env(script_dir: str) -> dict[str, str]:
    """Load key=value pairs from .env file (if present)."""
    env = dict(os.environ)
    env_path = os.path.join(script_dir, '.env')
    if os.path.isfile(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key, val = key.strip(), val.strip()
                if key and key not in env:
                    env[key] = val
    return env


def get_api_key(provider: str, env: dict[str, str]) -> str:
    """Retrieve API key from environment or .env file."""
    key_name = load_providers()[provider]['env_key']
    key = env.get(key_name, '')
    if not key:
        print(f"Error: {key_name} not found in environment or .env file.",
              file=sys.stderr)
        print(f"Set it in .env: {key_name}=your_key_here", file=sys.stderr)
        sys.exit(1)
    return key


def translate_batch(
    texts: list[str],
    llm: LLMConfig,
    system_prompt: str = "",
    quiet: bool = False,
    retries: int = 3,
) -> list[str]:
    """
    Send a batch of numbered texts to the LLM for 1:1 translation.
    Returns translated texts in the same order.
    """
    prompt_lines = [f"[{i}] {t}" for i, t in enumerate(texts, 1)]
    prompt = "\n".join(prompt_lines)

    last_error = None
    for attempt in range(retries):
        try:
            resp = llm._client().chat.completions.create(
                model=llm.model_name(),
                messages=[
                    {'role': 'system',
                     'content': system_prompt or _TRANSLATE_PROMPT_FALLBACK},
                    {'role': 'user', 'content': (
                        f"Translate these {len(texts)} subtitle lines to Chinese. "
                        f"Respond with exactly {len(texts)} numbered lines.\n\n{prompt}"
                    )},
                ],
                temperature=0.3,
                max_tokens=max(4096, len(texts) * 200),
            )
            return _parse_numbered_response(resp.choices[0].message.content, len(texts))

        except Exception as e:
            last_error = str(e)

        if attempt < retries - 1:
            wait = (attempt + 1) * 3
            if not quiet:
                print(f"  Retry {attempt + 1}/{retries} in {wait}s...", file=sys.stderr)
            time.sleep(wait)

    print(f"Error: Translation failed after {retries} attempts: {last_error}",
          file=sys.stderr)
    return [f"[ERROR] {t}" for t in texts]


def _parse_numbered_response(content: str, expected_count: int) -> list[str]:
    """Parse LLM response like '[1] text\\n[2] text\\n...' into a list."""
    # Find all [N] prefixed lines
    pattern = re.compile(r'\[(\d+)\]\s*(.*?)(?=\[(?:\d+)\]|\Z)', re.DOTALL)
    matches = pattern.findall(content)

    if not matches:
        # Fallback: try to parse line-by-line
        lines = [l.strip() for l in content.split('\n') if l.strip()]
        # Filter out non-translation lines
        translations = []
        for line in lines:
            m = re.match(r'^(?:\[?\d+\]?[.\s]*)?(.+)', line)
            if m:
                translations.append(m.group(1).strip())
        if len(translations) >= expected_count * 0.8:
            # Pad/truncate to expected count
            while len(translations) < expected_count:
                translations.append('')
            return translations[:expected_count]

        print(f"  Warning: Could not parse numbered response. "
              f"Got {len(translations)} lines, expected {expected_count}.",
              file=sys.stderr)
        return translations[:expected_count] if translations else [''] * expected_count

    # Sort by number and extract text
    result = {}
    for num_str, text in matches:
        num = int(num_str)
        text = text.strip()
        result[num] = text

    return [result.get(i, '') for i in range(1, expected_count + 1)]


# ─── 长句拆分 ────────────────────────────────────────────────────────────────

_SPLIT_PROMPT = r"""You are a subtitle splitter. Split long subtitle lines into shorter segments at natural pause points.

Rules:
- Split at: commas, clause boundaries, conjunctions (but, and, because, which, that, when, if, while...)
- Each segment MUST use words from the original — do NOT rephrase or change any words
- Each segment: 15-55 characters, keep meaning self-contained
- Preserve ALL original words across segments
- Return segments joined with `\N` (capital N, backslash N)

Example:
Input:  "As I film and compose more soundtracks, I've begun to view the two art forms as analogous."
Output: "As I film and compose more soundtracks,\NI've begun to view the two art forms as analogous."

Return ONLY one line per input, prefixed with the original index:
1: split text with \N separators
2: split text with \N separators"""


def _load_word_json(json_path: str) -> dict[tuple[float, float], list[dict]]:
    """加载 WhisperX word-level JSON → {(start, end): [words...]}."""
    import json as _json
    with open(json_path, 'r', encoding='utf-8') as f:
        data = _json.load(f)
    result = {}
    for seg in data.get('segments', []):
        words = seg.get('words', [])
        if words:
            result[(round(seg['start'], 2), round(seg['end'], 2))] = words
    return result


def _find_words_time(words: list[dict], seg_text: str, offset: int = 0) -> Optional[tuple[float, float]]:
    """在词列表中匹配 seg_text, 返回 (start, end)."""
    seg_words = seg_text.strip().split()
    if not seg_words:
        return None
    n = len(seg_words)
    first_w = seg_words[0].lower().strip(',.!?;:')
    for i in range(offset, len(words) - n + 1):
        if words[i]['word'].lower().strip(',.!?;:') != first_w:
            continue
        match = True
        for j in range(1, n):
            wj = words[i + j]['word'].lower().strip(',.!?;:')
            sj = seg_words[j].lower().strip(',.!?;:')
            if wj != sj:
                match = False
                break
        if match:
            return (words[i]['start'], words[i + n - 1]['end'])
    return None


def split_subtitles(
    subtitles: list[dict],
    ctx: SRTContext,
    llm: LLMConfig,
    split: SplitConfig,
    quiet: bool = False,
) -> list[dict]:
    """LLM 辅助拆分过长字幕。优先词级 JSON 对轴, 不可用时字符比例回退."""
    word_data: dict = {}
    if os.path.isfile(ctx.json):
        word_data = _load_word_json(ctx.json)
        if not quiet:
            print(f"  Word timestamps: {len(word_data)} segments from JSON",
                  file=sys.stderr)

    long_indices, long_texts = [], []
    for i, sub in enumerate(subtitles):
        if len(sub['text']) > split.max_chars or (sub['end'] - sub['start']) > split.max_duration:
            long_indices.append(i)
            long_texts.append(sub['text'])

    if not long_texts:
        return subtitles

    if not quiet:
        print(f"  Splitting {len(long_texts)} long subtitle(s) ({llm.provider})...",
              file=sys.stderr)

    prompt_lines = [f"{i + 1}: {t}" for i, t in enumerate(long_texts)]
    prompt = "Split each long subtitle at natural pause points:\n\n" + "\n".join(prompt_lines)

    content = ''
    try:
        resp = llm._client().chat.completions.create(
            model=llm.model_name(),
            messages=[
                {'role': 'system', 'content': _SPLIT_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.1,
        )
        content = resp.choices[0].message.content
    except Exception as e:
        if not quiet:
            print(f"  Warning: split LLM call failed: {e}, keeping original",
                  file=sys.stderr)
        return subtitles

    # 解析 LLM 返回
    splits: dict[int, list[str]] = {}
    for line in content.strip().split('\n'):
        line = line.strip()
        if not line or r'\N' not in line:
            continue
        m = re.match(r'^(\d+):\s*(.+)$', line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if idx < 0 or idx >= len(long_indices):
            continue
        segs = [s.strip() for s in m.group(2).split(r'\N') if s.strip()]
        if len(segs) >= 2:
            orig_i = long_indices[idx]
            splits[orig_i] = segs

    # 生成新字幕列表（优先词级 JSON，回退字符比例）
    new_subs = []
    for i, sub in enumerate(subtitles):
        if i not in splits:
            new_subs.append(sub)
            continue

        segments = splits[i]
        words = None
        if word_data:
            key = (round(sub['start'], 2), round(sub['end'], 2))
            words = word_data.get(key)
            if words is None:
                for k in word_data:
                    if abs(k[0] - sub['start']) < 0.5 and abs(k[1] - sub['end']) < 0.5:
                        words = word_data[k]
                        break

        if words:
            word_offset = 0
            for seg in segments:
                tr = _find_words_time(words, seg, word_offset)
                if tr:
                    new_subs.append({'start': tr[0], 'end': tr[1], 'text': seg})
                    word_offset += len(seg.strip().split())
                else:
                    # 回退
                    dur = sub['end'] - sub['start']
                    new_subs.append({
                        'start': sub['start'] + dur * word_offset / max(len(sub['text']), 1),
                        'end': sub['end'],
                        'text': seg,
                    })
        else:
            orig_len = max(len(sub['text']), 1)
            duration = sub['end'] - sub['start']
            char_pos = 0
            for seg in segments:
                seg_len = len(seg)
                r1 = char_pos / orig_len
                r2 = (char_pos + seg_len) / orig_len
                char_pos += seg_len
                new_subs.append({
                    'start': sub['start'] + duration * r1,
                    'end': sub['start'] + duration * r2,
                    'text': seg,
                })

    if not quiet:
        added = len(new_subs) - len(subtitles)
        print(f"  Split {len(splits)} subtitle(s), "
              f"{len(subtitles)} → {len(new_subs)} (+{added})",
              file=sys.stderr)

    return new_subs


def translate_subtitles(
    subtitles: list[dict],
    llm: LLMConfig,
    system_prompt: str = "",
    quiet: bool = False,
) -> list[str]:
    """Translate all subtitle texts in batches."""
    texts = [s['text'] for s in subtitles]
    _ = llm.resolve_key()

    if not quiet:
        print(f"Translator: {llm.provider} / {llm.model_name()}")
        print(f"Total lines: {len(texts)}\n")

    all_translations = []
    total_batches = (len(texts) + llm.batch_size - 1) // llm.batch_size
    for batch_idx in range(total_batches):
        start_idx = batch_idx * llm.batch_size
        end_idx = min(start_idx + llm.batch_size, len(texts))
        if not quiet:
            print(f"  Batch {batch_idx + 1}/{total_batches}: "
                  f"translating lines {start_idx + 1}-{end_idx}...", file=sys.stderr)
        all_translations.extend(translate_batch(
            texts[start_idx:end_idx], llm, system_prompt=system_prompt + _TRANSLATE_FORMAT, quiet=quiet))
    return all_translations


_TRANSLATE_FORMAT = "\n\nRespond with numbered lines only:\n  [1] translation\n  [2] translation\n  ..."
_PROOFREAD_FORMAT = "\n\nReturn each line as: [N] english ||| chinese\nDo NOT include EN:/ZH: prefixes in output. Keep original English if no ASR errors. Do not merge, split, or reorder."


def proofread_subtitles(
    subtitles: list[dict],
    translations: list[str],
    llm: LLMConfig,
    system_prompt: str = "",
    quiet: bool = False,
) -> tuple[list[str], list[str]]:
    """双语校对: 返回 (corrected_en, corrected_zh)."""
    en_texts = [s['text'] for s in subtitles]
    pr_llm = LLMConfig(
        provider=llm.pr_provider(), model=llm.pr_model(),
        api_key=llm.api_key, batch_size=max(15, llm.batch_size // 2),
    )

    if not quiet:
        print(f"Proofreader: {pr_llm.provider} / {pr_llm.model_name()}")
        print(f"Total lines: {len(en_texts)}\n")

    corrected_en, corrected_zh = [], []
    total_batches = (len(en_texts) + pr_llm.batch_size - 1) // pr_llm.batch_size
    for batch_idx in range(total_batches):
        start_idx = batch_idx * pr_llm.batch_size
        end_idx = min(start_idx + pr_llm.batch_size, len(en_texts))
        pairs = [f"EN: {en_texts[i]}\nZH: {translations[i]}" for i in range(start_idx, end_idx)]
        if not quiet:
            print(f"  Batch {batch_idx + 1}/{total_batches}: "
                  f"proofreading lines {start_idx + 1}-{end_idx}...", file=sys.stderr)
        results = translate_batch(pairs, pr_llm, system_prompt=system_prompt + _PROOFREAD_FORMAT, quiet=quiet)
        for r in results:
            if '|||' in r:
                en, zh = r.split('|||', 1)
                corrected_en.append(en.strip())
                corrected_zh.append(zh.strip())
            else:
                # LLM 没按格式返回，保留原文
                idx = len(corrected_en)
                corrected_en.append(en_texts[start_idx + idx] if idx < len(en_texts) else '')
                corrected_zh.append(r.strip())
    n = len(en_texts)
    return corrected_en[:n], corrected_zh[:n]


# ─── ASS 输出 ──────────────────────────────────────────────────────────────────

DESCRIPTION_TRANSLATE_PROMPT = """You are a professional translator. Translate the following YouTube video title, description, and tags from English to Simplified Chinese.

The input format is:
  Title: <original title>
  Description:
  <description text>
  Tags:
  <comma-separated tags>

Rules:
- First line of your response: translated title ONLY (one line)
- Then a blank line
- Then the translated description
- Then a blank line
- Then "标签：" followed by the translated tags (comma-separated)
- Preserve all URLs, email addresses, and social media handles exactly as-is
- Preserve all line breaks and paragraph structure in the description
- Translate naturally while keeping the original tone
- Do NOT add any explanations, preamble, or closing remarks"""


def translate_description(
    ctx: SRTContext,
    llm: LLMConfig,
    quiet: bool = False,
) -> str:
    """Translate .description to Chinese, output .zh.description."""
    import json

    # 从 .info.json 读取视频元数据
    info_json = os.path.join(ctx.dir, f'{ctx.name}.info.json')
    metadata_header = ""
    title = ""
    if os.path.isfile(info_json):
        try:
            with open(info_json, 'r', encoding='utf-8') as f:
                info = json.load(f)
            title = info.get('title', '')
            webpage_url = info.get('webpage_url', '')
            uploader = info.get('uploader', '') or info.get('channel', '')
            # ISO 8601 时间: timestamp 优先 (精确到秒+时区), upload_date 回退
            ts = info.get('timestamp')
            if ts:
                from datetime import datetime, timezone
                upload_time = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')
                # 插入冒号使时区符合 ISO 8601: +0000 → +00:00
                upload_time = upload_time[:-2] + ':' + upload_time[-2:]
            else:
                upload_date = info.get('upload_date', '')
                if upload_date and len(upload_date) == 8:
                    upload_time = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
                else:
                    upload_time = ''
            metadata_header = (
                f"原视频：{webpage_url}\n"
                f"原标题：{title}\n"
                f"原作者：{uploader}\n"
                f"上传时间：{upload_time}\n"
                f"\n=====\n\n"
            )
        except Exception:
            pass

    if os.path.isfile(ctx.desc):
        with open(ctx.desc, 'r', encoding='utf-8') as f:
            desc_text = f.read()
    else:
        desc_text = ""

    tags_text = ""
    tags_path = os.path.join(ctx.dir, f'{ctx.name}.tags.txt')
    if os.path.isfile(tags_path):
        try:
            with open(tags_path, 'r', encoding='utf-8') as f:
                raw = f.read().strip()
            all_tags = []
            for line in raw.split('\n'):
                line = line.strip()
                if line.startswith('[') and line.endswith(']'):
                    try:
                        parsed = __import__('ast').literal_eval(line)
                        if isinstance(parsed, list):
                            all_tags.extend(parsed)
                    except (ValueError, SyntaxError):
                        pass
            seen = set()
            unique = []
            for t in all_tags:
                if t.lower() not in seen:
                    seen.add(t.lower())
                    unique.append(t)
            if unique:
                tags_text = ', '.join(unique)
        except Exception:
            pass

    prompt = f"Title: {title}\n\nDescription:\n{desc_text}"
    if tags_text:
        prompt += f"\n\nTags:\n{tags_text}"

    if not desc_text.strip() and not title:
        with open(ctx.zh_desc, 'w', encoding='utf-8') as f:
            f.write(metadata_header)
        if not quiet:
            print(f"  .zh.description: {ctx.zh_desc}")
        return ctx.zh_desc

    try:
        resp = llm._client().chat.completions.create(
            model=llm.model_name(),
            messages=[
                {'role': 'system', 'content': DESCRIPTION_TRANSLATE_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.3,
            max_tokens=max(2048, (len(prompt) * 2)),
        )
        response = resp.choices[0].message.content
    except Exception as e:
        print(f"  Warning: Description translation failed: {e}", file=sys.stderr)
        return ctx.zh_desc

    # 解析: 第一行=中文标题, 空行后=翻译简介
    lines = response.strip().split('\n', 1)
    zh_title = lines[0].strip() if lines else ''
    zh_desc = lines[1].strip() if len(lines) > 1 else ''

    with open(ctx.zh_desc, 'w', encoding='utf-8') as f:
        if zh_title:
            f.write(f"{zh_title}\n\n")
        f.write(metadata_header)
        f.write(zh_desc)

    if not quiet:
        print(f"  .zh.description: {ctx.zh_desc}")

    return ctx.zh_desc


def load_template(template_path: str) -> tuple[str, str]:
    """
    Read template.ass, return (header_part, events_format_line).
    header_part = everything up to and including the [Events] Format line.
    """
    with open(template_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Split at [Events] section
    events_pos = content.find('\n[Events]\n')
    if events_pos == -1:
        print("Error: template.ass missing [Events] section.", file=sys.stderr)
        sys.exit(1)

    header = content[:events_pos + 1]  # including leading newline
    events_section = content[events_pos + 1:]

    # Extract the Format line
    format_match = re.search(r'Format:.*', events_section)
    if not format_match:
        print("Error: template.ass [Events] section missing Format line.",
              file=sys.stderr)
        sys.exit(1)

    events_header = '\n[Events]\n' + format_match.group(0) + '\n'
    return header, events_header


def format_srt_time(seconds: float) -> str:
    """秒 → SRT 时间戳 (HH:MM:SS,mmm)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = min(int(round((seconds - int(seconds)) * 1000)), 999)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt_generic(output_path: str, subtitles: list[dict], texts: Optional[list[str]] = None):
    """写入 SRT 文件. texts=None 时取 sub['text'] (英文), 否则用 texts (中文翻译)."""
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, sub in enumerate(subtitles, 1):
            text = texts[i - 1] if texts else sub['text']
            f.write(f"{i}\n")
            f.write(f"{format_srt_time(sub['start'])} --> {format_srt_time(sub['end'])}\n")
            f.write(f"{text}\n\n")


def write_zh_srt(output_path: str, subtitles: list[dict], translations: list[str]):
    """写入 .zh.srt 中文翻译缓存."""
    write_srt_generic(output_path, subtitles, translations)


def wrap_cjk(text: str, max_chars: int = 25) -> str:
    """
    在 CJK 文本中插入 \\N 软换行。
    拉丁文字靠空格分词自动换行, 但中文无空格, 渲染器不知道在哪断开。
    此函数按字符数断行, 尽量在标点处断开。
    """
    if len(text) <= max_chars:
        return text
    # 已有 \\N 则不再处理
    if '\\N' in text:
        return text

    result = []
    current = ''
    punct = '，。！？、；：）》」』】》'
    for ch in text:
        current += ch
        if len(current) >= max_chars and ch in punct:
            result.append(current)
            current = ''
    if current:
        # 残尾太短就拼到上一行
        if result and len(current) < max_chars * 0.4:
            result[-1] += current
        else:
            result.append(current)
    return '\\N'.join(result)


def write_ass(
    output_path: str,
    template_path: str,
    title: str,
    subtitles: list[dict],
    translations: list[str],
    bilingual: bool = True,
):
    """
    Write ASS file.
    - bilingual=True:  .zh-en.ass (English bi-en + Chinese bi-zh)
    - bilingual=False: .zh.ass (Chinese only, style zh)
    """
    header, events_header = load_template(template_path)

    # Fill in Title
    header = header.replace('Title:', f'Title: {title}')
    header = re.sub(r'Title:\s*\n', f'Title: {title}\n', header)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(header)
        f.write(events_header)

        if bilingual:
            # English original lines — style bi-en
            for sub in subtitles:
                text = sub['text'].replace('\n', '\\N')
                f.write(
                    f"Dialogue: 0,{sub['start_ass']},{sub['end_ass']},"
                    f"bi-en,,0,0,0,,{text}\n"
                )

        # Chinese translations (CJK 无空格, 需手动换行)
        ch_style = 'bi-zh' if bilingual else 'zh'
        for sub, trans in zip(subtitles, translations):
            text = trans.replace('\n', '\\N')
            text = wrap_cjk(text)
            f.write(
                f"Dialogue: 0,{sub['start_ass']},{sub['end_ass']},"
                f"{ch_style},,0,0,0,,{text}\n"
            )


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    # 从 .env 读取默认配置 (命令行参数可覆盖)
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _env = load_env(_script_dir)
    _providers = load_providers()
    _provider_keys = list(_providers.keys())
    _env_provider = _env.get('TRANSLATE_PROVIDER', '')
    if not _env_provider:
        print(f"Error: TRANSLATE_PROVIDER not set in .env. "
              f"Available: {', '.join(_provider_keys)}", file=sys.stderr)
        sys.exit(1)
    _env_model = _env.get('TRANSLATE_MODEL', '') or None

    parser = argparse.ArgumentParser(
        description='翻译 SRT 英文字幕为中文, 输出双语 .zh-en.ass (bi-en + bi-zh).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  %(prog)s video.srt                          # 输出 .zh-en.ass 双语字幕
  %(prog)s video.srt -t my_template.ass       # 指定模板
  %(prog)s video.srt -o output.zh-en.ass      # 指定输出路径
  %(prog)s video.srt --batch-size 30          # 每批 30 条 (默认 50)

翻译后端/模型从 .env 读取 (TRANSLATE_PROVIDER, TRANSLATE_MODEL)
        """,
    )

    parser.add_argument('srt', help='SRT 字幕文件路径')
    parser.add_argument('-t', '--template',
                        help='template.ass 模板路径 (默认: 脚本同目录)')
    parser.add_argument('-o', '--output',
                        help='输出 .zh-en.ass 路径 (默认: SRT 同目录, 同名 .zh-en.ass)')
    parser.add_argument('--batch-size', type=int, default=50,
                        help='每批翻译行数 (默认: 50)')
    parser.add_argument('--no-split', action='store_true',
                        help='禁用长句拆分 (默认: 自动拆分 >60字符 或 >3秒的句子)')
    parser.add_argument('--split-max-chars', type=int, default=60,
                        help='触发拆分的最大字符数 (默认: 60)')
    parser.add_argument('--split-max-duration', type=float, default=3.0,
                        help='触发拆分的最大时长秒 (默认: 3.0)')
    parser.add_argument('--proofread', action='store_true', default=True,
                        help='中英校对 (默认开启)')
    parser.add_argument('--glossary', metavar='PATH',
                        help='glossary.md 术语知识库路径 (注入翻译和校对 prompt)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='静默模式')

    args = parser.parse_args()

    if not os.path.isfile(args.srt):
        print(f"Error: SRT file not found: {args.srt}", file=sys.stderr)
        sys.exit(1)

    srt_path = os.path.abspath(args.srt)
    ctx = SRTContext.from_path(srt_path, args.output or '')
    script_dir = os.path.dirname(os.path.abspath(__file__))

    template_path = args.template or os.path.join(script_dir, 'template.ass')
    if not os.path.isfile(template_path):
        print(f"Error: template.ass not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    llm = LLMConfig(
        provider=_env_provider,
        model=_env_model,
        proofread_provider=_env.get('PROOFREAD_PROVIDER', ''),
        proofread_model=_env.get('PROOFREAD_MODEL', ''),
        batch_size=args.batch_size,
    )
    split = SplitConfig(
        enabled=not args.no_split,
        max_chars=args.split_max_chars, max_duration=args.split_max_duration,
    )

    # ── Parse SRT ──────────────────────────────────────────────────────────
    if not args.quiet:
        print(f"SRT:      {srt_path}")
    subtitles = parse_srt(srt_path)
    if not subtitles:
        print("Error: No subtitles found in SRT file.", file=sys.stderr)
        sys.exit(1)
    if not args.quiet:
        print(f"  Parsed {len(subtitles)} subtitle entries")

    system_prompt = load_prompt('translate_prompt', _TRANSLATE_PROMPT_FALLBACK)
    proofread_prompt = load_prompt('proofread_prompt', _PROOFREAD_PROMPT_FALLBACK)

    desc_context = load_description(ctx.desc)
    if desc_context:
        system_prompt += desc_context
        proofread_prompt += desc_context
        if not args.quiet:
            print(f"Description: .description 已注入到翻译/校对提示词 ({ctx.desc})")

    glossary_path = args.glossary or (ctx.glossary if os.path.isfile(ctx.glossary) else None)
    glossary_text = load_glossary(glossary_path)
    if glossary_text and not args.quiet:
        print(f"\nGlossary: glossary.md 已加载, 将注入翻译和校对提示词")
        system_prompt += glossary_text
        proofread_prompt += glossary_text

    # ── 长句拆分 ──────────────────────────────────────────────────────────
    if split.enabled:
        if os.path.isfile(ctx.split_srt):
            subtitles = parse_srt(ctx.split_srt)
            if not args.quiet:
                print(f"\n  .split.srt 缓存已存在, 跳过 LLM 分句: {ctx.split_srt}")
        else:
            subtitles = split_subtitles(subtitles, ctx, llm, split, args.quiet)
            for sub in subtitles:
                if 'start_ass' not in sub:
                    sub['start_ass'] = ass_time(sub['start'])
                if 'end_ass' not in sub:
                    sub['end_ass'] = ass_time(sub['end'])
            write_srt_generic(ctx.split_srt, subtitles)
            if not args.quiet:
                print(f"  .split.srt: {ctx.split_srt}")

        if not args.quiet:
            print(f"  Duration: {subtitles[0]['start_ass']} → {subtitles[-1]['end_ass']}")

    # ── 获取中文翻译 ──────────────────────────────────────────────────────
    if os.path.isfile(ctx.zh_srt):
        if not args.quiet:
            print(f"\nCache:    已有 .zh.srt, 跳过 LLM 翻译\n  {ctx.zh_srt}")
        zh_subs = parse_srt(ctx.zh_srt)
        translations = [s['text'] for s in zh_subs]
        if len(translations) != len(subtitles):
            print(f"Warning: .zh.srt 有 {len(translations)} 条, "
                  f"SRT 有 {len(subtitles)} 条, 数量不匹配." + (
                  f" 将截断对齐." if len(translations) > len(subtitles)
                  else " 缺失部分将留空."),
                  file=sys.stderr)
            while len(translations) < len(subtitles):
                translations.append('')
            translations = translations[:len(subtitles)]
    else:
        translations = translate_subtitles(subtitles, llm, system_prompt, args.quiet)
        write_zh_srt(ctx.zh_srt, subtitles, translations)
        if not args.quiet:
            print(f"  .zh.srt:  {ctx.zh_srt}")

    # ── 校对 ──────────────────────────────────────────────────────────────
    if args.proofread and _env.get('PROOFREAD', '1') != '0':
        if not args.quiet:
            print()
        corrected_en, translations = proofread_subtitles(
            subtitles, translations, llm, proofread_prompt, args.quiet
        )
        write_zh_srt(ctx.zh_srt, subtitles, translations)
        if not args.quiet:
            print(f"  .zh.srt:  {ctx.zh_srt} (updated with proofread)")
        # 用校对后的英文更新 subtitle text, 保存 .proofread.srt
        for i, sub in enumerate(subtitles):
            if i < len(corrected_en):
                sub['text'] = corrected_en[i]
        proofread_srt = os.path.join(ctx.dir, f'{ctx.name}.proofread.srt')
        write_srt_generic(proofread_srt, subtitles)
        if not args.quiet:
            print(f"  .proofread.srt: {proofread_srt}")

    # ── Write ASS ──────────────────────────────────────────────────────────
    if not args.quiet:
        print(f"\nTemplate: {template_path}")

    write_ass(ctx.zh_ass, template_path, ctx.name, subtitles, translations, bilingual=False)
    write_ass(ctx.zh_en_ass, template_path, ctx.name, subtitles, translations, bilingual=True)

    # ── 翻译视频简介 ──────────────────────────────────────────────────────
    if os.path.isfile(ctx.desc):
        if not args.quiet:
            print()
        translate_description(ctx, llm, args.quiet)

    if not args.quiet:
        print(f"\nOutput:   {ctx.zh_ass}")
        print(f"          {ctx.zh_en_ass}")
        print(f"Lines:    {len(subtitles)}")
        print(f"\nDone! .zh.ass + .zh-en.ass 双语字幕已生成。")
    else:
        print(ctx.zh_en_ass)
    print(f"OUTPUT_ASS={os.path.abspath(ctx.zh_en_ass)}")


if __name__ == '__main__':
    main()
