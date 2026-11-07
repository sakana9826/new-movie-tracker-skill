# -*- coding: utf-8 -*-
"""
把抓到的磁力按番号归并，每部作品保留一条最优磁力。

优先级（由高到低）：
  1. 破解 + 中文字幕（标题含"破解/无码/無碼/新模型" 且 含"字幕/中文/内嵌/中字"）
  2. 破解（标题含"破解/无码/無碼/新模型"）
  3. 其它（普通有码版）

同档内按发帖日期最新的优先。

用法：
  py -3.14 scripts/merge_magnets.py --input 磁力抓取_全量.json --output 彩月七緒_磁力清单.json
"""
import os
import re
import sys
import json
import argparse
from datetime import datetime


POJIE_RE = re.compile(r'破解|无码|無碼|新模型|無修正')
ZIMU_RE = re.compile(r'字幕|中文|内嵌|中字|漢化|中文字幕')
CODE_RE = re.compile(r'\b([A-Z]{2,6}-?(\d{2,5}))\b')


def norm_code(c):
    c = c.upper().strip()
    c = re.sub(r'-0+', '-', c)  # CAWB-40 -> CAWB-040
    return c


def extract_codes(title):
    """从标题里提取所有番号"""
    return [norm_code(m.group(1)) for m in CODE_RE.finditer(title or "")]


def classify(title, forum_fid=""):
    """返回优先级档位：0=破解+字幕(最高), 1=破解, 2=其它

    字幕判断来源：
    - 标题含字幕关键词（字幕/中文/内嵌/中字/漢化）
    - 或版区是「高清中文字幕」fid=103（该区发的破解帖自带中文字幕）
    """
    t = title or ""
    pojie = bool(POJIE_RE.search(t))
    zimu = bool(ZIMU_RE.search(t)) or str(forum_fid) == "103"
    if pojie and zimu:
        return 0
    if pojie:
        return 1
    return 2


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="磁力抓取_全量.json")
    parser.add_argument("--output", default="彩月七緒_磁力清单.json")
    args = parser.parse_args()

    data = json.load(open(args.input, encoding="utf-8"))
    # 兼容 list 或 dict 两种结构
    if isinstance(data, dict):
        records = data.get("threads") or data.get("results") or []
    else:
        records = data

    got = [r for r in records if r.get("magnets")]

    # 按番号归并：每部作品保留优先级最高、日期最新的那条
    best = {}  # code -> record
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
                # 优先档位高的；同档取日期新的
                if tier < cur["tier"] or (tier == cur["tier"] and date > cur["date"]):
                    best[code] = {"tier": tier, "date": date, "rec": r}

    out = []
    for code, info in sorted(best.items(), key=lambda kv: (kv[1]["tier"], kv[1]["date"])):
        r = info["rec"]
        tier = info["tier"]
        label = {0: "破解+字幕", 1: "破解", 2: "有码"}[tier]
        out.append({
            "code": code,
            "date": r.get("date", ""),
            "type": label,
            "title": r.get("title", ""),
            "magnet": (r.get("magnets") or [""])[0],
            "magnets": r.get("magnets", []),
        })

    json.dump(out, open(args.output, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 统计
    from collections import Counter
    c = Counter(o["type"] for o in out)
    print(f"共 {len(out)} 部作品:")
    for k in ["破解+字幕", "破解", "有码"]:
        if c[k]:
            print(f"  {k}: {c[k]} 部")
    print(f"saved {args.output}")


if __name__ == "__main__":
    main()
