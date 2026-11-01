"""阶段二：画像与现金流驱动的效用匹配。"""

from database import (
    get_cashflow_profile,
    get_user_risk_profile,
    get_wealth_holdings,
    get_wealth_products_grouped,
    is_silver_age,
)

# 2026-10-06 新增：画像与配置建议相关常量
FREELANCE_OCCUPATIONS = (
    "自由职业",
    "个体工商户",
    "私营企业主",
    "销售/市场",
    "服务业从业",
    "其他",
)

BUCKET_HINT = {
    # 只描述功能，不点具体产品名——这里点了名，模型就会把简称当成产品名照抄出去。
    "活钱管理": "随时可取，对应活期类产品",
    "稳健增值": "锁定期 30–180 天，对应月度与半年期产品",
    "长期增值": "持有 180 天以上，对应半年与年度期产品",
}

# 2026-10-15 新增：客户说的「这笔钱什么时候用」→ 产品期限窗口。
# 用钱时间是客户真正能回答的问题，也是理财经理一定会先问的一句。
HORIZON_WINDOW = {
    "活钱": (0, 0),
    "七日": (0, 7),
    "月度": (7, 30),
    "半年": (30, 180),
    "年度": (180, 360),
}

HORIZON_LABEL = {
    "活钱": "这笔钱随时可能要用",
    "七日": "这笔钱一周内用不上",
    "月度": "这笔钱一个月内用不上",
    "半年": "这笔钱半年内用不上",
    "年度": "这笔钱一年以上用不上",
}

# 把内部画像标签翻成客户能对上号的说法
STYLE_PLAIN = {
    "高周转·偏流动性": "资金进出比较频繁，需要留一部分随时能取",
    "沉淀资金·偏稳健锁期": "账上有沉淀的闲钱，可以适当锁定期限",
    "活钱+锁定期哑铃配置": "既需要留备用金，也有一笔长期不动的钱",
    "均衡配置": "收支比较平稳",
}

BASE_ALLOCATION = {
    "R1": {"活钱管理": 60, "稳健增值": 35, "长期增值": 5},
    "R2": {"活钱管理": 50, "稳健增值": 35, "长期增值": 15},
    "R3": {"活钱管理": 40, "稳健增值": 35, "长期增值": 25},
    "R4": {"活钱管理": 30, "稳健增值": 35, "长期增值": 35},
    "R5": {"活钱管理": 25, "稳健增值": 30, "长期增值": 45},
}


def _risk_num(level: str) -> int:
    return int(level[1]) if level and str(level).startswith("R") and len(level) > 1 else 3


def _summary(product: dict) -> dict:
    keys = (
        "product_id", "name", "category", "sub_category", "risk_level", "risk_label",
        "term_days", "term_label", "term_sub_label", "yield_pct", "yield_label",
        "expected_yield", "min_amount", "benchmark_desc", "asset_direction", "tags",
    )
    item = {key: product[key] for key in keys}
    return item


def _term_bucket(term_days) -> str:
    """2026-10-06 新增：把产品按持有期分档，便于做期限梯队。"""
    term = int(term_days or 0)
    if term <= 0:
        return "活期"
    if term <= 7:
        return "超短"
    if term <= 30:
        return "短期"
    if term <= 180:
        return "中期"
    return "长期"


def _diversify(scored: list, limit: int) -> list:
    """在打分排序基础上做期限分散：先每档取最优，再用剩余高分补齐。"""
    picked = []
    picked_ids = set()
    used_buckets = set()
    for item in scored:
        if len(picked) >= limit:
            break
        bucket = _term_bucket(item.get("term_days"))
        if bucket in used_buckets:
            continue
        picked.append(item)
        picked_ids.add(item["product_id"])
        used_buckets.add(bucket)
    for item in scored:
        if len(picked) >= limit:
            break
        if item["product_id"] in picked_ids:
            continue
        picked.append(item)
        picked_ids.add(item["product_id"])
    return picked


def _allocation_for(risk_level: str, high_turnover: bool, stable_idle: bool, older: bool) -> list:
    """按风险等级与流动性画像给出「活钱 / 稳健 / 长期」配置比例建议。未测评不套用默认等级。"""
    if risk_level not in BASE_ALLOCATION:
        return []
    base = dict(BASE_ALLOCATION[risk_level])
    if high_turnover:
        base["活钱管理"] += 10
        base["长期增值"] -= 10
    if stable_idle:
        base["长期增值"] += 10
        base["活钱管理"] -= 10
    if older:
        base["活钱管理"] += 15
        base["长期增值"] -= 15
    for key in base:
        base[key] = max(int(base[key]), 5)
    total = sum(base.values()) or 1
    rows = []
    ordered = ("活钱管理", "稳健增值", "长期增值")
    for idx, key in enumerate(ordered):
        if idx < len(ordered) - 1:
            percent = round(base[key] * 100.0 / total)
        else:
            # 最后一项吸收四舍五入误差，保证三项合计为 100%
            percent = 100 - sum(row["percent"] for row in rows)
        rows.append(
            {
                "bucket": key,
                "percent": percent,
                "hint": BUCKET_HINT[key],
            }
        )
    return rows


def build_profile_snapshot(user_id: str) -> dict:
    """2026-10-06 新增：汇总「风险画像 + 现金流画像 + 持仓」，供画像卡与推荐共用。"""
    profile = get_user_risk_profile(user_id)
    cash = get_cashflow_profile(user_id)
    holdings = get_wealth_holdings(user_id)
    r_level = profile.get("risk_level") or ""
    age_band = profile.get("age_band") or ""
    occupation = profile.get("occupation") or ""
    older = is_silver_age(age_band)
    freelance = occupation in FREELANCE_OCCUPATIONS

    style = "均衡配置"
    if cash["high_turnover"] and not cash["stable_idle"]:
        style = "高周转·偏流动性"
    elif cash["stable_idle"] and not cash["high_turnover"]:
        style = "沉淀资金·偏稳健锁期"
    elif cash["high_turnover"] and cash["stable_idle"]:
        style = "活钱+锁定期哑铃配置"

    tags = []
    if profile.get("risk_type") and r_level:
        tags.append(f"{profile['risk_type']} {r_level}")
    if older:
        tags.append("银发客群")
    if age_band:
        tags.append(str(age_band))
    if occupation:
        tags.append(occupation)
    if cash["high_turnover"]:
        tags.append("资金周转频繁")
    if cash["stable_idle"]:
        tags.append("闲钱沉淀充足")
    if not cash["high_turnover"] and not cash["stable_idle"]:
        tags.append("现金流中性")
    if hold_count := holdings["count"]:
        tags.append(f"已持 {hold_count} 只")

    return {
        "profile": profile,
        "cashflow": cash,
        "holdings": holdings,
        "risk_level": r_level or None,
        "risk_label": r_level or "未测评",
        "style": style,
        "older": older,
        "freelance": freelance,
        "tags": tags,
        "allocation": _allocation_for(
            r_level, cash["high_turnover"], cash["stable_idle"], older
        ),
    }


def match_wealth_products(user_id: str, limit: int = 3, horizon: str = "") -> dict:
    """按画像 + 用钱时间匹配产品。

    2026-10-15：新增 horizon（客户说的「这笔钱什么时候用」），并把 headline/explain
    从「系统播报」改成理财经理口吻的「依据 → 方案 → 请客户确认」。
    """
    snapshot = build_profile_snapshot(user_id)
    profile = snapshot["profile"]
    cash = snapshot["cashflow"]
    r_level = snapshot["risk_level"]
    style = snapshot["style"]
    held = {h["product_name"]: h for h in snapshot["holdings"]["holdings"]}
    candidates = get_wealth_products_grouped(max_risk_level=r_level)

    age_band = profile.get("age_band") or ""
    occupation = profile.get("occupation") or ""
    older = snapshot["older"]
    freelance = snapshot["freelance"]

    # 先按客户说的用钱时间收敛期限范围
    horizon_line = ""
    window = HORIZON_WINDOW.get(horizon)
    if window:
        lo, hi = window
        narrowed = [p for p in candidates if lo <= int(p.get("term_days") or 0) <= hi]
        if narrowed:
            candidates = narrowed
        horizon_line = HORIZON_LABEL.get(horizon, "") + "，我按这个时间来安排。"

    scored = []
    for p in candidates:
        term = int(p.get("term_days") or 0)
        score = 40
        reasons = []

        # 打分优先级：客户明说的用钱时间 > 账户流水推断。
        # 客户既然说了「这笔钱半年内不用」，就不该再用「进出频繁」把他推回 7 天产品。
        if window:
            _, hi = window
            if hi <= 0:
                score += 30
                reasons.append("您提到这笔钱随时可能要用，这款随时能取")
            else:
                score += max(0, 24 - abs(hi - term) // 7)
                if abs(hi - term) <= 30:
                    reasons.append(
                        f"您提到{HORIZON_LABEL.get(horizon, '这个用钱时间')}，"
                        f"这款的期限和您说的用钱时间对得上"
                    )
        else:
            if cash["high_turnover"] or freelance:
                if term <= 7:
                    score += 35
                    reasons.append("您近期资金进出比较频繁，这款随时能取，不会耽误用钱")
                elif term <= 30:
                    score += 8
            if cash["stable_idle"]:
                if 30 <= term <= 90:
                    score += 32
                    reasons.append(
                        f"您账上有约 ¥{cash['idle_cash']:.0f} 的闲钱短期用不上，放长一点更划算"
                    )
                elif term >= 180:
                    score += 10
            if not cash["high_turnover"] and not cash["stable_idle"]:
                if term <= 30:
                    score += 15
                    reasons.append("您的收支比较平稳，先用短期产品打底，随时可以再调整")

        if older:
            if term <= 90 and _risk_num(p["risk_level"]) <= 2:
                score += 12
                reasons.append("考虑到您的年龄，我优先选回撤可控、期限适中的")
            if term >= 360:
                score -= 8

        if _risk_num(p["risk_level"]) == _risk_num(r_level):
            score += 4
        if term == 0:
            score += 2

        # 持仓感知，避免重复推荐已持有的产品
        hold = held.get(p["name"])
        if hold:
            score -= 12
            reasons.append(
                f"您已持有 ¥{hold['amount']:.0f}，可以考虑加仓，也可以换个期限做搭配"
            )

        if not reasons:
            reasons.append(f"风险等级在您可承受的 {r_level} 范围内")

        item = _summary(p)
        item["plain"] = p.get("plain") or {}
        item["match_score"] = score
        item["reason"] = reasons[0]
        item["reasons"] = reasons
        item["held"] = bool(hold)
        item["held_amount"] = round(hold["amount"], 2) if hold else 0.0
        scored.append(item)

    # 同分时按期限、再按收益排序，保证每次跑出来的结果一致（不依赖数据库返回顺序）
    scored.sort(key=lambda x: (-x["match_score"], x["term_days"], -x["expected_yield"]))
    # 2026-10-06：在打分排序基础上做期限分散，避免推荐全部挤在同一持有期
    top = _diversify(scored, limit)

    # ==== 依据：先说清「我为什么这么筛」====
    eval_date = str(profile.get("risk_assessment_date") or "")[:10]
    risk_type = profile.get("risk_type")
    style_plain = STYLE_PLAIN.get(style, "")
    lead = (
        f"根据您最近一次风险测评（{eval_date}）的结果" if eval_date else "根据您的风险测评"
    )
    who = f"您属于{risk_type}（{r_level}）" if risk_type else f"您的风险等级为 {r_level}"
    style_sentence = f"从账户情况看，您{style_plain}。" if style_plain else ""
    headline = (
        f"{lead}，{who}。"
        f"{style_sentence}"
        f"我按这个方向为您筛选，没有纳入超出 {r_level} 的产品。"
    )

    # ==== 方案：钱怎么分、为什么这么分 ====
    explain = []
    if horizon_line:
        explain.append(horizon_line.rstrip("。"))
    if cash["idle_cash"] > 0:
        explain.append(
            f"您的可用活期约 ¥{cash['balance']:,.0f}，扣掉约 3 个月日常开支"
            f"（每月约 ¥{cash['monthly_expense']:,.0f}）后，可安排的闲钱约 "
            f"¥{cash['idle_cash']:,.0f}"
        )
    else:
        # 原有实现不管余额够不够都会说「闲钱约 ¥0」，再往下推荐产品就自相矛盾了
        reserve = cash["monthly_expense"] * 3
        explain.append(
            f"您的可用活期约 ¥{cash['balance']:,.0f}，按每月约 "
            f"¥{cash['monthly_expense']:,.0f} 的开支，我建议先留足 3 个月备用金"
            f"（约 ¥{reserve:,.0f}）；这次想投的金额不大的话，先从活钱类产品起步更稳妥"
        )
    if top:
        ladder = "、".join(
            f"{p['name']}（{_term_bucket(p.get('term_days'))}）" for p in top
        )
        explain.append(f"期限上这样搭配更合适：{ladder}")
    if snapshot["holdings"]["count"]:
        explain.append(
            f"您目前持有 {snapshot['holdings']['count']} 只理财、合计约 "
            f"¥{snapshot['holdings']['total_amount']:,.0f}，这次我尽量避开重复配置"
        )

    confirm_line = (
        "以上是按您的画像做的初步安排，您看是否合适？"
        "如果金额分配或期限想调整，直接跟我说，我重新给您安排一版。"
    )

    return {
        "profile": profile,
        "cashflow": cash,
        "holdings": snapshot["holdings"],
        "tags": snapshot["tags"],
        "allocation": snapshot["allocation"],
        "style": style,
        "style_plain": style_plain,
        "headline": headline,
        "explain": explain,
        "confirm_line": confirm_line,
        "horizon": horizon,
        "products": top,
    }
