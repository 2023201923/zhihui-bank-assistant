"""理财业务的确定性实现层。

2026-10-15：本模块不再自己做意图判断。原先的「关键词 → 分支」已删除，
只保留「合规校验 + 画像匹配 + 卡片构建」这些确定性能力，由 assistant.py
经 tools.py 调用。用钱时间（horizon）由模型从对话中理解后作为参数传入，
不再用正则去猜。
"""

import uuid
from typing import List, Optional

from database import (
    PENDING_ORDERS,
    get_user_risk_level,
    get_user_risk_profile,
    get_wealth_products_grouped,
    get_product_by_id,
    attach_payment_choice,
)
from agents.risk_survey import get_survey_card_payload
from agents.wealth_match import build_profile_snapshot, match_wealth_products

# 给客户的「用钱时间」快捷选项
HORIZON_CHOICES = (
    ("随时可能用", "这笔钱随时可能要用"),
    ("一周内不用", "这笔钱一周内用不上"),
    ("一个月内不用", "这笔钱一个月内用不上"),
    ("半年内不用", "这笔钱半年内用不上"),
    ("一年以上不用", "这笔钱一年以上用不上"),
)

# 方案卡上的「不满意出口」——替代「转人工」的关键设计：
# 客户想找人，往往只是因为除了接受没有别的动作可做。
# 每个动作都必须真的改变结果，不做只有样子没有作用的按钮。
RECO_ACTIONS = (
    {"label": "只要随时能取的", "text": "我只要随时能取的产品，帮我推荐"},
    {"label": "期限可以长一点", "text": "期限可以长一点，帮我推荐一年以上的产品"},
    {"label": "都不太合适", "text": "这几款都不太合适，帮我重新推荐一版"},
    {"label": "重新做风险测评", "text": "我要做风险测评"},
)


RISK_DISCLOSURE = "理财非存款，不保证本金和收益。业绩比较基准与过往业绩均不代表实际收益。"

def _resolve_product(product_id: str, products: list):
    """精确解析产品。

    2026-10-31：原实现用 `key in item["name"]` 做模糊匹配，且命中第一个就返回。
    「智汇·月度」是「智汇·月度·稳健」(起购 1000) 和「智汇·月度·进取」(起购 0.01)
    的共同前缀，一旦模型把产品名说短，就会静默解析到兄弟产品，导致卡片上的起购金额
    和实际购买校验的金额对不上。现在只接受精确匹配；前缀命中必须唯一。
    """
    product = get_product_by_id(product_id)
    if product:
        return product
    key = (product_id or "").strip()
    if not key:
        return None

    # 1) 精确命中：产品 ID、完整产品名、别名
    exact = [
        item
        for item in products
        if key == item["product_id"]
        or key == item["name"]
        or key in (item.get("aliases") or [])
    ]
    if len(exact) == 1:
        return exact[0]

    # 2) 前缀/包含匹配必须唯一，否则宁可让客户重新选择，也不猜
    fuzzy = [
        item
        for item in products
        if key in item["name"] or item["name"] in key
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    return None


def _summary(product: dict) -> dict:
    keys = (
        "product_id", "name", "category", "sub_category", "risk_level", "risk_label",
        "term_days", "term_label", "term_sub_label", "yield_pct", "yield_label",
        "min_amount", "benchmark_desc", "asset_direction", "tags",
    )
    return {key: product[key] for key in keys}


def _risk_num(level: str) -> int:
    return int(level[1]) if level and str(level).startswith("R") and len(level) > 1 else 3


def _detail_result(history: list, product: dict) -> dict:
    history.append({"role": "assistant", "content": f"已展示{product['name']}详情。"})
    return {
        "type": "WEALTH_DETAIL_CARD",
        "text": f"以下是【{product['name']}】的产品详情。",
        "card_data": {
            "risk_disclosure": RISK_DISCLOSURE,
            "product": product,
        },
    }


def _pick_recommended(risk: str, catalog: list, limit: int = 3) -> list:
    user_level = _risk_num(risk)
    eligible = [p for p in catalog if _risk_num(p["risk_level"]) <= user_level]
    return eligible[:limit]


def _recommend_sentence(risk: str, catalog: list, user_id: str = "") -> str:
    profile = get_user_risk_profile(user_id) if user_id else {}
    if profile.get("assessment_valid"):
        matched = match_wealth_products(user_id, limit=2)
        names = "、".join(p["name"] for p in matched["products"])
        return (
            f"结合您{profile.get('risk_type') or '未分型'}（{risk}）的风险测评结果，"
            f"我优先建议您看：{names}。"
        )
    if not risk:
        return "您尚未完成风险测评，需要先完成测评，才能按风险等级查看和推荐产品。"
    picks = _pick_recommended(risk, catalog)
    if not picks:
        return f"以您可承受的风险等级 {risk} 来看，目前没有合适的产品，建议先保持活期。"
    names = "、".join(p["name"] for p in picks)
    return f"以您可承受的风险等级 {risk} 来看，我优先建议您看：{names}。"


def _survey_result(history: list) -> dict:
    payload = get_survey_card_payload()
    text = "按监管要求，推荐理财前须完成有效风险测评（一年内有效）。请填写下方问卷后继续。"
    history.append({"role": "assistant", "content": text})
    return {"type": "SURVEY_CARD", "text": text, "card_data": payload}


def _personalized_result(history: list, user_id: str, horizon: str = "") -> dict:
    """出方案卡：依据（画像）→ 方案（怎么分）→ 确认口 + 不满意出口。"""
    matched = match_wealth_products(user_id, horizon=horizon)
    profile = matched["profile"]
    text = matched["headline"]
    history.append({"role": "assistant", "content": text})
    return {
        "type": "WEALTH_CATALOG_CARD",
        "text": text,
        "card_data": {
            "risk_level": profile.get("risk_level"),
            "risk_type": profile.get("risk_type"),
            "catalog_mode": "recommend",
            "style": matched.get("style_plain") or matched["style"],
            "tags": matched.get("tags") or [],
            "allocation": matched.get("allocation") or [],
            "headline": text,
            "explain": matched["explain"],
            "confirm_line": matched.get("confirm_line", ""),
            "horizon": matched.get("horizon", ""),
            "quick_actions": [dict(act) for act in RECO_ACTIONS],
            "products": matched["products"],
        },
    }


def _horizon_ask_result(history: list, user_id: str, reopen: bool = False) -> dict:
    """先确认风险取向（来自画像，不再问客户），再问用钱时间。

    风险偏好是测评已经算出来的，再让客户回答一遍反而显得不专业；
    用钱时间则是系统无法从流水推断的，必须问。
    """
    profile = get_user_risk_profile(user_id)
    risk = profile.get("risk_level") or get_user_risk_level(user_id) or "未测评"
    risk_type = profile.get("risk_type") or "未分型"
    eval_date = str(profile.get("risk_assessment_date") or "")[:10]
    eval_text = f"{eval_date} 完成的风险测评" if eval_date else "您的风险测评"

    if reopen:
        text = (
            "好的，那我们换个方向帮您重新配一版。\n\n"
            "想先跟您确认一下：这笔钱您大概什么时候会用？"
            "换个用钱时间，能配出来的方案差别会比较大。"
        )
    else:
        text = (
            f"根据您{eval_text}，您属于{risk_type}（{risk}），"
            f"我会在这个风险等级内为您挑选，不会推荐超出您承受范围的产品。\n\n"
            f"在给您方案之前，想先跟您确认一件事：这笔钱您大概什么时候会用？"
            f"有没有比较确定的用钱时间？"
        )
    history.append({"role": "assistant", "content": text})
    return {
        "type": "INFO_CARD",
        "text": text,
        "card_data": {
            "title": "为您配置理财方案",
            "sections": (
                [{"label": "用钱时间", "value": "待您重新确认"}]
                if reopen
                else [
                    {"label": "风险取向", "value": f"{risk_type} · {risk}（来自您的风险测评）"},
                    {"label": "用钱时间", "value": "待您确认"},
                ]
            ),
            "quick_actions": [
                {"label": label, "text": phrase} for label, phrase in HORIZON_CHOICES
            ],
        },
    }


def _profile_result(history: list, user_id: str) -> dict:
    """2026-10-06 新增：把风险画像 + 现金流画像 + 持仓 + 配置建议汇总成一张画像卡。"""
    snap = build_profile_snapshot(user_id)
    profile = snap["profile"]
    cash = snap["cashflow"]
    holdings = snap["holdings"]

    if profile.get("assessment_valid"):
        days = profile.get("days_since_eval")
        validity = f"{days} 天前评估 · 一年内有效" if days is not None else "一年内有效"
    elif profile.get("risk_assessment_date"):
        validity = "评估已超过一年 · 建议重新测评"
    else:
        validity = "尚未测评 · 需要先完成风险测评"

    age_occ = " / ".join(
        [t for t in [str(profile.get("age_band") or ""), str(profile.get("occupation") or "")] if t]
    ) or "—"

    level = profile.get("risk_level") or ""
    risk_type = profile.get("risk_type") or ""
    if level and risk_type:
        portrait = f"{risk_type} · {level}"
    elif level:
        portrait = level
    else:
        portrait = "未测评"

    sections = [
        {"label": "风险画像", "value": portrait},
        {
            "label": "测评得分",
            "value": f"{profile['risk_score']} 分" if profile.get("risk_score") is not None else "—",
        },
        {"label": "测评有效期", "value": validity},
        {"label": "年龄 / 职业", "value": age_occ},
        {"label": "近 90 日月均支出", "value": f"¥{cash['monthly_expense']:,.0f}"},
        {"label": "可投闲钱", "value": f"¥{cash['idle_cash']:,.0f}（已预留约 3 个月开支）"},
        {"label": "可用活期余额", "value": f"¥{cash['balance']:,.0f}"},
        {
            "label": "已持理财",
            "value": (
                f"{holdings['count']} 只 · ¥{holdings['total_amount']:,.0f}"
                if holdings["count"]
                else "暂无持仓"
            ),
        },
    ]

    if holdings["count"]:
        holding_line = "、".join(
            f"{h['product_name']} ¥{h['amount']:,.0f}" for h in holdings["holdings"][:4]
        )
    else:
        holding_line = "尚未购买理财产品，可从货架或推荐中挑选。"

    quick_actions = []
    if not profile.get("assessment_valid"):
        quick_actions.append({"label": "开始风险测评", "text": "我要做风险测评"})
    else:
        quick_actions.append({"label": "查看画像推荐", "text": "给我推荐合适的产品"})
    quick_actions.append({"label": "查看全部理财", "text": "公司有哪些理财产品"})

    if profile.get("assessment_valid") and level:
        text = (
            f"这是您的理财画像：{portrait}，"
            f"画像为「{snap['style']}」。"
        )
    else:
        text = "这是您的理财画像。完成风险测评后，才能按您的风险等级给出配置建议。"
    history.append({"role": "assistant", "content": text})
    return {
        "type": "PROFILE_CARD",
        "text": text,
        "card_data": {
            "title": "我的理财画像",
            "style": snap["style"],
            "tags": snap["tags"],
            "sections": sections,
            "allocation": snap["allocation"],
            "holdings": holdings["holdings"],
            "holding_line": holding_line,
            "assessment_valid": bool(profile.get("assessment_valid")),
            "risk_level": level or "未测评",
            "risk_type": profile.get("risk_type"),
            "quick_actions": quick_actions,
            "risk_disclosure": RISK_DISCLOSURE,
        },
    }


def require_valid_assessment(user_id: str, history: list):
    """阶段一：测评未完成或超过 365 天则挂起推荐。"""
    profile = get_user_risk_profile(user_id)
    if profile.get("assessment_valid"):
        return None
    return _survey_result(history)


def _catalog_result(
    history: list,
    risk: str,
    display_products: list,
    message: str,
    mode: str = "recommend",
    full_catalog: Optional[List[dict]] = None,
    user_id: str = "",
) -> dict:
    full_catalog = full_catalog or display_products
    if mode == "all":
        intro = (
            f"以下为公司目前在售的理财产品，共 {len(display_products)} 款。"
            f"{_recommend_sentence(risk, full_catalog, user_id=user_id)}"
            "点开可以看到每款产品的详细说明。"
        )
    else:
        intro = (
            f"结合您可承受的风险等级（{risk}），我为您选出了 {len(display_products)} 款产品。"
            "点开可以看到每款产品的详细说明。"
        )

    history.append({"role": "assistant", "content": intro})
    return {
        "type": "WEALTH_CATALOG_CARD",
        "text": intro,
        "card_data": {
            "risk_level": risk,
            "catalog_mode": mode,
            "products": [_summary(p) for p in display_products],
        },
    }


def _try_stage_purchase(history: list, user_id: str, product: dict, amount: float) -> dict:
    if amount < product["min_amount"]:
        reply = f"【{product['name']}】起购金额为 ¥{product['min_amount']} 元，您输入的 ¥{amount} 元不足，请重新填写。"
        history.append({"role": "assistant", "content": reply})
        return {"type": "TEXT", "text": reply, "card_data": None}

    order_id = str(uuid.uuid4())[:8]
    PENDING_ORDERS[order_id] = {
        "kind": "wealth",
        "order_id": order_id,
        "user_id": user_id,
        "product_id": product["product_id"],
        "product_name": product["name"],
        "amount": amount,
        "yield": product["yield_pct"],
        "status": "PENDING",
    }

    history.append({"role": "assistant", "content": f"已生成申购单：{product['name']} ¥{amount}"})
    card_data = {
        "card_kind": "wealth",
        "order_id": order_id,
        "product_name": product["name"],
        "amount": amount,
        "yield": product["yield_pct"],
        "term": product["term_label"],
        "notes": f"{product['risk_level']} | {product['sub_category']}",
    }
    attach_payment_choice(user_id, PENDING_ORDERS[order_id], card_data)
    return {
        "type": "ACTION_CARD",
        "text": "请核对申购信息，并选择用于付款的储蓄卡。",
        "card_data": card_data,
    }


def build_personalized_recommendation(user_id: str, history: list) -> dict:
    return _personalized_result(history, user_id)
