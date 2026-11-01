"""亲情反诈守护：安全防护中心卡片的文案生成。

2026-10-15：绑定与解析逻辑已改为结构化工具（tools.py 的 bind_guardian），
原先靠正则从自然语言里抠「姓名 / 关系 / 手机号」的做法已删除——
姓名这类信息让模型去理解远比剥停用词可靠。
"""


def _center_text(center: dict, user_name: str) -> str:
    if center.get("bound"):
        g = center["guardian"]
        text = (
            f"已为{user_name}绑定紧急联系人：{g['relation']}{g['name']}（{g['phone_masked']}）。"
            "一旦发生高风险转账，会立即短信通知TA。"
        )
        if center.get("alert_count"):
            text += f"近期已推送 {center['alert_count']} 条预警。"
        return text
    return (
        "您还没有绑定紧急联系人。绑定后，一旦系统识别到高风险转账，"
        "会立即短信通知您的家人，让他们帮您一起把关。"
    )


