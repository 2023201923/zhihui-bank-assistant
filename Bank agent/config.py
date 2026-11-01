import os

from openai import OpenAI

# 魔搭 SDK Token 放在环境变量 MODELSCOPE_TOKEN，不要写进仓库。
MODELSCOPE_TOKEN = os.environ.get("MODELSCOPE_TOKEN", "")

# 2. 初始化客户端，指向魔搭 API-Inference 地址
client = OpenAI(
    base_url="https://api-inference.modelscope.cn/v1",
    api_key=MODELSCOPE_TOKEN,
)

# 魔搭当前可调用、且明显强于 glm-4-flash 的免费模型。
# 122B 总参数、每步只激活约 10B，工具调用有官方支持，延迟适合多轮取数。
# 更大的 Qwen/Qwen3.5-397B-A17B 更慢，也更吃每日免费额度。
MODEL_NAME = "Qwen/Qwen3.5-122B-A10B"
# 主模型没出卡片、但回复像是在展示内容时，用这个更小的免费模型补查一次该调哪个工具。
# 调用失败会退回 MODEL_NAME，不另开接口。
CHECK_MODEL_NAME = "Qwen/Qwen3.5-35B-A3B"
# 该系列默认先长思考。银行对话以工具调用为主，关掉思考才能沿用现有非流式请求。
MODEL_EXTRA_BODY = {"enable_thinking": False}