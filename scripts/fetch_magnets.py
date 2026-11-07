# -*- coding: utf-8 -*-
"""
批量访问磁力帖详情页，提取明文 magnet: 链接。
磁力在帖子正文里是纯文本（【磁力链接】：magnet:?xt=urn:btih:...），用 inner_text 提取。
输入：磁力帖列表.json（tid + title + date）
输出：磁力抓取结果.json
"""
import os
import re
import sys
import json
import argparse
from urllib.parse import urljoin
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SKILL_DIR = Path(__file__).parent.parent


def find_chrome():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


MAGNET_RE = re.compile(r'magnet:\?xt=urn:btih:[a-fA-F0-9]{32,64}(?:&[^\s"\u201d\u201c<>，。；]*)?')


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="磁力帖列表.json")
    parser.add_argument("--output", default="磁力抓取结果.json")
    parser.add_argument("--base", default="https://www.sehuatang.org")
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out_dir = Path(args.output_dir)

    posts = json.load(open(args.input, encoding="utf-8"))
    print(f"[info] 待抓取磁力帖: {len(posts)}")

    chrome = find_chrome()
    if not chrome:
        print("[err] chrome not found")
        sys.exit(1)

    profile = out_dir / "chrome_profile"
    results = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=chrome,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()

        for idx, post in enumerate(posts, 1):
            href = post["tid"]
            url = href if href.startswith("http") else urljoin(base + "/", href)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                txt = page.inner_text("body")
                magnets = sorted(set(MAGNET_RE.findall(txt)))
                results.append({
                    "tid": post.get("tid"),
                    "title": post.get("title", ""),
                    "date": post.get("date", ""),
                    "url": url,
                    "magnets": magnets,
                })
                status = f"{len(magnets)}条磁力" if magnets else "无磁力"
                print(f"[{idx}/{len(posts)}] {status}  {post.get('title','')[:40]}")
            except PWTimeout:
                print(f"[{idx}/{len(posts)}] 超时  {post.get('title','')[:40]}")
                results.append({**post, "url": url, "magnets": [], "error": "timeout"})
            except Exception as e:
                print(f"[{idx}/{len(posts)}] 错误 {e}  {post.get('title','')[:40]}")
                results.append({**post, "url": url, "magnets": [], "error": str(e)})

        ctx.close()

    json.dump(results, open(out_dir / args.output, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    got = sum(1 for r in results if r.get("magnets"))
    print(f"\n[done] 成功提取磁力: {got}/{len(results)}，保存到 {args.output}")


if __name__ == "__main__":
    main()
