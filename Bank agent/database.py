import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from datetime import date, datetime
from typing import Dict, List, Optional

RISK_LABELS = {
    "R1": "R1低风险",
    "R2": "R2中低风险",
    "R3": "R3中风险",
    "R4": "R4中高风险",
    "R5": "R5高风险",
}

# 使用基于当前文件的绝对路径，防止命令行不同工作目录导致生成多个不同的 bank.db
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bank.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

# 内存中的待确认订单字典与对话历史
PENDING_ORDERS: Dict[str, dict] = {}
SESSION_HISTORIES: Dict[str, List[dict]] = {}
AUTH_TOKENS: Dict[str, str] = {}

PASSWORD_SALT = "zhihui-bank-demo-v1"
# 单笔达到该金额视为大额转账，用于连续转账诈骗识别
LARGE_TRANSFER_AMOUNT = 2000.0
# 资金类操作确认时必须校验交易密码
SENSITIVE_ORDER_KINDS = {"transfer", "wealth", "cross_lock"}

# ==== 2026-10-06 新增：转账限额与反洗钱风控阈值（参考主流银行零售账户）====
# 储蓄卡默认限额：单笔 / 日累计金额 / 日累计笔数 / 年累计金额
DEFAULT_DEBIT_LIMITS = {
    "single_limit": 20000.0,
    "daily_limit": 20000.0,
    "daily_count_limit": 10,
    "annual_limit": 2000000.0,
}
# 允许自助调整的上限，防止把限额调到不合理区间
MAX_SINGLE_LIMIT = 500000.0
MAX_DAILY_LIMIT = 1000000.0
MAX_DAILY_COUNT_LIMIT = 100
MAX_ANNUAL_LIMIT = 50000000.0
# 本笔转出后剩余额度低于该比例 → 临界预警
NEAR_LIMIT_RATIO = 0.2
# 当日转账笔数达到该值 → 高频预警
HIGH_FREQUENCY_COUNT = 5
# 夜间（含起始、不含结束）大额转账预警时段
NIGHT_START_HOUR, NIGHT_END_HOUR = 0, 6
# 整数大额（如 10000、20000、50000）预警起点
ROUND_AMOUNT_MIN = 10000.0

# 银发客群年龄段（用于适老版界面与理财画像）
SILVER_AGE_BANDS = ("51-60", "60岁以上", "61-65", "65+")

# ==== 2026-10-08 新增：亲情反诈守护（紧急联系人 / 子女预警通知）====
# 紧急联系人关系选项（适老版语音化交互用）
GUARDIAN_RELATIONS = ("子女", "父母", "配偶", "亲属", "其他")
# 触发「向紧急联系人推送预警」的风险等级
GUARDIAN_ALERT_LEVELS = ("high",)


def is_silver_age(age_band) -> bool:
    """2026-10-06 新增：判断是否属于银发客群（用于适老版默认开启）。"""
    text = str(age_band or "").strip()
    if not text:
        return False
    if text in SILVER_AGE_BANDS:
        return True
    return any(token in text for token in ("60岁", "61-65", "65+", "51-60", "退休"))


def hash_password(password: str) -> str:
    raw = f"{PASSWORD_SALT}:{password}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def mask_phone(phone: str) -> str:
    text = str(phone or "")
    if len(text) < 7:
        return text
    return f"{text[:3]}****{text[-4:]}"


def _fmt_money(value) -> str:
    """金额展示：None 视为「不限」。"""
    if value is None:
        return "不限"
    try:
        return f"¥{float(value):,.2f}"
    except (TypeError, ValueError):
        return "不限"


def _ceil_to(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return math.ceil(float(value) / step) * step

def get_user_balance(user_id: str = "user_001") -> float:
    """从 bank.db 查询真实活期余额"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM accounts WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return float(row["balance"]) if row else 0.0

def get_locked_funds(user_id: str = "user_001") -> float:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT locked_funds FROM accounts WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return float(row["locked_funds"]) if row else 0.0

def _plain_display(text: str) -> str:
    if not text:
        return text
    return str(text).replace("。", "")


def _normalize_rules(rules: dict) -> dict:
    if not rules:
        return {}
    keys = (
        "sale_time",
        "buy_fee",
        "confirm_rule",
        "cancel_rule",
        "redeem_fee",
        "redeem_way",
        "arrival",
        "closed_note",
    )
    normalized = dict(rules)
    for key in keys:
        if key in normalized and normalized[key]:
            normalized[key] = _plain_display(normalized[key])
    return normalized


def _wealth_row_to_dict(row) -> dict:
    risk = row["risk_level"]
    detail = {}
    if "detail_json" in row.keys() and row["detail_json"]:
        detail = json.loads(row["detail_json"])
    performance = detail.get("performance") or {}
    rules = detail.get("rules") or {}
    spotlight = detail.get("spotlight") or {}
    return {
        "product_id": row["product_id"],
        "name": row["name"],
        "category": row["category"],
        "sub_category": row["sub_category"],
        "risk_level": risk,
        "risk_label": RISK_LABELS.get(risk, risk),
        "term_days": row["term_days"],
        "term_label": row["term_label"],
        "term_sub_label": row["term_sub_label"],
        "yield_pct": f"{row['expected_yield'] * 100:.2f}%",
        "yield_label": row["yield_label"],
        "expected_yield": row["expected_yield"],
        "min_amount": row["min_amount"],
        "benchmark_desc": _plain_display(row["benchmark_desc"]),
        "asset_direction": _plain_display(row["asset_direction"]),
        "tags": json.loads(row["tags_json"]),
        "product_code": detail.get("product_code", ""),
        "slogan": detail.get("slogan", ""),
        "aliases": detail.get("aliases", []),
        # 2026-10-15 新增：理财经理对话用的人话字段（when/yield/asset/fit/note）
        "plain": detail.get("plain", {}),
        "highlights": detail.get("highlights", []),
        "buyer_count": detail.get("buyer_count", ""),
        "performance": performance,
        "rules": _normalize_rules(rules),
        "issuer": detail.get("issuer", ""),
        "allocations": detail.get("allocations", []),
        "spotlight": spotlight,
        "faqs": detail.get("faqs", []),
    }


def get_wealth_products_grouped(max_risk_level: str = "R3") -> list:
    """获取不超过客户风险等级的在售理财，按持有期限从短到长排列。

    未传入有效等级（未测评）时不按默认等级放行，返回空列表。
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM wealth_products WHERE status = 'ON_SALE' ORDER BY term_days")
    rows = cursor.fetchall()
    conn.close()

    level = str(max_risk_level or "")
    if not (level.startswith("R") and len(level) > 1 and level[1].isdigit()):
        return []
    max_num = int(level[1])
    result = []
    for row in rows:
        risk_num = int(row["risk_level"][1])
        if risk_num <= max_num:
            result.append(_wealth_row_to_dict(row))
    return result


def get_product_by_id(product_id: str) -> Optional[dict]:
    if not product_id:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM wealth_products WHERE product_id = ?", (product_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return _wealth_row_to_dict(row)


def get_user_risk_level(user_id: str = "user_001") -> str:
    """只返回库里的风险等级。未测评时为空，不填默认等级。"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT risk_level FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row["risk_level"]:
        return str(row["risk_level"])
    return ""


def ensure_user_profile_schema(conn=None):
    own = conn is None
    if own:
        conn = get_connection()
    cursor = conn.cursor()
    cols = {r[1] for r in cursor.execute("PRAGMA table_info(users)").fetchall()}
    extras = [
        ("risk_type", "TEXT"),
        ("risk_score", "INTEGER"),
        ("risk_assessment_date", "TEXT"),
        ("age_band", "TEXT"),
        ("occupation", "TEXT"),
        ("survey_json", "TEXT"),
        ("password_hash", "TEXT"),
        # 2026-10-08 新增：紧急联系人（适老版反诈预警推送对象）
        ("guardian_name", "TEXT"),
        ("guardian_phone", "TEXT"),
        ("guardian_relation", "TEXT"),
    ]
    for name, typ in extras:
        if name not in cols:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {name} {typ}")
    # 2026-10-08 新增：预警记录表（与用户表一起保证存在）
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS guardian_alerts (
            alert_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            guardian_name TEXT,
            guardian_phone TEXT,
            level TEXT,
            title TEXT,
            message TEXT,
            channel TEXT,
            status TEXT DEFAULT 'SENT',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    if own:
        conn.close()


_CARD_LIMIT_SCHEMA_READY = False


def ensure_card_limit_schema(conn=None):
    """2026-10-06 新增：卡片限额字段与流水付款卡字段的兼容迁移。

    老版本 bank.db 只有 daily_limit，且 transactions 没有 card_id。
    这里做「加列 + 回填 + 兜底默认值」，保证不重新 init_db 也能正常跑。
    """
    global _CARD_LIMIT_SCHEMA_READY
    own = conn is None
    if own and _CARD_LIMIT_SCHEMA_READY:
        return
    if own:
        conn = get_connection()
    cursor = conn.cursor()

    card_cols = {r[1] for r in cursor.execute("PRAGMA table_info(cards)").fetchall()}
    for name, typ in (
        ("single_limit", "REAL"),
        ("daily_count_limit", "INTEGER"),
        ("annual_limit", "REAL"),
    ):
        if name not in card_cols:
            cursor.execute(f"ALTER TABLE cards ADD COLUMN {name} {typ}")

    tx_cols = {r[1] for r in cursor.execute("PRAGMA table_info(transactions)").fetchall()}
    if "card_id" not in tx_cols:
        cursor.execute("ALTER TABLE transactions ADD COLUMN card_id TEXT")
        # 历史流水的付款卡写在备注「…｜付款卡 6222xxxx」里，这里回填出来
        cursor.execute(
            """
            UPDATE transactions
            SET card_id = TRIM(SUBSTR(notes, INSTR(notes, '付款卡 ') + 4))
            WHERE card_id IS NULL AND notes LIKE '%付款卡 %'
            """
        )

    # 储蓄卡限额兜底：缺失时补默认值；已有值保持不变
    cursor.execute(
        """
        UPDATE cards
        SET single_limit = COALESCE(single_limit, ?),
            daily_limit = COALESCE(daily_limit, ?),
            daily_count_limit = COALESCE(daily_count_limit, ?),
            annual_limit = COALESCE(annual_limit, ?)
        WHERE card_type LIKE '%储蓄%'
        """,
        (
            DEFAULT_DEBIT_LIMITS["single_limit"],
            DEFAULT_DEBIT_LIMITS["daily_limit"],
            DEFAULT_DEBIT_LIMITS["daily_count_limit"],
            DEFAULT_DEBIT_LIMITS["annual_limit"],
        ),
    )
    conn.commit()
    if own:
        _CARD_LIMIT_SCHEMA_READY = True
        conn.close()


def get_user_risk_profile(user_id: str = "user_001") -> dict:
    ensure_user_profile_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT user_id, name, risk_level, risk_type, risk_score,
               risk_assessment_date, age_band, occupation
        FROM users WHERE user_id = ?
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return {
            "user_id": user_id,
            "name": "",
            "risk_level": None,
            "risk_type": None,
            "risk_score": None,
            "risk_assessment_date": None,
            "age_band": None,
            "occupation": None,
            "assessment_valid": False,
            "days_since_eval": None,
        }
    eval_date = row["risk_assessment_date"]
    days_since = None
    valid = False
    if row["risk_level"] and eval_date:
        try:
            from datetime import datetime

            t_eval = datetime.strptime(str(eval_date)[:10], "%Y-%m-%d")
            days_since = (datetime.now() - t_eval).days
            valid = days_since <= 365
        except ValueError:
            valid = False
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "risk_level": row["risk_level"],
        "risk_type": row["risk_type"],
        "risk_score": row["risk_score"],
        "risk_assessment_date": eval_date,
        "age_band": row["age_band"],
        "occupation": row["occupation"],
        "assessment_valid": valid,
        "days_since_eval": days_since,
    }


def save_risk_assessment(
    user_id: str,
    risk_level: str,
    risk_type: str,
    risk_score: int,
    age_band: str,
    occupation: str,
    answers: dict,
) -> dict:
    ensure_user_profile_schema()
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE users
        SET risk_level = ?, risk_type = ?, risk_score = ?,
            risk_assessment_date = ?, age_band = ?, occupation = ?, survey_json = ?
        WHERE user_id = ?
        """,
        (
            risk_level,
            risk_type,
            risk_score,
            today,
            age_band,
            occupation,
            json.dumps(answers, ensure_ascii=False),
            user_id,
        ),
    )
    conn.commit()
    conn.close()
    return get_user_risk_profile(user_id)


def authenticate_user(account: str, password: str) -> Optional[dict]:
    """按用户号或手机号校验登录密码，成功返回可展示的用户资料。"""
    account = (account or "").strip()
    if not account or not password:
        return None
    ensure_user_profile_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT user_id, name, phone, risk_level, age_band, occupation, password_hash
        FROM users
        WHERE user_id = ? OR phone = ?
        """,
        (account, account),
    )
    row = cursor.fetchone()
    conn.close()
    if not row or not row["password_hash"]:
        return None
    if row["password_hash"] != hash_password(password):
        return None
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "phone": row["phone"],
        "phone_masked": mask_phone(row["phone"]),
        "risk_level": row["risk_level"],
        "age_band": row["age_band"],
        "occupation": row["occupation"],
        "silver": is_silver_age(row["age_band"]),
    }


def verify_user_password(user_id: str, password: str) -> bool:
    if not password:
        return False
    ensure_user_profile_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row["password_hash"]:
        return False
    return row["password_hash"] == hash_password(password)


def issue_auth_token(user_id: str) -> str:
    token = uuid.uuid4().hex
    AUTH_TOKENS[token] = user_id
    return token


def resolve_auth_token(token: str) -> Optional[str]:
    if not token:
        return None
    return AUTH_TOKENS.get(token)


def revoke_auth_token(token: str) -> None:
    AUTH_TOKENS.pop(token, None)


def _parse_tx_time(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None


def assess_transfer_risk(user_id: str, amount: float, payee: str) -> dict:
    """2026-10-06 增强：反洗钱视角的转账风险识别。

    覆盖：连续大额、单笔大额、首次大额收款人、当日高频、
    夜间大额、整数大额、快进快出、以及临近转账限额。
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT amount, counterparty, notes, created_at
        FROM transactions
        WHERE user_id = ? AND category = '转账' AND amount > 0
        ORDER BY datetime(created_at) DESC
        LIMIT 12
        """,
        (user_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    cursor.execute(
        """
        SELECT amount, counterparty, created_at
        FROM transactions
        WHERE user_id = ? AND category = '转账' AND amount < 0
          AND datetime(created_at) >= datetime('now', '-1 day')
        ORDER BY datetime(created_at) DESC
        LIMIT 5
        """,
        (user_id,),
    )
    incoming = [dict(r) for r in cursor.fetchall()]
    conn.close()

    now = datetime.now()
    known_payee = False
    large_recent = []
    today_count = 0
    today_total = 0.0
    for row in rows:
        counterparty = str(row["counterparty"] or "")
        if counterparty == payee:
            known_payee = True
        tx_time = _parse_tx_time(row["created_at"])
        if tx_time is None:
            continue
        if tx_time.date() == now.date():
            today_count += 1
            today_total += float(row["amount"])
        age_hours = (now - tx_time).total_seconds() / 3600.0
        if age_hours < 0 or age_hours > 72:
            continue
        tx_amount = float(row["amount"])
        if tx_amount >= LARGE_TRANSFER_AMOUNT:
            large_recent.append(
                {
                    "amount": round(tx_amount, 2),
                    "counterparty": counterparty,
                    "created_at": str(row["created_at"]),
                    "notes": row["notes"] or "",
                }
            )

    warnings = []
    level = "normal"
    title = ""
    if amount >= LARGE_TRANSFER_AMOUNT and large_recent:
        level = "high"
        title = "连续大额转账警示"
        brief = "；".join(
            f"{item['created_at'][5:16]} 向{item['counterparty']}转出¥{item['amount']:.0f}"
            for item in large_recent[:3]
        )
        warnings.append(
            f"近72小时内已有 {len(large_recent)} 笔不低于 ¥{LARGE_TRANSFER_AMOUNT:.0f} 的转账（{brief}）。"
            f"本次又将向「{payee}」转出 ¥{amount:.2f}。"
            "连续大额转出常见于冒充客服、投资导师或熟人借款诈骗，请先电话核实收款人，不要仅凭聊天记录汇款。"
        )
    elif amount >= 5000:
        level = "caution"
        title = "大额转账提醒"
        warnings.append(
            f"本次转出 ¥{amount:.2f}，属于单笔大额。请核对收款人，并确认是本人主动发起。"
        )

    if amount >= LARGE_TRANSFER_AMOUNT and not known_payee:
        if level == "normal":
            level = "caution"
            title = "首次大额收款人"
        warnings.append(f"「{payee}」不在您近期转账记录中。请勿向无法核实身份的账户汇款。")

    # ---- 2026-10-06 新增：反洗钱风控信号 ----
    if amount >= LARGE_TRANSFER_AMOUNT and today_count >= HIGH_FREQUENCY_COUNT:
        if level == "normal":
            level = "caution"
            title = title or "当日高频转账"
        warnings.append(
            f"今日您已发生 {today_count} 笔转账（合计 ¥{today_total:.2f}），频率明显偏高，"
            "请确认均为本人主动发起。"
        )

    if amount >= LARGE_TRANSFER_AMOUNT and NIGHT_START_HOUR <= now.hour < NIGHT_END_HOUR:
        if level == "normal":
            level = "caution"
            title = title or "夜间大额转账"
        warnings.append(
            f"当前为 {now.hour:02d}:00 前后的夜间时段，本次将转出 ¥{amount:.2f}。"
            "夜间大额转账是电信诈骗的高发场景，请再次核实收款人身份。"
        )

    if amount >= ROUND_AMOUNT_MIN and abs(amount % 1000) < 1e-6:
        if level == "normal":
            level = "caution"
            title = title or "整数大额转账"
        warnings.append(
            f"本笔为整数金额 ¥{amount:.0f}。整数大额转账常出现在投资、借贷类诈骗中，"
            "请确认收款用途真实。"
        )

    if amount >= LARGE_TRANSFER_AMOUNT and incoming:
        total_in = sum(abs(float(item["amount"])) for item in incoming)
        if level == "normal":
            level = "caution"
            title = title or "资金快进快出"
        warnings.append(
            f"近 24 小时内您有 ¥{total_in:.2f} 转入，紧接着又发起本笔转出，"
            "存在「快进快出」特征。请确认资金用途与来源合法。"
        )

    if warnings and level == "normal":
        level = "caution"
        title = title or "转账风险提示"

    return {
        "level": level,
        "title": title,
        "warnings": warnings,
        "require_ack": level != "normal",
        "recent_large": large_recent,
        "today_count": today_count,
        "today_total": round(today_total, 2),
    }


def get_cashflow_profile(user_id: str, days: int = 90) -> dict:
    """从流水提取周转与闲钱沉淀。支出为正、收入为负。"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT amount, created_at FROM transactions
        WHERE user_id = ?
          AND datetime(created_at) >= datetime('now', ?)
        """,
        (user_id, f"-{days} days"),
    )
    rows = cursor.fetchall()
    cursor.execute("SELECT balance FROM accounts WHERE user_id = ?", (user_id,))
    acc = cursor.fetchone()
    conn.close()

    balance = float(acc["balance"]) if acc else 0.0
    amounts = [float(r["amount"]) for r in rows]
    expenses = [a for a in amounts if a > 0]
    incomes = [-a for a in amounts if a < 0]
    n_days = max(days, 1)
    monthly_expense = sum(expenses) * (30.0 / n_days) if expenses else 0.0
    daily_out = sum(expenses) / n_days
    daily_in = sum(incomes) / n_days
    daily_flow = (sum(expenses) + sum(incomes)) / n_days
    mean_exp = (sum(expenses) / len(expenses)) if expenses else 0.0
    var = 0.0
    if len(expenses) > 1:
        var = sum((x - mean_exp) ** 2 for x in expenses) / len(expenses)
    std_exp = var ** 0.5
    k_reserve = 3.0
    idle = max(balance - k_reserve * monthly_expense, 0.0)
    turnover = daily_flow / max(balance, 1.0)
    high_turnover = turnover >= 0.04 or len(rows) >= 12
    stable_idle = idle >= 1500 and (std_exp <= mean_exp * 0.8 if mean_exp else True)

    return {
        "balance": round(balance, 2),
        "monthly_expense": round(monthly_expense, 2),
        "daily_out": round(daily_out, 2),
        "daily_in": round(daily_in, 2),
        "daily_flow": round(daily_flow, 2),
        "expense_std": round(std_exp, 2),
        "idle_cash": round(idle, 2),
        "turnover": round(turnover, 4),
        "high_turnover": high_turnover,
        "stable_idle": stable_idle,
        "tx_count": len(rows),
    }


def get_wealth_holdings(user_id: str) -> dict:
    """2026-10-06 新增：按产品汇总已购理财（来源 transactions 中 category='理财'）。"""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT counterparty AS product_name,
               SUM(amount) AS total_amount,
               COUNT(*) AS times,
               MAX(created_at) AS last_at
        FROM transactions
        WHERE user_id = ? AND category = '理财' AND amount > 0
        GROUP BY counterparty
        ORDER BY total_amount DESC
        """,
        (user_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    holdings = [
        {
            "product_name": row["product_name"] or "未知产品",
            "amount": round(float(row["total_amount"] or 0), 2),
            "times": int(row["times"] or 0),
            "last_at": str(row["last_at"] or "")[:10],
        }
        for row in rows
    ]
    return {
        "holdings": holdings,
        "total_amount": round(sum(h["amount"] for h in holdings), 2),
        "count": len(holdings),
    }


def get_user_brief(user_id: str) -> Optional[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, name, phone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "user_id": row["user_id"],
        "name": row["name"],
        "phone": row["phone"],
        "phone_masked": mask_phone(row["phone"]),
    }


def find_internal_payee(payer_user_id: str, query: str) -> Optional[dict]:
    """按姓名、手机号或 user_id 匹配本行其他客户（无需卡号）。"""
    text = (query or "").strip()
    if not text or text == payer_user_id:
        return None

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT user_id, name, phone
        FROM users
        WHERE user_id != ?
          AND (name = ? OR phone = ? OR user_id = ?)
        LIMIT 1
        """,
        (payer_user_id, text, text, text),
    )
    row = cursor.fetchone()
    if row:
        conn.close()
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "phone": row["phone"],
            "phone_masked": mask_phone(row["phone"]),
        }

    cursor.execute(
        "SELECT user_id, name, phone FROM users WHERE user_id != ?",
        (payer_user_id,),
    )
    peers = cursor.fetchall()
    conn.close()

    for peer in peers:
        name = str(peer["name"] or "")
        if not name:
            continue
        if name in text or text in name:
            return {
                "user_id": peer["user_id"],
                "name": name,
                "phone": peer["phone"],
                "phone_masked": mask_phone(peer["phone"]),
            }
    return None


def lookup_transfer_card(payer_user_id: str, query: str) -> Optional[dict]:
    """按收款卡号找账户。命中本行储蓄卡才入账；信用卡、本人卡和行外卡号分开标记。"""
    raw = re.sub(r"[\s-]", "", (query or "").strip())
    if not raw or not re.fullmatch(r"\d{6}\*{2,}\d{4}|\d{10,19}", raw):
        return None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.card_id, c.user_id, c.card_type, u.name, u.phone
        FROM cards c
        JOIN users u ON u.user_id = c.user_id
        """
    )
    rows = cursor.fetchall()
    conn.close()
    query_digits = re.sub(r"\D", "", raw)
    for row in rows:
        stored = str(row["card_id"] or "")
        if raw != stored and query_digits != re.sub(r"\D", "", stored):
            continue
        debit = "储蓄" in str(row["card_type"] or "")
        if row["user_id"] == payer_user_id:
            return {"self": True, "card_id": stored, "card_type": row["card_type"]}
        if not debit:
            return {"not_debit": True, "card_id": stored, "name": row["name"], "card_type": row["card_type"]}
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "card_id": stored,
            "card_type": row["card_type"],
            "phone_masked": mask_phone(row["phone"]),
        }
    return {"external": True, "card_id": raw}


def list_internal_payees(payer_user_id: str) -> List[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT user_id, name, phone
        FROM users
        WHERE user_id != ?
        ORDER BY user_id
        """,
        (payer_user_id,),
    )
    rows = [
        {
            "user_id": r["user_id"],
            "name": r["name"],
            "phone": r["phone"],
            "phone_masked": mask_phone(r["phone"]),
        }
        for r in cursor.fetchall()
    ]
    conn.close()
    return rows


def execute_account_deduction(
    user_id: str,
    amount: float,
    payee: str,
    notes: str,
    pay_card_id: str = "",
    payee_user_id: Optional[str] = None,
) -> bool:
    """执行转账：本行客户会同步入账；外部收款人仅扣款并记流水。"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        sender = get_user_brief(user_id)
        sender_name = sender["name"] if sender else user_id

        cursor.execute(
            "UPDATE accounts SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        if cursor.rowcount == 0:
            return False

        tx_notes = notes
        if pay_card_id:
            tx_notes = f"{notes}｜付款卡 {pay_card_id}"

        tx_out = f"tx_{str(uuid.uuid4())[:8]}"
        cursor.execute(
            """
            INSERT INTO transactions (
                tx_id, user_id, amount, category, counterparty, notes, card_id, created_at
            )
            VALUES (?, ?, ?, '转账', ?, ?, ?, datetime('now','localtime'))
            """,
            (tx_out, user_id, amount, payee, tx_notes, pay_card_id or None),
        )

        if payee_user_id and payee_user_id != user_id:
            cursor.execute(
                "UPDATE accounts SET balance = balance + ? WHERE user_id = ?",
                (amount, payee_user_id),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                return False
            recv_note = f"收到 {sender_name} 转账｜{notes}"
            tx_in = f"tx_{str(uuid.uuid4())[:8]}"
            cursor.execute(
                """
                INSERT INTO transactions (
                    tx_id, user_id, amount, category, counterparty, notes, created_at
                )
                VALUES (?, ?, ?, '转账', ?, ?, datetime('now','localtime'))
                """,
                (tx_in, payee_user_id, -amount, sender_name, recv_note),
            )

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_wealth_purchase(user_id: str, amount: float, product_name: str, pay_card_id: str = "") -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE accounts SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        if cursor.rowcount == 0:
            return False
        tx_id = f"tx_{str(uuid.uuid4())[:8]}"
        tx_notes = "理财申购"
        if pay_card_id:
            tx_notes = f"理财申购｜付款卡 {pay_card_id}"
        cursor.execute(
            """
            INSERT INTO transactions (
                tx_id, user_id, amount, category, counterparty, notes, card_id, created_at
            )
            VALUES (?, ?, ?, '理财', ?, ?, ?, datetime('now','localtime'))
            """,
            (tx_id, user_id, amount, product_name, tx_notes, pay_card_id or None),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def lock_account_funds(user_id: str, amount: float) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE accounts
            SET balance = balance - ?, locked_funds = locked_funds + ?
            WHERE user_id = ? AND balance >= ?
            """,
            (amount, amount, user_id, amount),
        )
        if cursor.rowcount == 0:
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _txn_window_sql(days: int = 30, start: str = None, end: str = None):
    """start 含当天，end 不含当天。没给起止日时用最近 days 天。"""
    if start and end:
        return (
            "datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)",
            (start, end),
        )
    return ("datetime(created_at) >= datetime('now', ?)", (f"-{int(days)} days",))


def get_category_spending(user_id: str, days: int = 30, start: str = None, end: str = None) -> List[dict]:
    window_sql, window_params = _txn_window_sql(days, start, end)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT category, SUM(amount) AS amount
        FROM transactions
        WHERE user_id = ?
          AND {window_sql}
          AND amount > 0
          AND category != '理财'
        GROUP BY category
        ORDER BY amount DESC
        """,
        (user_id, *window_params),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def get_recent_transactions(user_id: str, limit: int = 10, start: str = None, end: str = None) -> List[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    if start and end:
        cursor.execute(
            """
            SELECT tx_id, amount, category, counterparty, notes, created_at
            FROM transactions
            WHERE user_id = ?
              AND datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            (user_id, start, end, limit),
        )
    else:
        cursor.execute(
            """
            SELECT tx_id, amount, category, counterparty, notes, created_at
            FROM transactions
            WHERE user_id = ?
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def find_abnormal_transactions(user_id: str, start: str = None, end: str = None) -> List[dict]:
    """简单规则：单笔 >200 或高于该分类同期均值 2 倍。"""
    window_sql, window_params = _txn_window_sql(30, start, end)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT tx_id, amount, category, counterparty, created_at
        FROM transactions
        WHERE user_id = ?
          AND {window_sql}
        ORDER BY amount DESC
        """,
        (user_id, *window_params),
    )
    txs = [dict(r) for r in cursor.fetchall()]
    conn.close()

    by_cat: Dict[str, List[float]] = {}
    for t in txs:
        by_cat.setdefault(t["category"], []).append(float(t["amount"]))

    result = []
    for t in txs:
        amt = float(t["amount"])
        cat_avg = sum(by_cat.get(t["category"], [amt])) / max(len(by_cat.get(t["category"], [1])), 1)
        reason = None
        if amt >= 200:
            reason = "单笔金额偏高"
        elif amt >= cat_avg * 2 and amt >= 50:
            reason = "高于该分类近期均值"
        if reason:
            item = dict(t)
            item["reason"] = reason
            result.append(item)
    return result

def list_user_cards(user_id: str) -> List[dict]:
    ensure_card_limit_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT card_id, card_type, credit_limit, daily_limit, status,
               single_limit, daily_count_limit, annual_limit
        FROM cards WHERE user_id = ?
        """,
        (user_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    status_labels = {"NORMAL": "正常", "FROZEN": "已冻结", "LOST": "已挂失"}
    for row in rows:
        row["status_label"] = status_labels.get(row.get("status") or "", row.get("status") or "")
        row["is_debit"] = "储蓄" in str(row.get("card_type") or "")
        row["single_limit"] = row.get("single_limit")
        row["daily_limit"] = row.get("daily_limit")
        row["daily_count_limit"] = row.get("daily_count_limit")
        row["annual_limit"] = row.get("annual_limit")
    return rows


def get_payment_sources(user_id: str) -> List[dict]:
    """转账/申购可选付款工具：仅正常储蓄卡可付款，信用卡展示但不可选。

    2026-10-06：补充单笔/日累计/日笔数/年限额与「今日剩余可转额度」。
    """
    balance = get_user_balance(user_id)
    sources = []
    for card in list_user_cards(user_id):
        debit = bool(card.get("is_debit"))
        normal = card.get("status") == "NORMAL"
        usage = get_transfer_usage(user_id, card["card_id"]) if debit else {
            "today_amount": 0.0,
            "today_count": 0,
            "year_amount": 0.0,
        }
        if debit:
            payable = normal
            daily = card.get("daily_limit")
            remaining_daily = (
                None if daily is None else max(float(daily) - usage["today_amount"], 0.0)
            )
            caption = f"可用活期 {_fmt_money(balance)}｜今日剩余可转 {_fmt_money(remaining_daily)}"
            reason = "" if payable else f"{card['status_label']}，暂不可用于付款"
        else:
            payable = False
            limit = float(card["credit_limit"] or 0)
            caption = f"信用额度 {_fmt_money(limit)}"
            reason = "信用卡不支持转账和理财申购"
        sources.append(
            {
                "card_id": card["card_id"],
                "card_type": card["card_type"],
                "status": card["status"],
                "status_label": card["status_label"],
                "single_limit": card.get("single_limit"),
                "daily_limit": card.get("daily_limit"),
                "daily_count_limit": card.get("daily_count_limit"),
                "annual_limit": card.get("annual_limit"),
                "today_amount": usage["today_amount"],
                "today_count": usage["today_count"],
                "year_amount": usage["year_amount"],
                "remaining_daily_amount": None
                if card.get("daily_limit") is None
                else max(float(card["daily_limit"]) - usage["today_amount"], 0.0),
                "payable": payable,
                "caption": caption,
                "reason": reason,
            }
        )
    return sources


def resolve_payable_card(user_id: str, card_id: str) -> Optional[dict]:
    for source in get_payment_sources(user_id):
        if source["card_id"] == card_id and source["payable"]:
            return source
    return None


def attach_payment_choice(user_id: str, order: dict, card_data: dict) -> dict:
    sources = get_payment_sources(user_id)
    payable = [s for s in sources if s["payable"]]
    selected = payable[0]["card_id"] if payable else ""
    order["pay_card_id"] = selected
    card_data["payment_cards"] = sources
    card_data["selected_card_id"] = selected
    return card_data


def get_transfer_usage(user_id: str, card_id: str = "") -> dict:
    """2026-10-06 新增：统计该卡当日/当年的转出笔数与金额（支出为正）。"""
    ensure_card_limit_schema()
    conn = get_connection()
    cursor = conn.cursor()
    card_clause = ""
    params: List = [user_id]
    if card_id:
        # 兼容老流水：card_id 为空时回退到备注中的「付款卡 xxx」
        card_clause = " AND (card_id = ? OR (card_id IS NULL AND notes LIKE ?))"
        params.extend([card_id, f"%付款卡 {card_id}"])
    cursor.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN date(created_at) = date('now','localtime')
                              THEN amount ELSE 0 END), 0) AS today_amount,
            COALESCE(SUM(CASE WHEN date(created_at) = date('now','localtime')
                              THEN 1 ELSE 0 END), 0) AS today_count,
            COALESCE(SUM(CASE WHEN strftime('%Y', created_at) = strftime('%Y','now','localtime')
                              THEN amount ELSE 0 END), 0) AS year_amount,
            COUNT(*) AS total_count
        FROM transactions
        WHERE user_id = ? AND category = '转账' AND amount > 0
        {card_clause}
        """,
        tuple(params),
    )
    row = cursor.fetchone()
    conn.close()
    return {
        "today_amount": round(float(row["today_amount"] or 0), 2),
        "today_count": int(row["today_count"] or 0),
        "year_amount": round(float(row["year_amount"] or 0), 2),
        "total_count": int(row["total_count"] or 0),
    }


def check_transfer_limits(user_id: str, card_id: str, amount: float) -> dict:
    """2026-10-06 新增：转账前置校验（卡片状态 → 余额 → 四维限额）。

    返回结构同时供后端拦截与小程序展示使用：
      ok / code / message / lines（额度明细） / warnings（临界预警）
      remaining_*（本笔转出后仍可用的额度） / quick_actions（引导调额）
    """
    ensure_card_limit_schema()
    amount = float(amount or 0)
    card = next((c for c in list_user_cards(user_id) if c["card_id"] == card_id), None)

    def build(ok, code, message, usage=None, near=False, warnings=None, quick_actions=None):
        usage = usage or {"today_amount": 0.0, "today_count": 0, "year_amount": 0.0}
        balance = get_user_balance(user_id)
        src = card or {}
        single = src.get("single_limit")
        daily = src.get("daily_limit")
        daily_count = src.get("daily_count_limit")
        annual = src.get("annual_limit")

        after_daily = usage["today_amount"] + amount
        after_count = usage["today_count"] + 1
        after_year = usage["year_amount"] + amount

        remaining_daily = None if daily is None else max(float(daily) - after_daily, 0.0)
        remaining_count = None if daily_count is None else max(int(daily_count) - after_count, 0)
        remaining_annual = None if annual is None else max(float(annual) - after_year, 0.0)
        remaining_balance = max(balance - amount, 0.0)
        current_daily = None if daily is None else max(float(daily) - usage["today_amount"], 0.0)
        current_count = None if daily_count is None else max(int(daily_count) - usage["today_count"], 0)
        current_annual = None if annual is None else max(float(annual) - usage["year_amount"], 0.0)

        lines = [
            f"付款卡：{src.get('card_type') or '储蓄卡'} {src.get('card_id') or card_id}"
            f"（{src.get('status_label') or '未找到'}）",
            f"单笔限额 {_fmt_money(single)}｜本笔 {_fmt_money(amount)}",
            f"今日已转 {_fmt_money(usage['today_amount'])} / 上限 {_fmt_money(daily)}，"
            f"本笔转出后今日还可转 {_fmt_money(remaining_daily)}",
            f"今日已转 {usage['today_count']} 笔 / 上限 "
            f"{'不限' if daily_count is None else str(daily_count) + ' 笔'}，"
            f"本笔后剩余 {('不限' if remaining_count is None else str(remaining_count) + ' 笔')}",
            f"年度已转 {_fmt_money(usage['year_amount'])} / 上限 {_fmt_money(annual)}，"
            f"本笔转出后年度还可转 {_fmt_money(remaining_annual)}",
            f"活期余额 {_fmt_money(balance)}，本笔转出后剩余 {_fmt_money(remaining_balance)}",
        ]

        return {
            "ok": ok,
            "code": code,
            "message": message,
            "card_id": src.get("card_id") or card_id,
            "card_type": src.get("card_type") or "",
            "card_status": src.get("status") or "",
            "card_status_label": src.get("status_label") or "",
            "amount": round(amount, 2),
            "balance": round(balance, 2),
            "single_limit": single,
            "daily_limit": daily,
            "daily_count_limit": daily_count,
            "annual_limit": annual,
            "today_amount": usage["today_amount"],
            "today_count": usage["today_count"],
            "year_amount": usage["year_amount"],
            "after_daily_amount": round(after_daily, 2),
            "after_daily_count": after_count,
            "after_year_amount": round(after_year, 2),
            "after_balance": round(remaining_balance, 2),
            "remaining_daily_amount": None if remaining_daily is None else round(remaining_daily, 2),
            "remaining_daily_count": remaining_count,
            "remaining_annual_amount": None if remaining_annual is None else round(remaining_annual, 2),
            "remaining_balance": round(remaining_balance, 2),
            "current_remaining_daily_amount": None if current_daily is None else round(current_daily, 2),
            "current_remaining_daily_count": current_count,
            "current_remaining_annual_amount": None if current_annual is None else round(current_annual, 2),
            "near_limit": near,
            "warnings": warnings or [],
            "lines": lines,
            "quick_actions": quick_actions or [],
        }

    if not card_id:
        return build(
            False,
            "NO_PAYABLE_CARD",
            "您名下没有可用于转账的正常储蓄卡，请先解冻卡片或到「卡片管理」查看卡片状态。",
            quick_actions=[{"label": "查询我的银行卡", "text": "查询我的银行卡与额度管理"}],
        )
    if card is None:
        return build(
            False, "CARD_NOT_FOUND", f"未找到卡号 {card_id}，请重新选择付款储蓄卡。"
        )
    if not card.get("is_debit"):
        return build(
            False,
            "CARD_NOT_PAYABLE",
            f"{card['card_type']} {card_id} 不支持转账，请改用储蓄卡付款。",
        )
    if card.get("status") == "FROZEN":
        return build(
            False,
            "CARD_FROZEN",
            f"{card['card_type']} {card_id} 当前为「已冻结」，无法转账。可先解冻后再发起。",
            quick_actions=[
                {"label": f"解冻{card['card_type']}", "text": f"解冻我的{card['card_type']}"},
            ],
        )
    if card.get("status") == "LOST":
        return build(
            False,
            "CARD_LOST",
            f"{card['card_type']} {card_id} 当前为「已挂失」，无法转账。",
            quick_actions=[{"label": "查询我的银行卡", "text": "查询我的银行卡与额度管理"}],
        )
    if card.get("status") != "NORMAL":
        return build(
            False,
            "CARD_NOT_NORMAL",
            f"{card['card_type']} {card_id} 状态异常，暂不可用于转账。",
        )
    if amount <= 0:
        return build(False, "INVALID_AMOUNT", "转账金额必须大于 0。")

    usage = get_transfer_usage(user_id, card_id)
    single = card.get("single_limit")
    daily = card.get("daily_limit")
    daily_count = card.get("daily_count_limit")
    annual = card.get("annual_limit")
    balance = get_user_balance(user_id)
    card_label = f"{card['card_type']} {card_id}"

    if single is not None and amount > float(single):
        target = _ceil_to(amount, 1000.0)
        return build(
            False,
            "SINGLE_EXCEEDED",
            f"本笔 {_fmt_money(amount)} 超过 {card_label} 的单笔限额 {_fmt_money(single)}。"
            f"请降低金额，或将单笔限额调整到 {_fmt_money(target)} 以上。",
            usage,
            quick_actions=[
                {
                    "label": "调整单笔限额",
                    "text": f"把我的{card['card_type']}单笔限额调整为{int(target)}元",
                }
            ],
        )
    if daily_count is not None and usage["today_count"] + 1 > int(daily_count):
        target_count = int(usage["today_count"]) + 5
        return build(
            False,
            "DAILY_COUNT_EXCEEDED",
            f"今日已转账 {usage['today_count']} 笔，达到 {card_label} 的日累计笔数上限 "
            f"{daily_count} 笔。请明天再转，或先提高日累计笔数限额。",
            usage,
            quick_actions=[
                {
                    "label": "调整日笔数限额",
                    "text": f"把我的{card['card_type']}单日笔数限额调整为{target_count}笔",
                }
            ],
        )
    if daily is not None and usage["today_amount"] + amount > float(daily):
        remaining = max(float(daily) - usage["today_amount"], 0.0)
        target = _ceil_to(usage["today_amount"] + amount, 1000.0)
        return build(
            False,
            "DAILY_AMOUNT_EXCEEDED",
            f"今日已转账 {_fmt_money(usage['today_amount'])}（上限 {_fmt_money(daily)}），"
            f"本笔 {_fmt_money(amount)} 将超出日累计限额，当前剩余可用 {_fmt_money(remaining)}。"
            f"请降低金额，或将日累计限额调整到 {_fmt_money(target)} 以上。",
            usage,
            quick_actions=[
                {
                    "label": "调整单日限额",
                    "text": f"把我的{card['card_type']}单日限额调整为{int(target)}元",
                },
                {"label": "查询我的银行卡", "text": "查询我的银行卡与额度管理"},
            ],
        )
    if annual is not None and usage["year_amount"] + amount > float(annual):
        remaining = max(float(annual) - usage["year_amount"], 0.0)
        target = _ceil_to(usage["year_amount"] + amount, 10000.0)
        return build(
            False,
            "ANNUAL_EXCEEDED",
            f"本年度已转账 {_fmt_money(usage['year_amount'])}（上限 {_fmt_money(annual)}），"
            f"本笔 {_fmt_money(amount)} 将超出年度累计限额，当前剩余可用 {_fmt_money(remaining)}。"
            f"请降低金额，或将年度限额调整到 {_fmt_money(target)} 以上。",
            usage,
            quick_actions=[
                {
                    "label": "调整年度限额",
                    "text": f"把我的{card['card_type']}年度限额调整为{int(target)}元",
                }
            ],
        )
    if balance < amount:
        return build(
            False,
            "INSUFFICIENT_BALANCE",
            f"活期余额 {_fmt_money(balance)} 不足以支付本笔 {_fmt_money(amount)}。",
            usage,
        )

    warnings = []
    after_daily = usage["today_amount"] + amount
    after_count = usage["today_count"] + 1
    after_year = usage["year_amount"] + amount
    if daily and after_daily >= float(daily) * (1 - NEAR_LIMIT_RATIO):
        warnings.append(
            f"本笔转出后今日仅剩 {_fmt_money(max(float(daily) - after_daily, 0))} "
            f"可转额度（上限 {_fmt_money(daily)}），已接近日累计上限。"
        )
    if daily_count is not None and after_count >= int(daily_count):
        warnings.append(f"本笔转出后今日剩余 {max(int(daily_count) - after_count, 0)} 笔转账额度。")
    if annual and after_year >= float(annual) * (1 - NEAR_LIMIT_RATIO):
        warnings.append(
            f"本笔转出后本年度仅剩 {_fmt_money(max(float(annual) - after_year, 0))} "
            f"可转额度（上限 {_fmt_money(annual)}），已接近年度上限。"
        )
    return build(True, "OK", "额度校验通过。", usage, near=bool(warnings), warnings=warnings)

def apply_card_action(
    card_id: str,
    action: str,
    new_daily_limit: Optional[float] = None,
    new_single_limit: Optional[float] = None,
    new_daily_count_limit: Optional[int] = None,
    new_annual_limit: Optional[float] = None,
) -> bool:
    """卡片操作。2026-10-06 起支持单笔/日累计/日笔数/年累计四类限额调整。"""
    ensure_card_limit_schema()

    def _bad_number(value, upper):
        return value is not None and (float(value) <= 0 or float(value) > upper)

    if _bad_number(new_single_limit, MAX_SINGLE_LIMIT):
        return False
    if _bad_number(new_daily_limit, MAX_DAILY_LIMIT):
        return False
    if _bad_number(new_annual_limit, MAX_ANNUAL_LIMIT):
        return False
    if new_daily_count_limit is not None and (
        int(new_daily_count_limit) <= 0 or int(new_daily_count_limit) > MAX_DAILY_COUNT_LIMIT
    ):
        return False

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if action == "report_lost":
            cursor.execute("UPDATE cards SET status = 'LOST' WHERE card_id = ?", (card_id,))
        elif action == "freeze":
            cursor.execute("UPDATE cards SET status = 'FROZEN' WHERE card_id = ?", (card_id,))
        elif action == "unlock":
            cursor.execute("UPDATE cards SET status = 'NORMAL' WHERE card_id = ?", (card_id,))
        elif action == "adjust_limit":
            sets = []
            params: List = []
            if new_single_limit is not None:
                sets.append("single_limit = ?")
                params.append(float(new_single_limit))
            if new_daily_limit is not None:
                sets.append("daily_limit = ?")
                params.append(float(new_daily_limit))
            if new_daily_count_limit is not None:
                sets.append("daily_count_limit = ?")
                params.append(int(new_daily_count_limit))
            if new_annual_limit is not None:
                sets.append("annual_limit = ?")
                params.append(float(new_annual_limit))
            if not sets:
                return False
            params.append(card_id)
            cursor.execute(f"UPDATE cards SET {', '.join(sets)} WHERE card_id = ?", tuple(params))
        else:
            return False
        if cursor.rowcount == 0:
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def list_subscriptions(user_id: str) -> List[dict]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT sub_id, merchant, amount, billing_cycle, next_billing_date, status
        FROM subscriptions
        WHERE user_id = ?
        ORDER BY next_billing_date
        """,
        (user_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def cancel_subscription(sub_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE subscriptions SET status = 'CANCELLED' WHERE sub_id = ? AND status = 'ACTIVE'",
            (sub_id,),
        )
        if cursor.rowcount == 0:
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==== 2026-10-08 新增：亲情反诈守护（紧急联系人绑定 + 高风险预警推送）====
def ensure_guardian_schema(conn=None):
    """保证紧急联系人字段与预警记录表存在（老 bank.db 免重建）。"""
    own = conn is None
    if own:
        conn = get_connection()
    cursor = conn.cursor()
    cols = {r[1] for r in cursor.execute("PRAGMA table_info(users)").fetchall()}
    for name, typ in (
        ("guardian_name", "TEXT"),
        ("guardian_phone", "TEXT"),
        ("guardian_relation", "TEXT"),
    ):
        if name not in cols:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {name} {typ}")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS guardian_alerts (
            alert_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            guardian_name TEXT,
            guardian_phone TEXT,
            level TEXT,
            title TEXT,
            message TEXT,
            channel TEXT,
            status TEXT DEFAULT 'SENT',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    if own:
        conn.close()


def ensure_scenario_schema(conn=None):
    """场景专款子账本、份额流水、到点提醒。老库启动时补表。"""
    own = conn is None
    if own:
        conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS scenario_savings (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            scenario_type TEXT DEFAULT 'GIFT',
            notes TEXT NOT NULL,
            beneficiary TEXT DEFAULT '本人',
            target_amount REAL NOT NULL,
            current_principal REAL NOT NULL,
            accumulated_yield REAL DEFAULT 0,
            annual_yield REAL NOT NULL,
            product_code TEXT NOT NULL,
            product_name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            value_date TEXT NOT NULL,
            target_date TEXT NOT NULL,
            auto_redeem_date TEXT NOT NULL,
            last_yield_date TEXT,
            status TEXT NOT NULL,
            notification_status TEXT DEFAULT 'NONE',
            use_notice_status TEXT DEFAULT 'NONE'
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS scenario_transactions (
            txn_id TEXT PRIMARY KEY,
            scenario_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            txn_type TEXT NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            remark TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS scenario_notices (
            notice_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            scenario_id TEXT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'UNREAD',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.commit()
    if own:
        conn.close()


def _insert_scenario_notice(cursor, user_id: str, scenario_id: str, title: str, content: str) -> str:
    notice_id = f"nt_{uuid.uuid4().hex[:10]}"
    cursor.execute(
        """
        INSERT INTO scenario_notices (notice_id, user_id, scenario_id, title, content, status)
        VALUES (?, ?, ?, ?, ?, 'UNREAD')
        """,
        (notice_id, user_id, scenario_id, title, content),
    )
    return notice_id


def get_cash_management_product() -> Optional[dict]:
    """专款底层只允许在售的 R1 现金管理产品。"""
    product = get_product_by_id("w_cash")
    if not product or product.get("risk_level") != "R1":
        return None
    if product.get("status") not in (None, "ON_SALE"):
        return None
    return product


def list_active_scenarios(user_id: str) -> List[dict]:
    ensure_scenario_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM scenario_savings
        WHERE user_id = ? AND status = 'ACTIVE'
        ORDER BY target_date, created_at
        """,
        (user_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def list_scenario_history(user_id: str, limit: int = 30) -> List[dict]:
    """列出客户全部心愿备用金记录（存续中 + 已完成），供查看历史使用。

    2026-10-31：原先只查 status='ACTIVE'，客户存过但已回到可用余额的记录
    会完全看不见，于是助手回答「没有记录」。记录本身一直在库里。
    """
    ensure_scenario_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM scenario_savings
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_scenario(scenario_id: str) -> Optional[dict]:
    ensure_scenario_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM scenario_savings WHERE id = ?", (scenario_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def activate_scenario(user_id: str, plan: dict) -> tuple:
    """确认后：可用余额划入专款子账本，并记下申购流水。"""
    ensure_scenario_schema()
    amount = round(float(plan["amount"]), 2)
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE accounts
            SET balance = balance - ?, locked_funds = locked_funds + ?
            WHERE user_id = ? AND balance >= ?
            """,
            (amount, amount, user_id, amount),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return False, "活期可用余额不足，无法预留这笔专款"
        cursor.execute(
            """
            INSERT INTO scenario_savings (
                id, user_id, scenario_type, notes, beneficiary, target_amount,
                current_principal, accumulated_yield, annual_yield, product_code,
                product_name, value_date, target_date, auto_redeem_date,
                last_yield_date, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
            """,
            (
                plan["scenario_id"],
                user_id,
                plan.get("scenario_type") or "GIFT",
                plan["notes"],
                plan.get("beneficiary") or "本人",
                amount,
                amount,
                float(plan["annual_yield"]),
                plan["product_code"],
                plan["product_name"],
                plan["value_date"],
                plan["target_date"],
                plan["auto_redeem_date"],
                plan["value_date"],
            ),
        )
        cursor.execute(
            """
            INSERT INTO scenario_transactions (txn_id, scenario_id, user_id, txn_type, amount, remark)
            VALUES (?, ?, ?, 'SUBSCRIBE', ?, ?)
            """,
            (
                f"st_{uuid.uuid4().hex[:10]}",
                plan["scenario_id"],
                user_id,
                amount,
                f"活期划入专款并申购{plan['product_name']}",
            ),
        )
        conn.commit()
        return True, ""
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _accrue_scenario_row(cursor, row: dict, today: date, business_days_after) -> float:
    """按成交日后的交易日补提参考收益。business_days_after(start, end) 由调度层传入。"""
    principal = float(row["current_principal"])
    annual = float(row["annual_yield"])
    last = date.fromisoformat(row["last_yield_date"])
    added = 0.0
    cursor_day = last
    for day, span in business_days_after(last, today):
        added += principal * annual / 365.0 * span
        cursor_day = day
    added = round(added, 4)
    if added > 0 or cursor_day != last:
        cursor.execute(
            """
            UPDATE scenario_savings
            SET accumulated_yield = accumulated_yield + ?, last_yield_date = ?
            WHERE id = ?
            """,
            (added, cursor_day.isoformat(), row["id"]),
        )
        if added > 0:
            cursor.execute(
                """
                INSERT INTO scenario_transactions
                    (txn_id, scenario_id, user_id, txn_type, amount, remark)
                VALUES (?, ?, ?, 'YIELD', ?, ?)
                """,
                (
                    f"st_{uuid.uuid4().hex[:10]}",
                    row["id"],
                    row["user_id"],
                    round(added, 2),
                    f"交易日参考收益结转至 {cursor_day.isoformat()}",
                ),
            )
    return added


def settle_scenario(scenario_id: str, txn_type: str, business_days_after, today: date = None) -> Optional[dict]:
    """赎回或提前解冻：本金与已计提收益回到可用余额。"""
    ensure_scenario_schema()
    today = today or date.today()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM scenario_savings WHERE id = ?", (scenario_id,))
        row = cursor.fetchone()
        if not row or row["status"] != "ACTIVE":
            conn.rollback()
            return None
        item = dict(row)
        _accrue_scenario_row(cursor, item, today, business_days_after)
        cursor.execute("SELECT * FROM scenario_savings WHERE id = ?", (scenario_id,))
        item = dict(cursor.fetchone())
        principal = round(float(item["current_principal"]), 2)
        earned = round(float(item["accumulated_yield"]), 2)
        payout = round(principal + earned, 2)
        cursor.execute(
            """
            UPDATE accounts
            SET balance = balance + ?,
                locked_funds = CASE
                    WHEN locked_funds >= ? THEN locked_funds - ?
                    ELSE 0
                END
            WHERE user_id = ?
            """,
            (payout, principal, principal, item["user_id"]),
        )
        status = "COMPLETED" if txn_type == "REDEEM" else "TERMINATED"
        cursor.execute(
            """
            UPDATE scenario_savings
            SET status = ?, current_principal = 0, notification_status = 'SENT'
            WHERE id = ?
            """,
            (status, scenario_id),
        )
        remark = "到期赎回至活期可用余额" if txn_type == "REDEEM" else "客户提前解冻，赎回至活期可用余额"
        cursor.execute(
            """
            INSERT INTO scenario_transactions
                (txn_id, scenario_id, user_id, txn_type, amount, remark)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"st_{uuid.uuid4().hex[:10]}",
                scenario_id,
                item["user_id"],
                txn_type,
                payout,
                remark,
            ),
        )
        if txn_type == "REDEEM":
            title = "心愿备用金已回到可用余额"
            content = (
                f"「{item['notes']}」的心愿备用金已回来了。本金 ¥{principal:.2f}，"
                f"参考收益 ¥{earned:.2f}，合计 ¥{payout:.2f} 已回到活期可用余额。"
                f"计划用钱日是 {item['target_date']}。"
            )
        else:
            title = "心愿备用金已取回"
            content = (
                f"「{item['notes']}」的心愿备用金已取回。本金 ¥{principal:.2f}，"
                f"参考收益 ¥{earned:.2f}，合计 ¥{payout:.2f} 已回到活期可用余额。"
            )
        _insert_scenario_notice(cursor, item["user_id"], scenario_id, title, content)
        conn.commit()
        item.update(
            {
                "status": status,
                "payout": payout,
                "principal": principal,
                "earned": earned,
                "notice_title": title,
                "notice_content": content,
            }
        )
        return item
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_scenario_schedule(business_days_after, today: date = None) -> List[dict]:
    """按本机当地日期补提收益、到期赎回，并在用钱日再记一条提醒。"""
    ensure_scenario_schema()
    today = today or date.today()
    events = []
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM scenario_savings WHERE status = 'ACTIVE'")
    active_ids = [r["id"] for r in cursor.fetchall()]
    conn.close()

    for scenario_id in active_ids:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM scenario_savings WHERE id = ? AND status = 'ACTIVE'", (scenario_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            continue
        item = dict(row)
        # 当天创建的专款不在当天赎回：用钱日临近时 redeem_on 会被压到今天，
        # 否则客户刚确认完、几十秒后钱就自动回到活期，看起来像"根本没存过"。
        created_on = str(item.get("created_at") or "")[:10]
        if (
            item["auto_redeem_date"] <= today.isoformat()
            and (not created_on or created_on < today.isoformat())
        ):
            settled = settle_scenario(scenario_id, "REDEEM", business_days_after, today)
            if settled:
                events.append(settled)
            continue
        conn = get_connection()
        cursor = conn.cursor()
        try:
            _accrue_scenario_row(cursor, item, today, business_days_after)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM scenario_savings
        WHERE status = 'COMPLETED' AND target_date = ? AND use_notice_status = 'NONE'
        """,
        (today.isoformat(),),
    )
    due = [dict(r) for r in cursor.fetchall()]
    for item in due:
        content = (
            f"今天是「{item['notes']}」的计划用钱日（{item['target_date']}）。"
            "这笔心愿备用金已经回到活期可用余额，可以按原计划使用。"
        )
        _insert_scenario_notice(cursor, item["user_id"], item["id"], "今天是计划用钱日", content)
        cursor.execute(
            "UPDATE scenario_savings SET use_notice_status = 'SENT' WHERE id = ?",
            (item["id"],),
        )
        events.append({"id": item["id"], "user_id": item["user_id"], "notice_content": content})
    conn.commit()
    conn.close()
    return events


def list_unread_scenario_notices(user_id: str) -> List[dict]:
    ensure_scenario_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT notice_id, scenario_id, title, content, created_at
        FROM scenario_notices
        WHERE user_id = ? AND status = 'UNREAD'
        ORDER BY created_at
        """,
        (user_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def mark_scenario_notices_read(user_id: str, notice_ids: List[str]) -> None:
    if not notice_ids:
        return
    ensure_scenario_schema()
    conn = get_connection()
    cursor = conn.cursor()
    marks = ",".join("?" for _ in notice_ids)
    cursor.execute(
        f"""
        UPDATE scenario_notices SET status = 'READ'
        WHERE user_id = ? AND notice_id IN ({marks})
        """,
        [user_id, *notice_ids],
    )
    conn.commit()
    conn.close()


def get_guardian(user_id: str) -> Optional[dict]:
    """读取已绑定的紧急联系人（未绑定返回 None）。"""
    ensure_guardian_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT guardian_name, guardian_phone, guardian_relation FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row or not row["guardian_phone"]:
        return None
    return {
        "name": row["guardian_name"] or "家人",
        "phone": row["guardian_phone"],
        "phone_masked": mask_phone(row["guardian_phone"]),
        "relation": row["guardian_relation"] or "亲属",
    }


def bind_guardian(user_id: str, name: str, phone: str, relation: str = "亲属") -> Optional[dict]:
    """绑定 / 更新紧急联系人。手机号非法或用户不存在时返回 None。"""
    phone = str(phone or "").strip()
    if not re_match_phone(phone):
        return None
    ensure_guardian_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        conn.close()
        return None
    cursor.execute(
        """
        UPDATE users
        SET guardian_name = ?, guardian_phone = ?, guardian_relation = ?
        WHERE user_id = ?
        """,
        (
            (str(name or "").strip() or "家人")[:12],
            phone,
            (str(relation or "").strip() or "亲属")[:6],
            user_id,
        ),
    )
    conn.commit()
    conn.close()
    return get_guardian(user_id)


def re_match_phone(phone: str) -> bool:
    """中国大陆手机号简单校验（1 开头，第二位 3-9，共 11 位）。"""
    return bool(re.fullmatch(r"1[3-9]\d{9}", str(phone or "").strip()))


def record_guardian_alert(
    user_id: str,
    level: str,
    title: str,
    message: str,
    channel: str = "短信",
) -> Optional[dict]:
    """写入一条预警记录（模拟已推送给紧急联系人）。"""
    guardian = get_guardian(user_id)
    if not guardian:
        return None
    alert_id = str(uuid.uuid4())[:8]
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO guardian_alerts (
            alert_id, user_id, guardian_name, guardian_phone,
            level, title, message, channel, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SENT', ?)
        """,
        (
            alert_id,
            user_id,
            guardian["name"],
            guardian["phone"],
            level,
            title,
            message,
            channel,
            created_at,
        ),
    )
    conn.commit()
    conn.close()
    return {
        "alert_id": alert_id,
        "level": level,
        "title": title,
        "message": message,
        "channel": channel,
        "created_at": created_at,
    }


def list_guardian_alerts(user_id: str, limit: int = 8) -> List[dict]:
    ensure_guardian_schema()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT alert_id, level, title, message, channel, status, created_at
        FROM guardian_alerts
        WHERE user_id = ?
        ORDER BY datetime(created_at) DESC
        LIMIT ?
        """,
        (user_id, int(limit)),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def notify_guardian(user_id: str, level: str, title: str, message: str) -> dict:
    """高风险行为触发：向紧急联系人推送预警通知。未绑定则返回 sent=False。"""
    guardian = get_guardian(user_id)
    if not guardian:
        return {"sent": False, "reason": "尚未绑定紧急联系人", "guardian": None, "alert": None}

    brief = get_user_brief(user_id) or {}
    user_name = brief.get("name") or "您的家人"
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M")
    detail = str(message or "").strip().rstrip("。")
    sms_text = (
        f"【智汇银行·亲情守护】{user_name} 于 {now_text} 触发「{title or '高风险操作'}」预警："
        f"{detail}。请尽快电话联系确认，谨防电信诈骗。"
    )
    alert = record_guardian_alert(user_id, level, title, message, channel="短信")
    if alert:
        alert["sms_text"] = sms_text
    return {
        "sent": True,
        "channel": "短信",
        "guardian": guardian,
        "alert": alert,
        "sms_text": sms_text,
        "reason": "",
    }


def get_guardian_center(user_id: str) -> dict:
    """适老版「安全防护」卡数据：绑定状态 + 预警记录 + 反诈提示。"""
    guardian = get_guardian(user_id)
    alerts = list_guardian_alerts(user_id) if guardian else []
    tips = [
        "任何人以「客服退款、账户异常、安全账户」为由要求转账，都是诈骗。",
        "自称公检法、快递、投资导师要您汇款的，先挂断并拨打 96110 核实。",
        "不要让陌生人远程操作您的手机或共享屏幕。",
        "大额转账前，先给子女或家人打个电话确认。",
    ]
    return {
        "bound": bool(guardian),
        "guardian": guardian,
        "alerts": alerts,
        "alert_count": len(alerts),
        "tips": tips,
        "relations": list(GUARDIAN_RELATIONS),
    }
