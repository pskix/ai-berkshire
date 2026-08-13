#!/usr/bin/env python3
"""持仓硬事件监控 — 扫描新闻，命中卖出条款关键词才推 Slack。零外部依赖（仅 stdlib）。

设计原则（与 watch_alert.py 同一套纪律）：
- **只做机械匹配，不做判断**：脚本把「命中的新闻」与「对应的卖出条款原文」摆在一起，
  由人回 实盘记录/卖出条件.md 自行核对。脚本不说"该卖"或"该留"。
- **不用 LLM**：判断权既然在人，就没有让模型总结的必要——顺带消除幻觉风险与 token 成本。
- **无事不打扰**：同一篇文章只推一次（按文章 ID 去重）。
- **命中 ≠ 触发**：关键词法必然有误报，命中只代表"值得你扫一眼"。

分层监控中的第一层：只盯新闻可检测的硬事件（离任、立案、诉讼判决、并购、稀释融资）。
财务类条款（如"利润连续两季下滑"）季度才更新，不在日频扫描，留给财报后的全量复核。

用法：
    python3 tools/news_watch.py                 # 扫描并推送（cron 用这个）
    python3 tools/news_watch.py --dry-run       # 只打印，不推送、不写状态
    python3 tools/news_watch.py --days 3        # 只看最近 N 天（默认 2）
    python3 tools/news_watch.py --all           # 忽略去重，重扫全部（排查用）

Webhook 来源与 watch_alert.py 一致：SLACK_WEBHOOK_URL → local/slack_webhook.txt
需要 Python >= 3.8。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

_TIMEOUT = 20
_RETRY_ATTEMPTS = 3
_RETRY_DELAY = 2.0
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RULES = os.path.join(_ROOT, "data", "news_watch_rules.json")
_WEBHOOK_FILE = os.path.join(_ROOT, "local", "slack_webhook.txt")
_STATE = os.path.join(_ROOT, "local", "news_state.json")
_MAX_PUSH = 12          # 单次推送条数上限，超出只报数量，避免刷屏

_SEARCH = ("https://search-api-web.eastmoney.com/search/jsonp?cb=x&param="
           "%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22{kw}%22%2C%22type%22%3A%5B%22cmsArticleWebOld%22"
           "%5D%2C%22client%22%3A%22web%22%2C%22clientType%22%3A%22web%22%2C%22clientVersion%22%3A%22curr%22"
           "%2C%22param%22%3A%7B%22cmsArticleWebOld%22%3A%7B%22searchScope%22%3A%22default%22%2C%22sort%22"
           "%3A%22time%22%2C%22pageIndex%22%3A1%2C%22pageSize%22%3A{n}%7D%7D%7D")


def _force_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _curl(url):
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*", "--max-time", str(_TIMEOUT),
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         "-H", "Referer: https://so.eastmoney.com/", url],
        capture_output=True, timeout=_TIMEOUT + 10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"请求失败: {url[:80]}")
    return result.stdout.decode("utf-8", errors="replace")


def search_news(keyword: str, size: int = 20) -> list:
    """东方财富资讯搜索。返回 [{date,title,content,url,media}]。失败重试后抛错。"""
    url = _SEARCH.format(kw=quote(keyword), n=size)
    last = None
    for i in range(_RETRY_ATTEMPTS):
        try:
            raw = _curl(url)
            start, end = raw.find("("), raw.rfind(")")
            if start < 0 or end <= start:
                raise ValueError("jsonp 无法解析")
            data = json.loads(raw[start + 1:end])
            items = (data.get("result") or {}).get("cmsArticleWebOld") or []
            return [{"date": it.get("date", ""), "title": _clean(it.get("title", "")),
                     "content": _clean(it.get("content", "")), "url": it.get("url", ""),
                     "media": it.get("mediaName", ""), "id": it.get("code", "")} for it in items]
        except Exception as exc:                              # noqa: BLE001
            last = exc
            if i < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY * (i + 1))
    raise last


def _clean(s: str) -> str:
    """去掉搜索结果里的 <em> 高亮标签。"""
    return s.replace("<em>", "").replace("</em>", "").strip()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def webhook_url():
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if url:
        return url
    if os.path.exists(_WEBHOOK_FILE):
        with open(_WEBHOOK_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


def post_slack(text: str) -> bool:
    url = webhook_url()
    if not url:
        print("⚠️  未配置 Slack webhook，跳过推送")
        return False
    payload = json.dumps({"text": text}, ensure_ascii=False)
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*", "-X", "POST",
         "-H", "Content-Type: application/json; charset=utf-8",
         "--data-binary", "@-", url],
        input=payload.encode("utf-8"), capture_output=True, timeout=_TIMEOUT,
    )
    body = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 or body != "ok":
        print(f"❌ Slack 推送失败: rc={result.returncode} body={body!r}")
        return False
    return True


def within_days(date_str: str, days: int) -> bool:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt) >= datetime.now() - timedelta(days=days)
        except ValueError:
            continue
    return True          # 日期解析不了就保留，宁可多看一条


def match_clauses(item: dict, watch: dict) -> list:
    """匹配规则（实测降噪后）：
    1. **别名必须出现在标题里** —— 真正关于该公司的新闻几乎都会在标题点名；
       只在正文被顺带提及的，绝大多数是噪音（实测：一篇寒武纪的报道因正文提到
       MiniMax 而命中"开源 模型"）。
    2. 关键词在标题或正文命中即可；空格分隔的关键词表示"必须同时出现"。
    """
    if not any(a in item["title"] for a in watch["aliases"]):
        return []
    blob = f"{item['title']} {item['content']}"
    hits = []
    for clause in watch["clauses"]:
        kws = [k for k in clause["keywords"] if all(part in blob for part in k.split())]
        if kws:
            hits.append({"type": clause["type"], "text": clause["text"], "kw": kws})
    return hits


def scan(dry_run=False, days=2, ignore_state=False) -> int:
    cfg = load_json(_RULES, {})
    watches = cfg.get("watch", [])
    if not watches:
        print(f"⚠️  {_RULES} 中没有监控项")
        return 0

    state = load_json(_STATE, {})
    seen = set(state.get("seen_ids", []))
    found, errors = [], []

    for w in watches:
        try:
            items = search_news(w["aliases"][0])
        except Exception as exc:                              # noqa: BLE001
            errors.append(f"{w['name']}: {exc}")
            continue
        for it in items:
            if not within_days(it["date"], days):
                continue
            uid = it["id"] or it["url"]
            if not ignore_state and uid in seen:
                continue
            hits = match_clauses(it, w)
            if not hits:
                continue
            found.append({"name": w["name"], "item": it, "hits": hits})
            seen.add(uid)
        time.sleep(0.3)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for f in found:
        print(f"\n🔎 {f['name']}｜{f['item']['date']}｜{f['item']['media']}")
        print(f"   {f['item']['title']}")
        for h in f["hits"]:
            print(f"   ▸ [{h['type']}] 命中 {h['kw']}")
            print(f"     条款：{h['text']}")
        print(f"   {f['item']['url']}")
    for e in errors:
        print(f"❌ {e}")

    msg = None
    if found:
        blocks = []
        for f in found[:_MAX_PUSH]:
            lines = [f"*{f['name']}*｜{f['item']['date']}｜{f['item']['media']}",
                     f"<{f['item']['url']}|{f['item']['title']}>"]
            for h in f["hits"]:
                lines.append(f"    ▸ *[{h['type']}]* 命中关键词 `{'`, `'.join(h['kw'])}`")
                lines.append(f"    ▸ 条款原文：{h['text']}")
            blocks.append("\n".join(lines))
        more = f"\n\n_（另有 {len(found) - _MAX_PUSH} 条未列出）_" if len(found) > _MAX_PUSH else ""
        msg = (f"*持仓硬事件候选* · {stamp}\n"
               f"_以下为关键词机械匹配的候选新闻，**命中 ≠ 触发**。_\n"
               f"_请自行核对 `实盘记录/卖出条件.md` 后判断；脚本不给结论。_\n\n"
               + "\n\n".join(blocks) + more)
    if errors and msg:
        msg += "\n\n⚠️ 取数失败：\n" + "\n".join(f"· {e}" for e in errors)

    if msg and not dry_run:
        post_slack(msg) and print("\n✅ 已推送 Slack")
    elif msg:
        print(f"\n--- dry-run 预览 ---\n{msg}")
    else:
        print(f"\n无命中（{stamp}）— 不打扰")

    if not dry_run:
        os.makedirs(os.path.dirname(_STATE), exist_ok=True)
        state["seen_ids"] = sorted(seen)[-3000:]      # 只留最近 3000 条，防止无限增长
        state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(_STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)

    return 1 if errors and not found else 0


def main():
    _force_utf8_stdio()
    p = argparse.ArgumentParser(description="持仓硬事件监控 → Slack（只提示，不判断）")
    p.add_argument("--dry-run", action="store_true", help="只打印，不推送、不写状态")
    p.add_argument("--days", type=int, default=2, help="只看最近 N 天（默认 2）")
    p.add_argument("--all", action="store_true", help="忽略去重，重扫全部（排查用）")
    a = p.parse_args()
    return scan(dry_run=a.dry_run, days=a.days, ignore_state=a.all)


if __name__ == "__main__":
    sys.exit(main())
