# -*- coding: utf-8 -*-
"""
按演员名在 sehuatang.org 论坛搜索全部历史帖子，翻页抓取标题/链接/日期/磁力。
搜索结果页结构：<li class="pbw" id="tid"><h3 class="xs3"><a href="forum.php?mod=viewthread&tid=...">标题</a></h3>
日期在 <span>YYYY-MM-DD HH:MM</span>；磁力链接直接写在 <p> 内。
翻页参数 ?page=N，直到该页日期早于 cutoff 即停。
"""
import os
import re
import sys
import json
import argparse
from urllib.parse import quote, urljoin
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from sehuatang_common import pass_age_gate, is_age_gate

SKILL_DIR = Path(__file__).parent.parent

# 详情页正文磁力正则：磁力是正文纯文本，非 <a> 标签
MAGNET_RE = re.compile(r'magnet:\?xt=urn:btih:[a-fA-F0-9]{32,64}(?:&[^\s"\u201c\u201d<>，。；\u3000]*)?')

# 求片/悬赏/问答/讨论类：不进详情页
REQ_HEAD_RE = re.compile(r'^\s*[\[【]?(求|悬赏|寻找|谁能|谁有|求助|请问)')
REQ_PHRASE_KW = [
    "求彩月", "求资源", "求视频", "求片", "求合集", "求高清", "求4k", "求4K",
    "求字幕", "求写真", "求电子", "悬赏", "求女优",
]


def should_skip_detail(title):
    """根据标题判断是否跳过详情页（求片/悬赏/问答/讨论类）。"""
    t = (title or "").strip()
    if not t:
        return False
    if REQ_HEAD_RE.match(t):
        return True
    if any(k in t for k in REQ_PHRASE_KW):
        return True
    if re.match(r'^\s*[\[【]?(讨论|问答|提问|求助|请问)', t):
        return True
    return False


# ============ 按番号归并 / 优先级取舍 ============
POJIE_RE = re.compile(r'破解|无码|無碼|新模型|無修正')
ZIMU_RE = re.compile(r'字幕|中文|内嵌|中字|漢化|中文字幕')
CODE_RE = re.compile(r'\b([A-Z]{2,6}-?(\d{2,5}))\b')


def norm_code(c):
    c = c.upper().strip()
    c = re.sub(r'-0+', '-', c)  # 例如 CAWB-040 -> CAWB-40
    return c


def extract_codes(title):
    """从标题里提取所有番号（归一化）"""
    return [norm_code(m.group(1)) for m in CODE_RE.finditer(title or "")]


def classify(title, forum_fid=""):
    """返回优先级档位：0=破解+字幕(最高), 1=破解, 2=有码
    字幕判断：标题含字幕关键词，或版区 fid=103（高清中文字幕区，破解帖自带中字）
    """
    t = title or ""
    pojie = bool(POJIE_RE.search(t))
    zimu = bool(ZIMU_RE.search(t)) or str(forum_fid) == "103"
    if pojie and zimu:
        return 0
    if pojie:
        return 1
    return 2


def merge_by_code(records):
    """把帖子级记录按番号归并，每部作品保留一条最优磁力。

    优先级：破解+字幕(0) > 破解(1) > 有码(2)；同档取发帖日期最新。
    返回 [{code, date, type, title, magnet, magnets, forum_fid, forum_name, url}]
    """
    got = [r for r in records if r.get("magnets")]
    best = {}  # code -> {"tier", "date", "rec"}
    for r in got:
        title = r.get("title", "")
        date = r.get("date", "")
        forum_fid = r.get("forum_fid", "")
        tier = classify(title, forum_fid)
        for code in extract_codes(title):
            if not code:
                continue
            if code not in best:
                best[code] = {"tier": tier, "date": date, "rec": r}
            else:
                cur = best[code]
                if tier < cur["tier"] or (tier == cur["tier"] and date > cur["date"]):
                    best[code] = {"tier": tier, "date": date, "rec": r}

    label = {0: "破解+字幕", 1: "破解", 2: "有码"}
    out = []
    for code, info in sorted(best.items(), key=lambda kv: (kv[1]["tier"], kv[1]["date"])):
        r = info["rec"]
        out.append({
            "code": code,
            "date": r.get("date", ""),
            "type": label[info["tier"]],
            "title": r.get("title", ""),
            "magnet": (r.get("magnets") or [""])[0],
            "magnets": r.get("magnets", []),
            "forum_fid": r.get("forum_fid", ""),
            "forum_name": r.get("forum_name", ""),
            "url": r.get("url", ""),
        })
    return out


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


def parse_date(text):
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text or "")
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def strip_tags(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="彩月七緒")
    parser.add_argument("--cutoff", default="2024-01-23")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--base", default="https://www.sehuatang.org")
    parser.add_argument("--output-dir", default=".")
    parser.add_argument("--fids", default="36,103", help="只保留这些版区 fid（逗号分隔）。默认 36,103 = 亚洲无码原创 + 高清中文字幕（追踪区）")
    parser.add_argument("--no-detail", action="store_true", help="只抓列表页磁力，不进详情页")
    args = parser.parse_args()

    # 版区过滤：留空则全站搜索
    TARGET_FIDS = set(x.strip() for x in (args.fids or "").split(",") if x.strip())
    if TARGET_FIDS:
        print(f"[info] 只保留版区 fid: {sorted(TARGET_FIDS)}")
    else:
        print("[info] 全站搜索（不限版区）")

    base = args.base.rstrip("/")
    chrome = find_chrome()
    if not chrome:
        print("[err] chrome not found")
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "screenshots").mkdir(exist_ok=True)
    profile = out_dir / "chrome_profile"

    cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d")

    kw = quote(args.name)
    search_url = f"{base}/search.php?mod=forum&srchtxt={kw}&searchsubmit=yes"

    all_items = []
    stop = False
    page_urls = []  # 从第一页分页控件解析出的完整翻页 URL

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

        try:
            urls_to_visit = [search_url]
            visited = set()
            for idx in range(0, args.max_pages + 1):
                if not urls_to_visit:
                    break
                url = urls_to_visit.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                pageno = idx + 1
                print(f"[info] page {pageno}: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3500)

                # 自动过年龄确认页（"满18岁，请点此进入"）
                if is_age_gate(page):
                    pass_age_gate(page)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3500)

                content = page.content()
                title = page.title()

                # 搜索限流：Discuz 搜索频率限制（"搜索过于频繁，请30秒后再试"）
                rate_limit_kw = "搜索过于频繁"
                if rate_limit_kw in content or rate_limit_kw in title:
                    print("[warn] 搜索限流，等待 35 秒后重试...")
                    page.wait_for_timeout(35000)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3500)
                    if is_age_gate(page):
                        pass_age_gate(page)
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(3500)
                    content = page.content()
                    title = page.title()

                cf_kw = ["赫拉克利特", "亚里士多德", "cf-browser-verification", "challenge-platform"]
                if any(k in content or k in title for k in cf_kw):
                    print("[warn] cloudflare challenge, waiting up to 90s for manual pass...")
                    passed = False
                    for _ in range(90):
                        page.wait_for_timeout(1000)
                        try:
                            content = page.content()
                            title = page.title()
                        except Exception:
                            content = ""
                            title = ""
                        if not any(k in content or k in title for k in cf_kw):
                            passed = True
                            break
                    if not passed:
                        print("[err] cf timeout")
                        return

                # 第一页时解析分页控件，得到完整翻页 URL 模板 + 总页数，生成所有页
                if idx == 0:
                    pag_links = page.locator("a[href*='page=']")
                    page_nums = set()
                    page_tpl = None
                    for j in range(pag_links.count()):
                        try:
                            h = pag_links.nth(j).get_attribute("href") or ""
                            if "page=" in h and "mod=forum" in h:
                                m = re.search(r"page=(\d+)", h)
                                if m:
                                    page_nums.add(int(m.group(1)))
                                # 记录模板：把 page=N 替换成 {} 便于构造
                                tpl = re.sub(r"page=\d+", "page={}", h)
                                if page_tpl is None:
                                    page_tpl = urljoin(base + "/", tpl)
                        except Exception:
                            continue
                    # 用最大页码作为总页数，构造所有中间页 URL
                    if page_tpl is not None and page_nums:
                        max_page = max(page_nums)
                        for pn in range(2, max_page + 1):
                            full = page_tpl.format(pn)
                            if full not in visited and full not in urls_to_visit:
                                urls_to_visit.append(full)
                        print(f"[info] 总页数约 {max_page}，已生成 {len(urls_to_visit)} 个翻页 URL")
                    else:
                        print("[info] 未发现分页链接")

                # 解析每条结果 <li class="pbw">
                items = page.locator("li.pbw")
                n = items.count()
                page_min_date = None          # 整页所有帖子的最小日期（用于判断是否到 cutoff）
                new_count = 0
                for i in range(n):
                    try:
                        li = items.nth(i)
                        tid = li.get_attribute("id") or ""
                        h3 = li.locator("h3.xs3 a").first
                        href = h3.get_attribute("href") or "" if h3.count() > 0 else ""
                        text_title = h3.inner_text(timeout=1000).strip() if h3.count() > 0 else ""

                        # 提取版区 fid：<a href="forum-<fid>-1.html" class="xi1">版区名</a>
                        forum_fid = ""
                        forum_name = ""
                        fl = li.locator("a[href*='forum-'][class='xi1']")
                        for j in range(fl.count()):
                            try:
                                fh = fl.nth(j).get_attribute("href") or ""
                                mf = re.search(r"forum-(\d+)-\d+\.html", fh)
                                if mf:
                                    forum_fid = mf.group(1)
                                    forum_name = fl.nth(j).inner_text(timeout=500).strip()
                                    break
                            except Exception:
                                continue

                        # 日期：<p> 内最后一个 <span> 里的日期文本（先取，无论是否目标区，用于 cutoff 判断）
                        date_txt = ""
                        spans = li.locator("span")
                        for j in range(spans.count()):
                            t = spans.nth(j).inner_text(timeout=500).strip()
                            if re.search(r"\d{4}-\d{1,2}-\d{1,2}", t):
                                date_txt = t
                                break
                        d = parse_date(date_txt)
                        if d and (page_min_date is None or d < page_min_date):
                            page_min_date = d

                        # 只保留目标发片区，其余（求片/讨论/AI专区等）跳过
                        if TARGET_FIDS and forum_fid and forum_fid not in TARGET_FIDS:
                            continue

                        # 磁力链接：该 li 内的 magnet
                        magnets = set()
                        ma = li.locator("a[href^='magnet:']")
                        for j in range(ma.count()):
                            try:
                                mh = ma.nth(j).get_attribute("href")
                                if mh:
                                    magnets.add(mh)
                            except Exception:
                                pass
                        # 也扫 li 的 inner_html 里的 magnet 文本
                        li_html = li.inner_html()
                        for mm in re.findall(r"magnet:\?xt=urn:btih:[a-fA-F0-9]{20,}", li_html):
                            magnets.add(mm)

                        if not text_title and not href:
                            continue
                        all_items.append({
                            "tid": tid,
                            "title": text_title,
                            "href": href,
                            "url": urljoin(base + "/", href) if href else "",
                            "date": d.strftime("%Y-%m-%d") if d else "",
                            "date_raw": date_txt,
                            "forum_fid": forum_fid,
                            "forum_name": forum_name,
                            "magnets": sorted(magnets),
                        })
                        new_count += 1
                    except Exception:
                        continue

                print(f"[info]   parsed {new_count} items (cumulative {len(all_items)}), page min date {page_min_date.strftime('%Y-%m-%d') if page_min_date else '?'}")

                # 停止条件只看「整页是否有帖子」，不能因「本页过滤后为 0」就停止。
                # 因为限定版区后，某些中间页可能整页都是非目标版区的帖子（过滤后 new_count=0），
                # 但后面的页里仍有目标版区的历史帖子，必须继续翻页。
                if n == 0:
                    print("[info] no threads on this page (empty), stopping")
                    break

                if page_min_date and page_min_date < cutoff:
                    print(f"[info] reached cutoff ({page_min_date.strftime('%Y-%m-%d')} < {args.cutoff}), stopping")
                    stop = True
                    break

            # 去重（按 tid，无 tid 则按 url）
            dedup = {}
            for it in all_items:
                key = it["tid"] or it["url"] or it["title"]
                if key not in dedup:
                    dedup[key] = it

            print(f"\n[info] total unique: {len(dedup)}")

            # ============ 详情页磁力抓取 ============
            # 列表页可能不含磁力（磁力写在正文里），每个帖子再进详情页抓一遍，合并去重
            if not args.no_detail:
                detail_todo = []
                for it in dedup.values():
                    url = it.get("url") or (urljoin(base + "/", it["href"]) if it.get("href") else "")
                    if not url:
                        continue
                    if should_skip_detail(it.get("title", "")):
                        continue
                    detail_todo.append(it)

                print(f"[info] 详情页待抓取: {len(detail_todo)} 帖（已排除求片/讨论类）")

                for di, it in enumerate(detail_todo, 1):
                    url = it.get("url")
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2000)
                        if is_age_gate(page):
                            pass_age_gate(page)
                            page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(2000)
                        txt = page.inner_text("body")
                        detail_mags = sorted(set(MAGNET_RE.findall(txt)))
                        # 合并进已有 magnets（去重）
                        merged = set(it.get("magnets", [])) | set(detail_mags)
                        it["magnets"] = sorted(merged)
                        if detail_mags:
                            print(f"[{di}/{len(detail_todo)}] +{len(detail_mags)}条  {it.get('title','')[:38]}")
                        else:
                            print(f"[{di}/{len(detail_todo)}] 无新磁力  {it.get('title','')[:38]}")
                    except Exception as e:
                        print(f"[{di}/{len(detail_todo)}] 详情页失败  {it.get('title','')[:38]} err={e}")
            # ========================================

            # ============ 按番号归并，输出最终清单 ============
            final_list = merge_by_code(list(dedup.values()))
            print(f"\n[info] 归并后共 {len(final_list)} 部作品")
            from collections import Counter
            c = Counter(o["type"] for o in final_list)
            for k in ["破解+字幕", "破解", "有码"]:
                if c[k]:
                    print(f"  {k}: {c[k]} 部")
            # ================================================

            out_json = out_dir / f"search_{args.name}.json"
            out_json.write_text(json.dumps({
                "actor": args.name,
                "cutoff": args.cutoff,
                "searched_at": datetime.now().isoformat(),
                "count": len(dedup),
                "final_count": len(final_list),
                "threads": list(dedup.values()),
                "final": final_list,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            out_txt = out_dir / f"search_{args.name}.txt"
            lines = [f"actor: {args.name}", f"cutoff: {args.cutoff}", f"count: {len(dedup)}", "=" * 60]
            for it in dedup.values():
                lines.append(f"date: {it['date']} ({it['date_raw']})")
                lines.append(f"forum: {it.get('forum_name','')} (fid={it.get('forum_fid','')})")
                lines.append(f"title: {it['title']}")
                lines.append(f"url: {it['url']}")
                if it["magnets"]:
                    for mg in it["magnets"]:
                        lines.append(f"  magnet: {mg}")
                lines.append("-" * 50)
            out_txt.write_text("\n".join(lines), encoding="utf-8")

            # 最终清单 txt
            out_final = out_dir / f"{args.name}_磁力清单.txt"
            fl = [f"演员: {args.name}", f"cutoff: {args.cutoff}",
                  f"共 {len(final_list)} 部作品", "=" * 60]
            for o in final_list:
                fl.append(f"[{o['type']}] {o['code']}  {o['date']}")
                fl.append(f"  {o['title']}")
                fl.append(f"  {o['magnet']}")
                fl.append("-" * 50)
            out_final.write_text("\n".join(fl), encoding="utf-8")

            print(f"[done] saved {out_json} / {out_txt} / {out_final}")

        except PWTimeout:
            print("[err] page timeout")
        except Exception as e:
            print(f"[err] {e}")
            import traceback
            traceback.print_exc()
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
