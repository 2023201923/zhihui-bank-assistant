import json
import re
import uuid
from config import client, MODEL_NAME
from database import (
    MAX_ANNUAL_LIMIT,
    MAX_DAILY_COUNT_LIMIT,
    MAX_DAILY_LIMIT,
    MAX_SINGLE_LIMIT,
    PENDING_ORDERS,
    list_user_cards,
)

CARD_SYSTEM_PROMPT = """你是银行卡片管家。只能根据系统提供的真实卡片办理操作，禁止编造卡号或额度。
当用户明确要求挂失、冻结、解锁或调整限额时，调用 stage_card_action，card_id 必须来自列表。
限额分四类：单笔限额 new_single_limit、单日累计金额 new_daily_limit、
单日累计笔数 new_daily_count_limit、年度累计金额 new_annual_limit；用户只说「限额」时按单日累计金额处理。"""

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "stage_card_action",
            "description": "生成卡片操作确认单（挂失/冻结/解锁/调整单日限额）",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string", "description": "卡号标识"},
                    "action": {
                        "type": "string",
                        "enum": ["report_lost", "freeze", "unlock", "adjust_limit"],
                    },
                    "new_daily_limit": {
                        "type": "number",
                        "description": "新的单日累计转账限额（金额），其他操作可省略",
                    },
                    "new_single_limit": {
                        "type": "number",
                        "description": "新的单笔转账限额（金额），仅调整单笔限额时填写",
                    },
                    "new_daily_count_limit": {
                        "type": "integer",
                        "description": "新的单日累计转账笔数上限，仅调整笔数时填写",
                    },
                    "new_annual_limit": {
                        "type": "number",
                        "description": "新的年度累计转账限额（金额），仅调整年限额时填写",
                    },
                },
                "required": ["card_id", "action"],
            },
        },
    }
]

ACTION_LABELS = {
    "report_lost": "挂失",
    "freeze": "临时冻结",
    "unlock": "解除冻结",
    "adjust_limit": "调整转账限额",
}

# 各类限额的自助调整上限，与 database 中的校验保持一致
LIMIT_RULES = [
    ("new_single_limit", "单笔限额", MAX_SINGLE_LIMIT, "金额"),
    ("new_daily_limit", "单日累计限额", MAX_DAILY_LIMIT, "金额"),
    ("new_daily_count_limit", "单日累计笔数", MAX_DAILY_COUNT_LIMIT, "笔数"),
    ("new_annual_limit", "年度累计限额", MAX_ANNUAL_LIMIT, "金额"),
]


def _limit_text(card: dict) -> str:
    """把一张卡的四类限额拼成一行摘要。"""
    parts = []
    single = card.get("single_limit")
    daily = card.get("daily_limit")
    count = card.get("daily_count_limit")
    annual = card.get("annual_limit")
    if single is not None:
        parts.append(f"单笔 ¥{float(single):.0f}")
    if daily is not None:
        parts.append(f"单日 ¥{float(daily):.0f}")
    if count is not None:
        parts.append(f"单日 {int(count)} 笔")
    if annual is not None:
        parts.append(f"年度 ¥{float(annual):.0f}")
    return " · ".join(parts) or "未设置转账限额"


def _validate_new_limits(args: dict):
    """校验调额参数，返回 (是否通过, 错误提示, 变更说明)。"""
    changes = []
    for key, label, upper, unit in LIMIT_RULES:
        value = args.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False, f"{label}需要填写数字。", ""
        if number <= 0 or number > upper:
            limit_text = f"{upper:.0f} 元" if unit == "金额" else f"{int(upper)} 笔"
            return False, f"{label}需大于 0 且不超过 {limit_text}。", ""
        if unit == "笔数":
            changes.append(f"{label} {int(number)} 笔")
        else:
            changes.append(f"{label} ¥{number:,.0f}")
    if not changes:
        return False, "请说明要调整成多少，例如「把单日限额调整为 5 万元」。", ""
    return True, "", "、".join(changes)


def _list_card(cards: list, history: list) -> dict:
    items = []
    for card in cards:
        credit = card.get("credit_limit")
        meta = _limit_text(card)
        if credit:
            meta = f"信用额度 ¥{float(credit):.0f} · {meta}"
        items.append(
            {
                "title": card["card_type"],
                "kicker": card["card_id"],
                "value": card["status_label"],
                "meta": meta,
                "badge": "可用于转账/申购" if card.get("is_debit") and card.get("status") == "NORMAL" else "",
            }
        )
    reply = (
        "以下是您名下的银行卡与转账限额。挂失、冻结或调整限额时，请说明卡片类型；"
        "限额可按单笔、单日金额、单日笔数、年度金额四类调整。"
    )
    history.append({"role": "assistant", "content": reply})
    return {
        "type": "INFO_CARD",
        "text": reply,
        "card_data": {
            "card_kind": "card_list",
            "title": "我的银行卡",
            "items": items,
        },
    }


def stage_card_action(user_id: str, args: dict, history: list) -> dict:
    """卡片操作的结构化入口：模型定参数，这里做校验并生成确认单。"""
    cards = list_user_cards(user_id)
    card_id = args.get("card_id") or ""
    action = args.get("action") or ""
    card = next((c for c in cards if c["card_id"] == card_id), None)
    if not card:
        reply = f"未找到卡片 {card_id or '（未指定）'}，请先确认卡号。"
        history.append({"role": "assistant", "content": reply})
        return {"type": "TEXT", "text": reply, "card_data": None}

    new_limit_text = ""
    if action == "adjust_limit":
        passed, error, new_limit_text = _validate_new_limits(args)
        if not passed:
            history.append({"role": "assistant", "content": error})
            return {"type": "TEXT", "text": error, "card_data": None}
        if not card.get("is_debit"):
            reply = f"{card['card_type']} {card_id} 不支持转账，暂不支持调整转账限额。"
            history.append({"role": "assistant", "content": reply})
            return {"type": "TEXT", "text": reply, "card_data": None}

    order_id = str(uuid.uuid4())[:8]
    # 调整转账限额属于资金安全类操作，必须校验交易密码
    require_password = action == "adjust_limit"
    PENDING_ORDERS[order_id] = {
        "kind": "card",
        "order_id": order_id,
        "user_id": user_id,
        "card_id": card_id,
        "action": action,
        "require_password": require_password,
        "new_daily_limit": args.get("new_daily_limit"),
        "new_single_limit": args.get("new_single_limit"),
        "new_daily_count_limit": args.get("new_daily_count_limit"),
        "new_annual_limit": args.get("new_annual_limit"),
        "new_limit_text": new_limit_text,
        "status": "PENDING",
    }
    label = ACTION_LABELS.get(action, action)
    history.append({"role": "assistant", "content": f"已生成卡片操作：{label}"})
    text = f"请确认对 {card['card_type']} {card_id} 执行「{label}」。"
    if new_limit_text:
        text = f"请确认将 {card['card_type']} {card_id} 的「{new_limit_text}」。"
        text = f"{text} 该操作需要输入交易密码。"
    return {
        "type": "ACTION_CARD",
        "text": text,
        "card_data": {
            "card_kind": "card",
            "order_id": order_id,
            "card_id": card_id,
            "card_type": card["card_type"],
            "action": action,
            "action_label": label,
            "requires_password": require_password,
            "new_limit_text": new_limit_text,
            "new_daily_limit": args.get("new_daily_limit"),
            "new_single_limit": args.get("new_single_limit"),
            "new_daily_count_limit": args.get("new_daily_count_limit"),
            "new_annual_limit": args.get("new_annual_limit"),
        },
    }


