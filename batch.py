"""
batch.py — 批量字幕流水线 (Linux 并行)

用法:
  ./.venv/bin/python batch.py "URL1" "URL2" "URL3" [选项...]

说明:
  对多个 YouTube 链接并行执行 pipeline.sh，最大化利用 CPU/GPU/网络资源。
"""

import argparse
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='批量字幕流水线 — 并行执行 pipeline.sh',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "url1" "url2" "url3"
  %(prog)s -j 4 url1 url2 url3 url4 url5
  %(prog)s url1 url2 -B 0
        """,
    )
    parser.add_argument('urls', nargs='+', help='YouTube 链接列表')
    parser.add_argument('-j', '--jobs', type=int, default=os.cpu_count() or 4,
                        help=f'最大并行数 (默认: CPU 核心数 {os.cpu_count() or 4})')
    parser.add_argument('-B', '--burn', type=int, default=1,
                        help='硬压开关: 1=启用, 0=跳过 (默认: 1)')
    parser.add_argument('-r', '--report', default=None,
                        help='结果报告路径 (默认: 脚本同目录 batch-result.txt)')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='仅打印命令, 不执行')

    args = parser.parse_args()

    # ── 准备 ──────────────────────────────────────────────────────────────
    script_dir = os.path.dirname(os.path.abspath(__file__))
    pipeline_sh = os.path.join(script_dir, 'pipeline.sh')
    report_path = args.report or os.path.join(script_dir, 'batch-result.txt')

    start_time = datetime.now()
    total = len(args.urls)

    print("=" * 60)
    print(f"batch — {total} videos, max {args.jobs} parallel")
    print("=" * 60)
    print(f"Start:    {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Pipeline: {pipeline_sh}")
    print(f"Burn:     {'yes' if args.burn else 'no'}")
    print("=" * 60)

    if args.dry_run:
        for url in args.urls:
            cmd = f"bash {pipeline_sh} '{url}'"
            if not args.burn:
                cmd = f"BURN=0 {cmd}"
            print(f"[DRY RUN] {cmd}")
        return

    # ── 并行执行 ──────────────────────────────────────────────────────────
    results = []
    completed = 0
    failed = 0

    def run_one(url: str) -> dict:
        """Execute pipeline.sh for one URL, return result dict."""
        job_start = datetime.now()

        env = os.environ.copy()
        if not args.burn:
            env['BURN'] = '0'

        cmd = ['bash', pipeline_sh, url]
        try:
            proc = subprocess.run(
                cmd, env=env, cwd=script_dir,
                capture_output=True, text=True,
                timeout=7200,  # 2 hours max per video
            )
            ok = proc.returncode == 0
            output = proc.stdout[-2000:] if not ok else ""  # tail on failure
        except subprocess.TimeoutExpired:
            ok = False
            output = "Timeout (>2h)"
        except Exception as e:
            ok = False
            output = str(e)

        elapsed = round((datetime.now() - job_start).total_seconds() / 60, 1)
        return {
            'url': url,
            'ok': ok,
            'elapsed': elapsed,
            'started': job_start.strftime('%Y-%m-%d %H:%M:%S'),
            'output': output,
        }

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        future_map = {pool.submit(run_one, url): url for url in args.urls}

        for future in as_completed(future_map):
            url = future_map[future]
            result = future.result()

            completed += 1
            if not result['ok']:
                failed += 1

            remaining = total - completed
            avg_min = (datetime.now() - start_time).total_seconds() / 60 / completed
            eta = round(avg_min * remaining, 0)

            status = "OK" if result['ok'] else "FAIL"
            icon = "\033[32m" if result['ok'] else "\033[31m"
            print(f"{icon}[{completed}/{total}] {status} "
                  f"({result['elapsed']}min) [ETA {eta}min] "
                  f"← {url}\033[0m")

            if not result['ok']:
                brief = result['output'].strip().split('\n')[-3:]  # last 3 lines
                for line in brief:
                    print(f"  {line}")

            results.append(result)

    # ── 报告 ──────────────────────────────────────────────────────────────
    end_time = datetime.now()
    total_elapsed = round((end_time - start_time).total_seconds() / 60, 1)

    print()
    print("=" * 60)
    print("batch — All done!")
    print("=" * 60)
    print(f"Total:    {total}")
    print(f"Success:  {total - failed}")
    if failed:
        print(f"Failed:   {failed}")
    print(f"Elapsed:  {total_elapsed}min")
    print(f"Report:   {report_path}")
    print("=" * 60)

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"batch report — {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 60 + "\n")
        for i, r in enumerate(results, 1):
            s = "OK" if r['ok'] else "FAIL"
            f.write(f"{i:3d}. [{s}] {r['elapsed']:5}min  {r['url']}\n")
        f.write("=" * 60 + "\n")
        f.write(f"Total: {total}, Success: {total - failed}, "
                f"Failed: {failed}, Elapsed: {total_elapsed}min\n")


if __name__ == '__main__':
    main()
