"""转账业务的确定性实现层。

2026-10-15：原先把「用户说的话」用正则解析成收款人和金额，并按关键词决定分支，
现已删除。收款人、金额、附言由模型理解后作为参数传入（tools.py 的 stage_transfer），
本模块只负责卡片状态校验、四维限额、反洗钱风控与亲情守护预警。
"""

import json
import re
import uuid
from typing import Optional, Tuple

from config import client, MODEL_NAME
from database import (
    PENDING_ORDERS,
    assess_transfer_risk,
    attach_payment_choice,
    check_transfer_limits,
    lookup_transfer_card,
    notify_guardian,
    GUARDIAN_ALERT_LEVELS,
)

def _build_transfer_card(user_id: str, payee: str, amount: float, notes: str, history: list) -> dict:
    found = lookup_transfer_card(user_id, payee) or {}
    payee_card = found.get("card_id") or payee.strip()
    if found.get("user_id"):
        display_payee = found["name"]
        payee_user_id = found["user_id"]
        channel = "internal"
        payee_hint = f"本行储蓄卡 {payee_card} · {found['phone_masked']} · 确认后实时入账"
    else:
        display_payee = payee_card
        payee_user_id = None
        channel = "external"
        payee_hint = "未匹配到本行储蓄卡 · 演示环境仅扣款记流水"

    order_id = str(uuid.uuid4())[:8]
    order = {
        "kind": "transfer",
        "order_id": order_id,
        "user_id": user_id,
        "payee": display_payee,
        "payee_card": payee_card,
        "payee_user_id": payee_user_id,
        "transfer_channel": channel,
        "amount": amount,
        "notes": notes,
        "status": "PENDING",
    }
    PENDING_ORDERS[order_id] = order

    card_data = {
        "card_kind": "transfer",
        "order_id": order_id,
        "payee": display_payee,
        "payee_card": payee_card,
        "amount": amount,
        "notes": notes,
        "transfer_channel": channel,
        "payee_hint": payee_hint,
    }
    # 先选付款卡，再按「卡片状态 → 余额 → 单笔/日累计/日笔数/年累计限额」做前置校验
    attach_payment_choice(user_id, order, card_data)
    limits = check_transfer_limits(user_id, order.get("pay_card_id", ""), amount)
    if not limits["ok"]:
        # 校验不通过时不保留待确认订单，避免用户点确认才报错
        PENDING_ORDERS.pop(order_id, None)
        history.append({"role": "assistant", "content": limits["message"]})
        return _limit_blocked_card(display_payee, amount, limits)

    fraud = assess_transfer_risk(user_id, amount, display_payee)
    history.append({
        "role": "assistant",
        "content": f"已生成向 {display_payee} 转账 ¥{amount} 的确认单。",
    })
    if fraud["level"] == "high":
        lead = "检测到连续大额转账。请先阅读警示，核对收款人后再输入交易密码。"
    elif fraud["level"] == "caution":
        lead = "这笔转账需要额外留意。请阅读提示并输入交易密码后再提交。"
    else:
        lead = "请核对收款信息，选择付款储蓄卡后输入交易密码。"
    if limits.get("warnings"):
        lead = f"{lead} 本笔即将接近转账限额，请注意卡片额度。"
    card_data["fraud"] = fraud
    card_data["limit_info"] = limits
    card_data["limit_warnings"] = limits.get("warnings") or []
    # 2026-10-08 新增：识别到高风险转账时，立即向绑定的紧急联系人推送预警
    if fraud.get("level") in GUARDIAN_ALERT_LEVELS:
        notice = notify_guardian(
            user_id,
            fraud["level"],
            fraud.get("title") or "高风险转账",
            f"向「{display_payee}」转账 ¥{amount:.2f}",
        )
        order["guardian_notified"] = bool(notice.get("sent"))
        card_data["guardian_alert"] = notice
        if notice.get("sent"):
            lead = f"{lead} 已短信通知紧急联系人{notice['guardian']['name']}。"
    return {
        "type": "ACTION_CARD",
        "text": lead,
        "card_data": card_data,
    }


def _limit_blocked_card(payee: str, amount: float, limits: dict) -> dict:
    """额度/状态校验未通过时，返回说明卡并给出可一键发送的调额引导。"""
    sections = [
        {"label": "收款人", "value": payee or "—"},
        {"label": "转账金额", "value": f"¥{amount:,.2f}"},
    ]
    if limits.get("card_id"):
        sections.append(
            {
                "label": "付款卡",
                "value": f"{limits.get('card_type') or '储蓄卡'} {limits['card_id']}"
                f"（{limits.get('card_status_label') or '—'}）",
            }
        )
    if limits.get("current_remaining_daily_amount") is not None:
        sections.append(
            {"label": "今日剩余可转", "value": f"¥{limits['current_remaining_daily_amount']:,.2f}"}
        )
    if limits.get("current_remaining_daily_count") is not None:
        sections.append(
            {"label": "今日剩余笔数", "value": f"{limits['current_remaining_daily_count']} 笔"}
        )
    if limits.get("current_remaining_annual_amount") is not None:
        sections.append(
            {"label": "年度剩余可转", "value": f"¥{limits['current_remaining_annual_amount']:,.2f}"}
        )

    highlights = []
    for line in [limits.get("message")] + list(limits.get("lines") or []):
        if line and line not in highlights:
            highlights.append(line)

    return {
        "type": "INFO_CARD",
        "text": limits.get("message") or "当前卡片额度校验未通过。",
        "card_data": {
            "card_kind": "limit_alert",
            "title": "转账额度校验未通过",
            "sections": sections,
            "highlights": highlights,
            "quick_actions": limits.get("quick_actions") or [],
        },
    }


