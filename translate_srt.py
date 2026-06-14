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
from typing import Optional


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


def parse_srt(filepath: str) -> list[dict]:
    """
    解析 SRT 文件，返回 [{index, start, end, start_ass, end_ass, text}, ...].
    start/end 是秒 (float)，start_ass/end_ass 是 ASS 格式字符串.
    """
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        content = f.read()

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

# Provider configurations (OpenAI-compatible endpoints)
PROVIDERS = {
    'openrouter': {
        'url': 'https://openrouter.ai/api/v1/chat/completions',
        'default_model': 'anthropic/claude-sonnet-4-6',
        'env_key': 'OPENROUTER_API_KEY',
        'headers': lambda key: {
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'https://github.com/oculr/Subtitle-translation',
            'X-Title': 'Subtitle Translation',
        },
    },
    'deepseek': {
        'url': 'https://api.deepseek.com/v1/chat/completions',
        'default_model': 'deepseek-chat',
        'env_key': 'DEEPSEEK_API_KEY',
        'headers': lambda key: {
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
        },
    },
    'gemini': {
        'url': 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
        'default_model': 'gemini-2.5-pro',
        'env_key': 'GEMINI_API_KEY',
        'headers': lambda key: {
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
        },
    },
}

TRANSLATION_SYSTEM_PROMPT = """You are a professional subtitle translator specializing in English→Simplified Chinese translation.

Rules:
- Translate each numbered line 1:1 to natural, fluent Chinese (Simplified)
- Preserve \\\\N line breaks exactly — this is a subtitle soft-return marker, keep it in the SAME position
- Match the tone of the original: casual stays casual, formal stays formal
- Keep proper nouns, brand names, and technical terms in their original form if no standard Chinese translation exists
- Do NOT skip, merge, split, or add any items — exactly N input lines → N output lines

Netflix Chinese subtitle formatting:
- Do NOT use any punctuation marks — Chinese subtitles omit 。，！？ etc.
- The ONLY punctuation allowed is 《》 (book title marks) — keep these if present in the original
- Use a single space to replace all removed punctuation where natural pauses occur

Respond ONLY with numbered lines in this exact format:
  [1] translation
  [2] translation
  ...
No explanations, no preamble, no closing remarks"""

PROOFREAD_SYSTEM_PROMPT = """You are a Chinese subtitle proofreader. Review each numbered pair (EN original + ZH draft) against the English source.

Tasks:
- Fix mistranslations, omissions, or added content — the Chinese must match the English meaning 1:1
- Improve awkward or unnatural phrasing — the Chinese should read fluently as spoken subtitles
- Fix tone mismatches — casual/formal/informal register must match the original
- Ensure Netflix formatting: no punctuation except 《》, spaces for natural pauses
- Preserve \\\\N line breaks exactly in their original positions
- Do NOT merge, split, or reorder items — exactly N input pairs → N output lines

Respond ONLY with corrected numbered lines:
  [1] corrected translation
  [2] corrected translation
  ...
No explanations, no preamble, no closing remarks"""


def load_env(script_dir: str) -> dict[str, str]:
    """Load key=value pairs from .env file (if present)."""
    env = dict(os.environ)
    env_path = os.path.join(script_dir, '.env')
    if os.path.isfile(env_path):
        with open(env_path, 'r') as f:
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
    key_name = PROVIDERS[provider]['env_key']
    key = env.get(key_name, '')
    if not key:
        print(f"Error: {key_name} not found in environment or .env file.",
              file=sys.stderr)
        print(f"Set it in .env: {key_name}=your_key_here", file=sys.stderr)
        sys.exit(1)
    return key


def translate_batch(
    texts: list[str],
    provider: str,
    model: str,
    api_key: str,
    system_prompt: str = "",
    quiet: bool = False,
    retries: int = 3,
) -> list[str]:
    """
    Send a batch of numbered texts to the LLM for 1:1 translation.
    Returns translated texts in the same order.
    """
    import json
    import urllib.request
    import urllib.error

    cfg = PROVIDERS[provider]

    # Build numbered prompt
    prompt_lines = [f"[{i}] {t}" for i, t in enumerate(texts, 1)]
    prompt = "\n".join(prompt_lines)

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt or TRANSLATION_SYSTEM_PROMPT},
            {'role': 'user', 'content': (
                f"Translate these {len(texts)} subtitle lines to Chinese. "
                f"Respond with exactly {len(texts)} numbered lines.\n\n{prompt}"
            )},
        ],
        'temperature': 0.3,
        'max_tokens': max(4096, len(texts) * 200),
    }

    data = json.dumps(payload).encode('utf-8')
    headers = cfg['headers'](api_key)

    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(cfg['url'], data=data, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            content = body['choices'][0]['message']['content']
            return _parse_numbered_response(content, len(texts))

        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}"
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError) as e:
            last_error = str(e)
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


def translate_subtitles(
    subtitles: list[dict],
    provider: str = 'deepseek',
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    system_prompt: str = "",
    batch_size: int = 50,
    quiet: bool = False,
) -> list[str]:
    """Translate all subtitle texts in batches. Returns translations in order."""
    texts = [s['text'] for s in subtitles]

    if model is None:
        model = PROVIDERS[provider]['default_model']

    # Load API key
    env = load_env(os.path.dirname(os.path.abspath(__file__)))
    if api_key is None:
        api_key = get_api_key(provider, env)

    if not quiet:
        print(f"Translator: {provider} / {model}")
        print(f"Total lines: {len(texts)}")
        print()

    all_translations = []
    total_batches = (len(texts) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, len(texts))
        batch_texts = texts[start_idx:end_idx]

        if not quiet:
            print(f"  Batch {batch_idx + 1}/{total_batches}: "
                  f"translating lines {start_idx + 1}-{end_idx}...",
                  file=sys.stderr)

        batch_translations = translate_batch(
            batch_texts, provider, model, api_key, system_prompt=system_prompt, quiet=quiet
        )
        all_translations.extend(batch_translations)

    return all_translations


def proofread_subtitles(
    subtitles: list[dict],
    translations: list[str],
    provider: str = 'deepseek',
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    system_prompt: str = "",
    batch_size: int = 30,
    quiet: bool = False,
) -> list[str]:
    """
    中英校对: 将英文原文 + 中文初译成对送入 LLM 审校.
    返回校对后的中文翻译列表.
    """
    en_texts = [s['text'] for s in subtitles]

    if model is None:
        model = PROVIDERS[provider]['default_model']

    env = load_env(os.path.dirname(os.path.abspath(__file__)))
    if api_key is None:
        api_key = get_api_key(provider, env)

    if not quiet:
        print(f"Proofreader: {provider} / {model}")
        print(f"Total lines: {len(en_texts)}")
        print()

    all_corrected = []
    # 校对批次减半: 每个 item 包含 EN+ZH 双份文本
    proof_batch_size = max(15, batch_size // 2)
    total_batches = (len(en_texts) + proof_batch_size - 1) // proof_batch_size

    for batch_idx in range(total_batches):
        start_idx = batch_idx * proof_batch_size
        end_idx = min(start_idx + proof_batch_size, len(en_texts))

        # 每对 EN+ZH 作为 translate_batch 的一个 item
        # translate_batch 会编号为 [1], [2], ...
        pairs = []
        for i in range(start_idx, end_idx):
            pairs.append(
                f"EN: {en_texts[i]}\n"
                f"ZH: {translations[i]}"
            )

        if not quiet:
            print(f"  Batch {batch_idx + 1}/{total_batches}: "
                  f"proofreading lines {start_idx + 1}-{end_idx}...",
                  file=sys.stderr)

        corrected = translate_batch(
            pairs, provider, model, api_key,
            system_prompt=system_prompt,
            quiet=quiet,
        )
        all_corrected.extend(corrected)

    return all_corrected[:len(en_texts)]


# ─── ASS 输出 ──────────────────────────────────────────────────────────────────

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


def write_zh_srt(output_path: str, subtitles: list[dict], translations: list[str]):
    """
    写入 .zh.srt 文件 (中文翻译, 保留原始时间码).
    用作翻译缓存 — 存在时可跳过 LLM 直接合成双语 ASS。
    """
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, (sub, trans) in enumerate(zip(subtitles, translations), 1):
            f.write(f"{i}\n")
            f.write(f"{format_srt_time(sub['start'])} --> {format_srt_time(sub['end'])}\n")
            f.write(f"{trans}\n\n")


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
    _env_provider = _env.get('TRANSLATE_PROVIDER', 'openrouter')
    _env_model = _env.get('TRANSLATE_MODEL', '') or None

    parser = argparse.ArgumentParser(
        description='翻译 SRT 英文字幕为中文, 输出双语 .zh-en.ass (bi-en + bi-zh).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  %(prog)s video.srt                          # 输出 .zh-en.ass 双语字幕
  %(prog)s video.srt -t my_template.ass       # 指定模板
  %(prog)s video.srt -o output.zh-en.ass      # 指定输出路径
  %(prog)s video.srt --provider deepseek      # 使用 DeepSeek
  %(prog)s video.srt --model openai/gpt-4.1   # 通过 OpenRouter 指定模型
  %(prog)s video.srt --batch-size 30          # 每批 30 条 (默认 50)

默认翻译后端从 .env 读取 (TRANSLATE_PROVIDER, TRANSLATE_MODEL), 当前: {_env_provider}
        """,
    )

    parser.add_argument('srt', help='SRT 字幕文件路径')
    parser.add_argument('-t', '--template',
                        help='template.ass 模板路径 (默认: 脚本同目录)')
    parser.add_argument('-o', '--output',
                        help='输出 .zh-en.ass 路径 (默认: SRT 同目录, 同名 .zh-en.ass)')
    parser.add_argument('--provider', choices=['openrouter', 'deepseek', 'gemini'],
                        default=_env_provider,
                        help=f'LLM 后端 (默认: {_env_provider}, 来自 .env)')
    parser.add_argument('--model', default=_env_model,
                        help='模型名称 (默认: provider 内置默认模型)')
    parser.add_argument('--api-key',
                        help='API key (默认: 从 .env 读取)')
    parser.add_argument('--batch-size', type=int, default=50,
                        help='每批翻译行数 (默认: 50)')
    parser.add_argument('--system-prompt', metavar='PROMPT',
                        help='自定义翻译提示词 (默认: 内置 Netflix 规范提示词)')
    parser.add_argument('--proofread', action='store_true',
                        help='启用中英校对 (翻译后送入 LLM 二次审校)')
    parser.add_argument('--proofread-prompt', metavar='PROMPT',
                        help='自定义校对提示词 (默认: 内置校对提示词)')
    parser.add_argument('--title',
                        help='视频标题 (默认: 从 SRT 文件名推断)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='静默模式')

    args = parser.parse_args()

    # Validate SRT
    if not os.path.isfile(args.srt):
        print(f"Error: SRT file not found: {args.srt}", file=sys.stderr)
        sys.exit(1)

    # Determine paths
    srt_path = os.path.abspath(args.srt)
    srt_dir = os.path.dirname(srt_path)
    srt_name = os.path.splitext(os.path.basename(srt_path))[0]
    # 如果输入是 .beautified.srt, 去掉 .beautified 后缀
    if srt_name.endswith('.beautified'):
        srt_name = srt_name[:-len('.beautified')]
    script_dir = os.path.dirname(os.path.abspath(__file__))

    template_path = args.template or os.path.join(script_dir, 'template.ass')
    if not os.path.isfile(template_path):
        print(f"Error: template.ass not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or os.path.join(srt_dir, f"{srt_name}.zh-en.ass")

    # Infer title
    title = args.title or srt_name

    # ── Parse SRT ──────────────────────────────────────────────────────────
    if not args.quiet:
        print(f"SRT:      {srt_path}")
    subtitles = parse_srt(srt_path)
    if not subtitles:
        print("Error: No subtitles found in SRT file.", file=sys.stderr)
        sys.exit(1)
    if not args.quiet:
        print(f"  Parsed {len(subtitles)} subtitle entries")
        print(f"  Duration: {subtitles[0]['start_ass']} → {subtitles[-1]['end_ass']}")

    # 系统提示词: CLI > .env > 内置默认
    system_prompt = args.system_prompt or _env.get('TRANSLATE_SYSTEM_PROMPT', '') or ''
    proofread_prompt = args.proofread_prompt or _env.get('PROOFREAD_SYSTEM_PROMPT', '') or ''

    # ── 获取中文翻译 ──────────────────────────────────────────────────────
    # 始终使用 .zh.srt 作为翻译缓存: 已存在则跳过 LLM, 否则翻译后写入

    zh_srt_path = os.path.join(srt_dir, f"{srt_name}.zh.srt")
    zh_ass_path = os.path.join(srt_dir, f"{srt_name}.zh.ass")

    if os.path.isfile(zh_srt_path):
        # 已有 .zh.srt → 直接读取, 跳过 LLM
        if not args.quiet:
            print(f"\nCache:    已有 .zh.srt, 跳过 LLM 翻译")
            print(f"  {zh_srt_path}")
        zh_subs = parse_srt(zh_srt_path)
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
        # 调用 LLM 翻译 + 写入 .zh.srt 缓存
        translations = translate_subtitles(
            subtitles,
            provider=args.provider,
            model=args.model,
            api_key=args.api_key,
            system_prompt=system_prompt,
            batch_size=args.batch_size,
            quiet=args.quiet,
        )
        write_zh_srt(zh_srt_path, subtitles, translations)
        if not args.quiet:
            print(f"  .zh.srt:  {zh_srt_path}")

    # ── 校对 (Pass 2): 英文原文 + 中文初译 → LLM 审校 ────────────────────
    if args.proofread:
        if not args.quiet:
            print()
        translations = proofread_subtitles(
            subtitles,
            translations,
            provider=args.provider,
            model=args.model,
            api_key=args.api_key,
            system_prompt=proofread_prompt,
            batch_size=args.batch_size,
            quiet=args.quiet,
        )
        # 覆盖 .zh.srt 为校对版
        write_zh_srt(zh_srt_path, subtitles, translations)
        if not args.quiet:
            print(f"  .zh.srt:  {zh_srt_path} (updated with proofread)")

    # ── Write ASS ──────────────────────────────────────────────────────────
    # .zh.ass: 仅中文 (style=zh)
    # .zh-en.ass: 双语 (bi-en + bi-zh)
    if not args.quiet:
        print(f"\nTemplate: {template_path}")

    write_ass(zh_ass_path, template_path, title, subtitles, translations, bilingual=False)
    write_ass(output_path, template_path, title, subtitles, translations, bilingual=True)

    if not args.quiet:
        print(f"Output:   {zh_ass_path}")
        print(f"          {output_path}")
        print(f"Lines:    {len(subtitles)}")
        print()
        print("Done! .zh.ass + .zh-en.ass 双语字幕已生成。")
    else:
        print(output_path)
    print(f"OUTPUT_ASS={os.path.abspath(output_path)}")


if __name__ == '__main__':
    main()
