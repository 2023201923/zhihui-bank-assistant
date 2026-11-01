"""场景专款：虚拟子账本 + R1 活钱产品 + 按真实当地日期赎回和提醒。

小程序不单独记一套时钟。用钱日、赎回日都以运行服务的这台电脑的当地日期为准，
调度大约每 30 秒看一次，过了日期就会补做，不要求进程正好在零点醒着。
"""

import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta

from database import (
    PENDING_ORDERS,
    activate_scenario,
    attach_payment_choice,
    get_cash_management_product,
    get_user_balance,
    list_active_scenarios,
    run_scenario_schedule,
)

# 国务院放假日期。调休上班的周末这里仍当作非工作日，赎回只会更早，不会更晚。
_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3),
    date(2026, 2, 15), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 21), date(2026, 2, 22),
    date(2026, 2, 23),
    date(2026, 4, 4), date(2026, 4, 5), date(2026, 4, 6),
    date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 4), date(2026, 5, 5),
    date(2026, 6, 19), date(2026, 6, 20), date(2026, 6, 21),
    date(2026, 9, 25), date(2026, 9, 26), date(2026, 9, 27),
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4),
    date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),
    date(2027, 1, 1), date(2027, 1, 2), date(2027, 1, 3),
    date(2027, 10, 1), date(2027, 10, 2), date(2027, 10, 3), date(2027, 10, 4),
    date(2027, 10, 5), date(2027, 10, 6), date(2027, 10, 7),
}

_REDEEM_PAD_DAYS = 3
_scheduler_started = False


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _HOLIDAYS


def previous_business_day(day: date) -> date:
    cursor = day
    while not is_business_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def next_business_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_business_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def add_business_days(day: date, count: int) -> date:
    step = 1 if count > 0 else -1
    left = abs(count)
    cursor = day
    while left:
        cursor += timedelta(days=step)
        if is_business_day(cursor):
            left -= 1
    return cursor


def business_days_after(last: date, today: date):
    """(交易日, 距上一个计息点的自然日数)。周末和假日并进下一个交易日。"""
    found = []
    previous = last
    cursor = last + timedelta(days=1)
    while cursor <= today:
        if is_business_day(cursor):
            found.append((cursor, (cursor - previous).days))
            previous = cursor
        cursor += timedelta(days=1)
    return found


def _parse_explicit_date(text: str, today: date):
    raw = (text or "").strip()
    if not raw:
        return None
    matched = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", raw)
    if matched:
        return date(int(matched.group(1)), int(matched.group(2)), int(matched.group(3)))
    # 「下个月5号」「下月5号」「这个月20号」：原有规则只认「11月5号」，
    # 漏掉了最常见的「下个月N号」说法，导致心愿备用金无法建单。
    matched = re.search(r"(下个?月|本月|这个月)\s*(\d{1,2})\s*[号日]", raw)
    if matched:
        word, day_num = matched.group(1), int(matched.group(2))
        if word.startswith("下"):
            year = today.year + (1 if today.month == 12 else 0)
            month = 1 if today.month == 12 else today.month + 1
        else:
            year, month = today.year, today.month
        try:
            return date(year, month, day_num)
        except ValueError:
            return None
    matched = re.search(r"(\d{1,2})月(\d{1,2})日?", raw)
    if matched:
        month, day_num = int(matched.group(1)), int(matched.group(2))
        guessed = date(today.year, month, day_num)
        if guessed < today:
            guessed = date(today.year + 1, month, day_num)
        return guessed
    if "大后天" in raw:
        return today + timedelta(days=3)
    if "后天" in raw:
        return today + timedelta(days=2)
    if "明天" in raw:
        return today + timedelta(days=1)
    if "今天" in raw:
        return today
    weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    matched = re.search(r"(下下周|下周|周|星期|礼拜)([一二三四五六日天])", raw)
    if matched:
        target = weekday_map[matched.group(2)]
        ahead = (target - today.weekday()) % 7
        if matched.group(1) == "下周":
            # 本周这个星期还没到，才再加一周；已经落在下一周的不要再加 7 天。
            if ahead == 0 or target > today.weekday():
                ahead += 7
        elif matched.group(1) == "下下周":
            ahead += 14
        elif ahead == 0:
            ahead = 7
        return today + timedelta(days=ahead)
    if "下周" in raw:
        ahead = (0 - today.weekday()) % 7
        return today + timedelta(days=ahead or 7)
    return None


def date_stated_by_customer(texts, today: date = None):
    """只承认客户自己说过的用钱日。模型补的日期不算。"""
    today = today or date.today()
    for text in reversed(list(texts or [])):
        found = _parse_explicit_date(str(text or ""), today)
        if found:
            return found
    return None


def plan_schedule(raw_date: str, now: datetime = None):
    """根据客户说过的用钱日算出赎回日。没有未来日期时不编默认值。"""
    now = now or datetime.now()
    today = now.date()
    parsed = _parse_explicit_date(raw_date, today)
    if parsed is None:
        return None
    if parsed <= today:
        return {"error": "past", "parsed": parsed.isoformat()}
    target = parsed
    ready_by = target if is_business_day(target) else previous_business_day(target)
    redeem_on = add_business_days(ready_by, -_REDEEM_PAD_DAYS)
    if redeem_on <= today:
        # 2026-10-31：提前量不够时，原来的做法是把赎回日压到今天，
        # 结果客户刚确认完几十秒，调度线程就把钱赎回活期——看起来像"根本没存"。
        # 现在改为顺延到「用钱日」或「下一个交易日」，保证钱至少留到接近用钱日。
        redeem_on = max(ready_by, next_business_day(today))
    if redeem_on > ready_by:
        # 用钱日太近，赎回日反而落到用钱日之后：钱赶不上，不能生成方案。
        return {
            "error": "too_soon",
            "parsed": target.isoformat(),
            "redeem_on": redeem_on.isoformat(),
        }
    # 15:00 前申购，下一交易日开始计提；15:00 及以后再顺延一个交易日。
    value_on = today if now.time() < datetime.strptime("15:00", "%H:%M").time() else next_business_day(today)
    if not is_business_day(value_on):
        value_on = next_business_day(value_on - timedelta(days=1))
    return {
        "target_date": target.isoformat(),
        "auto_redeem_date": redeem_on.isoformat(),
        "value_date": value_on.isoformat(),
    }


def _cash_product() -> dict:
    product = get_cash_management_product()
    if not product:
        raise RuntimeError("在售产品里没有 R1 现金管理产品，专款计划不能建立")
    return product


def stage_cross_plan(user_id: str, args: dict, history: list, customer_texts=None) -> dict:
    from agents.cross_agent import parse_amount

    purpose = str(args.get("purpose") or args.get("notes") or "").strip()
    beneficiary = str(args.get("beneficiary") or "").strip()
    amount = parse_amount(args.get("amount"))
    today = date.today()
    stated = date_stated_by_customer(customer_texts, today)
    missing = []
    if not purpose:
        missing.append("用途")
    if amount <= 0:
        missing.append("预留金额")
    if stated is None:
        missing.append("用钱日")
    if missing:
        return {
            "type": "TEXT",
            "text": "",
            "card_data": None,
            "needs_more": {
                "缺少信息": missing,
                "已有信息": {
                    "用途": purpose or None,
                    "金额": amount or None,
                    "用钱日": stated.isoformat() if stated else None,
                },
                "下一步": "先向客户问清缺少的信息。用钱日必须等客户自己说，不要填默认日期，不要生成确认单",
            },
        }

    schedule = plan_schedule(stated.isoformat())
    if schedule.get("error") == "too_soon":
        return {
            "type": "TEXT",
            "text": "",
            "card_data": None,
            "needs_more": {
                "缺少信息": ["更晚的用钱日"],
                "已有信息": {
                    "客户提到的日期": schedule["parsed"],
                    "用途": purpose,
                    "金额": amount,
                },
                "下一步": "如实告诉客户：这个用钱日太近，资金来不及在用钱日前回到可用余额，"
                "请换一个更晚的日期。不要自动改成别的日期，也不要生成确认单",
            },
        }
    if schedule.get("error") == "past":
        return {
            "type": "TEXT",
            "text": "",
            "card_data": None,
            "needs_more": {
                "缺少信息": ["晚于今天的用钱日"],
                "已有信息": {"客户提到的日期": schedule["parsed"], "用途": purpose, "金额": amount},
                "下一步": "告诉客户这个日期不是今天之后，请对方重新确认用钱日。不要改成别的默认日期",
            },
        }

    available = round(get_user_balance(user_id), 2)
    if available < amount:
        return {
            "type": "TEXT",
            "text": "",
            "card_data": None,
            "blocked": {
                "余额不足": True,
                "可用余额": available,
                "预留金额": amount,
                "下一步": "告诉客户可用余额不够，请减少预留金额或先补足活期，不要生成确认单",
            },
        }

    product = _cash_product()
    scenario_id = f"SC_{date.today().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6].upper()}"
    order_id = uuid.uuid4().hex[:8]
    annual = float(product["expected_yield"])
    plan = {
        "scenario_id": scenario_id,
        "scenario_type": "GIFT",
        "notes": purpose,
        "beneficiary": beneficiary or "本人",
        "amount": amount,
        "annual_yield": annual,
        "product_code": product.get("product_code") or product["product_id"],
        "product_name": product["name"],
        **schedule,
    }
    PENDING_ORDERS[order_id] = {
        "kind": "cross_lock",
        "order_id": order_id,
        "user_id": user_id,
        "lock_amount": amount,
        "plan": plan,
        "status": "PENDING",
    }
    automation = (
        f"{schedule['auto_redeem_date']} 自动赎回至可用余额，并在小程序里提醒。"
        "参考收益不承诺、不保本。"
    )
    history.append({"role": "assistant", "content": f"已生成心愿备用金确认单：{purpose} ¥{amount:g}"})
    card_data = {
        "card_kind": "cross_lock",
        "card_title": "心愿备用金确认单",
        "order_id": order_id,
        "scenario_id": scenario_id,
        "beneficiary": beneficiary or "本人",
        "amount": amount,
        "notes": purpose,
        "product_name": product["name"],
        "yield": f"{product['yield_pct']}（{product['yield_label']}，不承诺）",
        "target_date": schedule["target_date"],
        "auto_redeem_date": schedule["auto_redeem_date"],
        "automation": automation,
    }
    attach_payment_choice(user_id, PENDING_ORDERS[order_id], card_data)
    text = (
        f"请确认把 ¥{amount:.2f} 存为心愿备用金，"
        f"底层为{product['name']}（R1 现金管理，参考{product['yield_label']} {product['yield_pct']}，不承诺收益）。"
        f"计划 {schedule['auto_redeem_date']} 赎回回可用余额，用钱日 {schedule['target_date']}。"
    )
    return {
        "type": "ACTION_CARD",
        "text": text,
        "card_data": card_data,
        "data": {
            "用途": purpose,
            "预留金额": amount,
            "受益人": beneficiary or "本人",
            "用钱日": schedule["target_date"],
            "赎回日": schedule["auto_redeem_date"],
            "底层产品": product["name"],
            "参考年化": product["yield_pct"],
            "下一步": "等客户在确认卡上输入密码；确认后才会划款并写入数据库",
        },
    }


def stage_release(user_id: str, args: dict, history: list) -> dict:
    scenario_id = str(args.get("scenario_id") or "").strip()
    keyword = str(args.get("notes") or args.get("purpose") or "").strip()
    active = list_active_scenarios(user_id)
    target = None
    raw_index = args.get("index")
    if raw_index not in (None, ""):
        try:
            position = int(raw_index) - 1
        except (TypeError, ValueError):
            position = -1
        if 0 <= position < len(active):
            target = active[position]
    elif scenario_id:
        target = next((row for row in active if row["id"] == scenario_id), None)
    elif keyword:
        target = next((row for row in active if keyword in row["notes"] or keyword in row["beneficiary"]), None)
    elif len(active) == 1:
        target = active[0]
    if not target:
        return {
            "type": "TEXT",
            "text": "",
            "card_data": None,
            "needs_more": {
                "在途专款": [
                    {
                        "scenario_id": row["id"],
                        "用途": row["notes"],
                        "本金": row["current_principal"],
                        "用钱日": row["target_date"],
                    }
                    for row in active
                ],
                "下一步": "在途专款不止一笔或没有匹配时，先问客户要解冻哪一笔，不要自己挑",
            },
        }
    order_id = uuid.uuid4().hex[:8]
    payout_hint = round(float(target["current_principal"]) + float(target["accumulated_yield"]), 2)
    PENDING_ORDERS[order_id] = {
        "kind": "cross_release",
        "order_id": order_id,
        "user_id": user_id,
        "scenario_id": target["id"],
        "require_password": True,
        "status": "PENDING",
    }
    history.append({"role": "assistant", "content": f"已生成取回心愿备用金确认单：{target['notes']}"})
    return {
        "type": "ACTION_CARD",
        "text": (
            f"取回后，「{target['notes']}」的本金和已计提参考收益会回到活期可用余额，"
            "用钱日当天不再提醒。请在卡片上确认。"
        ),
        "card_data": {
            "card_kind": "cross_release",
            "card_title": "取回心愿备用金",
            "order_id": order_id,
            "scenario_id": target["id"],
            "requires_password": True,
            "beneficiary": target["beneficiary"],
            "amount": payout_hint,
            "notes": target["notes"],
            "product_name": target["product_name"],
            "target_date": target["target_date"],
            "automation": "确认后立即赎回至可用余额，参考收益不承诺",
        },
        "data": {
            "scenario_id": target["id"],
            "用途": target["notes"],
            "当前本息约": payout_hint,
            "下一步": "等客户在确认卡上输入密码",
        },
    }


def run_due_scenarios():
    return run_scenario_schedule(business_days_after, date.today())


def start_scenario_worker():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        while True:
            try:
                run_due_scenarios()
            except Exception as exc:
                print(f"[scenario] 调度失败: {exc}")
            time.sleep(30)

    threading.Thread(target=_loop, name="scenario-schedule", daemon=True).start()
