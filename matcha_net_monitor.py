#!/usr/bin/env python3
"""
丸久小山园（中国大陆站 marukyu-koyamaen.net）上货监测 —— 只盯指定商品（默认：又玄 / Yugen）

这个站的列表页只显示"有货"的商品，缺货的直接不出现。所以逻辑是：
  列表里出现了关注的商品  → 有货 → 通知
  列表里没有               → 缺货/已售完

通知规则：
  - 从"没有"变成"有了"：立刻通知（邮件 + Bark 推送）
  - 持续有货期间，每隔 REMIND_EVERY 秒再提醒一次，直到它从列表里消失
  - 为防止偶尔抓到异常页面造成误判，要"连续两次都没看到"才认定已售完

依赖：pip install curl_cffi requests

环境变量（和之前一样）：
  SMTP_USER / SMTP_PASS / MAIL_TO      邮件
  BARK_KEY（可选）                      Bark 手机推送；BARK_LEVEL / BARK_CALL / BARK_SERVER 可选

用法：
  python3 matcha_net_monitor.py --dry-run     只检查并打印列表里每个商品，不发通知（先跑这个！）
  python3 matcha_net_monitor.py --dump        把列表页原始 HTML 存成 net_list_page.html，方便排查
  python3 matcha_net_monitor.py --test-mail   发测试邮件
  python3 matcha_net_monitor.py --test-push   发测试推送
  python3 matcha_net_monitor.py --once        检查一次就退出（配合 GitHub Actions / cron）
  python3 matcha_net_monitor.py               常驻运行，每 INTERVAL 秒检查一次
"""

import argparse
import html as htmllib
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

# 优先使用 curl_cffi（模拟真实 Chrome，减少被拦）；没装就退回普通 requests
try:
    from curl_cffi import requests as http
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:
    import requests as http
    IMPERSONATE = {}

# ====== 可调参数 ======
LIST_URL = "https://www.marukyu-koyamaen.net/category/select/cid/312/"
# 关注的商品：名称里包含任意一个关键词（不区分大小写）就算命中
WATCH_KEYWORDS = ["又玄", "Yugen","五十铃"]
# 额外按商品编号前缀匹配（旧站 Yugen 的编号以 1171 开头，如 1171020C1=20g、1171040C1=40g）
WATCH_SKU_PREFIX = ["1171"]
INTERVAL = 300         # 常驻模式检查间隔（秒）
REMIND_EVERY = 300     # 持续有货期间的重复提醒间隔（秒）；配合 5 分钟触发，建议不要小于 240
MISS_TO_CLEAR = 2      # 连续几次没在列表里看到，才认定已售完
STATE_FILE = Path(__file__).with_name("matcha_net_state.json")
DUMP_FILE = Path(__file__).with_name("net_list_page.html")
# ======================

if IMPERSONATE:
    HEADERS = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
else:
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

ITEM_SPLIT_RE = re.compile(r'<div\s+class="item"\s*>', re.I)
H3_RE = re.compile(r'<h3[^>]*>\s*<a[^>]*href="([^"]*?/pid/(\d+))"[^>]*>(.*?)</a>', re.I | re.S)
SKU_RE = re.compile(r'class="id_number"[^>]*>(.*?)</li>', re.I | re.S)
TOTAL_RE = re.compile(r"(\d+)\s*件\s*中")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def clean(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = htmllib.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def fetch(url, retries=2):
    for i in range(retries + 1):
        try:
            r = http.get(url, headers=HEADERS, timeout=30, **IMPERSONATE)
            r.raise_for_status()
            try:
                if (getattr(r, "encoding", None) or "").lower() in ("", "iso-8859-1"):
                    r.encoding = "utf-8"
            except Exception:
                pass
            return r.text
        except Exception:
            if i == retries:
                raise
            time.sleep(2 * (i + 1))


def parse_listing(page):
    """返回 [{pid, name, sku, url}]（列表页上出现的都是有货商品）"""
    items, seen = [], set()
    for chunk in ITEM_SPLIT_RE.split(page)[1:]:
        m = H3_RE.search(chunk)
        if not m:
            continue
        url, pid, name = m.group(1), m.group(2), clean(m.group(3))
        if pid in seen:
            continue
        seen.add(pid)
        sm = SKU_RE.search(chunk)
        sku = clean(sm.group(1)) if sm else ""
        items.append({"pid": pid, "name": name, "sku": sku, "url": url})
    return items


def is_watched(item):
    name = item["name"].lower()
    if any(k.strip().lower() in name for k in WATCH_KEYWORDS if k.strip()):
        return True
    sku = item["sku"].lower()
    return any(sku.startswith(p.strip().lower()) for p in WATCH_SKU_PREFIX if p.strip())


def scan():
    page = fetch(LIST_URL)
    items = parse_listing(page)
    total = TOTAL_RE.search(page)
    if total and int(total.group(1)) > len(items):
        log(f"警告：页面显示共 {total.group(1)} 件，但只解析到 {len(items)} 件，可能有多页，请告诉我补充翻页功能")
    return items, page


def send_mail(subject, body):
    user, pwd, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pwd and to):
        raise RuntimeError("请先设置环境变量 SMTP_USER / SMTP_PASS / MAIL_TO")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.send_message(msg)


def send_push(title, body, url=None):
    key = os.environ.get("BARK_KEY")
    if not key:
        raise RuntimeError("未设置 BARK_KEY")
    server = os.environ.get("BARK_SERVER", "https://api.day.app").rstrip("/")
    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "group": "matcha-net",
        "level": os.environ.get("BARK_LEVEL", "critical"),
        "volume": 10,
        "sound": "alarm",
    }
    if os.environ.get("BARK_CALL", "1") == "1":
        payload["call"] = "1"
    if url:
        payload["url"] = url
    r = http.post(f"{server}/push", json=payload, timeout=30, **IMPERSONATE)
    r.raise_for_status()


def notify(subject, mail_body, push_body, url=None):
    """邮件和推送各自独立发送；只要有一个成功就算通知成功，都失败才抛错（下一轮会重试）。"""
    ok, errors = False, []
    try:
        send_mail(subject, mail_body)
        ok = True
    except Exception as e:
        errors.append(f"邮件失败：{e}")
    if os.environ.get("BARK_KEY"):
        try:
            send_push(subject, push_body, url)
            ok = True
        except Exception as e:
            errors.append(f"推送失败：{e}")
    for e in errors:
        log(e)
    if not ok:
        raise RuntimeError("邮件和推送都发送失败：" + "；".join(errors))


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


def run_once(dry_run=False):
    items, _ = scan()
    watched = [it for it in items if is_watched(it)]
    log(f"检查完成：列表里共 {len(items)} 个有货商品，其中关注的有 {len(watched)} 个")

    if dry_run:
        for it in items:
            star = "★" if is_watched(it) else " "
            print(f"  {star} {it['name']}  [{it['sku']}]  {it['url']}")
        if not items:
            print("  （列表为空：要么确实都没货，要么页面结构和预期不符，可用 --dump 导出排查）")
        return

    now = time.time()
    state = load_state()
    sig = sorted(k.strip().lower() for k in WATCH_KEYWORDS + WATCH_SKU_PREFIX)
    if state.get("watch") != sig:       # 关注条件变了就重置
        state = {}
    prev_pids = set(state.get("pids", []))
    last_sent = state.get("last_notified", 0)
    miss = state.get("miss", 0)
    was_above = bool(state.get("above"))

    cur_pids = {it["pid"] for it in watched}
    if watched:
        miss, above, pids = 0, True, cur_pids
    else:
        miss += 1
        if miss >= MISS_TO_CLEAR:
            above, pids = False, set()
        else:                           # 只看不到一次，先保持原状态，防止误判
            above, pids = was_above, prev_pids
            if was_above:
                log(f"本次没看到关注的商品（第 {miss}/{MISS_TO_CLEAR} 次），暂不判定为售完")

    reason = None
    if watched:
        if not was_above:
            reason = "上货提醒"
        elif cur_pids - prev_pids:
            reason = "新增有货"
        elif now - last_sent >= REMIND_EVERY:
            reason = "仍有货"

    sent = False
    if reason:
        names = "、".join(it["name"] for it in watched)
        short = "、".join(re.sub(r"[（(].*", "", it["name"]).strip() for it in watched[:3])
        lines = [f"- {it['name']}（{it['sku']}）\n  {it['url']}" for it in watched]
        body = (f"关注的商品当前有货（{reason}）：\n\n" + "\n".join(lines)
                + f"\n\n列表页：{LIST_URL}")
        notify(f"🍵 {reason}：{short}", body, f"有货：{names}（点开直达商品页）", watched[0]["url"])
        log(f"已发送通知（{reason}）")
        sent = True
    elif watched:
        log(f"仍有货，距上次通知不足 {REMIND_EVERY} 秒，暂不重复发送")

    save_state({
        "above": above,
        "pids": sorted(pids),
        "miss": miss,
        "last_notified": now if sent else last_sent,
        "watch": sig,
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--test-mail", action="store_true")
    ap.add_argument("--test-push", action="store_true")
    args = ap.parse_args()

    if args.test_mail:
        send_mail("抹茶监测（大陆站）：测试邮件", "邮箱配置正常。")
        log("测试邮件已发送")
        return
    if args.test_push:
        send_push("🍵 抹茶监测：测试推送", "能听到响铃、锁屏能看到这条，就说明推送配置正常。", LIST_URL)
        log("测试推送已发送")
        return
    if args.dump:
        DUMP_FILE.write_text(fetch(LIST_URL), encoding="utf-8")
        log(f"已保存到 {DUMP_FILE}")
        return
    if args.dry_run or args.once:
        run_once(dry_run=args.dry_run)
        return

    log(f"开始常驻监测，每 {INTERVAL} 秒一次，关注：{WATCH_KEYWORDS}")
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"本轮出错：{e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
