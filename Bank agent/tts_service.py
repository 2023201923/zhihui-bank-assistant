"""2026-10-08 新增：语音播报（TTS）服务。

适老版提供「语音播报」能力：助手回复后用语音念给老人听。
默认先用微软神经网络语音（晓晓）。wss 连接显式走系统里的 http 代理，并优先 IPv4。
晓晓多次失败后，这一次改用 Windows 自带语音把这句话念出来。
下次同一句话仍会先再试晓晓，不会因为这次失败就一直用系统语音。
小程序通过 innerAudioContext 播放 `/api/tts` 返回的音频。

设计要点：
- 结果缓存：同一段文本、同一个音色只合成一次。
- 失败可降级：神经网络语音失败时用系统语音；都失败时接口返回 503。
"""

import hashlib
import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import wave

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "tts_cache")

# 神经网络音色与语速。晓晓偏温和，略放慢一点，方便老人听清。
TTS_VOICE = os.environ.get("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
TTS_EDGE_RATE = os.environ.get("TTS_EDGE_RATE", "-8%")
# 系统语音的语速（字/分钟），仅在降级到 pyttsx3 时使用
TTS_RATE = int(os.environ.get("TTS_RATE", "160"))
TTS_MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "180"))
TTS_TIMEOUT = int(os.environ.get("TTS_TIMEOUT", "30"))

_WORKER_SCRIPT = """
import sys
from tts_service import _get_engine
text, out_path = sys.argv[1], sys.argv[2]
engine = _get_engine()
engine.save_to_file(text, out_path)
engine.runAndWait()
"""

_engine = None
_engine_lock = threading.Lock()
_synth_lock = threading.Lock()
_import_error = None
_orig_getaddrinfo = socket.getaddrinfo
logger = logging.getLogger("uvicorn.error")


def _edge_ready() -> bool:
    try:
        import edge_tts  # noqa: F401

        return True
    except Exception:
        return False


def is_available() -> bool:
    """神经网络语音或系统语音，有一种能用即可。"""
    global _import_error
    if _edge_ready():
        return True
    try:
        import pyttsx3  # noqa: F401

        return True
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        _import_error = str(exc)
        return False


def unavailable_reason() -> str:
    if is_available():
        return ""
    return (
        "未安装语音合成依赖。请在 `Bank agent` 目录执行 "
        "`python -m pip install edge-tts pyttsx3` 后重启后端。"
    )


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            import pyttsx3

            engine = pyttsx3.init()
            try:
                engine.setProperty("rate", TTS_RATE)
                voices = engine.getProperty("voices") or []
                # 优先选中文语音（Windows 上通常是 Huihui/Kangkang）
                for voice in voices:
                    label = f"{voice.id} {getattr(voice, 'name', '')}".lower()
                    if any(key in label for key in ("chinese", "zh-cn", "zh_cn", "huihui", "kangkang")):
                        engine.setProperty("voice", voice.id)
                        break
            except Exception:
                pass
            _engine = engine
        return _engine


def normalize_text(text: str) -> str:
    """去掉富文本符号并按上限截断，避免念出多余停顿或超长音频。"""
    clean = str(text or "")
    for ch in ("\n", "\r", "\t"):
        clean = clean.replace(ch, " ")
    for ch in ("*", "#", "`", ">", "•"):
        clean = clean.replace(ch, "")
    clean = " ".join(clean.split())
    if len(clean) > TTS_MAX_CHARS:
        clean = clean[:TTS_MAX_CHARS]
    return clean


def _to_standard_wav(src_path: str, dst_path: str) -> None:
    """把 SAPI 写出的 wav 转成微信能播的标准 PCM。

    pyttsx3 在 Windows 上生成的文件有两处小程序解不了：
    fmt 块是 18 字节（标准是 16），采样率是 22050Hz。
    innerAudioContext 遇到这种文件会请求成功但没有声音。
    """
    with wave.open(src_path, "rb") as src:
        channels = src.getnchannels()
        width = src.getsampwidth()
        rate = src.getframerate()
        frames = src.readframes(src.getnframes())

    if channels == 1 and width == 2 and rate == 22050 and frames:
        count = len(frames) // 2
        samples = struct.unpack("<" + "h" * count, frames[: count * 2])
        doubled = [sample for sample in samples for _ in (0, 1)]
        frames = struct.pack("<" + "h" * len(doubled), *doubled)
        rate = 44100

    with wave.open(dst_path, "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(width)
        dst.setframerate(rate)
        dst.writeframes(frames)


def _cache_path(content: str, tag: str, suffix: str) -> str:
    key = hashlib.md5(f"{tag}\n{content}".encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{key}{suffix}")


def _mp3_to_wav(src_path: str, dst_path: str) -> None:
    """神经网络语音出来的是 mp3。微信之前只能稳定播放标准 PCM wav，这里转成同样的格式。"""
    import miniaudio

    decoded = miniaudio.decode_file(
        src_path,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=44100,
    )
    with wave.open(dst_path, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(decoded.sample_rate)
        dst.writeframes(decoded.samples)


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """晓晓服务的 IPv6 地址在这台机器上会被直接重置，先只查 IPv4。"""
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


def _speech_proxy() -> str | None:
    """晓晓走的是 wss。系统代理只登记了 http/https，要显式交给语音库才会走梯子。"""
    explicit = (os.environ.get("TTS_PROXY") or "").strip()
    if explicit:
        return explicit
    try:
        import urllib.request

        proxies = urllib.request.getproxies()
    except Exception:
        return None
    proxy = proxies.get("https") or proxies.get("http")
    return proxy or None


def _synthesize_edge(content: str, path: str) -> None:
    import asyncio
    import edge_tts

    mp3_path = path + ".mp3"
    if os.path.exists(mp3_path):
        os.remove(mp3_path)

    proxy = _speech_proxy()

    async def run() -> None:
        communicate = edge_tts.Communicate(
            content, TTS_VOICE, rate=TTS_EDGE_RATE, proxy=proxy
        )
        await communicate.save(mp3_path)

    socket.getaddrinfo = _ipv4_getaddrinfo
    try:
        asyncio.run(run())
    finally:
        socket.getaddrinfo = _orig_getaddrinfo
    if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
        raise RuntimeError("晓晓语音没有返回音频")
    _mp3_to_wav(mp3_path, path)
    try:
        os.remove(mp3_path)
    except OSError:
        pass


def _synthesize_sapi(content: str, path: str) -> None:
    raw_path = path + ".raw.wav"
    if os.path.exists(raw_path):
        os.remove(raw_path)
    # pyttsx3 引擎绑定在创建它的线程上，FastAPI 线程池换线程再调用 runAndWait 会永久卡住，
    # 所以每次合成都放到独立子进程里。
    try:
        subprocess.run(
            [sys.executable, "-c", _WORKER_SCRIPT, content, raw_path],
            cwd=BASE_DIR,
            timeout=TTS_TIMEOUT,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("语音合成超时")
    if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
        raise RuntimeError("语音合成失败，请确认系统已安装中文语音包")
    _to_standard_wav(raw_path, path)
    try:
        os.remove(raw_path)
    except OSError:
        pass


def synthesize(text: str) -> str:
    """先合成晓晓。多次失败时这一次用系统语音，但不挡住下次再试晓晓。"""
    content = normalize_text(text)
    if not content:
        raise ValueError("语音文本为空")

    os.makedirs(CACHE_DIR, exist_ok=True)
    edge_path = _cache_path(content, f"edge:{TTS_VOICE}:{TTS_EDGE_RATE}", ".wav")
    if os.path.exists(edge_path) and os.path.getsize(edge_path) > 44:
        return edge_path

    last_error = None
    if _edge_ready():
        with _synth_lock:
            if os.path.exists(edge_path) and os.path.getsize(edge_path) > 44:
                return edge_path
            for attempt in range(1, 4):
                try:
                    _synthesize_edge(content, edge_path)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    logger.warning("晓晓合成失败，第 %s 次：%s", attempt, exc)
                    for leftover in (edge_path, edge_path + ".mp3"):
                        try:
                            os.remove(leftover)
                        except OSError:
                            pass
                    if attempt < 3:
                        time.sleep(0.6)
        if last_error is None and os.path.exists(edge_path) and os.path.getsize(edge_path) > 44:
            return edge_path

    logger.warning("晓晓不可用，这一次改用系统语音：%s", last_error)
    sapi_path = _cache_path(content, f"sapi:{TTS_RATE}", ".wav")
    with _synth_lock:
        if not (os.path.exists(sapi_path) and os.path.getsize(sapi_path) > 44):
            _synthesize_sapi(content, sapi_path)
    if not os.path.exists(sapi_path) or os.path.getsize(sapi_path) == 0:
        raise RuntimeError(f"语音合成失败：{last_error}")
    return sapi_path
