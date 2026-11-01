"""2026-10-15 新增：统一工具层。

分工约定（本次改造的核心）：
- 模型负责「理解客户在问什么」和「决定去查哪些数据 / 做哪个动作」；
- 本文件负责「怎么算」——金额、限额、风控、风险等级过滤、合规话术一律是确定性代码。
  模型只能决定调用哪个工具、传什么参数，不能改写结论。

每个工具统一返回：
    {"data": {...}, "card": {...} | None}
data 回传给模型供其推理；card 是要展示给客户的前端卡片（沿用既有卡片格式，
所以小程序不需要改动）。
"""

import re
from typing import Callable, Dict, List, Optional

import agents.bill_agent as bill_agent
import agents.card_agent as card_agent
import agents.cross_agent as cross_agent
import agents.guard_agent as guard_agent
import agents.sub_agent as sub_agent
import agents.transfer_agent as transfer_agent
import agents.wealth_agent as wealth_agent
from agents.wealth_match import STYLE_PLAIN, build_profile_snapshot, match_wealth_products
from database import (
    bind_guardian,
    find_abnormal_transactions,
    get_cashflow_profile,
    get_category_spending,
    get_guardian_center,
    get_locked_funds,
    get_recent_transactions,
    get_user_balance,
    get_user_brief,
    get_user_risk_level,
    get_user_risk_profile,
    get_wealth_holdings,
    get_wealth_products_grouped,
    list_active_scenarios,
    list_scenario_history,
    list_internal_payees,
    list_subscriptions,
    list_user_cards,
)


def _risk_num(level: str) -> int:
    return int(level[1]) if level and str(level).startswith("R") and len(level) > 1 else 3


# 判断当前对话到底在聊理财，还是在聊具体消费（买礼物/买衣服等）。
# 用途：挡住模型把「可以推荐」误解成「推荐理财产品」。
# 不用单独的「产品」：日常会说「电子产品」「这件产品」，会把购物误判成理财，
# 从而不再屏蔽理财工具。「理财」已经覆盖「理财产品」。
_WEALTH_WORDS = re.compile(r"理财|基金|收益|投资|财富|资产配置|风险等级|持仓|申购")
_PURCHASE_WORDS = re.compile(
    r"买|购买|礼物|礼品|置办|采购|内衣|衣服|鞋|鲜花|蛋糕|预算|预留"
)


def _topic_is_purchase_not_wealth(ctx: dict) -> bool:
    recent = [
        str(m.get("content") or "")
        for m in (ctx.get("history") or [])[-6:]
        if m.get("role") == "user"
    ]
    recent.append(str(ctx.get("message") or ""))
    text = " ".join(recent)
    return bool(_PURCHASE_WORDS.search(text)) and not bool(_WEALTH_WORDS.search(text))


def topic_is_purchase_not_wealth(ctx: dict) -> bool:
    """供 assistant 判断是否要在本轮屏蔽理财工具。"""
    return _topic_is_purchase_not_wealth(ctx)


# 模型有时会把用钱时间说成「半年内」「一年以上」这类变体，统一收敛到 5 个标准档。
_HORIZON_ALIASES = (
    ("活钱", ("随时", "活钱", "零钱", "马上")),
    ("七日", ("七日", "一周", "礼拜", "7天", "七天")),
    ("月度", ("月度", "一个月", "30天", "月内", "月底")),
    ("半年", ("半年", "六个月", "6个月", "180天")),
    ("年度", ("年度", "一年", "12个月", "长期")),
)


def _normalize_horizon(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for canonical, keys in _HORIZON_ALIASES:
        if text == canonical or any(key in text for key in keys):
            return canonical
    return ""


def _horizon_backed_by_customer(horizon: str, ctx: dict) -> bool:
    """用钱时间必须是客户自己说过的。

    实测发现模型会凭空猜一个用钱时间去调用推荐工具，跳过「先问客户」这一步，
    结果客户没说过就被直接安排了方案。理解交给模型，但「有没有依据」由代码校验。
    """
    keys = next((ks for canonical, ks in _HORIZON_ALIASES if canonical == horizon), ())
    if not keys:
        return False
    for item in (ctx.get("history") or [])[-12:]:
        if item.get("role") != "user":
            continue
        text = str(item.get("content") or "")
        if horizon in text or any(key in text for key in keys):
            return True
    return False


def _money(value) -> float:
    return round(float(value or 0), 2)


def _brief_rows(rows: List[dict]) -> List[dict]:
    return [
        {
            "日期": str(r.get("created_at") or "")[:10],
            "分类": r.get("category"),
            "金额": _money(r.get("amount")),
            "对方": r.get("counterparty"),
        }
        for r in rows
    ]


def _product_brief(product: dict, user_risk: str) -> dict:
    return {
        "product_id": product["product_id"],
        "名称": product["name"],
        "风险等级": product["risk_level"],
        "期限": product["term_label"],
        "起购金额": product["min_amount"],
        "参考年化": product["yield_pct"],
        "是否在客户风险承受范围内": _risk_num(product["risk_level"]) <= _risk_num(user_risk),
        # 人话翻译层：模型对客户解释产品时应优先用这里的说法，而不是念合规字段
        "对客户解释可用": product.get("plain") or {},
    }


# ======================== 只读工具 ========================


def _t_account_overview(user_id: str, args: dict, ctx: dict) -> dict:
    """一次取到「总结客户财务状况」需要的全部基础数据。"""
    brief = get_user_brief(user_id) or {}
    profile = get_user_risk_profile(user_id)
    cash = get_cashflow_profile(user_id)
    cards = list_user_cards(user_id)
    data = {
        "客户姓名": brief.get("name"),
        "可用活期余额": _money(get_user_balance(user_id)),
        "锁定资金": _money(get_locked_funds(user_id)),
        "持有卡片": [
            {
                "card_id": c["card_id"],
                "类型": c["card_type"],
                "状态": c["status_label"],
                "可用于转账": bool(c.get("is_debit")),
            }
            for c in cards
        ],
        "风险等级": profile.get("risk_level") or "未测评",
        "风险类型": profile.get("risk_type"),
        "风险测评是否在有效期内": bool(profile.get("assessment_valid")),
        "风险测评日期": profile.get("risk_assessment_date"),
        "年龄段": profile.get("age_band"),
        "职业": profile.get("occupation"),
        "近90天月均支出": _money(cash["monthly_expense"]),
        "预留3个月开支后可投闲钱": _money(cash["idle_cash"]),
        "资金进出是否频繁": bool(cash["high_turnover"]),
        "是否有闲钱沉淀": bool(cash["stable_idle"]),
    }
    return {"data": data, "card": None}


def _t_spending_breakdown(user_id: str, args: dict, ctx: dict) -> dict:
    window = bill_agent.resolve_bill_window(args.get("year"), args.get("month"), ctx.get("message") or "")
    categories = get_category_spending(user_id, start=window["start"], end=window["end"])
    total = sum(c["amount"] for c in categories)
    abnormal = find_abnormal_transactions(user_id, start=window["start"], end=window["end"])
    data = {
        "统计窗口": window["label"],
        "支出合计": _money(total),
        "支出分类": [
            {
                "分类": c["category"],
                "金额": _money(c["amount"]),
                "占比": f"{round(c['amount'] / (total or 1) * 100, 1)}%",
            }
            for c in categories
        ],
        "异常交易": [
            {
                "日期": str(a.get("created_at") or "")[:10],
                "分类": a.get("category"),
                "金额": _money(a.get("amount")),
                "对方": a.get("counterparty"),
                "判定原因": a.get("reason"),
            }
            for a in abnormal[:5]
        ],
        "最近流水": _brief_rows(
            get_recent_transactions(user_id, limit=10, start=window["start"], end=window["end"])
        ),
    }
    # 只回数据。「要看账单」这类请求由 assistant.py 的直接出卡层保证（账单/流水/花了多少
    # /消费明细/花销），不依赖本工具带卡；本工具留给模型做分析用。
    # 早先这里绑了卡片，结果客户问「我的消费是不是太不理性了」只拿到一张账单卡、
    # 没有得到任何回答——卡片把分析顶掉了。
    return {"data": data, "card": None}


def _t_recent_transactions(user_id: str, args: dict, ctx: dict) -> dict:
    limit = int(args.get("limit") or 10)
    rows = get_recent_transactions(user_id, limit=max(1, min(limit, 50)))
    return {"data": {"流水条数": len(rows), "流水": _brief_rows(rows)}, "card": None}


def _t_wealth_holdings(user_id: str, args: dict, ctx: dict) -> dict:
    holdings = get_wealth_holdings(user_id)
    return {
        "data": {
            "持仓只数": holdings["count"],
            "持仓合计": _money(holdings["total_amount"]),
            "持仓明细": holdings["holdings"],
        },
        "card": None,
    }


def _t_risk_and_cashflow(user_id: str, args: dict, ctx: dict) -> dict:
    snap = build_profile_snapshot(user_id)
    profile = snap["profile"]
    cash = snap["cashflow"]
    # 把客户可投产品的完整名称一并给出：模型不需要（也不应该）自己拼产品名。
    assessed = bool(profile.get("assessment_valid")) and bool(snap.get("risk_level"))
    investable = get_wealth_products_grouped(max_risk_level=snap["risk_level"]) if assessed else []
    data = {
        "风险等级": snap.get("risk_label") or "未测评",
        "风险类型": profile.get("risk_type"),
        "测评是否有效": bool(profile.get("assessment_valid")),
        "测评日期": profile.get("risk_assessment_date"),
        "年龄段": profile.get("age_band"),
        "职业": profile.get("occupation"),
        "画像风格": snap["style"],
        "画像风格人话版": STYLE_PLAIN.get(snap["style"], snap["style"]),
        "画像标签": snap["tags"],
        "近90天月均支出": _money(cash["monthly_expense"]),
        "可用活期余额": _money(cash["balance"]),
        "预留约3个月开支后可投闲钱": _money(cash["idle_cash"]),
        "资金周转特征": {
            "资金进出频繁": bool(cash["high_turnover"]),
            "有闲钱沉淀": bool(cash["stable_idle"]),
        },
        "建议配置比例": snap["allocation"],
        "可投产品（名称必须原样使用，不要简化）": [
            {
                "名称": p["name"],
                "风险等级": p["risk_level"],
                "期限": p["term_label"],
                "参考年化": p["yield_pct"],
                "人话说明": (p.get("plain") or {}).get("when") or "",
            }
            for p in investable
        ],
        "已持理财": snap["holdings"],
    }
    return {"data": data, "card": None}


def _t_internal_payees(user_id: str, args: dict, ctx: dict) -> dict:
    peers = list_internal_payees(user_id)
    rows = []
    for peer in peers:
        debit_cards = [
            card["card_id"]
            for card in list_user_cards(peer["user_id"])
            if card.get("is_debit")
        ]
        rows.append(
            {
                "姓名": peer["name"],
                "储蓄卡卡号": debit_cards,
                "手机号": peer["phone_masked"],
            }
        )
    return {"data": {"可转账的本行储蓄卡": rows}, "card": None}


def _t_show_bill(user_id: str, args: dict, ctx: dict) -> dict:
    message = ctx.get("message") or ""
    card = bill_agent.run_bill_agent(
        user_id,
        message,
        ctx["scratch"],
        year=args.get("year"),
        month=args.get("month"),
    )
    return {"data": {"说明": card.get("text") or "已展示账单分析卡", "统计窗口": card["card_data"]["title"]}, "card": card}


def _t_show_cards(user_id: str, args: dict, ctx: dict) -> dict:
    cards = list_user_cards(user_id)
    return {
        "data": {
            "卡片": [
                {
                    "card_id": c["card_id"],
                    "类型": c["card_type"],
                    "状态": c["status_label"],
                    "可用于转账": bool(c.get("is_debit")),
                }
                for c in cards
            ]
        },
        "card": card_agent._list_card(cards, ctx["scratch"]) if cards else None,
    }


def _customer_texts(ctx: dict) -> list:
    texts = [
        str(item.get("content") or "")
        for item in (ctx.get("history") or [])
        if item.get("role") == "user"
    ]
    message = str(ctx.get("message") or "")
    if message and (not texts or texts[-1] != message):
        texts.append(message)
    return texts


def _purpose_from_customer(texts: list) -> str:
    for text in reversed(texts):
        raw = str(text or "").strip()
        if not re.search(r"心愿备用金|留\s*\d|单独存|备用金", raw):
            continue
        cleaned = re.sub(r"^(我想|我要|帮我|请)", "", raw).strip()
        cleaned = re.sub(r"留\s*\d+(?:\.\d+)?\s*[kK千]?", "", cleaned)
        return cleaned.strip("，,、 ") or raw
    return ""


_TRANSFER_AMOUNT = re.compile(
    r"^(?:转账|转|汇)?\d+(?:\.\d+)?[kK千元块]?$"
)


_CARD_IN_TEXT = re.compile(r"\d{6}\*{2,}\d{4}|\d{12,19}")


def _cards_in_text(text: str) -> list:
    compact = re.sub(r"[\s-]", "", str(text or ""))
    return _CARD_IN_TEXT.findall(compact)


def _transfer_amount(text: str) -> float:
    amount = cross_agent.parse_amount(text)
    thousand = re.search(r"(\d+(?:\.\d+)?)\s*千", str(text or ""))
    if thousand:
        return float(thousand.group(1)) * 1000
    return amount


def _amount_outside_card(text: str) -> float:
    raw = str(text or "")
    compact = re.sub(r"[\s-]", "", raw)
    stripped = _CARD_IN_TEXT.sub(" ", compact)
    if not re.search(r"\d|[kK千]", stripped):
        return 0
    if not re.search(r"转|汇|元|块|千|[kK]", raw) and not _TRANSFER_AMOUNT.fullmatch(stripped):
        return 0
    return _transfer_amount(stripped)


def prepare_transfer_followup(user_id: str, ctx: dict):
    """金额和收款储蓄卡卡号都已出现时补一张转账确认卡。只有姓名时改为追问卡号。"""
    texts = _customer_texts(ctx)
    if not texts:
        return None
    latest = str(texts[-1] or "").strip()
    recent = "\n".join(texts[-4:])
    if not re.search(r"转账|转给|汇给|汇款", recent):
        return None
    if re.search(r"心愿|备用金|账单|理财", latest) and not _cards_in_text(latest):
        return None
    latest_compact = re.sub(r"\s+", "", latest)
    latest_is_transfer = bool(
        re.search(r"转账|转给|汇给|汇款|卡号", latest)
        or _TRANSFER_AMOUNT.fullmatch(latest_compact)
        or _cards_in_text(latest)
    )
    if not latest_is_transfer:
        return None

    card_no = ""
    amount = 0
    for text in texts:
        found = _cards_in_text(text)
        if found:
            card_no = found[-1]
        parsed = _amount_outside_card(text)
        if parsed > 0:
            amount = parsed
    if amount > 0 and card_no:
        result = _t_stage_transfer(user_id, {"payee": card_no, "amount": amount}, ctx)
        if result.get("card"):
            return {"card": result["card"]}
        reason = (result.get("data") or {}).get("错误")
        if reason:
            return {"ask": reason}
        return None
    if amount > 0:
        return {"ask": "转账需要对方的储蓄卡卡号，只凭姓名无法办理。请提供收款储蓄卡卡号。"}
    return None


def scenario_followup_ready(ctx: dict) -> bool:
    """客户刚补上用钱日，且前面已经说了要留多少钱。"""
    from datetime import date
    from scenario_service import _parse_explicit_date

    texts = _customer_texts(ctx)
    if len(texts) < 2:
        return False
    if _parse_explicit_date(texts[-1], date.today()) is None:
        return False
    earlier = "\n".join(texts[:-1])
    if not re.search(r"心愿备用金|留\s*\d|单独存|备用金", earlier):
        return False
    return cross_agent.parse_amount(earlier) > 0


def _t_cross_scenario(user_id: str, args: dict, ctx: dict) -> dict:
    """把一笔钱单独预留，并可登记提醒。用途/金额/时间全部来自客户原话。"""
    texts = _customer_texts(ctx)
    args = dict(args or {})
    if cross_agent.parse_amount(args.get("amount")) <= 0:
        for text in reversed(texts):
            amount = cross_agent.parse_amount(text)
            if amount > 0 and re.search(r"\d", text):
                args["amount"] = amount
                break
    if not str(args.get("purpose") or args.get("notes") or "").strip():
        purpose = _purpose_from_customer(texts)
        if purpose:
            args["purpose"] = purpose
    result = cross_agent.stage_cross_plan(user_id, args, ctx["scratch"], texts)
    if result.get("needs_more"):
        return {"data": result["needs_more"], "card": None}
    if result.get("blocked"):
        return {"data": result["blocked"], "card": None}
    return {"data": result.get("data") or {}, "card": result}


def _t_release_scenario(user_id: str, args: dict, ctx: dict) -> dict:
    """提前取回一笔存续中的心愿备用金，本息回到可用余额。"""
    from scenario_service import stage_release

    result = stage_release(user_id, args or {}, ctx["scratch"])
    if result.get("needs_more"):
        return {"data": result["needs_more"], "card": None}
    return {"data": result.get("data") or {}, "card": result}


_ORDINALS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _wish_release_index(text: str):
    matched = re.search(r"第\s*([0-9]+|[一二三四五六七八九十])\s*笔", text or "")
    if not matched:
        return None
    token = matched.group(1)
    if token.isdigit():
        number = int(token)
    else:
        number = _ORDINALS.get(token)
    if not number or number < 1:
        return None
    return number


_WISH_INTENT = re.compile(r"心愿备用金|备用金|单独存|预留一笔|留一笔")
_AMOUNT_WITH_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(元|块|[kK]|千)")


def _amount_from_create_text(text: str) -> float:
    """只认带货币单位的金额。

    不能用 parse_amount 直接扫数字：客户说「11月5号」时，最后一段数字是 5，
    会被误当成 5 元并据此生成确认卡。要素不全时必须交回模型追问，不能猜。
    """
    matched = _AMOUNT_WITH_UNIT.search(str(text or ""))
    if not matched:
        return 0.0
    value = float(matched.group(1))
    if matched.group(2) in ("k", "K", "千"):
        value *= 1000
    return value


def _purpose_from_create_text(text: str) -> str:
    """从客户原话里剥掉指令、金额、日期，只留下用途。"""
    s = str(text or "")
    s = re.sub(r"\d+(?:\.\d+)?\s*(?:元|块|[kK]|千)", " ", s)
    s = re.sub(r"(下个?月|本月|这个月)\s*\d{1,2}\s*[号日]", " ", s)
    s = re.sub(r"\d{1,2}月\d{1,2}[号日]?", " ", s)
    s = re.sub(r"(下下周|下周|周|星期|礼拜)[一二三四五六日天]", " ", s)
    for word in (
        "心愿备用金", "备用金", "单独存", "预留一笔", "留一笔", "预留",
        "帮我", "麻烦", "我想", "我要", "给我", "请",
        "建一个", "建立一个", "新建", "创建", "设置", "添加", "设个", "建个",
    ):
        s = s.replace(word, " ")
    s = re.sub(r"(要花|用来|用于|花掉)", " ", s)
    s = re.sub(r"[，,。、；;：:\s]+", " ", s).strip()
    s = re.sub(r"^用\s*", "", s)
    s = re.sub(r"\s+用\s+", " ", s)
    return s.strip()


def prepare_wish_create(user_id: str, message: str, ctx: dict):
    """客户在一句话里同时说清「用途 + 金额 + 用钱日」时，直接生成确认卡。

    严格限制（2026-10-31）：
    - 必须出现明确的心愿备用金意图（心愿备用金 / 备用金 / 单独存 / 预留一笔）；
    - 金额、用钱日、用途三项缺一不可，任何一项解析不出来就返回 None，交回模型追问；
    - 日期只认客户自己说的，金额只解析客户原话，绝不填默认值。
    """
    from datetime import date

    from scenario_service import date_stated_by_customer

    text = str(message or "")
    if not _WISH_INTENT.search(text):
        return None
    amount = _amount_from_create_text(text)
    if amount <= 0:
        return None
    stated = date_stated_by_customer([text], date.today())
    if stated is None:
        return None
    purpose = _purpose_from_create_text(text)
    if not purpose:
        return None

    result = cross_agent.stage_cross_plan(
        user_id,
        {"purpose": purpose, "amount": amount, "target_date": stated.isoformat()},
        ctx["scratch"],
        [text],
    )
    if result.get("needs_more") or result.get("blocked"):
        return None
    return {"card": result}


def prepare_wish_release(user_id: str, message: str, ctx: dict):
    """客户要取消或取回心愿备用金时，直接准备取回确认卡。只是查看列表时返回 None。"""
    text = message or ""
    if not re.search(r"取消|取回|不要了|不预留|提前取出|解冻", text):
        return None
    if not re.search(r"备用金|心愿|预留|第\s*[0-9一二三四五六七八九十]\s*笔", text):
        return None
    args = {}
    index = _wish_release_index(text)
    if index:
        args["index"] = index
    result = _t_release_scenario(user_id, args, ctx)
    card = result.get("card")
    if card:
        return {"card": card}
    rows = (result.get("data") or {}).get("在途专款") or []
    if not rows:
        return {"ask": "您目前没有可以取回的心愿备用金。"}
    lines = [
        f"{i}. {row.get('用途') or '心愿备用金'} ¥{float(row.get('本金') or 0):,.0f}"
        for i, row in enumerate(rows, 1)
    ]
    return {"ask": "您要取回哪一笔心愿备用金？直接说第几笔即可。\n" + "\n".join(lines)}


def _t_show_wish_reserves(user_id: str, args: dict, ctx: dict) -> dict:
    """列出心愿备用金的全部记录：存续中 + 已完成历史。

    2026-10-31：原先只列 status='ACTIVE'，客户存过但已回到可用余额的记录
    完全不可见，助手就会回答"没有记录"。现在历史记录一并列出并标注状态。
    """
    active = list_active_scenarios(user_id)
    history = [
        row
        for row in list_scenario_history(user_id)
        if str(row.get("status") or "") != "ACTIVE"
    ]

    items = [
        {
            "title": row.get("notes") or "心愿备用金",
            "kicker": row.get("product_name") or "",
            "value": f"¥{float(row.get('current_principal') or 0):,.2f}",
            "meta": f"用钱日 {row.get('target_date') or '—'} · {row.get('auto_redeem_date') or '—'} 回到可用余额",
            "badge": "存续中",
        }
        for row in active
    ]
    # 已完成记录里，如果赎回日早于用钱日，说明是提前结束（含修复前被压到今天的历史数据）。
    # 如实标注，避免让客户以为这是正常的到期赎回。
    history_items = []
    for row in history:
        target = str(row.get("target_date") or "")
        closed = str(row.get("auto_redeem_date") or "")
        early = bool(target and closed and closed < target)
        history_items.append(
            {
                "title": row.get("notes") or "心愿备用金",
                "kicker": row.get("product_name") or "",
                "value": f"¥{float(row.get('target_amount') or 0):,.2f}",
                "meta": (
                    f"{'提前回到' if early else '已回到'}可用余额 · "
                    f"用钱日 {target or '—'} · 创建于 {str(row.get('created_at') or '')[:10]}"
                ),
                "badge": "提前结束" if early else "已完成",
            }
        )
    items += history_items

    if active or history:
        text = f"您目前有 {len(active)} 笔存续中的心愿备用金"
        if history:
            text += f"，另有 {len(history)} 笔已完成的历史记录"
        text += "。"
    else:
        text = "您目前没有心愿备用金记录。"

    return {
        "data": {
            "存续笔数": len(active),
            "历史笔数": len(history),
            "存续中": [
                {
                    "编号": row.get("id"),
                    "用途": row.get("notes"),
                    "本金": round(float(row.get("current_principal") or 0), 2),
                    "用钱日": row.get("target_date"),
                    "赎回日": row.get("auto_redeem_date"),
                    "状态": "存续中",
                }
                for row in active
            ],
            "已完成": [
                {
                    "编号": row.get("id"),
                    "用途": row.get("notes"),
                    "金额": round(float(row.get("target_amount") or 0), 2),
                    "用钱日": row.get("target_date"),
                    "创建于": str(row.get("created_at") or "")[:10],
                    "状态": (
                        "提前结束"
                        if str(row.get("auto_redeem_date") or "")
                        and str(row.get("auto_redeem_date") or "")
                        < str(row.get("target_date") or "")
                        else "已完成"
                    ),
                }
                for row in history
            ],
            "说明": "这里包含已在确认卡上输入密码、已经划款的记录（含已回到活期余额的）。"
            "未在卡片上确认的只能算确认单，不算记录。",
        },
        "card": {
            "type": "INFO_CARD",
            "text": text,
            "card_data": {"title": "心愿备用金", "items": items},
        },
    }


def _t_show_subscriptions(user_id: str, args: dict, ctx: dict) -> dict:
    subs = list_subscriptions(user_id)
    for s in subs:
        s["_user_id"] = user_id
    card = sub_agent._list_card(subs, ctx["scratch"]) if subs else {
        "type": "TEXT",
        "text": "您当前没有正在生效的自动续费或代扣。",
        "card_data": None,
    }
    return {
        "data": {
            "订阅": [
                {
                    "sub_id": s["sub_id"],
                    "商户": s["merchant"],
                    "金额": _money(s["amount"]),
                    "周期": s["billing_cycle"],
                    "下次扣费": s.get("next_billing_date"),
                    "状态": s["status"],
                }
                for s in subs
            ]
        },
        "card": card,
    }


def _t_show_security(user_id: str, args: dict, ctx: dict) -> dict:
    center = get_guardian_center(user_id)
    user_name = (get_user_brief(user_id) or {}).get("name") or "您"
    return {
        "data": {"安全防护中心": center},
        "card": {
            "type": "GUARDIAN_CARD",
            "text": guard_agent._center_text(center, user_name),
            "card_data": center,
        },
    }


def _t_show_wealth_profile(user_id: str, args: dict, ctx: dict) -> dict:
    return {
        "data": _t_risk_and_cashflow(user_id, args, ctx)["data"],
        "card": wealth_agent._profile_result(ctx["scratch"], user_id),
    }


def _t_show_wealth_catalog(user_id: str, args: dict, ctx: dict) -> dict:
    """展示在售货架。未完成有效风险测评时先打开问卷。"""
    blocked = wealth_agent.require_valid_assessment(user_id, ctx["scratch"])
    if blocked:
        return {"data": {"需要客户先完成风险测评": True}, "card": blocked}
    risk = get_user_risk_level(user_id)
    all_products = get_wealth_products_grouped(max_risk_level="R5")
    card = wealth_agent._catalog_result(
        ctx["scratch"],
        risk,
        all_products,
        "",
        mode="all",
        full_catalog=all_products,
        user_id=user_id,
    )
    return {
        "data": {
            "客户风险等级": risk,
            "在售产品": [_product_brief(p, risk) for p in all_products],
        },
        "card": card,
    }


def _t_show_wealth_detail(user_id: str, args: dict, ctx: dict) -> dict:
    blocked = wealth_agent.require_valid_assessment(user_id, ctx["scratch"])
    if blocked:
        return {"data": {"需要客户先完成风险测评": True}, "card": blocked}
    product_id = (args.get("product_id") or "").strip()
    all_products = get_wealth_products_grouped(max_risk_level="R5")
    product = wealth_agent._resolve_product(product_id, all_products)
    if not product:
        return {
            "data": {"错误": f"未找到产品 {product_id or '（未指定）'}", "可用的 product_id": [p["product_id"] for p in all_products]},
            "card": None,
        }
    risk = get_user_risk_level(user_id)
    if _risk_num(product["risk_level"]) > _risk_num(risk):
        # 合规：不向客户展示超出其风险等级的产品详情
        return {
            "data": {
                "合规阻断": f"{product['name']} 风险等级为 {product['risk_level']}，"
                f"高于客户当前 {risk}，按风险适当性要求不予展示",
                "建议": "如实告知客户不可投，不要介绍该产品的收益与投向",
            },
            "card": None,
        }
    return {
        "data": {"产品详情": _product_brief(product, risk), "产品全量字段": product},
        "card": wealth_agent._detail_result(ctx["scratch"], product),
    }


# ======================== 动作工具（确定性校验全部在这里） ========================


def _t_start_risk_assessment(user_id: str, args: dict, ctx: dict) -> dict:
    return {
        "data": {"说明": "已展示风险测评问卷，请客户填写后由服务端计分"},
        "card": wealth_agent._survey_result(ctx["scratch"]),
    }


def _t_recommend_wealth(user_id: str, args: dict, ctx: dict) -> dict:
    """合规校验 + 画像匹配。

    缺「用钱时间」时不猜、直接返回追问卡，让模型去问客户；
    风险等级过滤由 match_wealth_products 在代码里强制执行，模型无法绕过。
    """
    scratch = ctx["scratch"]
    if _topic_is_purchase_not_wealth(ctx):
        # 代码兜底：当前在聊具体消费，不是理财配置。
        # 实测模型会把「可以推荐」理解成「推荐理财产品」，必须在这里挡住。
        return {
            "data": {
                "不适用": "当前对话在讨论具体消费或礼物，不是理财配置。",
                "要求": "不要推荐理财产品。说明你只能协助资金预留与提醒；"
                "客户若想要商品推荐，如实说明不提供该服务。",
            },
            "card": None,
        }
    blocked = wealth_agent.require_valid_assessment(user_id, scratch)
    if blocked:
        return {"data": {"需要客户先完成风险测评": True}, "card": blocked}

    # 防一手：模型可能传来「半年内」这类变体，收敛到标准档再往下走，
    # 收敛不出来就当成没给（回到确认卡），绝不拿一个非法值去匹配。
    horizon = _normalize_horizon(args.get("horizon"))
    if horizon and not _horizon_backed_by_customer(horizon, ctx):
        # 客户没说过这个用钱时间，退回「先问客户」，不接受模型自己猜的值
        horizon = ""
    if not horizon:
        return {
            "data": {
                "缺少信息": "用钱时间",
                "下一步": "先向客户确认这笔钱大概什么时候会用，再带着 horizon 参数重新调用本工具",
                "可选 horizon 取值": ["活钱", "七日", "月度", "半年", "年度"],
            },
            "card": wealth_agent._horizon_ask_result(scratch, user_id),
        }
    matched = match_wealth_products(user_id, horizon=horizon)
    return {
        "data": {
            "用钱时间": horizon,
            "客户风险上限": matched["profile"].get("risk_level"),
            "可投闲钱": _money(matched["cashflow"]["idle_cash"]),
            "推荐产品": [
                {
                    "名称": p["name"],
                    "风险等级": p["risk_level"],
                    "期限": p["term_label"],
                    "参考年化": p["yield_pct"],
                    "推荐理由（可直接对客户讲）": p.get("reason"),
                }
                for p in matched["products"]
            ],
        },
        "card": wealth_agent._personalized_result(scratch, user_id, horizon=horizon),
    }


def _t_stage_wealth_purchase(user_id: str, args: dict, ctx: dict) -> dict:
    scratch = ctx["scratch"]
    blocked = wealth_agent.require_valid_assessment(user_id, scratch)
    if blocked:
        return {"data": {"需要客户先完成风险测评": True}, "card": blocked}
    all_products = get_wealth_products_grouped(max_risk_level="R5")
    product = wealth_agent._resolve_product((args.get("product_id") or "").strip(), all_products)
    if not product:
        return {
            "data": {
                "错误": f"未找到精确匹配的产品「{(args.get('product_id') or '').strip() or '未指定'}」",
                "要求": "请使用完整产品名或 product_id 重新指定，不要使用简称或前缀",
                "可选产品": [
                    {"product_id": p["product_id"], "名称": p["name"], "起购": p["min_amount"]}
                    for p in all_products
                ],
            },
            "card": None,
        }
    risk = get_user_risk_level(user_id)
    amount = args.get("amount")
    if amount is None or float(amount) <= 0:
        return {
            "data": {"缺少信息": "申购金额", "起购金额": product["min_amount"], "下一步": "先问客户要申购多少金额"},
            "card": None,
        }
    if not risk:
        return {
            "data": {"需要客户先完成风险测评": True, "说明": "风险等级为空，不能按默认等级申购"},
            "card": wealth_agent._survey_result(scratch),
        }
    if _risk_num(product["risk_level"]) > _risk_num(risk):
        return {
            "data": {
                "合规阻断": f"{product['name']} 风险等级 {product['risk_level']} 高于客户 {risk}，不能申购",
                "建议": "如实告知客户不可申购，不要生成确认单",
            },
            "card": None,
        }
    card = wealth_agent._try_stage_purchase(scratch, user_id, product, float(amount))
    return {"data": {"结果": card.get("text")}, "card": card}


def _t_stage_transfer(user_id: str, args: dict, ctx: dict) -> dict:
    """限额四维校验、反洗钱风控、卡片状态、亲情守护预警全部在 _build_transfer_card 内完成。"""
    from database import lookup_transfer_card

    scratch = ctx["scratch"]
    payee = (args.get("payee") or "").strip()
    amount = args.get("amount")
    notes = (args.get("notes") or "").strip() or "日常转账"
    if amount is None or float(amount) <= 0 or not payee:
        missing = []
        if not payee:
            missing.append("收款储蓄卡卡号")
        if amount is None or float(amount or 0) <= 0:
            missing.append("金额")
        return {
            "data": {"缺少信息": missing, "下一步": "先向客户问齐收款储蓄卡卡号和金额，再调用本工具"},
            "card": None,
        }
    found = lookup_transfer_card(user_id, payee)
    if not found:
        return {
            "data": {
                "缺少信息": ["收款储蓄卡卡号"],
                "下一步": "转账必须使用对方储蓄卡卡号，不要只凭姓名生成确认卡",
            },
            "card": None,
        }
    if found.get("self"):
        return {"data": {"错误": "不能转到本人的银行卡，请换一张收款储蓄卡。"}, "card": None}
    if found.get("not_debit"):
        return {"data": {"错误": f"{found.get('name') or '对方'}的这张是信用卡，收款需要储蓄卡卡号。"}, "card": None}
    card = transfer_agent._build_transfer_card(user_id, payee, float(amount), notes, scratch)
    return {"data": {"结果": card.get("text"), "订单已生成": card.get("type") == "ACTION_CARD"}, "card": card}


def _t_cancel_subscription(user_id: str, args: dict, ctx: dict) -> dict:
    sub_id = (args.get("sub_id") or "").strip()
    merchant = (args.get("merchant") or "").strip()
    subs = list_subscriptions(user_id)
    for s in subs:
        s["_user_id"] = user_id
    # 客户通常只会说商户名（「停掉爱奇艺」），不会说 sub_id。
    # 先按 ID 找，再按商户名模糊匹配，避免模型因为拿不到 ID 而编造「已生成」。
    target = next((s for s in subs if s["sub_id"] == sub_id), None) if sub_id else None
    if not target and merchant:
        target = next(
            (s for s in subs if merchant in str(s.get("merchant") or "")), None
        )
    if not target:
        return {
            "data": {
                "错误": f"未找到订阅 {merchant or sub_id or '（未指定）'}",
                "可用订阅": [
                    {"sub_id": s["sub_id"], "商户": s["merchant"]} for s in subs
                ],
                "下一步": "把可用订阅念给客户，问清要取消哪一项；不要声称已生成确认单",
            },
            "card": None,
        }
    return {"data": {"结果": f"已生成取消 {target['merchant']} 的确认单"}, "card": sub_agent._cancel_card(target, ctx["scratch"])}


def _t_stage_card_action(user_id: str, args: dict, ctx: dict) -> dict:
    card = card_agent.stage_card_action(user_id, args, ctx["scratch"])
    return {"data": {"结果": card.get("text")}, "card": card}


def _t_bind_guardian(user_id: str, args: dict, ctx: dict) -> dict:
    name = (args.get("name") or "").strip()
    phone = (args.get("phone") or "").strip()
    relation = (args.get("relation") or "").strip() or "亲属"
    if not name or not phone:
        return {
            "data": {
                "缺少信息": [x for x, ok in (("姓名", bool(name)), ("手机号", bool(phone))) if not ok],
                "下一步": "先向客户问齐家人姓名和手机号；不要用任何示例姓名或号码代替客户真实提供的信息",
            },
            "card": None,
        }
    guardian = bind_guardian(user_id, name, phone, relation)
    if not guardian:
        return {
            "data": {"错误": "手机号格式不正确", "建议": "请客户提供 11 位中国大陆手机号"},
            "card": None,
        }
    center = get_guardian_center(user_id)
    text = (
        f"绑定成功！已把{relation}{name}（{guardian['phone_masked']}）设为您的紧急联系人。"
        "今后发生高风险转账，会立即短信通知TA。"
    )
    return {
        "data": {"结果": text, "安全防护中心": center},
        "card": {"type": "GUARDIAN_CARD", "text": text, "card_data": center},
    }


# ======================== 注册表 ========================


def _fn(name: str, description: str, properties: dict, required: List[str] | None = None):
    params = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


TOOLS: Dict[str, Callable[[str, dict, dict], dict]] = {
    "get_account_overview": _t_account_overview,
    "get_spending_breakdown": _t_spending_breakdown,
    "get_recent_transactions": _t_recent_transactions,
    "get_wealth_holdings": _t_wealth_holdings,
    "get_risk_and_cashflow": _t_risk_and_cashflow,
    "get_internal_payees": _t_internal_payees,
    "show_bill_analysis": _t_show_bill,
    "show_bank_cards": _t_show_cards,
    "show_subscriptions": _t_show_subscriptions,
    "show_security_center": _t_show_security,
    "show_wealth_profile": _t_show_wealth_profile,
    "show_wealth_catalog": _t_show_wealth_catalog,
    "show_wealth_product_detail": _t_show_wealth_detail,
    "start_risk_assessment": _t_start_risk_assessment,
    "recommend_wealth_arrangement": _t_recommend_wealth,
    "stage_wealth_purchase": _t_stage_wealth_purchase,
    "stage_transfer": _t_stage_transfer,
    "cancel_subscription": _t_cancel_subscription,
    "stage_card_action": _t_stage_card_action,
    "bind_guardian": _t_bind_guardian,
    "set_up_scenario": _t_cross_scenario,
    "release_scenario": _t_release_scenario,
    "show_wish_reserves": _t_show_wish_reserves,
}


TOOL_SCHEMAS: List[dict] = [
    # ---- 只读：跨业务取数，模型据此自己组织回答 ----
    _fn(
        "get_account_overview",
        "取客户的账户全景：余额、锁定资金、卡片、风险等级与测评有效期、近90天月均支出、可投闲钱、资金周转特征。"
        "客户要求『总结我的财务状况』『帮我看看我的钱』这类综合分析时，先调用本工具。"
        "本工具的结果仅供你自己推理，不要把它当卡片讲给客户；客户要看固定视图请用 show_ 开头的工具。",
        {},
    ),
    _fn(
        "get_spending_breakdown",
        "取支出分类、异常交易和同期流水。客户没说时间就不要传年月，结果是近 30 天。"
        "客户点名了某月、上个月或这个月时，必须传入 year 和 month，不能再按近 30 天统计。",
        {
            "year": {"type": "integer", "description": "只有客户明确说了年份才传。只说几月时不要传，系统按今天判断：该月已过或就是本月用今年，还没到用去年。"},
            "month": {"type": "integer", "description": "客户点名的月份，1 到 12。没点名时间不要传。"},
        },
    ),
    _fn(
        "get_recent_transactions",
        "取最近的流水明细。",
        {"limit": {"type": "integer", "description": "取多少条，默认 10"}},
    ),
    _fn(
        "get_wealth_holdings",
        "取客户已持有的理财产品与金额。",
        {},
    ),
    _fn(
        "get_risk_and_cashflow",
        "取客户的风险画像与现金流画像：风险等级与类型、测评有效期、年龄职业、画像风格（含人话版说法）、"
        "月均支出、可投闲钱、周转特征、建议配置比例、已持理财。做理财建议前应先调用本工具。",
        {},
    ),
    _fn(
        "get_internal_payees",
        "取可以转账的本行储蓄卡。客户问能转给谁、对方卡号是多少时调用。转账本身仍要客户说出卡号。",
        {},
    ),
    # ---- 展示类：直接给客户看的固定视图 ----
    _fn(
        "show_bill_analysis",
        "账单分析卡片。客户想看账单、流水或某段时间的支出时使用。"
        "没说时间就不传年月，按近 30 天统计。说了某月、上个月或这个月，就传入对应年月。",
        {
            "year": {"type": "integer", "description": "只有客户明确说了哪一年才传。只说几月、今年几月时不要传年份。"},
            "month": {"type": "integer", "description": "客户点名的月份，1 到 12。没点名时间不要传。"},
        },
    ),
    _fn(
        "show_bank_cards",
        "银行卡与额度卡片。客户想看自己的卡、卡列表或转账限额时使用。",
        {},
    ),
    _fn(
        "show_subscriptions",
        "免密代扣与自动续费列表卡片。客户要查看代扣、自动续费或订阅时使用，包括「查看免密代扣与自动续费服务」。"
        "只是查看时不要改成取消确认单。",
        {},
    ),
    _fn("show_security_center", "安全防护卡片：紧急联系人、预警记录、防骗提示。客户想看防护或反诈情况时使用。", {}),
    _fn(
        "show_wealth_profile",
        "理财画像卡片。客户要看理财画像、风险等级或自己的理财情况时使用，包括「查看我的理财画像」。"
        "这是画像，不是产品推荐，也不是在售货架。",
        {},
    ),
    _fn(
        "show_wealth_catalog",
        "在售理财产品货架卡片。客户想了解或查看有哪些理财产品时使用。"
        "客户还没有一年内有效的风险测评时，不要调用本工具，改调用 start_risk_assessment。",
        {},
    ),
    _fn(
        "show_wealth_product_detail",
        "展示单只理财产品详情。风险等级高于客户的产品的会被合规拦截，此时应向客户如实说明不可投。",
        {
            "product_id": {
                "type": "string",
                "description": "产品 ID，必须取自货架或推荐结果，不要自己拼或套用示例",
            }
        },
        ["product_id"],
    ),
    # ---- 动作类：全部走确定性校验 ----
    _fn(
        "start_risk_assessment",
        "打开风险测评问卷，由客户在卡片里作答。客户要做或重做风险测评时使用。不要只说已经打开。",
        {},
    ),
    _fn(
        "recommend_wealth_arrangement",
        "按客户画像和这笔钱多久不用，生成理财配置方案。只在客户明确要理财建议时使用，买衣服、挑礼物不要用。"
        "客户还没说用钱时间也可以调用，工具会给出选择；已经说过就把 horizon 传进来。",
        {
            "horizon": {
                "type": "string",
                "description": "客户说的用钱时间，只能取：活钱（随时可能用）、七日（一周内不用）、"
                "月度（一个月内不用）、半年（半年内不用）、年度（一年以上不用）",
                "enum": ["活钱", "七日", "月度", "半年", "年度"],
            }
        },
    ),
    _fn(
        "stage_wealth_purchase",
        "生成理财申购确认单。金额低于起购、或产品风险高于客户等级时会被拦截。必须同时拿到产品与金额才调用。",
        {
            "product_id": {"type": "string", "description": "产品 ID"},
            "amount": {"type": "number", "description": "申购金额（元）"},
        },
        ["product_id", "amount"],
    ),
    _fn(
        "stage_transfer",
        "生成转账确认单。卡片状态、四维转账限额、反洗钱风控、亲情守护预警都在工具内判定，"
        "模型不得自行判断额度是否够。必须同时拿到收款储蓄卡卡号和金额才调用，payee 填卡号。"
        "只拿到姓名或手机号时不要调用，先追问储蓄卡卡号。信用卡不能作为收款卡。"
        "交易密码由客户在确认卡上输入，不要在对话里索要密码，也不要因为还没输密码而拒绝调用。",
        {
            "payee": {
                "type": "string",
                "description": "收款储蓄卡卡号，原样使用客户提供的号码，不要套用示例数字",
            },
            "amount": {"type": "number", "description": "转账金额（元）"},
            "notes": {"type": "string", "description": "转账附言/用途，客户没说再填「日常转账」"},
        },
        ["payee", "amount"],
    ),
    _fn(
        "cancel_subscription",
        "生成取消自动续费/代扣的确认单。客户通常只说商户名，这时直接传 merchant，"
        "必须原样使用客户说的那一项，不要替换成别的商户；知道 sub_id 也可以传。两者至少给一个。",
        {
            "sub_id": {"type": "string", "description": "订阅 ID，取自订阅列表返回结果（可选）"},
            "merchant": {
                "type": "string",
                "description": "商户名称，原样使用客户说的商户名",
            },
        },
    ),
    _fn(
        "stage_card_action",
        "生成卡片操作确认单：挂失 lock、冻结 freeze、解冻 unlock、调额 adjust_limit。调额需带新限额字段。",
        {
            "card_id": {"type": "string", "description": "卡片 ID"},
            "action": {
                "type": "string",
                "enum": ["lock", "freeze", "unlock", "adjust_limit"],
                "description": "lock=挂失, freeze=冻结, unlock=解冻, adjust_limit=调整转账限额",
            },
            "new_single_limit": {"type": "number", "description": "新的单笔限额（元），仅调额时填"},
            "new_daily_limit": {"type": "number", "description": "新的单日累计金额限额（元）"},
            "new_daily_count_limit": {"type": "integer", "description": "新的单日累计笔数"},
            "new_annual_limit": {"type": "number", "description": "新的年度累计限额（元）"},
        },
        ["card_id", "action"],
    ),
    _fn(
        "bind_guardian",
        "绑定或更新紧急联系人（亲情守护）。必须拿到姓名与手机号才调用。",
        {
            "name": {"type": "string", "description": "家人姓名"},
            "phone": {"type": "string", "description": "11 位手机号"},
            "relation": {"type": "string", "description": "关系，如 女儿 / 儿子 / 老伴"},
        },
        ["name", "phone"],
    ),
    _fn(
        "show_wish_reserves",
        "心愿备用金列表，**同时包含存续中和已完成的历史记录**（按状态标注）。"
        "客户问现在有哪些预留单、有几笔、以前存过什么、历史记录时都使用本工具。"
        "只展示已经确认并划款的记录；不要改成新建确认卡，也不要把未确认的确认单说成记录。"
        "若只有历史记录，也要如实列出，不要说成没有。",
        {},
    ),
    _fn(
        "set_up_scenario",
        "建立心愿备用金的确认卡。客户要把一笔钱单独存到某一天再用，并且用途、金额、用钱日都已经说过时使用。"
        "用钱日按客户的说法理解，下周三也可以，换算成日期后传入 target_date。缺任何一项就不要调用。"
        "受益人是可选项：客户没提就不要传、不要追问，也不要因此推迟生成确认卡。"
        "钱从活期划出，不要问付款卡。这里只出确认卡，客户输入密码后才写入数据库。赎回日以返回结果为准。",
        {
            "purpose": {
                "type": "string",
                "description": "资金用途。只照抄客户自己说过的用途，不要添加客户没提到的人名、日期、商品或数量",
            },
            "amount": {
                "type": "number",
                "description": "预留金额（元）。客户给区间时取上限",
            },
            "beneficiary": {
                "type": "string",
                "description": "受益人。只有客户明确说了给谁才填；没说就留空，不要从历史对话或示例里推断",
            },
            "target_date": {
                "type": "string",
                "description": "客户说的用钱日。可以先按今天换算成 YYYY-MM-DD 再传入。客户只说了「下周三」也要传，不要空着不调用。",
            },
        },
        ["purpose", "amount"],
    ),
    _fn(
        "release_scenario",
        "提前取回一笔存续中的心愿备用金，本金和已计提参考收益回到活期可用余额。"
        "客户说取消、取回、不要了、不预留了时调用，不要改成查看列表。"
        "客户说第几笔时传入 index，第一笔是 1，顺序与列表一致。只有一笔时可以不传。",
        {
            "scenario_id": {
                "type": "string",
                "description": "专款编号，格式 SC_YYYYMMDD_XXXXXX，取自列表返回结果；不知道就留空",
            },
            "notes": {"type": "string", "description": "用途或受益人里的关键词，用来匹配要取回的那一笔"},
            "index": {"type": "integer", "description": "第几笔，从 1 开始，顺序与心愿备用金列表一致"},
        },
    ),
]


def execute(name: str, user_id: str, args: Optional[dict], ctx: dict) -> dict:
    """执行工具。未知工具或工具内部异常都返回可读的 data，让模型能如实告知客户。"""
    impl = TOOLS.get(name)
    if not impl:
        return {"data": {"错误": f"未知工具 {name}"}, "card": None}
    try:
        result = impl(user_id, args or {}, ctx)
    except Exception as exc:  # pragma: no cover - 兜底，避免单点异常打断整轮对话
        return {
            "data": {
                "工具执行异常": f"{type(exc).__name__}: {exc}",
                "要求": "不要编造结果，如实告诉客户这一步暂时办不了，并建议稍后重试",
            },
            "card": None,
        }
    if not isinstance(result, dict):
        return {"data": {"结果": result}, "card": None}
    result.setdefault("card", None)
    return result
