"""银行理财风险承受能力评估问卷与计分。"""

RISK_TYPE_MAP = [
    (15, "保守型", "R1", "风险承受能力很低，投资很谨慎，不能接受本金损失", "一级"),
    (35, "谨慎型", "R2", "风险承受能力较低，对投资回报要求不高，能接受极小概率的本金损失", "一级、二级"),
    (60, "稳健型", "R3", "追求风险与收益的均衡，能接受较小概率的本金损失", "一级、二级、三级"),
    (80, "进取型", "R4", "倾向于追求投资收益，能接受中等概率的本金损失", "一级、二级、三级、四级"),
    (100, "激进型", "R5", "积极追求投资收益，能接受较高概率的本金损失", "一级、二级、三级、四级、五级"),
]

PRODUCT_RISK_LEGEND = [
    {"level": "一级 / R1", "desc": "提供本金保障"},
    {"level": "二级 / R2", "desc": "不提供本金保障，但本金亏损和预期收益不能实现的概率极低"},
    {"level": "三级 / R3", "desc": "不提供本金保障，本金亏损和预期收益不能实现的概率较低"},
    {"level": "四级 / R4", "desc": "不提供本金保障，本金亏损概率较低，但预期收益存在一定不确定性"},
    {"level": "五级 / R5", "desc": "不提供本金保障，本金亏损概率较高，预期收益实现的不确定性较大"},
]

OCCUPATIONS = [
    {"value": "企事业职员", "label": "企事业职员"},
    {"value": "公务员/事业单位", "label": "公务员 / 事业单位"},
    {"value": "专业技术人员", "label": "专业技术人员"},
    {"value": "医护人员", "label": "医护人员"},
    {"value": "教师/科研", "label": "教师 / 科研"},
    {"value": "金融从业", "label": "金融从业"},
    {"value": "销售/市场", "label": "销售 / 市场"},
    {"value": "个体工商户", "label": "个体工商户"},
    {"value": "私营企业主", "label": "私营企业主"},
    {"value": "自由职业", "label": "自由职业"},
    {"value": "制造业工人", "label": "制造业工人"},
    {"value": "服务业从业", "label": "服务业从业"},
    {"value": "农林牧渔", "label": "农林牧渔"},
    {"value": "学生", "label": "学生"},
    {"value": "退休", "label": "退休"},
    {"value": "无业/待业", "label": "无业 / 待业"},
    {"value": "其他", "label": "其他"},
]

# 分值越高越进取。年龄题：年轻分高、年长分低。
SURVEY_QUESTIONS = [
    {
        "id": "q1",
        "no": 1,
        "title": "您的年龄是？",
        "options": [
            {"key": "A", "text": "18–25 岁", "score": 8},
            {"key": "B", "text": "26–50 岁", "score": 10},
            {"key": "C", "text": "51–60 岁", "score": 5},
            {"key": "D", "text": "61–65 岁", "score": 2},
            {"key": "E", "text": "高于 65 岁", "score": 0},
        ],
    },
    {
        "id": "q2",
        "no": 2,
        "title": "您的家庭总资产净值为（折合人民币）？（不含自用住宅和私营企业等实业投资，含储蓄、保险、金融投资、实物投资，并扣除未结清贷款、信用卡账单等债务）",
        "options": [
            {"key": "A", "text": "15 万元及以下", "score": 1},
            {"key": "B", "text": "15 万元（不含）–50 万元（含）", "score": 4},
            {"key": "C", "text": "50 万元（不含）–100 万元（含）", "score": 6},
            {"key": "D", "text": "100 万元（不含）–1000 万元（含）", "score": 8},
            {"key": "E", "text": "1000 万元（不含）以上", "score": 10},
        ],
    },
    {
        "id": "q3",
        "no": 3,
        "title": "在您的家庭总资产净值中，可用于金融投资（储蓄存款除外）的比例为？",
        "options": [
            {"key": "A", "text": "小于 10%", "score": 1},
            {"key": "B", "text": "10% 至 25%", "score": 4},
            {"key": "C", "text": "25% 至 50%", "score": 6},
            {"key": "D", "text": "大于 50%", "score": 8},
        ],
    },
    {
        "id": "q4",
        "no": 4,
        "title": "以下哪项最符合您目前的投资配置？",
        "options": [
            {"key": "A", "text": "除存款、国债外，我几乎不投资其他金融产品", "score": 1},
            {"key": "B", "text": "大部分投资于存款、国债等，较少投资于股票、基金等风险产品", "score": 4},
            {"key": "C", "text": "资产均衡分布于存款、国债、银行理财、信托、股票、基金等", "score": 6},
            {"key": "D", "text": "大部分投资于股票、基金、外汇等高风险产品，较少投资于存款、国债", "score": 8},
        ],
    },
    {
        "id": "q5",
        "no": 5,
        "title": "您有多少年投资股票、基金、外汇、金融衍生产品等风险投资品的经验？",
        "options": [
            {"key": "A", "text": "没有经验", "score": 1},
            {"key": "B", "text": "有经验，但少于 2 年", "score": 3},
            {"key": "C", "text": "2 至 5 年", "score": 5},
            {"key": "D", "text": "5 至 8 年", "score": 7},
            {"key": "E", "text": "8 年以上", "score": 9},
        ],
    },
    {
        "id": "q6",
        "no": 6,
        "title": "以下哪项描述最符合您的投资态度？",
        "options": [
            {"key": "A", "text": "厌恶风险，不希望本金损失，希望获得稳定回报", "score": 1},
            {"key": "B", "text": "保守投资，不希望本金损失，愿意承担一定幅度的收益波动", "score": 4},
            {"key": "C", "text": "寻求资金的较高收益和成长性，愿意为此承担有限本金损失", "score": 7},
            {"key": "D", "text": "希望赚取高回报，愿意为此承担较大本金损失", "score": 10},
        ],
    },
    {
        "id": "q7",
        "no": 7,
        "title": "本金 100 万元，不提供保本承诺的情况下，您会选择哪一种投资机会？",
        "options": [
            {"key": "A", "text": "有 100% 的机会赢取 1000 元现金，并保证归还本金", "score": 2},
            {"key": "B", "text": "有 50% 的机会赢取 5 万元现金，并有较高可能性归还本金", "score": 5},
            {"key": "C", "text": "有 25% 的机会赢取 50 万元现金，并有一定可能性损失本金", "score": 8},
            {"key": "D", "text": "有 10% 的机会赢取 100 万元现金，并有较高可能性损失本金", "score": 10},
        ],
    },
    {
        "id": "q8",
        "no": 8,
        "title": "投资于理财、股票、基金等金融投资品（不含存款和国债）时，您可接受的最长投资期限是多久？",
        "options": [
            {"key": "A", "text": "1 年以下", "score": 2},
            {"key": "B", "text": "1–3 年", "score": 4},
            {"key": "C", "text": "3–5 年", "score": 6},
            {"key": "D", "text": "5 年以上", "score": 8},
        ],
    },
    {
        "id": "q9",
        "no": 9,
        "title": "您更期望的投资结果是？",
        "options": [
            {"key": "A", "text": "本金安全，收益接近存款即可", "score": 1},
            {"key": "B", "text": "收益略高于存款，可接受很小回撤", "score": 3},
            {"key": "C", "text": "追求较高收益，可接受一定波动", "score": 6},
            {"key": "D", "text": "追求资产迅速增值，可接受较大回撤", "score": 9},
        ],
    },
    {
        "id": "q10",
        "no": 10,
        "title": "您的投资出现何种程度的波动时，您会呈现明显的焦虑？",
        "options": [
            {"key": "A", "text": "本金无损失，但收益未达预期", "score": 1},
            {"key": "B", "text": "出现轻微本金损失", "score": 3},
            {"key": "C", "text": "本金 10% 以内的损失", "score": 5},
            {"key": "D", "text": "本金 20%–50% 的损失", "score": 7},
            {"key": "E", "text": "本金 50% 以上损失", "score": 9},
        ],
    },
    {
        "id": "q11",
        "no": 11,
        "title": "对您而言，保本比高收益更为重要？",
        "options": [
            {"key": "A", "text": "非常同意", "score": 1},
            {"key": "B", "text": "同意", "score": 3},
            {"key": "C", "text": "无所谓", "score": 5},
            {"key": "D", "text": "不同意", "score": 7},
            {"key": "E", "text": "非常不同意", "score": 9},
        ],
    },
]

_OPTION_INDEX = {
    q["id"]: {opt["key"]: opt for opt in q["options"]} for q in SURVEY_QUESTIONS
}

AGE_BAND_MAP = {
    "A": "18-25",
    "B": "26-50",
    "C": "51-60",
    "D": "61-65",
    "E": "65+",
}


def classify_score(score: int) -> dict:
    for cap, risk_type, r_level, meaning, suitable in RISK_TYPE_MAP:
        if score <= cap:
            return {
                "score": score,
                "risk_type": risk_type,
                "risk_level": r_level,
                "meaning": meaning,
                "suitable_products": suitable,
            }
    cap, risk_type, r_level, meaning, suitable = RISK_TYPE_MAP[-1]
    return {
        "score": score,
        "risk_type": risk_type,
        "risk_level": r_level,
        "meaning": meaning,
        "suitable_products": suitable,
    }


def score_answers(answers: dict) -> dict:
    """answers: {q1: 'A', q2: 'B', ...}"""
    total = 0
    missing = []
    for q in SURVEY_QUESTIONS:
        key = (answers.get(q["id"]) or "").strip().upper()
        opt = _OPTION_INDEX[q["id"]].get(key)
        if not opt:
            missing.append(q["no"])
            continue
        total += int(opt["score"])
    if missing:
        raise ValueError(f"请完成第 { '、'.join(str(n) for n in missing) } 题")
    result = classify_score(total)
    result["age_band"] = AGE_BAND_MAP.get((answers.get("q1") or "").strip().upper(), "")
    return result


def get_survey_card_payload() -> dict:
    return {
        "title": "投资者风险承受能力评估",
        "subtitle": "监管要求：推荐理财前须完成有效评估（一年内有效）",
        "occupations": OCCUPATIONS,
        "questions": [
            {
                "id": q["id"],
                "no": q["no"],
                "title": q["title"],
                "options": [{"key": o["key"], "text": o["text"]} for o in q["options"]],
            }
            for q in SURVEY_QUESTIONS
        ],
        "score_bands": [
            {"range": "−9～15 分", "type": "保守型", "level": "R1", "suitable": "一级"},
            {"range": "16～35 分", "type": "谨慎型", "level": "R2", "suitable": "一级至二级"},
            {"range": "36～60 分", "type": "稳健型", "level": "R3", "suitable": "一级至三级"},
            {"range": "61～80 分", "type": "进取型", "level": "R4", "suitable": "一级至四级"},
            {"range": "81～100 分", "type": "激进型", "level": "R5", "suitable": "一级至五级"},
        ],
        "product_risk_legend": PRODUCT_RISK_LEGEND,
        "answers": {},
        "occupation": "",
    }
