# New Movie Tracker

一个 Agent Skill，让 AI 自动监控 sehuatang.org 论坛新作更新。你只需要告诉 AI 喜欢哪些演员，它就会在论坛里筛选这些演员最近发布的作品，并可选提取磁力链接。

## 功能

- **对话式演员管理**：直接口述演员名字，Agent 自动维护追踪清单
- **多板块监控**：同时扫描 `forum-103`（有码）和 `forum-36`（无码/破解）
- **智能翻页**：自动翻页并提前停止，避免无效请求
- **磁力提取**：进入帖子详情页自动抓取 `magnet:?xt=urn:btih:` 链接
- **年龄确认页自动点击**：遇到"满18岁，请点此进入"会自动点击并重进（`sehuatang_common.py` 通用模块，所有脚本共用）
- **Cloudflare 穿透**：复用本地 Chrome 会话（`chrome_profile/`），首次验证后自动通行
- **单演员全量搜索**：按演员名搜索其全部作品并发帖记录，输出作品清单和磁力清单
- **中日文别名匹配**：`aliases.json` 维护别名组，标题用中文名/日文名都能命中

## 安装

方法一：直接把项目链接给你的ai 

方法二：
克隆到本地即可使用，支持通过 AI Agent 调用，也支持直接运行底层脚本。

```bash
git clone https://github.com/sakana9826/new-movie-tracker-skill.git
cd new-movie-tracker-skill
pip install playwright
python -m playwright install
```

> 需要 Windows + Python 3.9+ + Google Chrome。

### 作为 Agent Skill 使用

如果你的 AI Agent 支持读取 `SKILL.md` 指令文件，将本仓库放到 Agent 的技能目录下并启用即可。Agent 会读取 `SKILL.md` 中的指令，自动调用 `scripts/scan.py` 完成扫描。

## 使用方法

### 告诉 AI 你想追踪谁

安装启用后，直接用自然对话告诉你的 AI 演员名单，例如：

> "帮我追踪 佐々木さき、楪カレン、五日市芽依"

> "再加一个 森日向子，删掉 三上悠亜"

> "从我的 D:\Movies\Actors 文件夹导入演员名单"

Agent 会自动维护 `actors.json`，后续扫描都基于这份清单。

### 查询更新

演员清单设置好后，随时问：

- "帮我看看谁更新了"
- "最近三天有谁出新作"
- "sehuatang 上我喜欢的演员有更新吗"
- "扫一下 forum-36 和 forum-103，顺便把磁力链接也拿了"

Agent 会调用 `scripts/scan.py` 运行扫描并返回结果。

### 演员清单的几种维护方式

| 方式 | 示例对话 |
|------|---------|
| **口述添加** | "把 佐々木さき 加进清单" |
| **口述删除** | "删掉 三上悠亜" |
| **文件夹导入** | "从 D:\Movies\Actors 导入演员" |
| **直接编辑** | 修改目录下的 `actors.json` |

### 中日文名称别名

论坛标题可能使用中文名称，而你提供的是日文名称，或者反过来。项目现在支持别名组：

- `actors.json` 继续保存主追踪名单
- `aliases.json` 保存中日文别名映射
- `actors.json` 也可以直接写成 `{ "name": "...", "aliases": [...] }` 的形式

示例：

```json
{
  "aliases": {
    "三上悠亜": ["三上悠亚"]
  }
}
```

扫描时会把主名和别名一起匹配，并在结果里显示实际命中的 `matched_names`。脚本也会自动生成少量安全变体，例如 `々` 展开和常见日式汉字简化。

### 首次运行注意

因为目标网站有 Cloudflare 防护，**第一次运行时会弹出一个 Chrome 窗口**。如果出现人机验证请手动完成（最多等待 90 秒）。验证通过后，会话信息保存在 `chrome_profile/` 中，后续运行无需再次验证。

论坛的年龄确认页（"满18岁，请点此进入"）不需要手动处理，脚本会自动点击。

> 实测经验：新开浏览器会话偶尔会在首页撞上 Cloudflare "请稍候…" 挑战并超时，**直接重跑一次通常就能通过**。个别帖子详情页可能反复卡在挑战页（疑似帖子本身受限），换个时间再试或手动打开查看。

### 结果去重提示

论坛目前存在大量"同番号、不同源"的帖子（LTX 模型破解、自提征用、自译征用等反复放出）。判断是否真的有新内容时，应按 **番号 + 磁力链接 hash** 比对，而不是只看番号——同一个番号隔几天可能出新画质版本，值得换源重下。

## 手动运行脚本（不依赖 Agent）

```bash
cd new-movie-tracker

# 基础扫描（使用 actors.json 中的清单）
python scripts/scan.py --days 3

# 扫描并提取磁力链接
python scripts/scan.py --days 3 --fetch-magnets

# 临时指定演员扫描（不保存到清单）
python scripts/scan.py --actors 佐々木さき 楪カレン --days 3 --fetch-magnets

# 从文件夹导入并保存
python scripts/scan.py --actors-dir "D:\Movies\Actors" --save-actors

# 单演员全量搜索（搜出该演员全部作品帖 + 磁力，输出作品清单）
python scripts/search_actor.py "彩月七緒"

# 全量磁力抓取（配合搜索结果批量进详情页抓磁力，再合并去重）
python scripts/fetch_magnets_all.py
python scripts/merge_magnets.py
```

完整参数说明见 [reference.md](reference.md)。

## 目录结构

```
new-movie-tracker/
├── SKILL.md               # Agent 指令文件
├── README.md              # 本文件
├── actors.json            # 演员清单（由 Agent 自维护，不入库）
├── aliases.json           # 中日文别名映射（不入库）
├── reference.md           # 详细参数与排错文档
├── hot_posts.json         # 热帖缓存
├── chrome_profile/        # 持久化浏览器会话（勿删，不入库）
├── .gitignore
└── scripts/
    ├── scan.py            # 核心扫描脚本（论坛增量更新 + 磁力提取）
    ├── scan_hot.py        # 热帖扫描
    ├── search_actor.py    # 单演员全量搜索（作品清单 + 磁力清单）
    ├── fetch_magnets.py   # 磁力详情页批量抓取（列表输入）
    ├── fetch_magnets_all.py  # 全量磁力抓取（分页翻完）
    ├── merge_magnets.py   # 多次抓取结果合并去重
    └── sehuatang_common.py   # 通用模块：年龄确认页自动点击、访问保活
```

## 常见问题

**Q: Chrome 窗口弹出来又关了，没来得及点验证？**  
A: 重新运行即可。如果频繁触发验证，尝试关闭其他 Chrome 窗口后再运行，确保 Playwright 能独占 `chrome_profile`。

**Q: 为什么有些帖子没匹配到？**  
A: 脚本使用「子字符串匹配」。如果论坛标题使用了别名、缩写、或不同假名写法，就会漏掉。可以把中日文名称和常见别名加进 `aliases.json`。

**Q: 匹配到了不相关的帖子？**  
A: 检查 `actors.json` 中是否有过于简短或通用的名字（如 `000`、`fc2` 之类）。直接编辑删除即可。

**Q: 磁力链接提取失败？**  
A: 部分帖子可能使用网盘链接或其他分享方式，没有磁力链。此时结果中会显示 `magnets: (none found)`。个别帖子详情页会一直卡在 Cloudflare "请稍候…"（疑似帖子受限），可稍后重试或手动打开查看。

**Q: 同一部片反复出现？**  
A: 论坛会持续放出同一番号的更好版本（LTX 模型破解 > 新模型破解 > 普通破解，另有自提征用/自译征用）。按「番号 + 磁力 hash」判断新旧，画质更好的版本值得换源重下。

## License

MIT
