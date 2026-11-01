import json
import sqlite3

from database import (
    DB_PATH,
    ensure_card_limit_schema,
    ensure_user_profile_schema,
    hash_password,
)

# 2026-10-06 新增：各客户默认付款储蓄卡，用于回填历史转账流水的 card_id，
# 使「按卡统计日/年累计额度」在演示数据上就能看到效果。
TRANSFER_CARD_BY_USER = {
    "user_001": "622208******8801",
    "user_002": "622202******2210",
    "user_003": "622202******3309",
    "user_004": "622202******4412",
    "user_005": "622202******5501",
}

def init_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. 用户表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        risk_level TEXT,                 -- R1-R5，未测评为空
        risk_type TEXT,                  -- 保守型/谨慎型/稳健型/进取型/激进型
        risk_score INTEGER,
        risk_assessment_date TEXT,       -- 最近一次评估日期 YYYY-MM-DD
        age_band TEXT,
        occupation TEXT,
        survey_json TEXT,
        password_hash TEXT
    )
    """)
    ensure_user_profile_schema(conn)

    # 2. 资金账户表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        balance REAL NOT NULL,       -- 可用活期余额
        locked_funds REAL DEFAULT 0, -- 跨场景锁定的活期资金
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    """)

    # 3. 交易流水表 (用于智能转账记录与账单分析)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        tx_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        amount REAL NOT NULL,        -- 交易金额 (正数支出，负数收入)
        category TEXT NOT NULL,      -- 餐饮、交通、购物、转账、娱乐等
        counterparty TEXT NOT NULL,  -- 对手方名称 / 商户名
        notes TEXT,                  -- 备注
        card_id TEXT,                -- 2026-10-06 新增：关联付款卡，用于按卡统计转账限额
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    """)

    # 4. 卡片管理表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cards (
        card_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        card_type TEXT NOT NULL,     -- 储蓄卡 / 信用卡
        credit_limit REAL,           -- 信用额度
        daily_limit REAL,            -- 单日累计交易限额（金额）
        status TEXT DEFAULT 'NORMAL',-- NORMAL(正常), FROZEN(冻结), LOST(挂失)
        single_limit REAL,           -- 2026-10-06 新增：单笔交易限额
        daily_count_limit INTEGER,   -- 2026-10-06 新增：单日累计交易笔数上限
        annual_limit REAL,           -- 2026-10-06 新增：年度累计转账限额
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    """)
    ensure_card_limit_schema(conn)

    # 5. 订阅/代扣服务表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        sub_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        merchant TEXT NOT NULL,      -- 签约商户 (如爱奇艺、网易云)
        amount REAL NOT NULL,        -- 每期代扣金额
        billing_cycle TEXT NOT NULL, -- MONTHLY / YEARLY
        next_billing_date DATE,      -- 下次扣费日期
        status TEXT DEFAULT 'ACTIVE',-- ACTIVE(生效中), CANCELLED(已取消)
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    """)

    # 清空旧测试数据并初始化样例
    cursor.execute("DELETE FROM subscriptions")
    cursor.execute("DELETE FROM cards")
    cursor.execute("DELETE FROM transactions")
    cursor.execute("DELETE FROM accounts")
    cursor.execute("DELETE FROM guardian_alerts")
    cursor.execute("DELETE FROM users")

    _seed_users(cursor)

    _seed_wealth_products(cursor)

    conn.commit()
    conn.close()
    print(f"数据库初始化完成：{DB_PATH}")


def _seed_users(cursor):
    """写入 users 表中的五位客户，登录密码与交易密码均为 123456，流水彼此独立。"""
    pwd = hash_password("123456")
    users = [
        ("user_001", "马鹿马", "13800001111", None, None, None, None, None, None, pwd),
        (
            "user_002", "朴综星", "13800002222", None, None, None,
            None, "26-30岁", "艺人", pwd,
        ),
        (
            "user_003", "朴成训", "13800003333", "R3", "稳健型", 58,
            "2026-08-20", "26-30岁", "艺人", pwd,
        ),
        (
            "user_004", "高苏尧", "13800004444", "R1", "保守型", 18,
            "2026-07-15", "60岁以上", "退休", pwd,
        ),
        (
            "user_005", "吴世勋", "13800005555", None, None, None,
            None, "26-30岁", "艺人", pwd,
        ),
    ]
    cursor.executemany(
        """
        INSERT INTO users (
            user_id, name, phone, risk_level, risk_type, risk_score,
            risk_assessment_date, age_band, occupation, password_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        users,
    )
    # 2026-10-08 新增：为银发客群预置紧急联系人（用于演示亲情反诈预警）
    cursor.executemany(
        """
        UPDATE users
        SET guardian_name = ?, guardian_phone = ?, guardian_relation = ?
        WHERE user_id = ?
        """,
        [
            ("周可", "13800004466", "女儿", "user_004"),
        ],
    )
    cursor.executemany(
        "INSERT INTO accounts VALUES (?, ?, ?, ?)",
        [
            ("acc_001", "user_001", 18650.0, 0.0),
            ("acc_002", "user_002", 52800.0, 2000.0),
            ("acc_003", "user_003", 12800.0, 0.0),
            ("acc_004", "user_004", 86320.0, 0.0),
            ("acc_005", "user_005", 24500.0, 0.0),
        ],
    )
    cursor.executemany(
        """
        INSERT INTO cards (
            card_id, user_id, card_type, credit_limit,
            daily_limit, status, single_limit, daily_count_limit, annual_limit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # 信用卡：不支持转账/申购，限额仅作展示
            ("622202******1234", "user_001", "信用卡", 20000.0, 5000.0, "NORMAL", None, 20, None),
            # 储蓄卡：参与转账四维限额校验（单笔 / 日累计金额 / 日累计笔数 / 年累计金额）
            ("622208******8801", "user_001", "储蓄卡", None, 50000.0, "NORMAL", 50000.0, 20, 5000000.0),
            ("622202******2210", "user_002", "储蓄卡", None, 20000.0, "NORMAL", 20000.0, 10, 2000000.0),
            ("622202******2218", "user_002", "信用卡", 30000.0, 8000.0, "NORMAL", None, 20, None),
            ("622202******3306", "user_003", "信用卡", 15000.0, 8000.0, "NORMAL", None, 20, None),
            ("622202******3309", "user_003", "储蓄卡", None, 20000.0, "NORMAL", 20000.0, 10, 2000000.0),
            ("622202******4412", "user_004", "储蓄卡", None, 10000.0, "NORMAL", 10000.0, 5, 1000000.0),
            ("622202******5501", "user_005", "储蓄卡", None, 30000.0, "NORMAL", 30000.0, 10, 3000000.0),
            ("622202******5508", "user_005", "信用卡", 18000.0, 6000.0, "NORMAL", None, 20, None),
        ],
    )
    sample_txs = [
        ("tx_u1_s1", "user_001", -15000.0, "工资", "智汇银行代发", "8月工资", "2026-08-05 09:00:00"),
        ("tx_u1_s2", "user_001", -15000.0, "工资", "智汇银行代发", "9月工资", "2026-09-05 09:00:00"),
        ("tx_u1_01", "user_001", 35.5, "餐饮", "美团外卖", "午餐", "2026-09-15 12:30:00"),
        ("tx_u1_02", "user_001", 128.0, "餐饮", "海底捞火锅", "朋友聚餐", "2026-09-18 19:20:00"),
        ("tx_u1_03", "user_001", 15.0, "交通", "滴滴出行", "通勤打车", "2026-09-20 09:10:00"),
        ("tx_u1_04", "user_001", 299.0, "购物", "京东商城", "数码配件", "2026-09-25 15:00:00"),
        ("tx_u1_05", "user_001", 25.0, "订阅", "网易云音乐", "黑胶VIP月卡", "2026-09-28 00:05:00"),
        ("tx_u1_06", "user_001", 42.0, "餐饮", "瑞幸咖啡", "早餐", "2026-09-22 08:20:00"),
        ("tx_u1_07", "user_001", 68.0, "交通", "地铁充值", "通勤", "2026-09-10 08:00:00"),
        ("tx_u1_08", "user_001", 560.0, "购物", "盒马鲜生", "日用采购", "2026-09-12 19:40:00"),
        ("tx_u1_09", "user_001", 88.0, "餐饮", "肯德基", "晚餐", "2026-09-08 18:10:00"),
        ("tx_u1_10", "user_001", 36.0, "交通", "滴滴出行", "加班打车", "2026-09-27 21:30:00"),
        ("tx_u1_11", "user_001", 199.0, "购物", "天猫超市", "日用品", "2026-08-20 20:00:00"),
        ("tx_u1_12", "user_001", 45.0, "餐饮", "美团外卖", "午餐", "2026-08-18 12:10:00"),
        ("tx_u1_13", "user_001", 3200.0, "转账", "房东陈姐", "9月房租", "2026-09-01 10:00:00"),
        ("tx_u1_14", "user_001", 21.0, "订阅", "iCloud", "云存储", "2026-09-15 00:05:00"),
        ("tx_u1_15", "user_001", 500.0, "转账", "妈妈", "生活费", "2026-09-20 20:10:00"),
        ("tx_u1_16", "user_001", 86.0, "转账", "同事小周", "聚餐AA", "2026-09-18 21:05:00"),
        ("tx_u1_17", "user_001", 168.0, "娱乐", "猫眼电影", "周末电影", "2026-09-21 19:00:00"),
        ("tx_u2_s1", "user_002", -9200.0, "工资", "经纪公司代发", "8月报酬", "2026-08-08 10:00:00"),
        ("tx_u2_s2", "user_002", -9200.0, "工资", "经纪公司代发", "9月报酬", "2026-09-08 10:00:00"),
        ("tx_u2_01", "user_002", 46.0, "餐饮", "餐厅", "工作日午餐", "2026-09-16 12:10:00"),
        ("tx_u2_02", "user_002", 32.0, "餐饮", "咖啡店", "工作日午餐", "2026-09-23 12:05:00"),
        ("tx_u2_03", "user_002", 18.0, "交通", "公交卡充值", "通勤", "2026-09-02 08:20:00"),
        ("tx_u2_04", "user_002", 126.0, "购物", "新华书店", "教辅资料", "2026-09-11 16:40:00"),
        ("tx_u2_05", "user_002", 2000.0, "转账", "父母", "月度生活费", "2026-09-06 19:00:00"),
        ("tx_u2_06", "user_002", 3000.0, "理财", "智汇·月度·进取", "闲钱申购", "2026-09-10 11:20:00"),
        ("tx_u2_07", "user_002", 15.0, "订阅", "爱奇艺", "黄金会员", "2026-09-18 00:05:00"),
        ("tx_u2_08", "user_002", 86.0, "医疗", "社区医院", "体检加项", "2026-08-22 09:30:00"),
        ("tx_u2_09", "user_002", 240.0, "购物", "盒马鲜生", "周末采购", "2026-09-27 18:10:00"),
        ("tx_u2_10", "user_002", 5000.0, "理财", "智汇·半年·稳健", "季度闲钱", "2026-08-15 14:00:00"),
        ("tx_u3_s1", "user_003", -11000.0, "工资", "智汇银行代发", "8月工资", "2026-08-06 09:30:00"),
        ("tx_u3_s2", "user_003", -11000.0, "工资", "智汇银行代发", "9月工资", "2026-09-06 09:30:00"),
        ("tx_u3_01", "user_003", 268.0, "餐饮", "海底捞火锅", "客户宴请", "2026-09-19 20:10:00"),
        ("tx_u3_02", "user_003", 86.0, "交通", "滴滴出行", "拜访客户", "2026-09-19 21:40:00"),
        ("tx_u3_03", "user_003", 1299.0, "购物", "京东商城", "手机配件", "2026-09-14 22:15:00"),
        ("tx_u3_04", "user_003", 49.0, "订阅", "腾讯视频", "会员月卡", "2026-09-21 00:05:00"),
        ("tx_u3_05", "user_003", 1800.0, "转账", "房东", "9月房租", "2026-09-03 09:00:00"),
        ("tx_u3_06", "user_003", 320.0, "娱乐", "KTV", "团队活动", "2026-09-26 22:30:00"),
        ("tx_u3_07", "user_003", 6000.0, "转账", "王强", "临时借款", "2026-10-05 09:12:00"),
        ("tx_u3_08", "user_003", 4500.0, "转账", "李经理", "投资保证金", "2026-10-05 11:40:00"),
        ("tx_u3_09", "user_003", 58.0, "餐饮", "瑞幸咖啡", "早餐", "2026-10-04 08:40:00"),
        # 2026-10-06 新增：理财持仓样例，用于「我的理财画像」与持仓去重推荐演示
        ("tx_u3_10", "user_003", 2000.0, "理财", "智汇·活钱", "活钱打理", "2026-08-20 10:00:00"),
        ("tx_u3_11", "user_003", 5000.0, "理财", "智汇·月度·进取", "季度闲钱", "2026-09-01 14:30:00"),
        ("tx_u4_s1", "user_004", -6800.0, "养老金", "社保中心", "8月养老金", "2026-08-10 08:00:00"),
        ("tx_u4_s2", "user_004", -6800.0, "养老金", "社保中心", "9月养老金", "2026-09-10 08:00:00"),
        ("tx_u4_01", "user_004", 186.0, "购物", "永辉超市", "日用品", "2026-09-12 10:20:00"),
        ("tx_u4_02", "user_004", 240.0, "医疗", "市人民医院", "门诊", "2026-09-18 09:40:00"),
        ("tx_u4_03", "user_004", 62.0, "餐饮", "社区食堂", "午餐", "2026-09-22 11:30:00"),
        ("tx_u4_04", "user_004", 800.0, "转账", "孙女周可", "开学红包", "2026-09-01 15:00:00"),
        ("tx_u4_05", "user_004", 128.0, "缴费", "国网电力", "电费", "2026-09-16 09:00:00"),
        ("tx_u4_06", "user_004", 96.0, "购物", "药店", "常用药", "2026-09-25 16:10:00"),
        ("tx_u4_07", "user_004", 45.0, "餐饮", "社区食堂", "午餐", "2026-10-02 11:20:00"),
        # 2026-10-06 新增：银发客群持仓样例（R1 保守型只持有活钱管理）
        ("tx_u4_08", "user_004", 10000.0, "理财", "智汇·活钱", "养老钱活期打理", "2026-08-25 09:30:00"),
        ("tx_u5_s1", "user_005", -12000.0, "工资", "经纪公司代发", "8月报酬", "2026-08-07 10:00:00"),
        ("tx_u5_s2", "user_005", -12000.0, "工资", "经纪公司代发", "9月报酬", "2026-09-07 10:00:00"),
        ("tx_u5_01", "user_005", 88.0, "餐饮", "咖啡店", "早餐", "2026-09-14 09:00:00"),
        ("tx_u5_02", "user_005", 520.0, "购物", "品牌服饰", "换季采购", "2026-09-20 15:30:00"),
        ("tx_u5_03", "user_005", 300.0, "转账", "马鹿马", "聚餐分摊", "2026-09-19 21:00:00"),
        ("tx_u5_04", "user_005", 36.0, "交通", "滴滴出行", "行程", "2026-09-22 22:10:00"),
        ("tx_u5_05", "user_005", 199.0, "娱乐", "演唱会周边", "收藏", "2026-09-26 14:00:00"),
    ]
    cursor.executemany(
        """
        INSERT INTO transactions (
            tx_id, user_id, amount, category, counterparty, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        sample_txs,
    )
    # 2026-10-06：为历史转账流水补齐付款卡，便于按卡统计日/年累计额度
    for owner_id, debit_card_id in TRANSFER_CARD_BY_USER.items():
        cursor.execute(
            """
            UPDATE transactions SET card_id = ?
            WHERE user_id = ? AND category = '转账' AND amount > 0
            """,
            (debit_card_id, owner_id),
        )
    cursor.executemany(
        "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("sub_01", "user_001", "网易云音乐", 25.0, "MONTHLY", "2026-10-28", "ACTIVE"),
            ("sub_02", "user_001", "iCloud 200G", 21.0, "MONTHLY", "2026-10-15", "ACTIVE"),
            ("sub_03", "user_002", "爱奇艺", 15.0, "MONTHLY", "2026-10-18", "ACTIVE"),
            ("sub_04", "user_003", "腾讯视频", 49.0, "MONTHLY", "2026-10-21", "ACTIVE"),
        ],
    )


def _plain_text(text: str) -> str:
    """展示文案统一去掉句末句号，避免同一块信息标点不一致。"""
    if not text:
        return text
    return str(text).replace("。", "")


def _detail(
    code, slogan, highlights, buyers, since_return, periods, bars, start, end,
    term_days, term_label, allocations, spotlight, aliases, plain=None,
):
    if term_days <= 0:
        timeline = ["现在买入", "实时确认", "实时可赎"]
        closed = _plain_text("无封闭期，资金实时可赎回")
        confirm = _plain_text("全天可买，实时确认份额")
        cancel = _plain_text("实时成交，不支持撤单")
        redeem_way = _plain_text("实时赎回，赎回资金实时可用")
        arrival = _plain_text("实时到账")
    else:
        timeline = ["现在买入", "下一交易日确认份额", term_label]
        closed = _plain_text(f"本产品设置最短持有期，最短持有 {term_days} 天内封闭运作，不能赎回")
        confirm = _plain_text("每交易日 17:00 前购买按当日申请，通常于下一交易日确认份额")
        cancel = _plain_text("交易日 17:00 前的购买申请，可于当日 17:00 前撤销")
        redeem_way = _plain_text(f"最短持有期内不能赎回，持有满 {term_days} 天后每个交易日可赎回")
        arrival = _plain_text("赎回最快 T+2 到账，遇非交易日顺延")
    payload = {
        "product_code": code,
        "slogan": slogan,
        "highlights": highlights,
        "buyer_count": buyers,
        "aliases": aliases,
        # 2026-10-15 新增：人话翻译层。合规字段继续给说明书用，
        # 这里的说法给理财经理的对话用（when/yield/asset/fit/note）。
        "plain": plain or {},
        "performance": {
            "since_inception_return": since_return,
            "bars": bars,
            "start_date": start,
            "end_date": end,
            "periods": periods,
        },
        "rules": {
            "timeline": timeline,
            "sale_time": _plain_text("每天 0:00-23:59 可买，每交易日 17:00 前购买视为当日申请"),
            "buy_fee": "不收取",
            "confirm_rule": confirm,
            "cancel_rule": cancel,
            "redeem_fee": "不收取",
            "redeem_way": redeem_way,
            "arrival": arrival,
            "closed_note": closed,
        },
        "issuer": "智汇理财有限责任公司",
        "allocations": allocations,
        "spotlight": spotlight,
        "faqs": [
            {"q": "本产品投资目标和策略是什么？", "a": spotlight["body"]},
            {"q": "产品是保本的吗？本金可能亏损吗？", "a": "不保本。理财非存款，本金和收益可能因市场波动蒙受损失，极端情况下可能损失全部本金。"},
            {"q": "历史收益或业绩比较基准代表实际收益吗？", "a": "不代表。业绩比较基准不是预期收益率，过往业绩不代表未来表现，也不等于产品实际收益。"},
            {"q": "想用钱，封闭期可以提前取出吗？", "a": closed},
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


WEALTH_PRODUCTS = [
    (
        "w_week_plus", "智汇·七日·进取", "稳健低波", "七日", "R2", 7,
        "最短持有7天", "持满后每个交易日可赎", 0.0298, "成立以来年化", 1.0,
        "50%×央行活期存款利率 + 50%×中债0-3个月国债财富指数收益率。",
        "固定收益类资产不低于80%，主投同业存单与高评级短期信用债，严格控制久期与回撤。",
        json.dumps(["R2中低风险", "1元起购", "历史周周正收益", "低波动"], ensure_ascii=False),
        _detail(
            "ZH0702", "放满七日，落袋再续",
            ["精挑细选", "每日可买", "申赎0费用"], "超 12 万次购买",
            "1.46%",
            [
                {"label": "近1月", "change": "0.21%", "annualized": "2.52%"},
                {"label": "近3月", "change": "0.68%", "annualized": "2.74%"},
                {"label": "近6月", "change": "1.41%", "annualized": "2.86%"},
                {"label": "今年以来", "change": "2.05%", "annualized": "2.93%"},
                {"label": "成立以来", "change": "1.46%", "annualized": "2.98%"},
            ],
            [24, 33, 41, 52, 60, 69, 78, 86],
            "2026.03.25", "2026.09.28", 7, "最短持有7天",
            [
                {"name": "固定收益类资产", "limit": "不低于80%"},
                {"name": "现金及流动性资产", "limit": "其余部分"},
            ],
            {"title": "短久期固收，控制回撤", "body": "主投同业存单与高评级短期信用债，严格控制久期与净值波动。"},
            ["七日盈", "七日精进", "七日"],
            plain={
                "when": "放满七天之后，随时可以取出来",
                "yield": "比活期高不少，会有很小的波动",
                "asset": "主要是短期债券和银行存单，不投股票",
                "fit": "一周内用不上的钱，能接受一点点波动",
                "note": "历史上每周都是正收益，但不代表以后一直如此",
            },
        ),
    ),
    (
        "w_month_plus", "智汇·月度·进取", "稳健低波", "月持", "R2", 30,
        "最短持有30天", "持满后每个交易日可赎", 0.0324, "成立以来年化", 0.01,
        "30%×央行活期存款利率 + 70%×中债0-3个月国债财富指数收益率。",
        "固收资产不低于80%，优先股等权益类资产不高于5%，金融衍生品资产不高于5%。",
        json.dumps(["超10万次购买", "历史周周正收益", "0.01元起购"], ensure_ascii=False),
        _detail(
            "ZH3003", "一个月后要用的钱放这里",
            ["精挑细选", "每日可买", "申赎0费用"], "超 16 万次购买",
            "1.72%",
            [
                {"label": "近1月", "change": "0.24%", "annualized": "2.86%"},
                {"label": "近3月", "change": "0.76%", "annualized": "3.05%"},
                {"label": "近6月", "change": "1.55%", "annualized": "3.12%"},
                {"label": "今年以来", "change": "2.21%", "annualized": "3.18%"},
                {"label": "成立以来", "change": "1.72%", "annualized": "3.24%"},
            ],
            [26, 35, 44, 54, 63, 72, 81, 90],
            "2026.03.25", "2026.09.28", 30, "最短持有30天",
            [
                {"name": "固定收益类资产", "limit": "不低于80%"},
                {"name": "权益类资产（仅限优先股）", "limit": "不高于5%"},
                {"name": "金融衍生品类资产", "limit": "不高于5%"},
            ],
            {"title": "主投债券，投资风格偏稳健", "body": "不低于80%固收资产，主投债券。严控组合久期，用票息积累收益并控制回撤。"},
            ["月月安", "月度精进", "月持"],
            plain={
                "when": "放满一个月之后，随时可以取出来",
                "yield": "月度的收益水平，会有小幅波动",
                "asset": "以债券为主，不参与股票二级市场投资",
                "fit": "一个月内用不上、希望收益比活钱高一些的钱",
                "note": "短期不用又不甘心只拿活期利息，可以优先看这款",
            },
        ),
    ),
    # ==== 2026-10-06 新增：扩充货架，覆盖 R1 保守型与更多期限档位 ====
    (
        "w_cash", "智汇·活钱", "活钱管理", "活期", "R1", 0,
        "随时取用", "T+0 快赎，实时到账", 0.0198, "7日年化", 0.01,
        "中国人民银行公布的同期活期存款基准利率。",
        "100% 投资于银行现金、同业存单、政策性金融债与国债逆回购等高流动性资产。",
        json.dumps(["0.01元起购", "T+0快赎", "现金管理"], ensure_ascii=False),
        _detail(
            "ZH8802", "比活期更划算的零钱袋",
            ["精挑细选", "实时可赎", "申赎0费用"], "超 65 万人持有",
            "0.98%",
            [
                {"label": "近1月", "change": "0.16%", "annualized": "1.90%"},
                {"label": "近3月", "change": "0.48%", "annualized": "1.94%"},
                {"label": "近6月", "change": "0.96%", "annualized": "1.96%"},
                {"label": "今年以来", "change": "1.35%", "annualized": "1.97%"},
                {"label": "成立以来", "change": "0.98%", "annualized": "1.95%"},
            ],
            [20, 28, 36, 44, 52, 60, 68, 76],
            "2026.04.10", "2026.09.28", 0, "随时取用",
            [{"name": "现金、同业存单及政策性金融债", "limit": "100%"}],
            {"title": "活期增强，随用随取", "body": "全部投资于高流动性货币市场工具，不参与权益与杠杆，力求本金稳、取用快。"},
            ["随手金", "日日盈", "活钱", "零钱袋"],
            plain={
                "when": "这笔钱随时可能要用，取的时候马上就能用",
                "yield": "比活期存款多一点点，具体多多少不保证",
                "asset": "钱主要放在银行和国债里，是最稳的那一类",
                "fit": "日常备用金、随时要动用的钱",
                "note": "不用挑买入时间，随时能买、随时能取",
            },
        ),
    ),
    (
        "w_week_steady", "智汇·七日·稳健", "稳健低波", "七日", "R1", 7,
        "最短持有7天", "持满后每个交易日可赎", 0.0223, "成立以来年化", 100.0,
        "七天通知存款基准利率 + 30BP。",
        "主投国债、政策性金融债与高评级同业存单，组合久期不超过 0.5 年。",
        json.dumps(["R1低风险", "100元起购", "周周正收益"], ensure_ascii=False),
        _detail(
            "ZH0703", "七天一个周期，稳稳拿利息",
            ["低波动", "每日可买", "申赎0费用"], "超 9 万人持有",
            "1.12%",
            [
                {"label": "近1月", "change": "0.18%", "annualized": "2.16%"},
                {"label": "近3月", "change": "0.54%", "annualized": "2.19%"},
                {"label": "近6月", "change": "1.08%", "annualized": "2.22%"},
                {"label": "今年以来", "change": "1.52%", "annualized": "2.24%"},
                {"label": "成立以来", "change": "1.12%", "annualized": "2.23%"},
            ],
            [18, 26, 33, 41, 49, 56, 64, 71],
            "2026.05.06", "2026.09.28", 7, "最短持有7天",
            [
                {"name": "国债与政策性金融债", "limit": "不低于60%"},
                {"name": "高评级同业存单", "limit": "其余部分"},
            ],
            {"title": "利率债打底，久期更短", "body": "以国债与政策性金融债为主，久期控制在半年以内，净值波动小。"},
            ["周周利", "七日稳健", "通知存款"],
            plain={
                "when": "放满七天之后，随时可以取出来",
                "yield": "收益比活钱高一些，波动很小",
                "asset": "国债和银行存单为主，不碰股票",
                "fit": "一周内用不上、但一点波动都不想承担的钱",
                "note": "本金几乎没有波动，适合求稳的客户",
            },
        ),
    ),
    (
        "w_month_steady", "智汇·月度·稳健", "稳健低波", "月持", "R1", 30,
        "最短持有30天", "持满后每个交易日可赎", 0.0238, "成立以来年化", 1000.0,
        "30%×央行活期存款利率 + 70%×中债0-1年国债财富指数收益率。",
        "以国债、存款类资产与高评级短债为主，不参与权益投资，适合养老资金的稳健增值。",
        json.dumps(["R1低风险", "养老适配", "月度理财"], ensure_ascii=False),
        _detail(
            "ZH3004", "一个月的安稳，给养老钱加一点息",
            ["低波动", "关注养老", "申赎0费用"], "超 4 万人持有",
            "1.20%",
            [
                {"label": "近1月", "change": "0.19%", "annualized": "2.30%"},
                {"label": "近3月", "change": "0.58%", "annualized": "2.34%"},
                {"label": "近6月", "change": "1.16%", "annualized": "2.37%"},
                {"label": "今年以来", "change": "1.63%", "annualized": "2.38%"},
                {"label": "成立以来", "change": "1.20%", "annualized": "2.38%"},
            ],
            [16, 24, 31, 39, 47, 55, 62, 70],
            "2026.05.20", "2026.09.28", 30, "最短持有30天",
            [
                {"name": "国债与存款类资产", "limit": "不低于70%"},
                {"name": "高评级短期信用债", "limit": "不高于30%"},
            ],
            {"title": "不碰权益，专为求稳资金设计", "body": "全部配置于利率债、存款类与高评级短债，不做权益和衍生品，适合养老与备用金。"},
            ["安享月利", "月度稳健", "养老理财"],
            plain={
                "when": "放满一个月之后，随时可以取出来",
                "yield": "比七日产品再高一点，波动很小",
                "asset": "国债、银行存款和高评级短期债券，不投股票",
                "fit": "一个月内用不上、专门留给养老或备用的钱",
                "note": "适合不愿看到本金波动的客户",
            },
        ),
    ),
    (
        "w_half_steady", "智汇·半年·稳健", "稳健增值", "半年", "R2", 180,
        "最短持有180天", "持满后每个交易日可赎", 0.0345, "近1年年化", 1000.0,
        "中债信用债总指数收益率×85% + 活期利率×15%。",
        "不低于 95% 投资于债券等固收资产，精选高评级信用债，不参与股票二级市场投资。",
        json.dumps(["R2中低风险", "纯债策略", "半年闲钱"], ensure_ascii=False),
        _detail(
            "ZH1807", "只做债券，把波动关在门外",
            ["纯债策略", "每日可买", "申赎0费用"], "超 7 万人持有",
            "2.15%",
            [
                {"label": "近1月", "change": "0.25%", "annualized": "3.08%"},
                {"label": "近3月", "change": "0.79%", "annualized": "3.26%"},
                {"label": "近6月", "change": "1.62%", "annualized": "3.38%"},
                {"label": "近1年", "change": "3.42%", "annualized": "3.45%"},
                {"label": "成立以来", "change": "2.15%", "annualized": "3.42%"},
            ],
            [27, 37, 46, 56, 64, 72, 80, 88],
            "2025.10.16", "2026.09.28", 180, "最短持有180天",
            [
                {"name": "债券等固定收益类资产", "limit": "不低于95%"},
                {"name": "现金类资产", "limit": "约5%"},
            ],
            {"title": "纯债运作，不做股票", "body": "合同约定不投资股票，全部收益来自债券票息与久期管理，回撤相对可控。"},
            ["纯债优享", "半年宁", "半年稳健"],
            plain={
                "when": "放满半年之后，随时可以取出来",
                "yield": "比月度再高一些，波动仍然不大",
                "asset": "全部是债券和存款类资产，合同约定不投股票",
                "fit": "半年内确定用不上的钱",
                "note": "期限放长一点，收益空间就高一点",
            },
        ),
    ),
    (
        "w_half_plus", "智汇·半年·进取", "稳中求进", "半年", "R3", 180,
        "最短持有180天", "持满后每个交易日可赎", 0.0385, "成立以来年化", 1000.0,
        "中债新综合财富指数×85% + 沪深300指数×15%。",
        "约 85% 配置固收资产做安全垫，约 15% 参与权益与可转债，力争在可控回撤下增厚收益。",
        json.dumps(["R3中风险", "固收+策略", "半年期"], ensure_ascii=False),
        _detail(
            "ZH1808", "固收打底，小步参与权益",
            ["固收+", "每日可买", "申赎0费用"], "超 3 万人持有",
            "2.40%",
            [
                {"label": "近1月", "change": "0.28%", "annualized": "3.35%"},
                {"label": "近3月", "change": "0.85%", "annualized": "3.48%"},
                {"label": "近6月", "change": "1.76%", "annualized": "3.72%"},
                {"label": "今年以来", "change": "2.52%", "annualized": "3.80%"},
                {"label": "成立以来", "change": "2.40%", "annualized": "3.85%"},
            ],
            [22, 34, 30, 50, 58, 68, 66, 86],
            "2026.02.12", "2026.09.28", 180, "最短持有180天",
            [
                {"name": "固定收益类资产", "limit": "约85%"},
                {"name": "权益及可转债资产", "limit": "约15%"},
            ],
            {"title": "固收打底，权益做增强", "body": "大部分仓位配置债券，小部分参与权益与可转债，净值会随市场波动，可能出现阶段性回撤。"},
            ["均衡添利", "半年精进", "固收+"],
            plain={
                "when": "放满半年之后，随时可以取出来",
                "yield": "比半年稳健高，但会出现阶段性回撤",
                "asset": "大约 85% 债券，另有约 15% 参与股票和可转债",
                "fit": "半年用不上、能接受净值小幅波动的钱",
                "note": "市场不好的时候，可能会看到短期浮亏",
            },
        ),
    ),
    (
        "w_year_steady", "智汇·年度·稳健", "稳中求进", "一年", "R3", 360,
        "最短持有360天", "持满后每个交易日可赎", 0.0412, "成立以来年化", 10000.0,
        "中债新综合财富指数×75% + 沪深300指数×20% + 恒生指数×5%。",
        "约 75% 固收资产提供安全垫，约 25% 参与境内外权益与量化中性策略，追求长期超额收益。",
        json.dumps(["R3中风险", "长钱优选", "多资产"], ensure_ascii=False),
        _detail(
            "ZH3607", "一年期长钱，求稳也求进",
            ["多资产", "封闭运作", "申赎0费用"], "超 2 万人持有",
            "3.15%",
            [
                {"label": "近1月", "change": "0.33%", "annualized": "3.68%"},
                {"label": "近3月", "change": "0.92%", "annualized": "3.82%"},
                {"label": "近6月", "change": "1.95%", "annualized": "3.98%"},
                {"label": "今年以来", "change": "2.94%", "annualized": "4.05%"},
                {"label": "成立以来", "change": "3.15%", "annualized": "4.12%"},
            ],
            [18, 30, 26, 46, 54, 66, 62, 84],
            "2025.11.28", "2026.09.28", 360, "最短持有360天",
            [
                {"name": "固定收益类资产", "limit": "约75%"},
                {"name": "境内外权益及量化策略", "limit": "约25%"},
            ],
            {"title": "固收为主，权益增强更进取", "body": "固收提供安全垫，权益与量化中性策略贡献弹性，适合能承受一定波动的长钱。"},
            ["增利优选", "盈年进", "一年稳健"],
            plain={
                "when": "放满一年之后可以赎回",
                "yield": "一年期的收益水平，会有波动",
                "asset": "约 75% 债券，约 25% 参与境内外权益和量化策略",
                "fit": "一年以上确定不用的钱",
                "note": "时间放长一些，收益空间也大一些",
            },
        ),
    ),
    (
        "w_year_plus", "智汇·年度·进取", "稳中求进", "一年", "R4", 360,
        "最短持有360天", "持满后每个交易日可赎", 0.0520, "成立以来年化", 10000.0,
        "中证偏股型基金指数×40% + 中债新综合财富指数×60%。",
        "约 60% 固收资产、约 40% 权益资产，重点配置成长行业与优质上市公司，净值波动明显大于 R3 产品。",
        json.dumps(["R4中高风险", "成长风格", "波动较大"], ensure_ascii=False),
        _detail(
            "ZH3608", "接受波动，换取更高弹性",
            ["成长风格", "封闭运作", "申赎0费用"], "新品在售",
            "3.80%",
            [
                {"label": "近1月", "change": "0.52%", "annualized": "4.60%"},
                {"label": "近3月", "change": "1.35%", "annualized": "4.85%"},
                {"label": "近6月", "change": "2.48%", "annualized": "5.02%"},
                {"label": "今年以来", "change": "3.62%", "annualized": "5.10%"},
                {"label": "成立以来", "change": "3.80%", "annualized": "5.20%"},
            ],
            [12, 38, 22, 58, 44, 74, 52, 90],
            "2025.12.15", "2026.09.28", 360, "封闭360天",
            [
                {"name": "固定收益类资产", "limit": "约60%"},
                {"name": "权益类资产", "limit": "约40%"},
            ],
            {"title": "权益仓位更高，波动更大", "body": "权益仓位约四成，净值会随股市明显波动，仅适合风险承受能力 R4 及以上的客户。"},
            ["成长精选", "年度精进", "权益增强"],
            plain={
                "when": "放满一年之后可以赎回",
                "yield": "收益空间最大，波动也最明显",
                "asset": "约 60% 债券，约 40% 投资股票",
                "fit": "一年以上不用、能接受较大波动的钱",
                "note": "净值会跟随股市明显涨跌，请谨慎选择",
            },
        ),
    ),
]


# 2026-10-15 新增：货架体系化重命名后，把老演示数据里的持仓名称迁到新名称。
# 持仓是按 transactions.counterparty（产品名）汇总的，不改这里画像会显示「没有持仓」。
WEALTH_PRODUCT_RENAMES = {
    "随手金": "智汇·活钱",
    "日日盈": "智汇·活钱",
    "周周利": "智汇·七日·稳健",
    "七日盈": "智汇·七日·进取",
    "安享月利": "智汇·月度·稳健",
    "月月安": "智汇·月度·进取",
    "双月盈": "智汇·月度·进取",
    "季季增": "智汇·月度·进取",
    "半年宁": "智汇·半年·稳健",
    "纯债优享": "智汇·半年·稳健",
    "均衡添利": "智汇·半年·进取",
    "盈年进": "智汇·年度·稳健",
    "增利优选": "智汇·年度·稳健",
    "成长精选": "智汇·年度·进取",
}


def migrate_wealth_product_names(cursor):
    """把流水里的老产品名替换为新名称，可重复执行（幂等）。"""
    changed = 0
    for old_name, new_name in WEALTH_PRODUCT_RENAMES.items():
        cursor.execute(
            "UPDATE transactions SET counterparty = ? WHERE counterparty = ?",
            (new_name, old_name),
        )
        changed += cursor.rowcount or 0
    return changed


def _seed_wealth_products(cursor):
    cursor.execute("DROP TABLE IF EXISTS wealth_products")
    cursor.execute("""
    CREATE TABLE wealth_products (
        product_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        sub_category TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        term_days INTEGER NOT NULL,
        term_label TEXT NOT NULL,
        term_sub_label TEXT NOT NULL,
        expected_yield REAL NOT NULL,
        yield_label TEXT NOT NULL,
        min_amount REAL NOT NULL,
        benchmark_desc TEXT NOT NULL,
        asset_direction TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        status TEXT DEFAULT 'ON_SALE'
    )
    """)
    cursor.executemany("""
        INSERT INTO wealth_products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ON_SALE')
    """, WEALTH_PRODUCTS)


def update_wealth_database():
    """仅重建理财货架，不改动账户、流水等其他表。"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    ensure_user_profile_schema()
    _seed_wealth_products(cursor)
    migrated = migrate_wealth_product_names(cursor)
    conn.commit()
    conn.close()
    print(f"理财数据库更新完成：已注入 {len(WEALTH_PRODUCTS)} 款金选理财产品。")
    if migrated:
        print(f"已同步 {migrated} 条历史持仓的产品名称。")


if __name__ == "__main__":
    init_database()
