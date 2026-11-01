"""订阅与代扣的确定性实现层。

2026-10-15：原先靠关键词匹配商户名、按关键词判断是否取消的做法已删除。
要取消哪一笔由模型理解后作为 sub_id 传进来（tools.py 的 cancel_subscription）。
"""

import json
import re
import uuid
from config import client, MODEL_NAME
from database import PENDING_ORDERS, list_subscriptions

CYCLE_LABELS = {"MONTHLY": "每月", "YEARLY": "每年"}
STATUS_LABELS = {"ACTIVE": "生效中", "CANCELLED": "已取消"}

def _cycle_label(code: str) -> str:
    return CYCLE_LABELS.get(str(code or "").upper(), str(code or ""))


def _list_card(subs: list, history: list) -> dict:
    active = [s for s in subs if s["status"] == "ACTIVE"]
    if not active:
        reply = "当前没有生效中的免密代扣。办理新签约请到网点或 App 支付设置。"
        history.append({"role": "assistant", "content": reply})
        return {"type": "TEXT", "text": reply, "card_data": None}

    items = []
    for s in active:
        items.append(
            {
                "title": s["merchant"],
                "kicker": f"{_cycle_label(s['billing_cycle'])}代扣",
                "value": f"¥{s['amount']:.2f}",
                "meta": f"下次扣费 {s['next_billing_date']}",
                "badge": STATUS_LABELS.get(s["status"], s["status"]),
            }
        )
    reply = "以下是您账户中正在生效的免密代扣。若要取消，直接说商户名称即可。"
    history.append({"role": "assistant", "content": reply})
    return {
        "type": "INFO_CARD",
        "text": reply,
        "card_data": {
            "card_kind": "subscription_list",
            "title": "订阅 / 代扣服务",
            "items": items,
        },
    }


def _cancel_card(sub: dict, history: list) -> dict:
    order_id = str(uuid.uuid4())[:8]
    PENDING_ORDERS[order_id] = {
        "kind": "sub_cancel",
        "order_id": order_id,
        "user_id": sub.get("_user_id"),
        "sub_id": sub["sub_id"],
        "merchant": sub["merchant"],
        "amount": sub["amount"],
        "status": "PENDING",
    }
    history.append({"role": "assistant", "content": f"已生成取消 {sub['merchant']} 确认单"})
    return {
        "type": "ACTION_CARD",
        "text": "请确认取消以下自动续费/代扣：",
        "card_data": {
            "card_kind": "sub_cancel",
            "order_id": order_id,
            "merchant": sub["merchant"],
            "amount": sub["amount"],
            "billing_cycle": f"{_cycle_label(sub['billing_cycle'])}扣费",
            "notes": f"下次原计划扣费 {sub['next_billing_date']}",
        },
    }


