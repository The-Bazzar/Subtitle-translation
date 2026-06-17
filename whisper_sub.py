#!/usr/bin/env python3
"""
whisper_sub.py — 调用 WhisperX 生成字幕，可选注入 .description 作为上下文

用法:
  python3 whisper_sub.py <视频文件> [选项]

如果视频同目录下存在 .description 文件，将其内容作为 initial_prompt
传给 WhisperX，帮助模型理解专业词汇和上下文，提高转录准确性。
"""

import argparse
import os
import sys


def make_initial_prompt(desc_path: str) -> str:
    """读取 .description 构造 initial_prompt，不存在或为空则返回空."""
    if not os.path.isfile(desc_path):
        return ""
    try:
        with open(desc_path, 'r', encoding='utf-8') as f:
            text = f.read().strip()
    except OSError:
        return ""
    if not text:
        return ""

    # 截断过长简介 (提示词表，太长无益)
    max_chars = 1500
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(' ', 1)[0]

    return text


def main():
    parser = argparse.ArgumentParser(
        description='WhisperX 字幕生成 (支持 .description 上下文注入)'
    )
    parser.add_argument('video', help='视频文件路径')
    parser.add_argument('--lang', default='en', help='视频语言 (默认: en)')
    parser.add_argument('--model', default='large-v3-turbo', help='模型 (默认: large-v3-turbo)')
    parser.add_argument('--output-dir', default='.', help='输出目录 (默认: 当前)')
    parser.add_argument('--compute-type', default='float16',
                        help='计算精度 (GPU: float16, CPU: int8)')
    parser.add_argument('--device', default='cuda', help='设备 (cuda / cpu)')
    parser.add_argument('--description', help='.description 路径 (默认: 自动检测)')
    parser.add_argument('-q', '--quiet', action='store_true', help='静默模式')

    args = parser.parse_args()

    if not os.path.isfile(args.video):
        print(f"Error: Video file not found: {args.video}", file=sys.stderr)
        sys.exit(1)

    video_path = os.path.abspath(args.video)
    video_dir = os.path.dirname(video_path)
    video_name = os.path.splitext(os.path.basename(video_path))[0]

    # 自动检测 .description
    desc_path = args.description
    if not desc_path:
        candidate = os.path.join(video_dir, f"{video_name}.description")
        if os.path.isfile(candidate):
            desc_path = candidate

    initial_prompt = make_initial_prompt(desc_path) if desc_path else ""

    # ── WhisperX 三阶段流程 ──────────────────────────────────────────────
    import whisperx
    import gc
    import torch

    if not args.quiet:
        print(f"WhisperX: {args.model} / {args.compute_type} / {args.device}")
        print(f"Video:    {video_path}")
        print(f"Language: {args.lang}")
        if initial_prompt:
            print(f"Prompt:   {desc_path} ({len(initial_prompt)} chars)")

    device = args.device
    compute_type = args.compute_type
    if device == 'cpu':
        compute_type = 'int8'

    # 1. Transcribe
    if not args.quiet:
        print("\n1. Loading model & transcribing...")
    model = whisperx.load_model(args.model, device, compute_type=compute_type)
    audio = whisperx.load_audio(video_path)

    transcribe_opts = {"batch_size": 16}
    if initial_prompt:
        transcribe_opts["prompt"] = initial_prompt

    result = model.transcribe(audio, **transcribe_opts)

    # 2. Align (word-level timestamps)
    if not args.quiet:
        print("2. Aligning...")
    model_a, metadata = whisperx.load_align_model(
        language_code=args.lang, device=device
    )
    result = whisperx.align(
        result["segments"], model_a, metadata, audio, device,
        return_char_alignments=False,
    )

    # 3. Write SRT
    os.makedirs(args.output_dir, exist_ok=True)
    output_path = os.path.join(args.output_dir, f"{video_name}.srt")

    if not args.quiet:
        print(f"3. Writing:  {output_path}")

    def ts(sec: float) -> str:
        h = int(sec // 3600)
        m = int(sec % 3600 // 60)
        s = int(sec % 60)
        ms = int(sec * 1000 % 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(output_path, 'w', encoding='utf-8') as f:
        for i, seg in enumerate(result["segments"], 1):
            text = seg["text"].strip()
            f.write(f"{i}\n{ts(seg['start'])} --> {ts(seg['end'])}\n{text}\n\n")

    del model, model_a, metadata, result
    gc.collect()
    if device == 'cuda':
        torch.cuda.empty_cache()

    if not args.quiet:
        print(f"\nDone! {os.path.abspath(output_path)}")
    else:
        print(os.path.abspath(output_path))


if __name__ == '__main__':
    main()
