#!/usr/bin/env python3
"""
beautify_srt.py — 美化 SRT 字幕时间码，对齐到场景切换
Similar to Subtitle Edit's "Beautify timecodes" feature (Netflix 规范).

算法 (Netflix Timed Text Style Guide):
  0. 检测视频帧率, 所有帧数参数按实际 fps 换算为秒
  1. 用 ffmpeg 检测视频场景切换点 (最小间隔 7 帧)
  2. 字幕入点吸附到前一个场景切换点 (7 帧以内)
  3. 字幕出点吸附到下一个场景切换点前 2 帧 (7 帧以内)
  4. 修复连续字幕之间的重叠和间隙问题 (>500ms 合并)
  5. 强制最短 1000ms / 最长 8000ms 时长
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional


# ─── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Subtitle:
    index: int
    start: float          # seconds
    end: float            # seconds
    text: str
    original_start: float = 0.0
    original_end: float = 0.0


@dataclass
class BeautifyOptions:
    scene_threshold: float = 0.15       # 场景检测灵敏度 (0-1)
    snap_frames: int = 7                # 吸附到场景切换的最大帧数
    end_offset_frames: int = 2          # 出点对齐到场景前 N 帧
    min_scene_interval_frames: int = 7  # 场景切换最小帧间隔
    use_keyframes: bool = False         # 默认不用关键帧 (各视频帧率不同)
    keyframe_snap_frames: int = 2       # 关键帧吸附最大帧数 (仅 --use-keyframes)
    min_duration: float = 1.0           # 最短字幕时长 (秒) — Netflix: 1000ms
    max_duration: float = 8.0           # 最长字幕时长 (秒) — Netflix: 8000ms
    min_gap: float = 0.083              # 字幕最小间距 (秒) — Netflix: 2帧
    max_gap_merge: float = 0.5          # 小于此值的间隙合并 (秒) — Netflix: 500ms
    extend_to_scene: bool = False       # 延伸到场景切换 (Netflix 不启用)
    no_scene_snap: bool = False         # 完全跳过场景吸附
    preview: bool = False
    quiet: bool = False
    fps: float = 24.0                   # 视频帧率 (运行时由 ffprobe 检测)


# ─── SRT 时间格式转换 ──────────────────────────────────────────────────────────

_SRT_TIME_RE = re.compile(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})')


def parse_srt_time(time_str: str) -> float:
    """将 SRT 时间戳 (HH:MM:SS,mmm) 转为秒."""
    m = _SRT_TIME_RE.match(time_str.strip())
    if not m:
        raise ValueError(f"Invalid SRT timestamp: {time_str!r}")
    h, mi, s, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def format_srt_time(seconds: float) -> str:
    """将秒转为 SRT 时间戳 (HH:MM:SS,mmm)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = min(int(round((seconds - int(seconds)) * 1000)), 999)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ─── SRT 解析 / 写入 ───────────────────────────────────────────────────────────

def parse_srt(filepath: str) -> list[Subtitle]:
    """解析 SRT 文件，返回 Subtitle 列表."""
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        content = f.read()

    # 按空行分割字幕块
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

        time_match = re.match(
            r'(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})',
            lines[1]
        )
        if not time_match:
            continue

        start = parse_srt_time(time_match.group(1))
        end = parse_srt_time(time_match.group(2))
        text = '\n'.join(lines[2:])

        subtitles.append(Subtitle(
            index=index,
            start=start,
            end=end,
            text=text,
            original_start=start,
            original_end=end,
        ))

    return subtitles


def write_srt(subtitles: list[Subtitle], filepath: str):
    """写入 SRT 文件."""
    with open(filepath, 'w', encoding='utf-8') as f:
        for i, sub in enumerate(subtitles, 1):
            f.write(f"{i}\n")
            f.write(f"{format_srt_time(sub.start)} --> {format_srt_time(sub.end)}\n")
            f.write(f"{sub.text}\n\n")


# ─── 视频分析: 帧率 + 场景切换 + 关键帧 ───────────────────────────────────────

def get_frame_rate(video_path: str) -> float:
    """
    用 ffprobe 检测视频帧率.
    返回 fps (float), 检测失败时回退到 24.0.
    """
    cmd = [
        'ffprobe', '-v', 'quiet',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=r_frame_rate',
        '-of', 'csv=p=0',
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        fps_str = result.stdout.strip()
        if '/' in fps_str:
            num, den = fps_str.split('/')
            if den == '0':
                return 24.0
            return float(num) / float(den)
        if fps_str:
            return float(fps_str)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return 24.0


def get_scene_changes(video_path: str, threshold: float = 0.25,
                      min_interval_sec: float = 0.3,
                      quiet: bool = False) -> list[float]:
    """
    用 ffmpeg 的 select + showinfo 滤镜检测场景切换.
    返回场景切换点的时间戳列表 (秒).

    min_interval_sec: 合并间距小于此值的相邻场景切换 (Netflix: 7帧)
    """
    cmd = [
        'ffmpeg',
        '-i', video_path,
        '-vf', f"select='gt(scene,{threshold})',showinfo",
        '-vsync', 'vfr',
        '-f', 'null', '-',
    ]

    if not quiet:
        print("  Running ffmpeg scene detection (this may take a while)...",
              file=sys.stderr)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("Warning: Scene detection timed out (10 min). Skipping.",
              file=sys.stderr)
        return []
    except FileNotFoundError:
        print("Error: ffmpeg not found. Is it installed?", file=sys.stderr)
        return []

    times: list[float] = []
    pts_re = re.compile(r'pts_time:([0-9]+\.?[0-9]*)')

    for line in result.stderr.split('\n'):
        m = pts_re.search(line)
        if m:
            try:
                times.append(float(m.group(1)))
            except ValueError:
                pass

    # 去重排序, 合并太近的切换点
    times = sorted(set(times))
    merged: list[float] = []
    for t in times:
        if not merged or t - merged[-1] >= min_interval_sec:
            merged.append(t)
    return merged


def get_keyframes(video_path: str, quiet: bool = False) -> list[float]:
    """
    用 ffprobe 提取所有关键帧 (I-frame) 的时间戳.
    3 级回退策略: -skip_frame nokey → packet flags → frame key_frame.

    注意: 默认流程不调用此函数 (各视频编码/帧率差异大, 场景吸附已足够).
    """
    times: list[float] = []

    # 方法 1: -skip_frame nokey (快速, 但对 VP9/webm 可能无效)
    cmd1 = [
        'ffprobe', '-v', 'quiet',
        '-select_streams', 'v:0',
        '-skip_frame', 'nokey',
        '-show_entries', 'frame=pkt_pts_time',
        '-of', 'csv=p=0',
        video_path,
    ]

    try:
        result = subprocess.run(cmd1, capture_output=True, text=True, timeout=120)
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if line:
                try:
                    times.append(float(line))
                except ValueError:
                    pass
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 方法 2: 解析 packet flags (更慢但兼容性更好)
    if not times:
        if not quiet:
            print("  Method 1 returned no keyframes, trying packet-based method...",
                  file=sys.stderr)
        cmd2 = [
            'ffprobe', '-v', 'quiet',
            '-select_streams', 'v:0',
            '-show_entries', 'packet=pts_time,flags',
            '-of', 'csv=p=0',
            video_path,
        ]
        try:
            result = subprocess.run(cmd2, capture_output=True, text=True, timeout=300)
            for line in result.stdout.strip().split('\n'):
                parts = line.strip().split(',')
                if len(parts) >= 2 and 'K' in parts[-1]:
                    try:
                        times.append(float(parts[0]))
                    except ValueError:
                        pass
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # 方法 3: 解析 frame key_frame 标志 (最慢)
    if not times:
        if not quiet:
            print("  Method 2 returned no keyframes, trying frame-based method...",
                  file=sys.stderr)
        cmd3 = [
            'ffprobe', '-v', 'quiet',
            '-select_streams', 'v:0',
            '-show_entries', 'frame=key_frame,pkt_pts_time',
            '-of', 'csv=p=0',
            video_path,
        ]
        try:
            result = subprocess.run(cmd3, capture_output=True, text=True, timeout=600)
            for line in result.stdout.strip().split('\n'):
                parts = line.strip().split(',')
                if len(parts) >= 2 and parts[0] == '1':
                    try:
                        times.append(float(parts[1]))
                    except ValueError:
                        pass
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if not times and not quiet:
        print("  Warning: All keyframe extraction methods failed. "
              "Using scene changes only.", file=sys.stderr)

    return sorted(set(times))


# ─── 核心算法 ──────────────────────────────────────────────────────────────────

def snap_to_previous(value: float, targets: list[float],
                     max_distance: float) -> float:
    """将 value 吸附到 targets 中最近的前一个值 (≤ value), 距离不超过 max_distance."""
    if not targets:
        return value

    candidates = [t for t in targets if t <= value]
    if not candidates:
        return value

    best = max(candidates)
    if value - best <= max_distance:
        return best
    return value


def snap_end_to_scene_before(end: float, scene_changes: list[float],
                              snap_sec: float, end_offset: float) -> float:
    """
    Netflix 规则: 字幕出点对齐到场景切换前 N 帧.
    查找 end 之后的第一个场景切换, 将 end 吸附到 scene - offset,
    前提是距离在 snap_sec 以内.
    """
    if not scene_changes:
        return end

    later = [s for s in scene_changes if s >= end]
    if not later:
        return end

    next_scene = min(later)
    target = next_scene - end_offset
    if target < 0:
        target = 0.0
    if abs(target - end) <= snap_sec:
        return target
    return end


def snap_to_nearest(value: float, targets: list[float],
                    max_distance: float) -> float:
    """将 value 吸附到 targets 中最近的值, 距离不超过 max_distance."""
    if not targets:
        return value

    best = min(targets, key=lambda t: abs(t - value))
    if abs(best - value) <= max_distance:
        return best
    return value


def beautify_subtitles(
    subs: list[Subtitle],
    scene_changes: list[float],
    keyframes: list[float],
    opts: BeautifyOptions,
) -> list[Subtitle]:
    """
    核心美化流程 (Netflix 规范):
      1. 入点吸附到前一个场景切换点 (7帧内)
      2. 出点吸附到下一个场景切换点前 2 帧 (7帧内)
      3. (可选) 微调到关键帧
      4. (可选) 延伸到场景切换
      5. 修复重叠、合并小间隙
      6. 强制最短/最长时长
    """

    snap_sec = opts.snap_frames / opts.fps          # 吸附距离 (秒)
    end_offset = opts.end_offset_frames / opts.fps   # 出点偏移 (秒)

    # ── 第 1 步: 吸附到场景切换 ──────────────────────────────────────────
    if scene_changes and not opts.no_scene_snap:
        for sub in subs:
            # 入点: 吸附到前一个场景切换 (字幕不该在场景切换中间开始)
            sub.start = snap_to_previous(sub.start, scene_changes, snap_sec)
            # 出点: 吸附到下一个场景切换前 2 帧 (Netflix 规范)
            sub.end = snap_end_to_scene_before(
                sub.end, scene_changes, snap_sec, end_offset
            )

    # ── 第 2 步: (可选) 微调到关键帧 ──────────────────────────────────────
    if keyframes and opts.use_keyframes:
        kf_snap_sec = opts.keyframe_snap_frames / opts.fps
        for sub in subs:
            sub.start = snap_to_nearest(sub.start, keyframes, kf_snap_sec)
            sub.end = snap_to_nearest(sub.end, keyframes, kf_snap_sec)

    # ── 第 3 步: (可选) 延伸到场景切换 ────────────────────────────────────
    if opts.extend_to_scene and scene_changes and not opts.no_scene_snap:
        for i, sub in enumerate(subs):
            later_scenes = [s for s in scene_changes if s > sub.end]
            if not later_scenes:
                continue
            next_scene = min(later_scenes)
            # 延伸到场景切换 (保留 end_offset 不出戏)
            target = next_scene - end_offset
            gap_to_target = target - sub.end
            if 0 < gap_to_target <= snap_sec * 1.5:
                if i + 1 < len(subs):
                    max_extend = subs[i + 1].start - opts.min_gap
                    if target <= max_extend:
                        sub.end = target
                else:
                    sub.end = target

    # ── 第 4 步: 修复重叠和间隙 ──────────────────────────────────────────
    for i in range(len(subs)):
        if i + 1 >= len(subs):
            break
        curr = subs[i]
        nxt = subs[i + 1]

        gap = nxt.start - curr.end

        if gap < 0:
            # 重叠：缩短当前字幕
            curr.end = nxt.start - opts.min_gap
        elif 0 < gap < opts.max_gap_merge:
            # 小间隙：延伸当前字幕来填充
            curr.end = nxt.start - opts.min_gap

    # ── 第 5 步: 强制最短/最长时长 ───────────────────────────────────────
    for i, sub in enumerate(subs):
        duration = sub.end - sub.start

        if duration < opts.min_duration:
            desired_end = sub.start + opts.min_duration
            if i + 1 < len(subs):
                max_end = subs[i + 1].start - opts.min_gap
                sub.end = min(desired_end, max_end)
            else:
                sub.end = desired_end

        elif duration > opts.max_duration:
            if i + 1 < len(subs):
                desired_end = sub.start + opts.max_duration
                sub.end = min(sub.end, subs[i + 1].start - opts.min_gap)

    # ── 最终检查 ─────────────────────────────────────────────────────────
    for sub in subs:
        if sub.start < 0.0:
            sub.start = 0.0
        if sub.end <= sub.start:
            sub.end = sub.start + opts.min_duration

    return subs


# ─── 变化摘要 ──────────────────────────────────────────────────────────────────

def summarize_changes(subs: list[Subtitle], fps: float) -> str:
    """生成美化前后的对比摘要 (帧数按实际 fps 换算)."""
    total_start_shift = 0.0
    total_end_shift = 0.0
    changed_count = 0

    for sub in subs:
        sd = abs(sub.start - sub.original_start)
        ed = abs(sub.end - sub.original_end)
        total_start_shift += sd
        total_end_shift += ed
        if sd > 0.001 or ed > 0.001:
            changed_count += 1

    n = len(subs) if subs else 1
    frame_ms = 1000.0 / fps if fps > 0 else 41.7
    lines = [
        f"  Frame rate:           {fps:.2f} fps ({frame_ms:.1f} ms/frame)",
        f"  Total subtitles:      {len(subs)}",
        f"  Subtitles modified:   {changed_count} ({changed_count*100/n:.0f}%)",
        f"  Avg start shift:      {total_start_shift/n*1000:.1f} ms "
        f"({total_start_shift/n*1000/frame_ms:.1f} frames)",
        f"  Avg end shift:        {total_end_shift/n*1000:.1f} ms "
        f"({total_end_shift/n*1000/frame_ms:.1f} frames)",
    ]
    return '\n'.join(lines)


# ─── 查找 SRT 文件 ─────────────────────────────────────────────────────────────

def is_valid_srt(filepath: str) -> bool:
    """快速检查文件是否为真正的 SRT 格式 (而非 ASS/VTT 伪装的 .srt)."""
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            head = f.read(256).lstrip()
        return bool(re.match(r'\d+\s*\n', head))
    except (OSError, UnicodeDecodeError):
        return False


def find_srt(video_path: str) -> Optional[str]:
    """根据视频路径自动查找对应的 SRT 字幕文件."""
    video_dir = os.path.dirname(os.path.abspath(video_path))
    video_name = os.path.splitext(os.path.basename(video_path))[0]

    candidates = [
        os.path.join(video_dir, f"{video_name}.srt"),
        os.path.join(video_dir, f"{video_name}.en.srt"),
    ]
    for c in candidates:
        if os.path.isfile(c) and is_valid_srt(c):
            return c

    # 搜索同目录下任意有效的 .srt
    ass_candidates = []
    try:
        for fname in sorted(os.listdir(video_dir)):
            if fname.endswith('.srt'):
                full = os.path.join(video_dir, fname)
                if is_valid_srt(full):
                    return full
                else:
                    ass_candidates.append(full)
    except OSError:
        pass

    if ass_candidates:
        print(
            f"Note: Found {len(ass_candidates)} .srt file(s) but they appear to be "
            f"ASS/SSA format, not SRT. Use an SRT subtitle file.",
            file=sys.stderr,
        )

    return None


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='美化 SRT 字幕时间码 — 对齐到场景切换 (Netflix 规范).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s video.mp4                      # 自动查找同目录 .srt → .beautified.srt
  %(prog)s video.mp4 subtitle.srt         # 指定字幕 → .beautified.srt
  %(prog)s video.mp4 -o result.srt        # 输出到指定文件
  %(prog)s video.mp4 -o video.srt         # 覆盖原文件 (需显式指定 -o)
  %(prog)s video.mp4 --scene-threshold 0.2 --snap-frames 5
  %(prog)s video.mp4 --preview            # 预览变化 (不写入)
  %(prog)s video.mp4 --backup             # 覆盖前备份原文件 (仅 -o 同文件时有效)
        """,
    )

    parser.add_argument('video', help='视频文件路径')
    parser.add_argument('srt', nargs='?', help='SRT 字幕文件路径 (未指定则自动查找)')
    parser.add_argument('-o', '--output',
                        help='输出 SRT 路径 (默认: <原名>.beautified.srt, 不覆盖原文件)')

    # 场景检测
    parser.add_argument('--scene-threshold', type=float, default=0.25,
                        help='场景检测灵敏度 (0.1-0.5, 默认: 0.25)')

    # 帧数参数 (按实际 fps 换算为秒)
    parser.add_argument('--snap-frames', type=int, default=7,
                        help='吸附到场景切换的最大帧数 (默认: 7)')
    parser.add_argument('--end-offset-frames', type=int, default=2,
                        help='出点对齐到场景切换前 N 帧 (默认: 2)')
    parser.add_argument('--min-scene-interval-frames', type=int, default=7,
                        help='场景切换最小帧间隔 (默认: 7)')

    # 关键帧 (默认关闭)
    parser.add_argument('--use-keyframes', action='store_true',
                        help='启用关键帧吸附 (默认关闭, 各视频帧率不同建议不用)')
    parser.add_argument('--keyframe-snap-frames', type=int, default=2,
                        help='关键帧吸附最大帧数 (默认: 2, 需 --use-keyframes)')

    # 时长 / 间距
    parser.add_argument('--min-duration', type=float, default=1.0,
                        help='最短字幕时长, 秒 (默认: 1.0, Netflix: 1000ms)')
    parser.add_argument('--max-duration', type=float, default=8.0,
                        help='最长字幕时长, 秒 (默认: 8.0, Netflix: 8000ms)')
    parser.add_argument('--min-gap', type=float, default=0.083,
                        help='字幕最小间距, 秒 (默认: 0.083, Netflix: 2帧)')
    parser.add_argument('--max-gap-merge', type=float, default=0.5,
                        help='小于此值的间隙自动合并, 秒 (默认: 0.5, Netflix: 500ms)')

    # 行为开关
    parser.add_argument('--extend', action='store_true',
                        help='延伸字幕填充到场景切换前的间隙 (Netflix 默认不启用)')
    parser.add_argument('--no-scene-snap', action='store_true',
                        help='完全跳过场景切换吸附')
    parser.add_argument('--preview', action='store_true',
                        help='仅预览变化, 不写入文件')
    parser.add_argument('--backup', action='store_true',
                        help='覆盖前创建 .bak 备份')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='静默模式')

    args = parser.parse_args()

    # 验证视频文件
    if not os.path.isfile(args.video):
        print(f"Error: Video file not found: {args.video}", file=sys.stderr)
        sys.exit(1)

    # ── 检测帧率 ─────────────────────────────────────────────────────────
    fps = get_frame_rate(args.video)
    if not args.quiet:
        print(f"Video:  {args.video}")
        print(f"Frame rate: {fps:.2f} fps")

    # 确定 SRT 路径
    if args.srt:
        srt_path = args.srt
    else:
        srt_path = find_srt(args.video)

    if not srt_path:
        print("Error: No SRT file found. Specify one explicitly.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(srt_path):
        print(f"Error: SRT file not found: {srt_path}", file=sys.stderr)
        sys.exit(1)

    if not args.quiet:
        print(f"SRT:    {srt_path}")
        print()

    # 解析 SRT
    if not args.quiet:
        print("Parsing SRT file...")
    subs = parse_srt(srt_path)
    if not subs:
        print("Error: No subtitles found in SRT file.", file=sys.stderr)
        sys.exit(1)
    if not args.quiet:
        print(f"  Found {len(subs)} subtitles [{format_srt_time(subs[0].start)} → "
              f"{format_srt_time(subs[-1].end)}]")

    # 组装选项
    opts = BeautifyOptions(
        scene_threshold=args.scene_threshold,
        snap_frames=args.snap_frames,
        end_offset_frames=args.end_offset_frames,
        min_scene_interval_frames=args.min_scene_interval_frames,
        use_keyframes=args.use_keyframes,
        keyframe_snap_frames=args.keyframe_snap_frames,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        min_gap=args.min_gap,
        max_gap_merge=args.max_gap_merge,
        extend_to_scene=args.extend,
        no_scene_snap=args.no_scene_snap,
        preview=args.preview,
        quiet=args.quiet,
        fps=fps,
    )

    # 打印帧数→秒的换算
    if not args.quiet:
        snap_sec = opts.snap_frames / fps
        end_off_sec = opts.end_offset_frames / fps
        min_int_sec = opts.min_scene_interval_frames / fps
        print(f"\n  Netflix parameters @ {fps:.2f} fps:")
        print(f"    snap distance:        {opts.snap_frames} frames = {snap_sec*1000:.0f} ms")
        print(f"    end offset:           {opts.end_offset_frames} frames = {end_off_sec*1000:.0f} ms")
        print(f"    min scene interval:   {opts.min_scene_interval_frames} frames = {min_int_sec*1000:.0f} ms")

    # ── 场景检测 ─────────────────────────────────────────────────────────
    scene_changes: list[float] = []
    if not opts.no_scene_snap:
        min_interval_sec = opts.min_scene_interval_frames / fps
        if not args.quiet:
            print(f"\nDetecting scene changes "
                  f"(threshold={opts.scene_threshold}, "
                  f"min interval={opts.min_scene_interval_frames} frames)...")
        scene_changes = get_scene_changes(
            args.video, opts.scene_threshold,
            min_interval_sec=min_interval_sec,
            quiet=args.quiet,
        )
        if not args.quiet:
            print(f"  Found {len(scene_changes)} scene changes")
            if scene_changes:
                duration = scene_changes[-1] - scene_changes[0] if len(scene_changes) > 1 else 0
                if duration > 0:
                    print(f"  Avg interval: {duration/len(scene_changes):.1f}s")

    # ── 关键帧提取 (仅 --use-keyframes) ──────────────────────────────────
    keyframes: list[float] = []
    if opts.use_keyframes:
        if not args.quiet:
            print("\nExtracting keyframes (--use-keyframes enabled)...")
        keyframes = get_keyframes(args.video, quiet=args.quiet)
        if not args.quiet:
            print(f"  Found {len(keyframes)} keyframes")

    if not scene_changes and not keyframes:
        print("\nWarning: No scene changes or keyframes found. Output will be unchanged.",
              file=sys.stderr)

    # ── 美化 ─────────────────────────────────────────────────────────────
    if not args.quiet:
        print("\nBeautifying timecodes...")
    beautified = beautify_subtitles(subs, scene_changes, keyframes, opts)

    # ── 输出 ─────────────────────────────────────────────────────────────
    print()
    print(summarize_changes(beautified, fps))

    if args.preview:
        print("\n── Preview (first 25 changes) ──")
        shown = 0
        for sub in beautified:
            sd = sub.start - sub.original_start
            ed = sub.end - sub.original_end
            if abs(sd) > 0.001 or abs(ed) > 0.001:
                shown += 1
                if shown > 25:
                    break
                marker_parts = []
                if abs(sd) > 0.001:
                    marker_parts.append(f"start {sd*1000:+.0f}ms")
                if abs(ed) > 0.001:
                    marker_parts.append(f"end {ed*1000:+.0f}ms")
                marker = f"  ← {'; '.join(marker_parts)}"
                print(f"  #{sub.index}")
                print(f"    {format_srt_time(sub.original_start)} --> {format_srt_time(sub.original_end)}")
                print(f"    {format_srt_time(sub.start)} --> {format_srt_time(sub.end)}{marker}")
                print(f"    {sub.text[:80]}{'...' if len(sub.text) > 80 else ''}")
                print()

        remaining = sum(1 for s in beautified
                        if abs(s.start - s.original_start) > 0.001
                        or abs(s.end - s.original_end) > 0.001)
        if remaining > 25:
            print(f"  ... and {remaining - 25} more changed subtitles")
    else:
        if args.output:
            output_path = args.output
        else:
            # 默认不覆盖原文件, 输出 <原名>.beautified.srt
            srt_dir = os.path.dirname(os.path.abspath(srt_path))
            srt_base = os.path.splitext(os.path.basename(srt_path))[0]
            output_path = os.path.join(srt_dir, f"{srt_base}.beautified.srt")

        if args.backup and output_path == srt_path:
            backup_path = srt_path + '.bak'
            shutil.copy2(srt_path, backup_path)
            if not args.quiet:
                print(f"\nBackup saved: {backup_path}")

        write_srt(beautified, output_path)
        if not args.quiet:
            print(f"\nBeautified SRT saved: {output_path}")
        print(f"OUTPUT_SRT={os.path.abspath(output_path)}")


if __name__ == '__main__':
    main()
