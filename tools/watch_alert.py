#!/usr/bin/env python3
"""价格线监控 — 比对 data/alert_rules.json 的规则，触发才推 Slack。零外部依赖（仅 stdlib）。

设计原则（与 ai-berkshire 投研纪律一致）：
- 只做**机械比对**：价格线来自 data/alert_rules.json（源自 reports/ 下的自有研究报告估值锚点），
  脚本不产生投资建议。
- **无事不打扰**：只有规则从"未触发"变为"触发"时才推送；同一条规则不会天天刷屏。
- 触发 ≠ 卖出：sell_review 是强制重审，sell_absurd 是重答"还愿意按这个价买下整个公司吗"。
- **沉默必须可信**：取数失败会重试 3 次；若仍有 ≥50% 规则查不成，则判定"检查不能"并推送告警——
  因为此时的"无触发"不代表没异常，只代表没查成。健康度同样只在跳变时通知，不会天天刷屏。

用法：
    python3 tools/watch_alert.py                # 检查并推送（cron 用这个）
    python3 tools/watch_alert.py --dry-run      # 只打印，不推送、不写状态
    python3 tools/watch_alert.py --always       # 无论是否触发都推一份现价摘要
    python3 tools/watch_alert.py --test         # 发一条测试消息，验证 webhook 通道

Webhook 来源（按优先级）：环境变量 SLACK_WEBHOOK_URL → local/slack_webhook.txt（已 gitignore）。
需要 Python >= 3.8。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

_TIMEOUT = 15
_RETRY_ATTEMPTS = 3          # 取数失败重试次数（网络瞬断、行情源抖动）
_RETRY_DELAY = 2.0           # 重试间隔基数（秒），按次数线性退避
_DEGRADED_RATIO = 0.5        # 失败比例达到此值即判定"检查不能"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RULES = os.path.join(_ROOT, "data", "alert_rules.json")
_WEBHOOK_FILE = os.path.join(_ROOT, "local", "slack_webhook.txt")
_STATE = os.path.join(_ROOT, "local", "alert_state.json")

_TYPE_LABEL = {
    "sell_absurd": ("🔴", "估值离谱线"),
    "sell_review": ("🟡", "触发重审"),
    "buy_zone": ("🟢", "进入30%安全边际线"),
    "scenario_low": ("🔵", "触及悲观情景价"),
}


def _force_utf8_stdio():
    """Windows/GBK 控制台下保证中文与符号不炸（与 financial_rigor.py 同约定）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _curl(url):
    """curl --noproxy 直连，绕过系统代理。腾讯行情返回 GBK。"""
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         url],
        capture_output=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"请求失败: {url}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("gbk", errors="replace")


def fetch_quote(code: str) -> dict:
    """取腾讯行情。A股/港股/美股共用同一 ~ 分隔协议，字段位相同。

    字段位：1=名称 2=代码 3=现价 4=昨收 30=时间 31=涨跌额 32=涨跌幅%
    """
    raw = _curl(f"https://qt.gtimg.cn/q={code}")
    start, end = raw.find('"'), raw.rfind('"')
    if start < 0 or end <= start:
        raise ValueError(f"行情返回无法解析: {code}")
    f = raw[start + 1:end].split("~")
    if len(f) < 33 or not f[3].strip():
        raise ValueError(f"行情字段不足或无价格（代码是否有效？）: {code}")
    return {
        "name": f[1],
        "code": f[2],
        "price": float(f[3]),
        "prev_close": float(f[4]) if f[4].strip() else None,
        "time": f[30],
        "change_pct": f[32],
    }


def fetch_quote_retry(code: str, attempts: int = _RETRY_ATTEMPTS) -> dict:
    """带重试的取数。网络瞬断/行情源抖动是最常见的失败原因，重试即可恢复。

    只有 attempts 次全部失败才向上抛，由调用方计入 errors。
    """
    last = None
    for i in range(attempts):
        try:
            return fetch_quote(code)
        except Exception as exc:                          # noqa: BLE001 — 交由重试与上层处理
            last = exc
            if i < attempts - 1:
                time.sleep(_RETRY_DELAY * (i + 1))
    raise last


def evaluate(rule: dict, quote: dict) -> bool:
    """判断规则是否触发。condition: above / below。"""
    price, line = quote["price"], float(rule["price"])
    cond = rule.get("condition", "above")
    if cond == "above":
        return price >= line
    if cond == "below":
        return price <= line
    raise ValueError(f"未知 condition: {cond}")


def rule_key(rule: dict) -> str:
    return f"{rule['code']}|{rule.get('type','')}|{rule['price']}|{rule.get('condition','above')}"


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
        print("⚠️  未配置 Slack webhook（SLACK_WEBHOOK_URL 或 local/slack_webhook.txt），跳过推送")
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


def check(dry_run=False, always=False) -> int:
    cfg = load_json(_RULES, {})
    rules = cfg.get("rules", [])
    if not rules:
        print(f"⚠️  {_RULES} 中没有规则")
        return 0

    state = load_json(_STATE, {})
    quotes, fired, lines, errors = {}, [], [], []

    for rule in rules:
        code = rule["code"]
        if code not in quotes:
            try:
                quotes[code] = fetch_quote_retry(code)
            except Exception as exc:                      # noqa: BLE001 — 单只失败不应中断整轮
                errors.append(f"{rule['name']}({code}): {exc}")
                continue
        quote = quotes[code]
        try:
            triggered = evaluate(rule, quote)
        except ValueError as exc:
            errors.append(f"{rule['name']}: {exc}")
            continue

        key = rule_key(rule)
        was = state.get(key, {}).get("triggered", False)
        icon, label = _TYPE_LABEL.get(rule.get("type", ""), ("•", rule.get("type", "")))
        cur = rule.get("currency", "")
        arrow = "≥" if rule.get("condition", "above") == "above" else "≤"
        summary = (f"{icon} *{rule['name']}* {label}｜现价 {cur} {quote['price']:g}"
                   f"（{quote['change_pct']}%）{arrow} 线 {cur} {rule['price']:g}")

        if triggered and not was:                          # 只在"未触发 → 触发"跳变时报警
            fired.append(f"{summary}\n    ▸ {rule.get('note','')}\n    ▸ 依据：{rule.get('ref','')}")
        lines.append(f"{'✓' if triggered else '·'} {rule['name']} {cur}{quote['price']:g} "
                     f"（{quote['change_pct']}%） {arrow} {rule['price']:g} [{label}]")

        state[key] = {"triggered": triggered, "last_price": quote["price"],
                      "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for line in lines:
        print(line)
    for err in errors:
        print(f"❌ {err}")

    # ---- 健康度判定：区分"检查后无异常"与"根本没检查成" ----
    total = len(rules)
    evaluated = len(lines)
    failed = total - evaluated
    degraded = total > 0 and (evaluated == 0 or failed / total >= _DEGRADED_RATIO)

    health = state.get("_health", {})
    was_degraded = health.get("degraded", False)

    msg = None
    if fired:
        msg = (f"*价格线触发提醒* · {stamp}\n\n" + "\n\n".join(fired) +
               "\n\n_触发 ≠ 卖出。请回 repo 重审论文后再决定。_")
    elif always:
        msg = f"*每日价格摘要* · {stamp}\n```\n" + "\n".join(lines) + "\n```"
    if errors and msg:
        msg += "\n\n⚠️ 取数失败：\n" + "\n".join(f"· {e}" for e in errors)

    # 健康度只在跳变时通知，避免长时间断网天天刷屏（与"无事不打扰"一致）
    health_msg = None
    if degraded and not was_degraded:
        health_msg = (f"⚠️ *价格监控：检查不能* · {stamp}\n"
                      f"{total} 条规则中 {failed} 条取数失败（已重试 {_RETRY_ATTEMPTS} 次）。\n"
                      f"*本轮的「无触发」不可信——不是没异常，是没查成。*\n\n"
                      + "\n".join(f"· {e}" for e in errors[:10])
                      + ("\n· …" if len(errors) > 10 else "")
                      + "\n\n_恢复后会再推一条通知。_")
    elif was_degraded and not degraded:
        health_msg = (f"✅ *价格监控：已恢复* · {stamp}\n"
                      f"{total} 条规则全部取数成功，监控恢复正常。")

    if degraded != was_degraded:
        health = {"degraded": degraded,
                  "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "failed": failed, "total": total}
    else:
        health.setdefault("degraded", degraded)
        health["failed"], health["total"] = failed, total
    state["_health"] = health

    for out in (health_msg, msg):
        if not out:
            continue
        if dry_run:
            print(f"\n--- dry-run 预览 ---\n{out}")
        elif post_slack(out):
            print("✅ 已推送 Slack")

    if not (msg or health_msg):
        if degraded:
            print(f"\n⚠️ 检查不能（{stamp}）— {failed}/{total} 条取数失败，且已通知过，不重复打扰")
        else:
            print(f"\n无触发（{stamp}）— 不打扰")

    if not dry_run:
        os.makedirs(os.path.dirname(_STATE), exist_ok=True)
        with open(_STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)

    return 1 if degraded else 0


def main():
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="价格线监控 → Slack 提醒")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送、不写状态")
    parser.add_argument("--always", action="store_true", help="无触发也推送现价摘要")
    parser.add_argument("--test", action="store_true", help="发一条测试消息验证 webhook")
    args = parser.parse_args()

    if args.test:
        ok = post_slack(f"✅ ai-berkshire 价格监控通道测试 · "
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                        f"_收到这条说明 webhook 正常，后续只在规则触发时才会打扰你。_")
        # 成功时也要出声：静默成功会让人误以为脚本没跑
        print("✅ 测试消息已发送，请到 Slack 确认是否收到" if ok else "❌ 测试消息发送失败")
        return 0 if ok else 1
    return check(dry_run=args.dry_run, always=args.always)


if __name__ == "__main__":
    sys.exit(main())
