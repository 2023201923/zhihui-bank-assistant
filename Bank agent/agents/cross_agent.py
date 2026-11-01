"""跨场景资金安排：把一笔钱单独预留出来，并可登记提醒。

2026-10-23：原实现是一个只会办「生日送花」的子助手——提示词、唯一的工具、
确认单文案全部写死为生日场景，任何输入都会被掰成生日方案（客户说给朋友买
保暖内衣，它也能编出「下个月生日前三天提醒」）。

现在改为通用结构化实现：
- 用途、金额、时间、受益人全部来自客户原话，缺什么问什么；
- 不假设生日、不默认金额、不自动加「鲜花蛋糕」；
- 这个模块只管资金安排与提醒，不负责推荐商品。
"""

import re


def parse_amount(value) -> float:
    """把客户口里的金额解析成数字：支持区间取上限、1k 这类写法。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", "").replace("，", "")
    text = re.sub(
        r"(\d+(?:\.\d+)?)\s*[kK]",
        lambda m: str(float(m.group(1)) * 1000),
        text,
    )
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if not nums:
        return 0.0
    # 区间（800-1000 / 800~1k）取上限，和工具说明保持一致
    return float(nums[-1])


def stage_cross_plan(user_id: str, args: dict, history: list, customer_texts=None) -> dict:
    """生成专款预留确认单。日期、赎回和落库在 scenario_service 里完成。"""
    from scenario_service import stage_cross_plan as _stage

    return _stage(user_id, args, history, customer_texts)
