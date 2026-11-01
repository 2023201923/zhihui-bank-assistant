from typing import List, Dict
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from database import (
    get_user_balance,
    execute_account_deduction,
    execute_wealth_purchase,
    apply_card_action,
    cancel_subscription,
    PENDING_ORDERS,
    SESSION_HISTORIES,
    save_risk_assessment,
    ensure_user_profile_schema,
    authenticate_user,
    verify_user_password,
    issue_auth_token,
    resolve_auth_token,
    revoke_auth_token,
    assess_transfer_risk,
    check_transfer_limits,
    resolve_payable_card,
    get_payment_sources,
    find_internal_payee,
    lookup_transfer_card,
    ensure_card_limit_schema,
    ensure_guardian_schema,
    ensure_scenario_schema,
    activate_scenario,
    settle_scenario,
    list_unread_scenario_notices,
    mark_scenario_notices_read,
    get_guardian,
    get_guardian_center,
    bind_guardian,
    notify_guardian,
    GUARDIAN_ALERT_LEVELS,
    SENSITIVE_ORDER_KINDS,
)
# 2026-10-15：不再按关键词分发到各业务 agent，改由模型驱动的统一助手编排工具。
from assistant import run_assistant
from scenario_service import business_days_after, start_scenario_worker
from agents.wealth_agent import build_personalized_recommendation
from agents.risk_survey import score_answers
from config import client, MODEL_NAME
from tts_service import synthesize as tts_synthesize, is_available as tts_available, unavailable_reason

app = FastAPI(title="AI Banking Agent - Multi-Domain")
ensure_user_profile_schema()
ensure_card_limit_schema()
ensure_guardian_schema()
ensure_scenario_schema()
start_scenario_worker()

class ChatRequest(BaseModel):
    user_id: str
    message: str
    token: str = ""


class ConfirmRequest(BaseModel):
    user_id: str
    order_id: str
    token: str = ""
    password: str = ""
    risk_ack: bool = False
    pay_card_id: str = ""


class LoginRequest(BaseModel):
    account: str
    password: str


class LogoutRequest(BaseModel):
    token: str = ""


def _require_login(user_id: str, token: str) -> None:
    bound = resolve_auth_token(token)
    if not bound or bound != user_id:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")


def _trim_history(history: List[dict]) -> List[dict]:
    if len(history) > 20:
        return history[-20:]
    return history


@app.post("/api/auth/login")
def login(req: LoginRequest):
    user = authenticate_user(req.account, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    token = issue_auth_token(user["user_id"])
    guardian = get_guardian(user["user_id"])
    return {
        "success": True,
        "token": token,
        "user_id": user["user_id"],
        "name": user["name"],
        "phone_masked": user["phone_masked"],
        "risk_level": user["risk_level"],
        "age_band": user.get("age_band") or "",
        "occupation": user.get("occupation") or "",
        # 2026-10-06：银发客群前端默认切到适老版界面
        "silver": bool(user.get("silver")),
        # 2026-10-08：亲情守护绑定状态（适老版安全防护）
        "guardian": guardian,
        "balance": get_user_balance(user["user_id"]),
    }


@app.post("/api/auth/logout")
def logout(req: LogoutRequest):
    revoke_auth_token(req.token)
    return {"success": True}


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest):
    _require_login(req.user_id, req.token)
    user_id = req.user_id
    history = SESSION_HISTORIES.setdefault(user_id, [])

    # 2026-10-15：模型自主决定调用哪些工具，金额与风控在工具内判定。
    result = run_assistant(user_id, req.message, history)

    SESSION_HISTORIES[user_id] = _trim_history(history)
    result["current_balance"] = get_user_balance(user_id)
    return result


@app.get("/api/scenario/notices")
def scenario_notices(user_id: str, token: str = ""):
    """小程序轮询未读的专款到账、用钱日提醒。时间以服务端当地日期为准。"""
    _require_login(user_id, token)
    return {"success": True, "notices": list_unread_scenario_notices(user_id)}


class NoticeReadRequest(BaseModel):
    user_id: str
    token: str = ""
    notice_ids: List[str] = []


@app.post("/api/scenario/notices/read")
def scenario_notices_read(req: NoticeReadRequest):
    _require_login(req.user_id, req.token)
    mark_scenario_notices_read(req.user_id, req.notice_ids or [])
    return {"success": True}


def _complete_order(user_id: str, order: dict) -> dict:
    kind = order.get("kind", "transfer")

    if kind == "transfer":
        ok = execute_account_deduction(
            user_id,
            order["amount"],
            order["payee"],
            order.get("notes", "无"),
            order.get("pay_card_id", ""),
            order.get("payee_user_id"),
        )
        if not ok:
            return {"success": False, "message": "账户余额不足，转账未成功"}
        pay_from = f"{order.get('pay_card_type') or '储蓄卡'} {order.get('pay_card_id')}" if order.get("pay_card_id") else "活期账户"
        if order.get("transfer_channel") == "internal":
            msg = (
                f"转账成功！已从{pay_from}向本行客户 {order['payee']} 汇出 ¥{order['amount']:.2f}，"
                f"对方活期已实时入账。"
            )
        else:
            msg = f"转账成功！已从{pay_from}向 {order['payee']} 汇出 ¥{order['amount']:.2f}。"

    elif kind == "wealth":
        ok = execute_wealth_purchase(
            user_id,
            order["amount"],
            order["product_name"],
            order.get("pay_card_id", ""),
        )
        if not ok:
            return {"success": False, "message": "余额不足，申购未成功"}
        pay_from = f"{order.get('pay_card_type') or '储蓄卡'} {order.get('pay_card_id')}" if order.get("pay_card_id") else "活期账户"
        msg = f"申购成功！已用{pay_from}买入 {order['product_name']} ¥{order['amount']:.2f}。"

    elif kind == "card":
        ok = apply_card_action(
            order["card_id"],
            order["action"],
            order.get("new_daily_limit"),
            order.get("new_single_limit"),
            order.get("new_daily_count_limit"),
            order.get("new_annual_limit"),
        )
        if not ok:
            return {"success": False, "message": "卡片操作失败：请检查卡片状态或限额是否在允许范围内"}
        if order["action"] == "adjust_limit":
            msg = f"限额调整成功：{order['card_id']} 已更新为 {order.get('new_limit_text') or '新额度'}。"
        else:
            msg = f"卡片 {order['card_id']} 操作已生效：{order['action']}。"

    elif kind == "sub_cancel":
        ok = cancel_subscription(order["sub_id"])
        if not ok:
            return {"success": False, "message": "取消订阅失败，可能已取消或不存在"}
        msg = f"已取消 {order['merchant']} 的自动续费/代扣。"

    elif kind == "cross_lock":
        plan = order.get("plan") or {}
        ok, reason = activate_scenario(user_id, plan) if plan else (False, "预留单缺少方案数据")
        if not ok:
            return {"success": False, "message": reason}
        msg = (
            f"已为「{plan['notes']}」存入心愿备用金 ¥{float(plan['amount']):.2f}，"
            f"底层为{plan['product_name']}。用钱日 {plan['target_date']}，"
            f"{plan['auto_redeem_date']} 会回到可用余额，并在小程序提醒您。"
            "参考收益不承诺。"
        )

    elif kind == "cross_release":
        settled = settle_scenario(order["scenario_id"], "FORCE_REDEEM", business_days_after)
        if not settled:
            return {"success": False, "message": "这笔心愿备用金还没在存续中，暂时没法取回"}
        msg = settled["notice_content"]

    else:
        return {"success": False, "message": "未知订单类型"}

    order["status"] = "COMPLETED"
    history = SESSION_HISTORIES.setdefault(user_id, [])
    history.append({"role": "assistant", "content": msg})
    return {
        "success": True,
        "message": msg,
        "current_balance": get_user_balance(user_id),
        "order_id": order["order_id"],
    }


@app.post("/api/order/cancel")
def cancel_order(req: ConfirmRequest):
    _require_login(req.user_id, req.token)
    order = PENDING_ORDERS.get(req.order_id)
    if not order or order.get("status") != "PENDING":
        raise HTTPException(status_code=400, detail="订单不存在或已被处理")
    if order.get("user_id") and order["user_id"] != req.user_id:
        raise HTTPException(status_code=403, detail="无权操作该订单")
    order["status"] = "CANCELLED"
    return {"success": True, "message": "已取消本次操作"}


@app.post("/api/transfer/confirm")
@app.post("/api/order/confirm")
def confirm_order(req: ConfirmRequest):
    _require_login(req.user_id, req.token)
    order = PENDING_ORDERS.get(req.order_id)
    if not order or order.get("status") != "PENDING":
        raise HTTPException(status_code=400, detail="订单不存在或已被处理")
    if order.get("user_id") and order["user_id"] != req.user_id:
        raise HTTPException(status_code=403, detail="无权操作该订单")

    kind = order.get("kind", "transfer")
    fraud = None
    # 2026-10-06：资金类订单，以及显式标记 require_password 的订单（如调整转账限额），都要校验交易密码
    needs_password = kind in SENSITIVE_ORDER_KINDS or bool(order.get("require_password"))
    if kind in SENSITIVE_ORDER_KINDS:
        requested_card = req.pay_card_id or order.get("pay_card_id") or ""
        source = resolve_payable_card(req.user_id, requested_card)
        if not source:
            blocked = next(
                (s for s in get_payment_sources(req.user_id) if s["card_id"] == requested_card),
                None,
            )
            reason = (blocked or {}).get("reason") or "请选择一张状态正常的储蓄卡"
            return {"success": False, "message": f"付款卡不可用：{reason}"}
        order["pay_card_id"] = source["card_id"]
        order["pay_card_type"] = source["card_type"]

        if kind == "transfer":
            # 2026-10-06 新增：确认前再次做「卡片状态 + 余额 + 四维限额」校验，防止绕过前端
            limits = check_transfer_limits(
                req.user_id, source["card_id"], float(order.get("amount") or 0)
            )
            if not limits["ok"]:
                return {
                    "success": False,
                    "message": limits["message"],
                    "limit_block": limits,
                    "quick_actions": limits.get("quick_actions") or [],
                }
            payee_card = order.get("payee_card") or ""
            found = lookup_transfer_card(req.user_id, payee_card) if payee_card else None
            if found and found.get("user_id"):
                order["payee_user_id"] = found["user_id"]
                order["payee"] = found["name"]
                order["transfer_channel"] = "internal"
            elif payee_card:
                order["payee_user_id"] = None
                order["payee"] = payee_card
                order["transfer_channel"] = "external"
            else:
                internal = find_internal_payee(req.user_id, order.get("payee", ""))
                if internal:
                    order["payee_user_id"] = internal["user_id"]
                    order["payee"] = internal["name"]
                    order["transfer_channel"] = "internal"
                else:
                    order["payee_user_id"] = None
                    order["transfer_channel"] = "external"

    if needs_password and not verify_user_password(req.user_id, req.password):
        return {"success": False, "message": "交易密码错误", "password_error": True}

    if kind == "transfer":
        fraud = assess_transfer_risk(req.user_id, float(order["amount"]), order.get("payee", ""))
        if fraud["require_ack"] and not req.risk_ack:
            return {
                "success": False,
                "message": "请先确认已阅读诈骗风险提示",
                "fraud": fraud,
            }
    result = _complete_order(req.user_id, order)
    if fraud and result.get("success") and fraud["level"] != "normal":
        result["message"] = f"{result['message']} 本次转账已记录风险提示。"
    # 2026-10-08 新增：高风险转账完成后，向绑定家人推送预警（消费类订单不触发）
    if (
        kind == "transfer"
        and result.get("success")
        and fraud
        and fraud.get("level") in GUARDIAN_ALERT_LEVELS
        and not order.get("guardian_notified")
    ):
        notice = notify_guardian(
            req.user_id,
            fraud["level"],
            fraud.get("title") or "高风险转账",
            f"向「{order.get('payee')}」转账 ¥{float(order.get('amount') or 0):.2f}",
        )
        order["guardian_notified"] = bool(notice.get("sent"))
        if notice.get("sent"):
            result["guardian_alert"] = notice
            result["message"] = (
                f"{result['message']} 已同步通知紧急联系人{notice['guardian']['name']}。"
            )
    return result


class SurveyRequest(BaseModel):
    user_id: str
    answers: dict
    occupation: str = ""
    token: str = ""


@app.post("/api/risk/survey")
def submit_risk_survey(req: SurveyRequest):
    _require_login(req.user_id, req.token)
    try:
        scored = score_answers(req.answers or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    occupation = (req.occupation or "").strip() or "其他"
    save_risk_assessment(
        req.user_id,
        scored["risk_level"],
        scored["risk_type"],
        scored["score"],
        scored.get("age_band") or "",
        occupation,
        req.answers,
    )
    history = SESSION_HISTORIES.setdefault(req.user_id, [])
    recommendation = build_personalized_recommendation(req.user_id, history)
    SESSION_HISTORIES[req.user_id] = _trim_history(history)
    return {
        "success": True,
        "score": scored["score"],
        "risk_type": scored["risk_type"],
        "risk_level": scored["risk_level"],
        "meaning": scored["meaning"],
        "suitable_products": scored["suitable_products"],
        "message": (
            f"评估完成：{scored['risk_type']}（{scored['risk_level']}），得分 {scored['score']}。"
            f"{scored['meaning']}。适合产品风险等级：{scored['suitable_products']}。"
        ),
        "recommendation": recommendation,
    }


@app.get("/api/user/balance")
def query_balance(user_id: str, token: str = ""):
    _require_login(user_id, token)
    return {
        "user_id": user_id,
        "balance": get_user_balance(user_id),
    }


# ==== 2026-10-08 新增：亲情反诈守护 + 语音播报 ====
class GuardianBindRequest(BaseModel):
    user_id: str
    token: str = ""
    name: str = ""
    phone: str = ""
    relation: str = "亲属"


@app.get("/api/guardian/center")
def guardian_center(user_id: str, token: str = ""):
    """适老版安全防护卡：绑定状态 + 预警记录 + 反诈提示。"""
    _require_login(user_id, token)
    return {"success": True, "data": get_guardian_center(user_id)}


@app.post("/api/guardian/bind")
def guardian_bind(req: GuardianBindRequest):
    """绑定 / 更新紧急联系人（子女）。"""
    _require_login(req.user_id, req.token)
    guardian = bind_guardian(req.user_id, req.name, req.phone, req.relation)
    if not guardian:
        raise HTTPException(status_code=400, detail="请填写 11 位有效的手机号")
    return {
        "success": True,
        "message": (
            f"已把{guardian['relation']}{guardian['name']}（{guardian['phone_masked']}）"
            "设为紧急联系人，高风险转账会立即通知TA。"
        ),
        "data": get_guardian_center(req.user_id),
    }


@app.get("/api/tts/status")
def tts_status():
    """小程序启动时探测语音播报是否可用。"""
    return {"available": tts_available(), "reason": unavailable_reason()}


@app.get("/api/tts")
def tts_endpoint(text: str = ""):
    """把一段文字合成为 wav。

    微信播放器会带 Range 请求。FileResponse 会回 206，wav 被截断后就没有声音。
    这里始终返回完整文件。
    """
    content = (text or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="缺少待播报文本")
    try:
        path = tts_synthesize(content)
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        raise HTTPException(status_code=503, detail=f"语音合成不可用：{exc}")
    with open(path, "rb") as audio_file:
        payload = audio_file.read()
    return Response(
        content=payload,
        media_type="audio/wav",
        headers={
            "Accept-Ranges": "none",
            "Cache-Control": "no-store",
            "Content-Length": str(len(payload)),
        },
    )
