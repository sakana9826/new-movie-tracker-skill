# -*- coding: utf-8 -*-
"""扫描论坛列表页，提取回复数，过滤热帖，交叉对比追踪列表"""
import sys, re, json
from pathlib import Path
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SKILL_DIR = Path(__file__).parent.parent

def find_chrome():
    for path in [
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    ]:
        if Path(path).exists():
            return path
    return None

def parse_date_text(text, today):
    text = text.strip()
    if not text:
        return None
    if text.startswith('今天') or text == '刚刚':
        return today
    if text.startswith('昨天'):
        return today - timedelta(days=1)
    if text.startswith('前天'):
        return today - timedelta(days=2)
    m = re.match(r'(\d+)\s*小时前', text)
    if m:
        h = int(m.group(1))
        return today if h < 24 else today - timedelta(days=1)
    # 尝试解析 MM-DD 或 YYYY-MM-DD
    for pat in [r'(\d{1,2})-(\d{1,2})', r'(\d{4})-(\d{1,2})-(\d{1,2})']:
        m = re.search(pat, text)
        if m:
            try:
                if len(m.groups()) == 2:
                    return datetime(today.year, int(m.group(1)), int(m.group(2)))
                else:
                    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    # 加载追踪列表
    with open(SKILL_DIR / 'actors.json', encoding='utf-8') as f:
        tracked = set(json.load(f)['actors'])
    # 加载别名
    with open(SKILL_DIR / 'aliases.json', encoding='utf-8') as f:
        alias_data = json.load(f)
    alias_map = alias_data.get('aliases', {})
    # 构建所有搜索名
    all_names = set(tracked)
    for name, als in alias_map.items():
        all_names.add(name)
        all_names.update(als)

    chrome = find_chrome()
    if not chrome:
        print('[err] Chrome not found')
        sys.exit(1)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=2)

    out_dir = Path('.')
    user_data_dir = str(out_dir / 'chrome_profile')
    screenshot_dir = out_dir / 'screenshots'
    screenshot_dir.mkdir(exist_ok=True)

    results = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            executable_path=chrome,
            headless=False,  # 用有头模式过Cloudflare
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ],
            viewport={'width': 1366, 'height': 768},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        )
        page = context.pages[0] if context.pages else context.new_page()

        cf_keywords = ['赫拉克利特', '亚里士多德', 'cf-browser-verification', 'challenge-platform']

        for forum_id in [103, 36]:
            for pagenum in range(1, 5):
                url = f'https://www.sehuatang.org/forum-{forum_id}-{pagenum}.html'
                print(f'[info] {url}')
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    page.wait_for_timeout(3000)

                    content = page.content()
                    title = page.title()
                    if any(k in content or k in title for k in cf_keywords):
                        print('[err] Cloudflare blocked')
                        context.close()
                        sys.exit(1)

                    # JS提取列表行数据
                    rows = page.evaluate('''
                    () => {
                        const results = [];
                        let trs = document.querySelectorAll('tbody[id^="normalthread_"]');
                        if (!trs || trs.length === 0) {
                            trs = document.querySelectorAll('tbody tr');
                        }
                        for (const tr of trs) {
                            const a = tr.querySelector('th a[href*="thread-"]') || tr.querySelector('a.s.xst');
                            if (!a) continue;
                            const title = a.innerText.trim();
                            const href = a.href;

                            // 正确提取回复数（不是查看数）
                            // Discuz结构: td.num 里第一个<a>是回复，第二个<a>或下一个td.num是查看
                            let replies = 0;

                            // 方法1: 找带有"回复"em标签的td/祖先
                            const numTds = tr.querySelectorAll('td.num');
                            if (numTds.length >= 1) {
                                // 第一个td.num = 回复数
                                const repA = numTds[0].querySelector('a');
                                if (repA) replies = parseInt(repA.innerText.trim()) || 0;
                            }

                            // 方法2: 如果上面没拿到，找包含"回复"文字的相邻数字
                            if (replies === 0) {
                                const allTds = tr.querySelectorAll('td');
                                for (const td of allTds) {
                                    const em = td.querySelector('em');
                                    if (em && em.innerText.includes('回复')) {
                                        const repA = td.querySelector('a');
                                        if (repA) replies = parseInt(repA.innerText.trim()) || 0;
                                        break;
                                    }
                                }
                            }

                            // 方法3: 直接按位置取 - Discuz标准: tds[3]=回复, tds[4]=查看
                            if (replies === 0) {
                                const allTds = tr.querySelectorAll('td');
                                // 跳过第0个(图标)和第1个(标题在th不在td)
                                // td索引: 0=图标, 1=空, 2=作者, 3=回复, 4=查看
                                if (allTds.length >= 4) {
                                    const repText = allTds[3].innerText.trim();
                                    replies = parseInt(repText) || 0;
                                }
                            }

                            // 日期
                            let dateStr = '';
                            const em = tr.querySelector('em');
                            if (em && !em.innerText.includes('回复') && !em.innerText.includes('查看')) {
                                dateStr = em.innerText.trim();
                            }
                            const spanTitle = tr.querySelector('span[title]');
                            if (spanTitle) {
                                const t = spanTitle.getAttribute('title');
                                if (t) dateStr = t;
                            }
                            results.push({title, href, replies, dateStr});
                        }
                        return results;
                    }
                    ''')

                    print(f'  抓到 {len(rows)} 行')
                    if not rows:
                        break

                    for row in rows:
                        dt = parse_date_text(row['dateStr'], today)
                        row['date_parsed'] = dt.isoformat() if dt else None
                        results.append(row)

                    # 检查本页最旧帖子是否已过 cutoff
                    dates = [parse_date_text(r['dateStr'], today) for r in rows]
                    valid_dates = [d for d in dates if d]
                    if valid_dates and max(valid_dates) < cutoff:
                        print(f'  [info] 本页最旧帖子已过cutoff，停止')
                        break

                except Exception as e:
                    print(f'[err] {e}')
                    break

        context.close()

    # 过滤：最近2天 + 回复>200
    hot = [r for r in results if r.get('replies', 0) > 200 and r.get('date_parsed')]
    hot = [r for r in hot if datetime.fromisoformat(r['date_parsed']) >= cutoff]

    print(f'\n=== 最近2天 回复>200 的帖子: {len(hot)} 个 ===')

    tracked_posts = []
    untracked_posts = []

    for r in hot:
        title = r['title']
        # 检查是否包含追踪演员名
        matched_actors = [a for a in tracked if a in title]
        # 也检查别名
        matched_names = [n for n in all_names if n in title]
        if matched_actors or matched_names:
            tracked_posts.append(r)
        else:
            untracked_posts.append(r)

    print(f'\n--- 追踪演员的新帖 ({len(tracked_posts)} 个) ---')
    for r in sorted(tracked_posts, key=lambda x: -x['replies']):
        matched = [a for a in tracked if a in r['title']]
        print(f'  [{r["replies"]}回复] {r["title"][:70]}')
        print(f'    演员: {", ".join(matched)}')
        print(f'    {r["href"]}')

    print(f'\n--- 未追踪但高热度 ({len(untracked_posts)} 个) ---')
    for r in sorted(untracked_posts, key=lambda x: -x['replies']):
        print(f'  [{r["replies"]}回复] {r["title"][:70]}')
        print(f'    {r["href"]}')

    # 保存结果
    output = {
        'scan_time': datetime.now().isoformat(),
        'cutoff': cutoff.isoformat(),
        'tracked_posts': tracked_posts,
        'untracked_posts': untracked_posts,
    }
    out_path = out_dir / 'hot_posts.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f'\n[done] 结果已保存到 {out_path}')

if __name__ == '__main__':
    main()
