# -*- coding: utf-8 -*-
"""
Scan sehuatang.org forum posts and match against an actor list.
Supports multiple actor input methods: json file, directory, or direct arguments.
Uses local Chrome with persistent context to reduce Cloudflare friction.
Supports multi-page, multi-forum scanning, and optional magnet link extraction.
"""
import os
import sys
import re
import json
import argparse
import unicodedata
from urllib.parse import urljoin
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from sehuatang_common import is_age_gate, pass_age_gate

SKILL_DIR = Path(__file__).parent.parent

CJK_SIMPLIFIED_CHARS = str.maketrans({
    "亜": "亚",
    "亞": "亚",
    "愛": "爱",
    "榮": "荣",
    "栄": "荣",
    "紗": "纱",
    "櫻": "樱",
    "桜": "樱",
    "瀬": "濑",
    "澤": "泽",
    "沢": "泽",
    "鈴": "铃",
    "鳳": "凤",
    "嶋": "岛",
    "島": "岛",
    "濱": "滨",
    "邊": "边",
    "邉": "边",
    "実": "实",
    "實": "实",
    "廣": "广",
    "広": "广",
    "麗": "丽",
    "優": "优",
    "夢": "梦",
    "咲": "咲",
    "篠": "筱",
    "來": "来",
    "龍": "龙",
    "聖": "圣",
    "華": "华",
    "葉": "叶",
})


def normalize_name(name: str) -> str:
    return unicodedata.normalize("NFKC", str(name)).strip()


def expand_iteration_mark(text: str) -> str:
    chars = []
    for ch in text:
        if ch == "々" and chars:
            chars.append(chars[-1])
        else:
            chars.append(ch)
    return "".join(chars)


def generated_name_variants(name: str) -> set[str]:
    base = normalize_name(name)
    if not base:
        return set()

    variants = {base, expand_iteration_mark(base)}
    for item in list(variants):
        variants.add(item.translate(CJK_SIMPLIFIED_CHARS))
    return {v for v in variants if v}


def as_name_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def add_alias_group(alias_map: dict[str, set[str]], names) -> None:
    group = []
    seen = set()
    for raw_name in as_name_list(names):
        for name in generated_name_variants(raw_name):
            if name not in seen:
                seen.add(name)
                group.append(name)

    if len(group) < 2:
        return

    for name in group:
        alias_map.setdefault(name, set()).update(other for other in group if other != name)


def merge_alias_data(alias_map: dict[str, set[str]], data) -> None:
    if not data:
        return

    if isinstance(data, dict):
        if "aliases" in data and "name" not in data and "actor" not in data:
            merge_alias_data(alias_map, data.get("aliases"))
            merge_alias_data(alias_map, data.get("actor_aliases"))
            return

        if "name" in data or "actor" in data:
            name = data.get("name") or data.get("actor")
            aliases = data.get("aliases") or data.get("names") or data.get("variants") or []
            add_alias_group(alias_map, [name] + as_name_list(aliases) if name else aliases)
            return

        for name, aliases in data.items():
            add_alias_group(alias_map, [name] + as_name_list(aliases))
        return

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = item.get("name") or item.get("actor")
                aliases = item.get("aliases") or item.get("names") or item.get("variants") or []
                add_alias_group(alias_map, [name] + as_name_list(aliases) if name else aliases)
            elif isinstance(item, list):
                add_alias_group(alias_map, item)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_actor_data(data) -> tuple[set[str], dict[str, set[str]]]:
    actors = set()
    alias_map: dict[str, set[str]] = {}

    def add_actor_entry(item) -> None:
        if isinstance(item, str):
            name = normalize_name(item)
            if name:
                actors.add(name)
            return

        if not isinstance(item, dict):
            return

        name = item.get("name") or item.get("actor")
        aliases = item.get("aliases") or item.get("names") or item.get("variants") or []
        names = [name] + as_name_list(aliases) if name else as_name_list(aliases)
        if name:
            actors.add(normalize_name(name))
        elif names:
            actors.add(normalize_name(names[0]))
        add_alias_group(alias_map, names)

    if isinstance(data, dict):
        raw_actors = data.get("actors", [])
        if isinstance(raw_actors, dict):
            actors.update(normalize_name(name) for name in raw_actors if normalize_name(name))
            merge_alias_data(alias_map, raw_actors)
        else:
            items = as_name_list(raw_actors) if isinstance(raw_actors, str) else (raw_actors or [])
            for item in items:
                add_actor_entry(item)

        merge_alias_data(alias_map, data.get("aliases"))
        merge_alias_data(alias_map, data.get("actor_aliases"))
        return actors, alias_map

    if isinstance(data, list):
        for item in data:
            add_actor_entry(item)

    return actors, alias_map


def load_alias_map(args, inline_aliases: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    alias_map: dict[str, set[str]] = {}
    if args.aliases_file and os.path.exists(args.aliases_file):
        try:
            merge_alias_data(alias_map, read_json(args.aliases_file))
            print(f"[info] loaded aliases from {args.aliases_file}")
        except Exception as e:
            print(f"[warn] failed to load aliases file: {e}")

    if inline_aliases:
        for name, aliases in inline_aliases.items():
            add_alias_group(alias_map, [name] + list(aliases))

    return alias_map


def expand_actor_names(actors: set[str], alias_map: dict[str, set[str]]) -> set[str]:
    expanded = set()
    pending = list(actors)

    while pending:
        raw_name = pending.pop()
        related = generated_name_variants(raw_name)
        related.update(alias_map.get(normalize_name(raw_name), set()))

        for name in related:
            if name and name not in expanded:
                expanded.add(name)
                pending.append(name)

    return expanded


def get_actors_from_dir(actors_dir: str) -> set:
    actors = set()
    if not os.path.isdir(actors_dir):
        return actors
    for name in os.listdir(actors_dir):
        full = os.path.join(actors_dir, name)
        if os.path.isdir(full) and not name.startswith("."):
            actors.add(name)
    return actors


def load_actors(args) -> tuple[set[str], dict[str, set[str]]]:
    """Load actors with priority: --actors > --actors-file > --actors-dir > error"""
    actors = set()
    inline_aliases: dict[str, set[str]] = {}
    source = ""

    # 1. Direct argument list
    if args.actors:
        actors, inline_aliases = parse_actor_data({"actors": args.actors})
        source = "command-line argument"
        if args.save_actors and actors:
            save_actors_file(actors, args.actors_file)
            print(f"[info] saved {len(actors)} actors to {args.actors_file}")
        return actors, inline_aliases

    # 2. JSON file
    if args.actors_file and os.path.exists(args.actors_file):
        try:
            actors, inline_aliases = parse_actor_data(read_json(args.actors_file))
            source = f"json file ({args.actors_file})"
            if actors:
                print(f"[info] loaded {len(actors)} actors from {source}")
                return actors, inline_aliases
        except Exception as e:
            print(f"[warn] failed to load actors file: {e}")

    # 3. Directory
    if args.actors_dir and os.path.isdir(args.actors_dir):
        actors = get_actors_from_dir(args.actors_dir)
        source = f"directory ({args.actors_dir})"
        if actors:
            print(f"[info] loaded {len(actors)} actors from {source}")
            if args.save_actors:
                save_actors_file(actors, args.actors_file)
                print(f"[info] saved {len(actors)} actors to {args.actors_file}")
            return actors, inline_aliases

    print("[err] no actor list found. provide one via --actors, --actors-file, or --actors-dir")
    print("[hint] example: --actors 佐々木さき 楪カレン 五日市芽依")
    sys.exit(1)


def save_actors_file(actors: set, path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "actors": sorted(list(actors)),
            "updated": datetime.now().isoformat(),
        }, f, ensure_ascii=False, indent=2)


def build_match_index(actors: set[str], alias_map: dict[str, set[str]]) -> dict[str, set[str]]:
    match_index: dict[str, set[str]] = {}
    for actor in actors:
        for name in expand_actor_names({actor}, alias_map):
            match_index.setdefault(name, set()).add(actor)
    return match_index


def parse_date(text: str, today: datetime) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    if text.startswith("今天") or text == "刚刚":
        return today
    if text.startswith("昨天"):
        return today - timedelta(days=1)
    if text.startswith("前天"):
        return today - timedelta(days=2)

    m = re.match(r"(\d+)\s*分钟前", text)
    if m:
        return today
    m = re.match(r"(\d+)\s*小时前", text)
    if m:
        hours = int(m.group(1))
        return today if hours < 24 else today - timedelta(days=1)
    m = re.match(r"(\d+)\s*天前", text)
    if m:
        return today - timedelta(days=int(m.group(1)))

    if re.match(r"^\d{1,2}:\d{2}$", text):
        return today

    text = text.split()[0]
    patterns = [
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", lambda m: datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        (r"(\d{4})/(\d{1,2})/(\d{1,2})", lambda m: datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        (r"(\d{1,2})-(\d{1,2})", lambda m: datetime(today.year, int(m.group(1)), int(m.group(2)))),
        (r"(\d{1,2})/(\d{1,2})", lambda m: datetime(today.year, int(m.group(1)), int(m.group(2)))),
    ]
    for pat, builder in patterns:
        m = re.match(pat, text)
        if m:
            try:
                return builder(m)
            except ValueError:
                continue
    return None


def find_chrome():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), r"Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def build_page_url(base_url: str, page: int) -> str:
    if page == 1:
        return base_url
    replaced = re.sub(r"(-\d+)(\.html?)$", lambda m: f"-{page}{m.group(2)}", base_url)
    if replaced == base_url:
        if "?" in base_url:
            replaced = re.sub(r"[?&]page=\d+", "", base_url)
            replaced += f"&page={page}"
        else:
            replaced = f"{base_url}?page={page}"
    return replaced


def parse_posts_from_page(page):
    posts = []
    try:
        page.wait_for_selector("table#threadlisttableid, #threadlist, tbody[id^='normalthread']", timeout=10000)
    except PlaywrightTimeout:
        pass

    rows = page.locator("tbody[id^='normalthread']")
    row_count = rows.count()
    if row_count > 0:
        for i in range(row_count):
            row = rows.nth(i)
            try:
                title_el = row.locator("a.s.xst, a.xst, th a[href*='thread']").first
                title = title_el.inner_text(timeout=2000).strip() if title_el.count() > 0 else ""
                href = title_el.get_attribute("href") if title_el.count() > 0 else ""
                date_el = row.locator("td.by em, td.by span, .by em, .by span").first
                date_text = date_el.inner_text(timeout=2000).strip() if date_el.count() > 0 else ""
                if title:
                    posts.append({"title": title, "date_text": date_text, "href": str(href)})
            except Exception:
                continue
    else:
        links = page.locator("a[href*='thread']")
        n = links.count()
        seen = set()
        for i in range(min(n, 200)):
            try:
                el = links.nth(i)
                title = el.inner_text(timeout=1000).strip()
                href = el.get_attribute("href") or ""
                if title and title not in seen and len(title) > 4:
                    seen.add(title)
                    date_text = ""
                    try:
                        parent = el.locator("xpath=../../..")
                        if parent.count() > 0:
                            de = parent.locator("em, span.date, .by em").first
                            date_text = de.inner_text(timeout=1000).strip() if de.count() > 0 else ""
                    except Exception:
                        pass
                    posts.append({"title": title, "date_text": date_text, "href": str(href)})
            except Exception:
                continue
    return posts


def extract_magnets(page, href: str, forum_url: str) -> list[str]:
    if not href:
        return []
    full_url = urljoin(forum_url, href)
    magnets = set()
    try:
        page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        if is_age_gate(page):
            pass_age_gate(page)
            page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

        magnet_anchors = page.locator('a[href^="magnet:"]')
        for i in range(magnet_anchors.count()):
            try:
                href_val = magnet_anchors.nth(i).get_attribute("href")
                if href_val:
                    magnets.add(href_val)
            except Exception:
                continue

        text = page.content()
        found = re.findall(r'magnet:\?xt=urn:btih:[a-fA-F0-9]+(?:&[^"\s<>]+)?', text)
        for m in found:
            magnets.add(m)

    except Exception as e:
        print(f"[warn] failed to extract magnets from {full_url}: {e}")
    return list(magnets)


def scrape(args):
    actors, inline_aliases = load_actors(args)
    alias_map = load_alias_map(args, inline_aliases)
    match_index = build_match_index(actors, alias_map)
    match_names = set(match_index.keys())
    print(f"[info] tracking {len(actors)} actors ({len(match_names)} names including aliases)")

    chrome = find_chrome()
    if not chrome:
        print("[err] chrome not found")
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    user_data_dir = out_dir / "chrome_profile"
    screenshot_dir = out_dir / "screenshots"
    screenshot_dir.mkdir(exist_ok=True)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=args.days - 1)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            executable_path=chrome,
            headless=args.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            all_matched = []
            total_posts = 0
            total_pages_scanned = 0
            cf_keywords = ["赫拉克利特", "亚里士多德", "希腊谚语", "佛教谚语", "cf-browser-verification", "challenge-platform"]

            for forum_url in args.urls:
                print(f"\n[info] scanning forum: {forum_url}")
                seen_hrefs = set()
                forum_posts = []
                last_page_num = 0

                for page_num in range(1, args.max_pages + 1):
                    url = build_page_url(forum_url, page_num)
                    print(f"[info] opening page {page_num}: {url}")
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)

                    # 自动过"满18岁，请点此进入"年龄确认页（与全量抓取脚本同一套逻辑）
                    if is_age_gate(page):
                        pass_age_gate(page)
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(3000)


                    title = page.title()
                    content = page.content()
                    is_cf = any(k in content or k in title for k in cf_keywords)

                    if is_cf:
                        print("[warn] cloudflare challenge detected")
                        if not args.headless:
                            print("[hint] please complete verification in the opened chrome window (if any). waiting up to 90s...")
                            for _ in range(90):
                                page.wait_for_timeout(1000)
                                try:
                                    content = page.content()
                                    title = page.title()
                                except Exception:
                                    # Page is navigating, likely challenge passed
                                    content = ""
                                    title = ""
                                still_cf = any(k in content or k in title for k in cf_keywords)
                                if not still_cf:
                                    print("[info] challenge passed")
                                    break
                            else:
                                print("[err] timeout waiting for challenge")
                                page.screenshot(path=str(screenshot_dir / f"cf_timeout_{page_num}.png"))
                                context.close()
                                return
                        else:
                            print("[err] blocked by cloudflare in headless mode. run once without --headless to pass challenge.")
                            context.close()
                            return

                    posts = parse_posts_from_page(page)
                    new_posts = 0
                    for post in posts:
                        if post["href"] and post["href"] not in seen_hrefs:
                            seen_hrefs.add(post["href"])
                            forum_posts.append(post)
                            new_posts += 1
                        elif not post["href"] and post["title"] not in {p["title"] for p in forum_posts}:
                            forum_posts.append(post)
                            new_posts += 1

                    print(f"[info] page {page_num}: {len(posts)} rows, {new_posts} new posts (total {len(forum_posts)})")
                    total_posts += len(posts)
                    total_pages_scanned += 1
                    last_page_num = page_num

                    if page_num >= 2 and posts:
                        dates = [parse_date(p["date_text"], today) for p in posts]
                        valid_dates = [d for d in dates if d]
                        if valid_dates and max(valid_dates) < cutoff:
                            print(f"[info] page {page_num} oldest post is before cutoff, stopping early")
                            break

                    if page_num < args.max_pages:
                        page.wait_for_timeout(2000)

                for post in forum_posts:
                    dt = parse_date(post["date_text"], today)
                    if dt and dt >= cutoff:
                        matched_names = sorted(name for name in match_names if name in post["title"])
                        found = sorted({actor for name in matched_names for actor in match_index.get(name, {name})})
                        if found:
                            item = {
                                "date": dt.strftime("%Y-%m-%d"),
                                "date_raw": post["date_text"],
                                "title": post["title"],
                                "href": post["href"],
                                "actors": found,
                                "matched_names": matched_names,
                                "forum": forum_url,
                            }
                            if args.fetch_magnets and post.get("href"):
                                print(f"[info] fetching magnets for: {post['title'][:40]}...")
                                item["magnets"] = extract_magnets(page, post["href"], forum_url)
                            all_matched.append(item)

            if not all_matched:
                lines = []
                lines.append("=" * 60)
                lines.append(f"scan_time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                lines.append(f"actors: {len(actors)}")
                lines.append(f"match_names: {len(match_names)}")
                lines.append(f"posts_scanned: {total_posts}")
                lines.append(f"pages_scanned: {total_pages_scanned}")
                lines.append(f"range: {cutoff.strftime('%Y-%m-%d')} ~ {today.strftime('%Y-%m-%d')}")
                lines.append("=" * 60)
                lines.append("\nno matched posts in the recent period.")
                result_text = "\n".join(lines)
                print("\n" + result_text)
                (out_dir / "result.txt").write_text(result_text, encoding="utf-8")
                page.screenshot(path=str(screenshot_dir / "last_run.png"))
                print(f"[done] results saved to {out_dir}")
                context.close()
                return

            lines = []
            lines.append("=" * 60)
            lines.append(f"scan_time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"actors: {len(actors)}")
            lines.append(f"match_names: {len(match_names)}")
            lines.append(f"posts_scanned: {total_posts}")
            lines.append(f"pages_scanned: {total_pages_scanned}")
            lines.append(f"forums: {len(args.urls)}")
            lines.append(f"range: {cutoff.strftime('%Y-%m-%d')} ~ {today.strftime('%Y-%m-%d')}")
            lines.append("=" * 60)
            lines.append(f"\nmatched {len(all_matched)} posts\n")

            for m in all_matched:
                lines.append(f"date: {m['date']} ({m['date_raw']})")
                lines.append(f"title: {m['title']}")
                lines.append(f"actors: {', '.join(m['actors'])}")
                if m.get("matched_names") and sorted(m["actors"]) != sorted(m["matched_names"]):
                    lines.append(f"matched_names: {', '.join(m['matched_names'])}")
                lines.append(f"href: {m['href']}")
                if "magnets" in m:
                    if m["magnets"]:
                        lines.append(f"magnets:")
                        for mg in m["magnets"]:
                            lines.append(f"  - {mg}")
                    else:
                        lines.append("magnets: (none found)")
                lines.append("-" * 40)

            result_text = "\n".join(lines)
            print("\n" + result_text)

            (out_dir / "result.txt").write_text(result_text, encoding="utf-8")
            (out_dir / "last_result.json").write_text(
                json.dumps({
                    "scan_time": datetime.now().isoformat(),
                    "cutoff": cutoff.strftime("%Y-%m-%d"),
                    "today": today.strftime("%Y-%m-%d"),
                    "matched": all_matched,
                    "total_posts": total_posts,
                    "pages_scanned": total_pages_scanned,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            page.screenshot(path=str(screenshot_dir / "last_run.png"))
            print(f"[done] results saved to {out_dir}")

        except PlaywrightTimeout:
            print("[err] page timeout")
            page.screenshot(path=str(screenshot_dir / "timeout.png"))
        except Exception as e:
            print(f"[err] {e}")
            import traceback
            traceback.print_exc()
            try:
                page.screenshot(path=str(screenshot_dir / "error.png"))
            except Exception:
                pass
        finally:
            context.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    default_actors_file = str(SKILL_DIR / "actors.json")

    parser = argparse.ArgumentParser(description="Scan sehuatang forum for actor updates")
    parser.add_argument("--actors", nargs="+", default=None, help="Direct actor names (space-separated)")
    parser.add_argument("--actors-file", default=default_actors_file, help="Path to actors JSON file")
    parser.add_argument("--aliases-file", default=str(SKILL_DIR / "aliases.json"), help="Path to alias mapping JSON file")
    parser.add_argument("--actors-dir", default=r"E:\sakana", help="Local actor directory (fallback)")
    parser.add_argument("--save-actors", action="store_true", help="Save loaded actors to --actors-file")
    parser.add_argument("--urls", nargs="+", default=[
        "https://www.sehuatang.org/forum-103-1.html",
        "https://www.sehuatang.org/forum-36-1.html",
    ], help="Target forum URLs to scan")
    parser.add_argument("--days", type=int, default=3, help="How many recent days to check")
    parser.add_argument("--max-pages", type=int, default=5, help="Max pages per forum")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--output-dir", default=".", help="Directory for results")
    parser.add_argument("--fetch-magnets", action="store_true", help="Open matched threads and extract magnet links")
    args = parser.parse_args()
    scrape(args)


if __name__ == "__main__":
    main()
