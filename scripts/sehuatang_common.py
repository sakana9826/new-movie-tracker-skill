# -*- coding: utf-8 -*-
"""
sehuatang.org 自动过年龄确认页（"满18岁，请点此进入"）的通用函数。
点击 .enter-btn 触发跳转并设置 cookie，之后持久化 profile 就不再弹。
"""
import re


def is_age_gate(page):
    """判断当前页面是否为年龄确认页（含 enter-btn）"""
    try:
        return page.locator("a.enter-btn").count() > 0
    except Exception:
        return False


def pass_age_gate(page, timeout=15):
    """如果当前是年龄确认页，自动点击"满18岁，请点此进入"并等待跳转。

    返回 True 表示已处理（或本来就不是确认页），False 表示点击失败。
    """
    if not is_age_gate(page):
        return True  # 不是确认页，无需处理

    print("[gate] 检测到年龄确认页，自动点击进入...")
    try:
        # 点击第一个 enter-btn（中英文两个按钮，href 都是 ./）
        btn = page.locator("a.enter-btn").first
        btn.click(timeout=5000)
        # 等待跳转 / cookie 设置完成
        page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(2000)
        # 确认已离开确认页
        if is_age_gate(page):
            # 再试一次直接导航
            page.goto("./", wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(2000)
        print("[gate] 已进入论坛")
        return not is_age_gate(page)
    except Exception as e:
        print(f"[gate] 自动点击失败: {e}")
        return False


def ensure_access(page, url, timeout=15):
    """访问 url，若落在年龄确认页则自动点过，返回是否成功进入目标页。"""
    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    page.wait_for_timeout(3000)
    if is_age_gate(page):
        pass_age_gate(page, timeout)
        # 重新导航到目标 url
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(3000)
    return not is_age_gate(page)
