import re
from datetime import date, timedelta

from database import get_category_spending, get_recent_transactions, find_abnormal_transactions

_MONTH_WORDS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}
_MONTH_TOKEN = r"(1[0-2]|0?[1-9]|十[一二]|[一二三四五六七八九]|十)"


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _month_number(token: str):
    if token.isdigit():
        month = int(token)
        return month if 1 <= month <= 12 else None
    return _MONTH_WORDS.get(token)


def _parse_period(text: str, today: date):
    """从客户原话里取出明确的年月。没点名时间就返回空。"""
    named = re.search(rf"(\d{{4}})年\s*{_MONTH_TOKEN}月", text)
    if named:
        return int(named.group(1)), _month_number(named.group(2))
    if re.search(r"上个?月", text):
        previous = date(today.year, today.month, 1) - timedelta(days=1)
        return previous.year, previous.month
    if re.search(r"这个月|本月", text):
        return today.year, today.month
    found = re.search(_MONTH_TOKEN + r"月", text)
    if not found:
        return None, None
    month = _month_number(found.group(1))
    if not month:
        return None, None
    if "去年" in text:
        return today.year - 1, month
    if "今年" in text:
        return today.year, month
    return None, month


def resolve_bill_window(year=None, month=None, message: str = "", today: date = None) -> dict:
    """客户原话里的年月优先。没写年份就按今天推算，不采用模型自己猜的年份。"""
    today = today or date.today()
    stated_year, stated_month = _parse_period(message or "", today)
    model_year = _as_int(year)
    model_month = _as_int(month)
    if stated_month and 1 <= stated_month <= 12:
        month = stated_month
        if stated_year:
            year = stated_year
        else:
            year = today.year if stated_month <= today.month else today.year - 1
    else:
        month = model_month if model_month and 1 <= model_month <= 12 else None
        year = model_year
        if month and not year:
            year = today.year if month <= today.month else today.year - 1
    if month and 1 <= month <= 12 and year and 2000 <= year <= 2100:
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            return {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "label": f"{year}年{month}月",
                "named_month": True,
            }
    start = today - timedelta(days=30)
    end = today + timedelta(days=1)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "label": "近 30 日",
        "named_month": False,
    }


def run_bill_agent(user_id: str, message: str, history: list, year=None, month=None) -> dict:
    """账单分析。没指定时间时看近 30 天；指定了年月就只统计那一个月。"""
    history.append({"role": "user", "content": message})
    window = resolve_bill_window(year, month, message)
    start, end, label = window["start"], window["end"], window["label"]

    categories = get_category_spending(user_id, start=start, end=end)
    total = sum(c["amount"] for c in categories)
    denom = total if total > 0 else 1.0
    sections = [
        {
            "label": row["category"],
            "value": f"¥{row['amount']:.2f}",
            "percent": round(row["amount"] / denom * 100, 1),
        }
        for row in categories
    ]

    abnormal = find_abnormal_transactions(user_id, start=start, end=end)
    highlights = []
    for item in abnormal[:5]:
        highlights.append(
            f"{item['created_at'][:10]} {item['category']} ¥{item['amount']:.2f} "
            f"({item['counterparty']}) — {item['reason']}"
        )
    if not highlights:
        highlights.append(f"{label}未发现明显异常大额或偏离习惯的消费。")

    recent = get_recent_transactions(user_id, limit=5, start=start, end=end)
    recent_lines = [
        f"{t['created_at'][:10]} {t['category']} -¥{t['amount']:.2f} {t['counterparty']}"
        for t in recent
    ]
    if not recent_lines:
        recent_lines.append(f"{label}没有支出流水。")

    summary = (
        f"{label}支出合计 ¥{total:.2f}，共 {len(categories)} 个消费分类。"
        " 详情见下方分析卡片。"
    )
    history.append({"role": "assistant", "content": summary})

    return {
        "type": "INFO_CARD",
        "text": summary,
        "card_data": {
            "card_kind": "bill_summary",
            "title": f"账单与消费分析（{label}）",
            "sections": sections,
            "highlights": highlights,
            "extra_rows": recent_lines,
            "extra_title": f"{label}流水" if window["named_month"] else "最近 5 笔流水",
        },
    }
