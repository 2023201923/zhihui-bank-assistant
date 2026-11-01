"""统一智能助手（语义驱动 + 统一语义守卫版）。

每句客户消息优先交由主模型自主决策。
若主模型仅输出纯文本，由轻量语义守卫补查看类卡片，或改写虚假的「已办理」说法。
守卫不能代发办理确认单。客户补上用钱日且用途、金额已经说过时，再补一张心愿备用金确认卡。转账收款人和金额已经说过、主模型却只回文字时，再补一张转账确认卡。
"""

import json
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from config import CHECK_MODEL_NAME, MODEL_EXTRA_BODY, MODEL_NAME, client
from database import PENDING_ORDERS
import tools as tool_layer

MAX_STEPS = 5
MAX_HISTORY_TURNS = 10

# 最近一张「待客户在卡片上确认」的办理卡。客户用「是 / 确认 / 好的」回复时，
# 把这张卡重新发到底部，而不是让模型去猜「已确认」或让客户往上翻。
LAST_PENDING_CARD: Dict[str, dict] = {}

_AFFIRM = re.compile(
    r"^(是|是的|对|对的|好|好的|可以|行|确认|确定|没问题|嗯|ok|okay)[。！!，,.\s]*$",
    re.I,
)


def _is_affirmation(message: str) -> bool:
    return bool(_AFFIRM.match(str(message or "").strip()))

ASSISTANT_PROMPT = """你是智汇银行的专业客户经理，在微信小程序中通过对话协助客户办理业务。

【核心原则】
1. 界面完全依赖工具产出的「卡片」完成交互。
   凡是涉及展示数据、出具问卷、生成办理确认单，必须调用对应的工具生成卡片。绝对禁止在未调工具的情况下在文本里宣称「已为您办理/已成功生成/已为您展示」。
2. 要素不全时，用专业、有亲和力的口吻向客户追问缺失项，一次只问一个核心要素；信息齐全后立即调用对应工具。
3. 已经出示办理确认卡后，不要再问「是否确认」这类需要客户用文字回答的问题。
   客户回复「是 / 确认 / 好的」也**不能**算完成办理——必须在卡片上点击并输入交易密码。
   这时只提醒客户去看确认卡操作，不要声称「已确认」「已办理」。
4. 区分日常消费与银行业务：
   - 挑选商品、问衣服款式属于日常消费，如实说明不提供商品导购，严禁调用理财工具。
   - 客户若要「单独存一笔钱到某天使用/备用金/送礼储备」，属于「心愿备用金」。钱从活期划出，不要问哪张卡。

【业务与工具指引】
· 账单/流水：查账单或某月花费调用 show_bill_analysis。客户只说了几月、没说年份时不要传 year，由系统按今天判断。客户明确说了今年、去年或某一年，才传入年份。纯分析消费趋势调用 get_spending_breakdown 后文字解答。
· 转账：必须拿到对方储蓄卡卡号和金额，再调用 stage_transfer，payee 填卡号。只说了姓名或手机号还不够，追问储蓄卡卡号。问本行有哪些收款卡，用 get_internal_payees。交易密码只在确认卡上输入，对话里不要索要密码，也不要因为还没输密码就拒绝出确认卡。
· 银行卡：查看卡片列表与限额调用 show_bank_cards；挂失、冻结、解冻、调额在明确卡片后调用 stage_card_action。
· 订阅管理：查看免密扣费/自动续费列表调用 show_subscriptions；明确取消具体商户调用 cancel_subscription。
· 安全防护：查看安全防护状态调用 show_security_center；绑定紧急联系人调用 bind_guardian。
· 理财业务：查看持仓画像调用 show_wealth_profile；一年内无有效测评时，了解产品或索取建议必须先调用 start_risk_assessment 打开问卷；测评有效后，查在售产品用 show_wealth_catalog，查单只产品用 show_wealth_product_detail，要配置建议用 recommend_wealth_arrangement。产品和金额都确定了要买，调用 stage_wealth_purchase。
· 心愿备用金：统一称呼为「心愿备用金」。用途、金额、用钱日都齐了，调用 set_up_scenario，参数是 purpose、amount、target_date。用钱日按客户原话换算，不要编一个客户没说过的日期。缺哪样只问哪样；受益人属于可选项，客户没提就不要传、也不要追问，更不要因此推迟生成确认卡。赎回日只照抄工具返回的结果。只是查看有哪些时调用 show_wish_reserves。客户要取消、取回、不要了，调用 release_scenario，不要改成列表。说第几笔就传 index，第一笔是 1，顺序和列表一致。

【表达要求】
微信小程序不支持 Markdown，严禁输出 **、##、`、表格或列表符号。回复简洁精炼（3句以内），客观可信，不承诺保本与保收益。
"""

GUARDRAIL_SYSTEM_PROMPT = """你是一个银行业务一致性与合规审核员。
主模型在最新一轮对话中没有调用任何卡片工具，仅输出了一段纯文本回复。
请根据上下文判断主模型的纯文本回复是否存在以下问题，并严格返回 JSON 格式：

1. MISSING_TOOL（漏调工具）：客户明确要求查看某项固定卡片视图（账单、银行卡列表、订阅列表、安全中心、理财画像、理财货架、心愿备用金列表、风险测评问卷），主模型却只用文字回答。
2. FALSE_PROMISE（虚假承诺）：主模型没有生成确认卡片，却宣称已经办理、已经预留、已经冻结、已经挂失或已经生成确认单。
3. PASS（正常放行）：主模型正在追问缺失要素、拒绝非银行业务，或者在做分析解答。

只能补调查看类工具，不要补调任何办理确认单：
show_bill_analysis, show_bank_cards, show_subscriptions, show_security_center,
show_wealth_profile, show_wealth_catalog, show_wealth_product_detail,
start_risk_assessment, show_wish_reserves

账单只在客户明确说了年份时才在 tool_args 里传 year。只说了几月就不要传 year。

只返回这一段 JSON，不要 markdown，不要解释：
{"verdict":"PASS","tool_name":null,"tool_args":{},"revised_reply":null}

verdict 只能是 PASS、MISSING_TOOL、FALSE_PROMISE。
MISSING_TOOL 时 tool_name 必须是上面列出的一个工具，tool_args 是对象。
FALSE_PROMISE 时 tool_name 为 null，revised_reply 用一句自然的话说明这一步还没有生成确认卡、所以没有执行。上下文里还缺信息就只问缺的那一项；信息已经齐了就请客户再确认一次，不要声称已经办成。
改写的回复里绝对不能向客户索要交易密码——密码只在确认卡上输入，对话里永远不要问。
其他情况 tool_name 和 revised_reply 都是 null。"""


# 守卫只能补查看类卡片。办理确认单仍由主模型决定，避免检查模型编造金额、日期或收款人。
_GUARD_TOOLS = {
    "show_bill_analysis",
    "show_bank_cards",
    "show_subscriptions",
    "show_security_center",
    "show_wealth_profile",
    "show_wealth_catalog",
    "show_wealth_product_detail",
    "start_risk_assessment",
    "show_wish_reserves",
}


def _trim(messages: List[dict]) -> List[dict]:
    """保留系统提示词与最近轮次上下文。"""
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return system + rest[-MAX_HISTORY_TURNS * 2 :]


def _tool_call_payload(call) -> dict:
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.function.name,
            "arguments": call.function.arguments or "{}",
        },
    }


def _text_result(text: str, history: list) -> dict:
    history.append({"role": "assistant", "content": text})
    return {"type": "TEXT", "text": text, "card_data": None}


def _card_history_note(card: dict) -> str:
    """提取卡片核心摘要写入历史，保障跨轮指代消解。"""
    cd = card.get("card_data") or {}
    kind = cd.get("card_kind")
    if kind == "cross_lock":
        return (
            f"[系统记录：已展示心愿备用金确认卡，用途「{cd.get('notes') or '未注明'}」，"
            f"金额 ¥{cd.get('amount')}，受益人 {cd.get('beneficiary') or '本人'}，"
            f"用钱日 {cd.get('target_date') or '未定'}，赎回日 {cd.get('auto_redeem_date') or '未定'}。"
            "状态：待客户点击确认]"
        )
    if kind == "cross_release":
        return f"[系统记录：已展示取回心愿备用金确认卡，用途「{cd.get('notes') or '未注明'}」，待客户确认]"
    if kind == "wealth":
        return f"[系统记录：已展示理财申购确认卡，产品 {cd.get('product_name')}，金额 ¥{cd.get('amount')}，待客户确认]"
    if kind in ("transfer", "card", "sub_cancel"):
        return f"[系统记录：已展示{kind}操作确认卡，待客户点击确认]"
    return card.get("text") or "[系统记录：已展示业务卡片]"


def sanitize_outgoing(text: str) -> str:
    """移除 Markdown 标记，保障小程序原生展示效果。"""
    if not text:
        return ""
    cleaned = text
    for token in ("**", "__", "`"):
        cleaned = cleaned.replace(token, "")
    lines = []
    for raw in cleaned.split("\n"):
        line = raw.rstrip()
        stripped = line.lstrip()
        if stripped.startswith("#"):
            line = stripped.lstrip("#").strip()
        elif stripped[:2] in ("- ", "* ", "+ "):
            line = stripped[2:].strip()
            if line:
                line = f"· {line}"
        lines.append(line)
    return "\n".join(lines).strip()


def _parse_guard_payload(content: str) -> Tuple[str, Optional[str], Dict[str, Any], Optional[str]]:
    text = (content or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("guardrail response has no JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("guardrail response is not an object")
    verdict = data.get("verdict") or "PASS"
    if verdict not in ("PASS", "MISSING_TOOL", "FALSE_PROMISE"):
        verdict = "PASS"
    tool_name = data.get("tool_name")
    tool_args = data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {}
    if tool_name not in _GUARD_TOOLS:
        tool_name = None
        if verdict == "MISSING_TOOL":
            verdict = "PASS"
    revised_reply = data.get("revised_reply")
    if not isinstance(revised_reply, str) or not revised_reply.strip():
        revised_reply = None
    return verdict, tool_name, tool_args, revised_reply


def run_semantic_guardrail(
    user_message: str,
    draft_reply: str,
    history: List[dict]
) -> Tuple[str, Optional[str], Dict[str, Any], Optional[str]]:
    """主模型只有文字时，用检查模型判断要不要补一张查看卡片。检查模型失败时改用主模型。"""
    prompt = (
        f"客户消息：{user_message}\n"
        f"最近上下文历史：{json.dumps(history[-3:], ensure_ascii=False)}\n"
        f"主模型文本草稿：{draft_reply}"
    )
    models = []
    for name in (CHECK_MODEL_NAME, MODEL_NAME):
        if name and name not in models:
            models.append(name)
    for model in models:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": GUARDRAIL_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                extra_body=MODEL_EXTRA_BODY,
            )
            content = response.choices[0].message.content or ""
            return _parse_guard_payload(content)
        except Exception:
            continue
    return "PASS", None, {}, None


def _card_result(card: dict, history: list, user_id: str = "") -> dict:
    history.append({"role": "assistant", "content": _card_history_note(card)})
    # 记住最近一张办理卡，供客户下一轮用「是 / 确认」时重新展示
    if user_id and card.get("type") == "ACTION_CARD" and card.get("card_data"):
        LAST_PENDING_CARD[user_id] = card
    return {
        "type": card.get("type") or "TEXT",
        "text": card.get("text") or "",
        "card_data": card.get("card_data"),
    }


def run_assistant(user_id: str, message: str, history: list) -> dict:
    """主调度循环：主模型决策 -> 工具执行与卡片截断 -> 统一语义守卫兜底。"""
    history.append({"role": "user", "content": message})
    ctx: dict = {"scratch": [], "history": history, "message": message}

    # 上一轮刚出示过办理确认卡，客户回「是 / 确认 / 好的」：
    # 聊天文字无法代替卡片确认，但也不应该让客户往上翻去找卡片——
    # 直接把那张待确认卡片重新发到底部。
    pending = LAST_PENDING_CARD.get(user_id)
    if pending and _is_affirmation(message):
        order_id = (pending.get("card_data") or {}).get("order_id") or ""
        entry = PENDING_ORDERS.get(order_id) or {}
        if order_id and entry.get("status") == "PENDING":
            history.append(
                {
                    "role": "assistant",
                    "content": "[系统记录：重新展示待确认卡片，等待客户在卡片上输入交易密码]",
                }
            )
            return {
                "type": pending.get("type") or "ACTION_CARD",
                "text": "请在下方确认卡上核对信息、点击确认并输入交易密码。聊天里回复无法代替卡片确认。",
                "card_data": pending.get("card_data"),
            }

    purchase_topic = tool_layer.topic_is_purchase_not_wealth(ctx)
    wealth_tools = {
        "show_wealth_catalog",
        "show_wealth_product_detail",
        "recommend_wealth_arrangement",
        "stage_wealth_purchase",
    }
    active_tools = [
        schema for schema in tool_layer.TOOL_SCHEMAS
        if not (purchase_topic and schema["function"]["name"] in wealth_tools)
    ]

    today = date.today()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    weekday_str = weekdays[today.weekday()]
    time_anchor = (
        f"\n今天是{today.year}年{today.month}月{today.day}日，星期{weekday_str}。"
        "相对时间（如明天、下周三、这个月、上个月）请严格结合此基准换算为具体时间。"
    )

    messages: List[dict] = _trim(
        [{"role": "system", "content": ASSISTANT_PROMPT + time_anchor}] + history
    )

    cards: List[dict] = []
    final_text = ""

    # ================= 1. 主模型自主决策循环 =================
    for step in range(MAX_STEPS):
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=active_tools,
            tool_choice="auto",
            temperature=0.1,
            extra_body=MODEL_EXTRA_BODY,
        )
        choice = response.choices[0].message

        # 主模型决定给出文本回复，停止循环
        if not choice.tool_calls:
            final_text = (choice.content or "").strip()
            break

        messages.append(
            {
                "role": "assistant",
                "content": choice.content or "",
                "tool_calls": [_tool_call_payload(c) for c in choice.tool_calls],
            }
        )

        for call in choice.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = tool_layer.execute(call.function.name, user_id, args, ctx)
            if result.get("card"):
                cards.append(result["card"])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result.get("data"), ensure_ascii=False, default=str),
                }
            )

        # 只要产生卡片，立即截断返回，不再多跑一轮复述
        if cards:
            break

    # ================= 2. 正常卡片返回 =================
    # 取消、取回心愿备用金时，不接受模型改成的列表卡。
    release = tool_layer.prepare_wish_release(user_id, message, ctx)
    if release and release.get("card"):
        return _card_result(release["card"], history, user_id)
    if release and release.get("ask"):
        return _text_result(release["ask"], history)

    # 客户一句话给齐用途/金额/用钱日时，直接出确认卡；
    # 缺任何一项都不在这里兜底，交回主模型去追问。
    if not cards:
        wish = tool_layer.prepare_wish_create(user_id, message, ctx)
        if wish and wish.get("card"):
            return _card_result(wish["card"], history, user_id)

    if cards:
        return _card_result(cards[-1], history, user_id)

    # ================= 3. 语义守卫（仅在无卡片且有文本时介入） =================
    verdict, tool_name, tool_args, revised_reply = "PASS", None, {}, None
    if final_text:
        verdict, tool_name, tool_args, revised_reply = run_semantic_guardrail(
            user_message=message,
            draft_reply=final_text,
            history=history
        )

        # 3.1 守卫判定漏调查看工具：只执行白名单内的展示工具
        if verdict == "MISSING_TOOL" and tool_name in _GUARD_TOOLS:
            result = tool_layer.execute(tool_name, user_id, tool_args, ctx)
            repaired_card = result.get("card")
            if repaired_card:
                return _card_result(repaired_card, history, user_id)

    # 客户刚补上用钱日，且前面已经说了用途和金额：直接出确认卡，不让检查模型另编日期
    if tool_layer.scenario_followup_ready(ctx):
        result = tool_layer.execute("set_up_scenario", user_id, {}, ctx)
        if result.get("card"):
            return _card_result(result["card"], history, user_id)

    # 收款人和金额都已经说过，主模型却在文字里要密码：直接出转账确认卡
    transfer = tool_layer.prepare_transfer_followup(user_id, ctx)
    if transfer and transfer.get("card"):
        return _card_result(transfer["card"], history, user_id)
    if transfer and transfer.get("ask"):
        return _text_result(transfer["ask"], history)

    if final_text:
        # 3.2 守卫判定虚假承诺：使用检查模型改写后的话术
        if verdict == "FALSE_PROMISE" and revised_reply:
            return _text_result(sanitize_outgoing(revised_reply), history)

        # 3.3 正常放行（正当追问要素或常规解答）
        return _text_result(sanitize_outgoing(final_text), history)

    # 兜底返回
    return _text_result(
        "您好，请问有什么可以帮您？我可以为您查询账单、管理银行卡、设置心愿备用金或提供理财咨询服务。",
        history,
    )
