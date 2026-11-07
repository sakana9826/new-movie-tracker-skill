# -*- coding: utf-8 -*-
"""
遍历彩月七緒全部帖子详情页，提取正文中的 magnet: 链接。
策略：
- 按番号分组，每个番号遍历其所有帖子（不只筛标题带"磁力"的）
- 正文用 inner_text 提取，磁力是纯文本（【磁力链接】：magnet:...）
- 断点续传：已抓过的 tid 跳过，浏览器崩溃自动重启继续
- 每个番号抓完所有帖子后，记录所有 magnet（供后续按最新/破解优先归并）
"""
import os
import re
import sys
import json
import time
import argparse
from urllib.parse import urljoin
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from sehuatang_common import pass_age_gate, is_age_gate

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


MAGNET_RE = re.compile(r'magnet:\?xt=urn:btih:[a-fA-F0-9]{32,64}(?:&[^\s"\u201c\u201d<>，。；\u3000]*)?')


def extract_tid(href):
    m = re.search(r'tid=(\d+)', href or "")
    return m.group(1) if m else (href or "")


def norm_code(c):
    return re.sub(r'-0+', '-', c.upper())


# 求片类关键词：只在标题开头出现或作为明确短语时才判为求片
REQ_HEAD_RE = re.compile(r'^\s*[\[【]?(求|悬赏|寻找|谁能|谁有|求助|请问)')
REQ_PHRASE_KW = [
    "求彩月", "求资源", "求视频", "求片", "求合集", "求高清", "求4k", "求4K",
    "求字幕", "求写真", "求电子", "悬赏", "求女优",
]
DISCUSS_KW = ["讨论", "问答", "提问", "求助", "请问"]


def should_skip(title):
    """根据标题判断是否跳过该帖子（不进详情页）。

    只跳过「求片/悬赏/问答/讨论」类。注意：不跳过含「破解/ED2K/无码」的
    发片帖，也不跳过片名正文里含「求」字的正常发片帖。
    """
    t = (title or "").strip()
    if not t:
        return False
    # 求片：标题开头就是"求/悬赏"等
    if REQ_HEAD_RE.match(t):
        return True
    # 求片：标题含明确求片短语
    if any(k in t for k in REQ_PHRASE_KW):
        return True
    # 讨论/问答：开头标记
    if re.match(r'^\s*[\[【]?(讨论|问答|提问|求助|请问)', t):
        return True
    return False


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="search_彩月七緒.json")
    parser.add_argument("--output", default="磁力抓取_全量.json")
    parser.add_argument("--base", default="https://www.sehuatang.org")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--resume", default=None, help="续传文件路径（已有的抓取结果）")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out_dir = Path(args.output_dir)
    profile = out_dir / "chrome_profile"

    data = json.load(open(args.input, encoding="utf-8"))
    threads = data["threads"]

    # 按 tid 去重
    seen_tid = {}
    for t in threads:
        tid = extract_tid(t.get("href"))
        if tid and tid not in seen_tid:
            seen_tid[tid] = t
    uniq_threads = list(seen_tid.values())
    print(f"[info] 去重后帖子数: {len(uniq_threads)}")

    # 预过滤：跳过「内容隐藏/回复可见」类和「求片/悬赏/问答/讨论」类
    kept = [t for t in uniq_threads if not should_skip(t.get("title", ""))]
    skipped = len(uniq_threads) - len(kept)
    print(f"[info] 跳过 {skipped} 帖（隐藏/求片/讨论类），保留 {len(kept)} 帖")
    uniq_threads = kept

    # 断点续传：已抓过的 tid
    done = {}
    if args.resume and Path(args.resume).exists():
        for r in json.load(open(args.resume, encoding="utf-8")):
            tid = extract_tid(r.get("tid") or r.get("href"))
            if tid:
                done[tid] = r

    todo = [t for t in uniq_threads if extract_tid(t.get("href")) not in done]
    print(f"[info] 待抓取: {len(todo)}，已抓过: {len(done)}")

    chrome = find_chrome()
    if not chrome:
        print("[err] chrome not found")
        sys.exit(1)

    results = list(done.values())

    def save():
        json.dump(results, open(out_dir / args.output, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    ctx = None
    page = None
    p = None

    try:
        p = sync_playwright().start()
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            executable_path=chrome,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()

        for idx, t in enumerate(todo, 1):
            tid = extract_tid(t.get("href"))
            href = t.get("href", "")
            url = href if href.startswith("http") else urljoin(base + "/", href)

            # 每 20 个刷新一次浏览器，避免累积崩溃
            if idx > 1 and idx % 20 == 0:
                try:
                    ctx.close()
                except Exception:
                    pass
                time.sleep(1)
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    executable_path=chrome,
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                    viewport={"width": 1366, "height": 768},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                )
                page = ctx.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                # 自动过年龄确认页
                if is_age_gate(page):
                    pass_age_gate(page)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                txt = page.inner_text("body")
                magnets = sorted(set(MAGNET_RE.findall(txt)))
                # 「内容隐藏/回复可见」的帖子：正文无磁力且提示隐藏，标记 hidden 不再重试
                hidden = (not magnets) and any(
                    k in txt for k in ["内容隐藏", "回复可见", "回复后可见", "请回复", "查看本贴", "游客"]
                )
                rec = {
                    "tid": tid,
                    "href": href,
                    "title": t.get("title", ""),
                    "date": t.get("date", ""),
                    "url": url,
                    "magnets": magnets,
                }
                if hidden:
                    rec["hidden"] = True
                results.append(rec)
                done[tid] = rec
                flag = f"{len(magnets)}条磁力" if magnets else ("隐藏" if hidden else "无")
                print(f"[{idx}/{len(todo)}] {flag}  {t.get('title','')[:38]}")
            except Exception as e:
                # 尝试重启浏览器
                try:
                    ctx.close()
                except Exception:
                    pass
                time.sleep(1)
                try:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=str(profile),
                        executable_path=chrome,
                        headless=False,
                        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                        viewport={"width": 1366, "height": 768},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    )
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                    if is_age_gate(page):
                        pass_age_gate(page)
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2000)
                    txt = page.inner_text("body")
                    magnets = sorted(set(MAGNET_RE.findall(txt)))
                    rec = {"tid": tid, "href": href, "title": t.get("title", ""),
                           "date": t.get("date", ""), "url": url, "magnets": magnets}
                    results.append(rec)
                    done[tid] = rec
                    flag = f"{len(magnets)}条磁力" if magnets else "无"
                    print(f"[{idx}/{len(todo)}] {flag}(重试)  {t.get('title','')[:38]}")
                except Exception as e2:
                    print(f"[{idx}/{len(todo)}] 失败  {t.get('title','')[:38]} err={e2}")
                    results.append({"tid": tid, "href": href, "title": t.get("title", ""),
                                    "date": t.get("date", ""), "url": url,
                                    "magnets": [], "error": str(e2)})
                    done[tid] = results[-1]

            # 每 5 个保存一次
            if idx % 5 == 0:
                save()
    finally:
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass
        if p:
            try:
                p.stop()
            except Exception:
                pass

    save()
    got = sum(1 for r in results if r.get("magnets"))
    print(f"\n[done] 共 {len(results)} 帖，含磁力 {got} 帖，保存到 {args.output}")


if __name__ == "__main__":
    main()
