import argparse
import base64
from collections import deque
import ctypes
import hashlib
import ipaddress
import json
import math
import os
import platform
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

import requests
from PIL import Image
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from update_config import (
    LATEST_UPDATE_JSON,
    LOCAL_STATIC_UPDATE_URL,
    RELEASE_SITE_DIR_NAME,
    UPDATE_CHECK_URL,
)
from updater import (
    UpdateCancelled,
    UpdateError,
    check_for_update,
    download_update_file,
    format_file_size,
)
from version_info import (
    APP_DATA_DIR_NAME,
    APP_DISPLAY_NAME,
    APP_EDITION,
    APP_NAME,
    APP_VERSION,
    APP_VERSION_TAG,
    CHANGELOG_NAME,
)


PIL_LANCZOS = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS


# Windows 窗口版打包程序可能没有有效控制台句柄；命令行自检时保留输出，句柄无效时降级到空设备。
for stream_name in ("stdout", "stderr"):
    standard_stream = getattr(sys, stream_name, None)
    try:
        if standard_stream is None:
            raise OSError("missing standard stream")
        standard_stream.reconfigure(encoding="utf-8")
        standard_stream.write("")
        standard_stream.flush()
    except Exception:
        setattr(sys, stream_name, open(os.devnull, "w", encoding="utf-8"))


def get_app_dir():
    # 配置、结果和随包工具都以可执行文件所在目录为基准，源码运行时则回到当前脚本目录。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_dir():
    # PyInstaller 单文件模式会把只读资源解压到 _MEIPASS，不能与用户数据目录混用。
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
RESOURCE_DIR = get_resource_dir()
APP_ICON_PATH = RESOURCE_DIR / "assets" / "app_icon.ico"
DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 900
MIN_WINDOW_WIDTH = 1120
MIN_WINDOW_HEIGHT = 760
TOOLS_DIR = APP_DIR / "tools"
RESOURCE_TOOLS_DIR = RESOURCE_DIR / "tools"
DATA_DIR = APP_DIR / APP_DATA_DIR_NAME
CONFIG_PATH = DATA_DIR / "config" / "stream_config.json"
DEFAULT_IMAGE_DIR = DATA_DIR / "frames"
DEFAULT_RESULTS_DIR = DATA_DIR / "results"
LOGS_DIR = DATA_DIR / "logs"
CRASH_DIR = DATA_DIR / "crash_reports"
SUPPORT_DIR = DATA_DIR / "support_packages"
DOCS_DIR = DATA_DIR / "docs"
UPDATE_DIR = APP_DIR / "updates"
RELEASE_SITE_DIR = APP_DIR / RELEASE_SITE_DIR_NAME
RESOURCE_RELEASE_SITE_DIR = RESOURCE_DIR / RELEASE_SITE_DIR_NAME
DEFAULT_UPDATE_INFO = UPDATE_CHECK_URL
LEGACY_DATA_DIR_NAMES = ["Traffic Light_V2.0_数据"]
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
PROMPT_NONE_NAME = "无（自行填写）"
CUSTOM_PROMPT_PREFIX = "我的模板："
CONFIG_SNAPSHOT_SCHEMA = "video-stream-analyzer-config-v1"
SENSITIVE_CONFIG_KEYS = {"api_key", "rtsp_password"}
PERSISTENT_LOG_LOCK = threading.Lock()
GUI_EVENT_QUEUE_LIMIT = 6000
REALTIME_TERMINAL_CACHE_LIMIT = 20000


def calculate_initial_window_geometry(screen_width, screen_height):
    """Fit the initial window to the current desktop, including DPI-scaled desktops."""
    screen_width = max(1, int(screen_width))
    screen_height = max(1, int(screen_height))
    available_width = max(MIN_WINDOW_WIDTH, screen_width - 80)
    available_height = max(MIN_WINDOW_HEIGHT, screen_height - 100)
    width = min(DEFAULT_WINDOW_WIDTH, available_width)
    height = min(DEFAULT_WINDOW_HEIGHT, available_height)
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    return width, height, x, y


def enable_windows_dpi_awareness():
    """Use physical pixels on Windows so resize and window bounds stay consistent."""
    if os.name != "nt":
        return "not-windows"

    import ctypes

    try:
        user32 = ctypes.windll.user32
        set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if set_context:
            set_context.argtypes = [ctypes.c_void_p]
            set_context.restype = ctypes.c_bool
            if set_context(ctypes.c_void_p(-4)):
                return "per-monitor-v2"
    except (AttributeError, OSError, ValueError):
        pass

    try:
        shcore = ctypes.windll.shcore
        set_awareness = getattr(shcore, "SetProcessDpiAwareness", None)
        if set_awareness:
            result = int(set_awareness(2))
            if result in {0, -2147024891}:
                return "per-monitor"
    except (AttributeError, OSError, ValueError):
        pass

    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError, ValueError):
        pass
    return "unchanged"


VISION_MODEL_HINTS = (
    "vision",
    "visual",
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "gemini",
    "claude-3",
    "glm-4v",
    "internvl",
    "llava",
    "minicpm-v",
    "mllama",
    "pixtral",
    "qwen3-vl-plus",
    "qwen3-vl-flash",
    "qwen-vl-max",
    "qwen-vl-plus",
    "qwen2.5-vl",
    "qwen2-vl",
    "vl",
)

# 预设只负责提供常用分析口径，用户输入的自定义提示词仍由配置文件单独保存。
PROMPT_PRESETS = {
    PROMPT_NONE_NAME: "",
    "通用详细报告": (
        "请分析这张图片，并用清晰结构输出：\n"
        "1. 画面概述：说明整体场景。\n"
        "2. 关键对象：列出主要人物、车辆、设备、文字或物体。\n"
        "3. 异常与风险：指出异常、隐患或值得关注的细节；没有就写“未发现明显异常”。\n"
        "4. 结论：用一两句话总结。"
    ),
    "现场巡检报告": (
        "请以现场巡检报告形式分析这张图片，输出：\n"
        "1. 巡检对象：说明画面中的设备、区域或环境。\n"
        "2. 当前状态：判断是否正常运行或存在异常。\n"
        "3. 风险等级：低/中/高，并说明原因。\n"
        "4. 处理建议：给出可执行的下一步建议。\n"
        "5. 结论：一句话总结。"
    ),
    "巡检异常风险": (
        "请以巡检人员视角分析这张图片，按“画面概述、设备/环境、异常风险、处理建议、结论”输出。"
        "重点关注破损、遮挡、火灾隐患、人员车辆异常、设备状态异常和环境风险。"
    ),
    "安全生产隐患": (
        "请从安全生产角度分析这张图片，重点检查人员防护、设备运行、消防通道、临边洞口、"
        "堆放杂乱、明火烟雾、违规操作和其他安全隐患。按“场景、隐患、风险等级、整改建议、结论”输出。"
    ),
    "交通道路分析": (
        "请分析这张道路或交通场景图片，输出：道路环境、车辆/行人情况、交通秩序、拥堵或事故迹象、"
        "异常风险、处置建议。没有异常请明确写“未发现明显交通异常”。"
    ),
    "施工现场分析": (
        "请以工程施工管理视角分析这张图片，关注施工区域、机械设备、人员作业、安全防护、材料堆放、"
        "文明施工和进度线索。按“现场概况、关键对象、问题风险、建议、结论”输出。"
    ),
    "无人机视角分析": (
        "请从无人机视角分析这张图像，描述地物、道路、建筑、植被、水体、人员车辆、"
        "施工或异常区域，并给出简短结论。"
    ),
    "安防监控摘要": (
        "请以安防监控摘要形式输出，包含：场景、人员、车辆、可疑行为、异常事件、风险等级、结论。"
    ),
    "仓储物流盘点": (
        "请分析这张仓储或物流场景图片，关注货架、托盘、包裹、车辆、人员、通道占用、堆放规范和异常风险。"
        "按“场景概况、可见货物/设备、异常点、管理建议、结论”输出。"
    ),
    "设备仪表读数": (
        "请分析图片中的设备、屏幕、仪表或铭牌。如果能识别读数、状态灯、报警信息或文字，请逐项列出；"
        "无法确认的内容请写“无法从图中可靠识别”，不要编造。最后给出状态判断。"
    ),
    "文字与标识提取": (
        "请尽可能提取图片中的文字、标识、编号、车牌、仪表标签或警示牌内容。"
        "按“可识别文字、位置说明、可能含义、无法确认内容”输出；不确定时请注明。"
    ),
    "质量缺陷检查": (
        "请以质量检查员视角分析这张图片，关注破损、污渍、变形、缺件、错位、裂纹、锈蚀、遮挡、"
        "包装异常或安装异常。按“检查对象、疑似缺陷、严重程度、复核建议、结论”输出。"
    ),
    "人员行为识别": (
        "请分析画面中的人员数量、位置、动作和行为状态，关注跌倒、聚集、闯入、滞留、违规操作、未戴防护用品等情况。"
        "只基于可见画面判断，不确定时请说明。"
    ),
    "医疗/实验室场景": (
        "请以非诊断、非医疗建议的方式描述图片中的医疗或实验室场景，关注设备、人员操作、防护、环境整洁、"
        "明显风险和需要人工复核的细节。不要做疾病诊断。"
    ),
    "农业/林业观察": (
        "请分析这张农业、林业或自然环境图片，关注作物/植被状态、病虫害迹象、水土情况、道路/设施、"
        "异常区域和后续巡查建议。"
    ),
    "事件快速摘要": (
        "请快速判断这张图片是否包含异常事件。输出：\n"
        "1. 一句话摘要。\n"
        "2. 是否异常：是/否/不确定。\n"
        "3. 主要证据。\n"
        "4. 建议动作。"
    ),
    "简短一句话": "请用一句清晰中文概括这张图片的主要内容。",
}

AUTO_STREAM_FORMAT = "自动识别（推荐：直接粘贴地址）"
DEFAULT_STREAM_FORMAT = AUTO_STREAM_FORMAT
STREAM_FORMAT_PRESETS = {
    AUTO_STREAM_FORMAT: {
        "example": "rtsp://摄像头IP:554/stream1",
        "hint": "不知道格式就保持这一项。直接粘贴播放地址，软件会根据地址自动识别。",
    },
    "RTSP 摄像头 / 国标平台转RTSP": {
        "example": "rtsp://摄像头IP:554/stream1",
        "hint": "适合海康、大华等摄像头，或国标平台转出的 RTSP 地址。账号密码建议填在“安全接入 / 加密RTSP”区域。",
    },
    "GB28181 国标平台转 HTTP-FLV": {
        "example": "http://国标平台IP:端口/rtp/设备国标编号_通道编号.live.flv",
        "hint": "适合国标平台或视频网关输出的 HTTP-FLV 地址，延迟低，平台常见。",
    },
    "GB28181 国标平台转 HLS(m3u8)": {
        "example": "http://国标平台IP:端口/rtp/设备国标编号_通道编号/hls.m3u8",
        "hint": "适合国标平台输出的 m3u8 地址，兼容性好，延迟通常略高。",
    },
    "HLS(m3u8) 直播流": {
        "example": "https://服务器地址/live/camera01.m3u8",
        "hint": "适合直播平台或流媒体服务输出的 m3u8 地址。",
    },
    "HTTP-FLV 直播流": {
        "example": "http://服务器地址/live/camera01.flv",
        "hint": "适合低延迟直播流，常见于流媒体网关或国标平台。",
    },
    "RTMP/RTMPS 直播流": {
        "example": "rtmp://服务器地址/live/camera01",
        "hint": "适合传统直播推流/拉流服务。",
    },
    "SRT 低延迟流": {
        "example": "srt://服务器地址:9000?mode=caller",
        "hint": "适合跨公网低延迟传输，需要流媒体服务端支持 SRT。",
    },
    "RTP/UDP 组播或单播": {
        "example": "udp://组播地址:端口",
        "hint": "适合局域网组播/单播流。需要确认本机网络和防火墙允许接收。",
    },
}

RTSP_TRANSPORT_MODE_LABELS = {
    "auto": "自动（TCP优先，失败试UDP）",
    "tcp": "固定 TCP",
    "udp": "固定 UDP",
}
RTSP_TRANSPORT_MODE_VALUES = list(RTSP_TRANSPORT_MODE_LABELS.values())

CAPTURE_MODE_LABELS = {
    "interval": "连续抽帧",
    "point": "指定时间点",
    "range": "指定时间段",
}
CAPTURE_MODE_VALUES = list(CAPTURE_MODE_LABELS.values())

# 所有配置项在这里给出保守默认值，旧版本配置缺少字段时可以直接向前兼容。
DEFAULT_CONFIG = {
    "image_dir": str(DEFAULT_IMAGE_DIR),
    "results_dir": str(DEFAULT_RESULTS_DIR),
    "source_type": "file",
    "video_file": "",
    "stream_url": "",
    "stream_format": DEFAULT_STREAM_FORMAT,
    "rtsp_username": "",
    "rtsp_password": "",
    "rtsp_use_tls": False,
    "connection_mode": "public",
    "api_url": "",
    "api_key": "",
    "model": "",
    "prompt": PROMPT_PRESETS["通用详细报告"],
    "selected_prompt_preset": "通用详细报告",
    "custom_prompt_templates": {},
    "max_retries": 3,
    "max_image_size": 1080,
    "concurrency": 1,
    "max_tokens": 1500,
    "temperature": 0.3,
    "request_timeout": 60,
    "delete_processed": True,
    "process_existing": False,
    "frame_interval": 10,
    "capture_mode": "interval",
    "capture_point_time": "00:00:00",
    "capture_start_time": "00:00:00",
    "capture_end_time": "00:01:00",
    "ffmpeg_low_cpu": True,
    "ffmpeg_threads": 1,
    "stream_low_latency": True,
    "stream_fast_first_frame": True,
    "stream_drop_stale_frames": False,
    "stream_max_pending_frames": 3,
    "stream_auto_reconnect": True,
    "stream_reconnect_attempts": 5,
    "stream_probe_before_start": False,
    "stream_probe_timeout": 12,
    "stream_open_timeout": 30,
    "stream_first_frame_timeout": 120,
    "rtsp_transport_mode": "auto",
    "auto_start_tunnel": False,
    "ssh_host": "",
    "ssh_port": 22,
    "ssh_user": "",
    "ssh_key_path": "",
    "ssh_open_terminal": False,
    "ssh_local_port": 8080,
    "ssh_remote_host": "",
    "ssh_remote_port": 8000,
    "ssh_api_path": "",
    "ssh_tunnel_command": "",
    "log_retention_days": 30,
    "update_url": DEFAULT_UPDATE_INFO,
    "update_timeout": 8,
}


def api_key_looks_like_url(value, api_url=""):
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return True
    normalized_api = str(api_url or "").strip()
    return bool(normalized_api and text == normalized_api)


def sanitize_api_key(value, api_url=""):
    text = str(value or "").strip()
    return "" if api_key_looks_like_url(text, api_url) else text


def capture_mode_value(value):
    text = str(value or "").strip()
    if text in CAPTURE_MODE_LABELS:
        return text
    for key, label in CAPTURE_MODE_LABELS.items():
        if text == label:
            return key
    aliases = {
        "continuous": "interval",
        "default": "interval",
        "single": "point",
        "exact": "point",
        "segment": "range",
    }
    return aliases.get(text.lower(), "interval")


def capture_mode_display(value):
    return CAPTURE_MODE_LABELS.get(capture_mode_value(value), CAPTURE_MODE_LABELS["interval"])


def parse_capture_time(value, field_name="时间"):
    text = str(value or "").strip().replace("：", ":")
    if not text:
        return 0.0
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        seconds = float(text)
    else:
        parts = text.split(":")
        if len(parts) not in {2, 3}:
            raise ValueError(f"{field_name}格式不正确，请填写秒数、MM:SS 或 HH:MM:SS")
        try:
            if len(parts) == 2:
                hours = 0
                minutes = int(parts[0])
                seconds_part = float(parts[1])
            else:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds_part = float(parts[2])
        except ValueError as exc:
            raise ValueError(f"{field_name}格式不正确，请填写数字时间") from exc
        if hours < 0 or minutes < 0 or minutes >= 60 or seconds_part < 0 or seconds_part >= 60:
            raise ValueError(f"{field_name}超出范围，分钟和秒必须小于 60")
        seconds = hours * 3600 + minutes * 60 + seconds_part
    if seconds < 0:
        raise ValueError(f"{field_name}不能小于 0")
    return seconds


def format_ffmpeg_seconds(value):
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def format_capture_seconds(value):
    total = float(value)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    seconds = total - hours * 3600 - minutes * 60
    if abs(seconds - round(seconds)) < 0.001:
        second_text = f"{int(round(seconds)):02d}"
    else:
        second_text = f"{seconds:06.3f}".rstrip("0").rstrip(".")
    return f"{hours:02d}:{minutes:02d}:{second_text}"


def sanitize_capture_config(config):
    config["capture_mode"] = capture_mode_value(config.get("capture_mode", "interval"))
    config["frame_interval"] = int_from(config.get("frame_interval"), 10, 1, 3600)
    config["capture_point_time"] = str(config.get("capture_point_time", "00:00:00")).strip()
    config["capture_start_time"] = str(config.get("capture_start_time", "00:00:00")).strip()
    config["capture_end_time"] = str(config.get("capture_end_time", "00:01:00")).strip()
    return config


def remove_ffmpeg_option_with_value(options, option):
    cleaned = []
    skip_next = False
    for item in options:
        if skip_next:
            skip_next = False
            continue
        if item == option:
            skip_next = True
            continue
        cleaned.append(item)
    return cleaned


def build_capture_plan(config, source_type):
    mode = capture_mode_value(config.get("capture_mode", "interval"))
    interval = int_from(config.get("frame_interval"), 10, 1, 3600)
    source_type = "stream" if source_type == "stream" else "file"
    time_axis = "任务启动后的实时" if source_type == "stream" else "视频时间轴"
    plan = {
        "mode": mode,
        "interval": interval,
        "input_options": [],
        "output_options": [],
        "video_filter": "",
        "finite": mode in {"point", "range"},
        "disable_local_readrate": source_type == "file" and mode in {"point", "range"},
        "first_frame_wait": 0.0,
        "summary": "",
    }

    if mode == "point":
        point = parse_capture_time(config.get("capture_point_time"), "抽帧时间点")
        point_text = format_ffmpeg_seconds(point)
        plan["point"] = point
        plan["output_options"] = ["-frames:v", "1"]
        plan["first_frame_wait"] = point if source_type == "stream" else 0.0
        if source_type == "file":
            if point > 0:
                plan["input_options"] = ["-ss", point_text]
            plan["summary"] = f"{time_axis} {format_capture_seconds(point)} 抽 1 帧"
        else:
            plan["video_filter"] = f"trim=start={point_text},setpts=PTS-STARTPTS"
            plan["summary"] = f"{time_axis}第 {format_capture_seconds(point)} 抽 1 帧"
        return plan

    if mode == "range":
        start = parse_capture_time(config.get("capture_start_time"), "抽帧开始时间")
        end = parse_capture_time(config.get("capture_end_time"), "抽帧结束时间")
        if end <= start:
            raise ValueError("抽帧结束时间必须大于开始时间")
        duration = end - start
        start_text = format_ffmpeg_seconds(start)
        end_text = format_ffmpeg_seconds(end)
        duration_text = format_ffmpeg_seconds(duration)
        plan.update({"start": start, "end": end, "duration": duration})
        plan["first_frame_wait"] = start if source_type == "stream" else 0.0
        if source_type == "file":
            if start > 0:
                plan["input_options"] = ["-ss", start_text]
            plan["output_options"] = ["-t", duration_text]
            plan["video_filter"] = f"fps=1/{interval}"
        else:
            plan["input_options"] = ["-t", end_text]
            plan["video_filter"] = (
                f"trim=start={start_text}:end={end_text},setpts=PTS-STARTPTS,fps=1/{interval}"
            )
        plan["summary"] = (
            f"{time_axis} {format_capture_seconds(start)} 到 {format_capture_seconds(end)}，"
            f"每 {interval} 秒抽 1 帧"
        )
        return plan

    plan["video_filter"] = f"fps=1/{interval}"
    plan["summary"] = (
        f"每 {interval} 秒按真实时间分析 1 帧"
        if source_type == "stream"
        else f"按视频时间轴每 {interval} 秒抽 1 帧"
    )
    return plan


def capture_summary_from_config(config, source_type):
    try:
        return build_capture_plan(config, source_type).get("summary", "")
    except ValueError as exc:
        return f"抽帧设置需修正：{exc}"


def legacy_config_paths():
    paths = []
    for name in LEGACY_DATA_DIR_NAMES:
        paths.append(APP_DIR / name / "config" / "stream_config.json")
    return paths


def is_path_under(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def migrate_legacy_runtime_paths(config):
    for legacy_name in LEGACY_DATA_DIR_NAMES:
        legacy_dir = APP_DIR / legacy_name
        if is_path_under(config.get("image_dir", ""), legacy_dir):
            config["image_dir"] = str(DEFAULT_IMAGE_DIR)
        if is_path_under(config.get("results_dir", ""), legacy_dir):
            config["results_dir"] = str(DEFAULT_RESULTS_DIR)
    return config


def load_config():
    # 先铺默认值，再覆盖磁盘配置；这样新增字段不会要求用户手工迁移配置文件。
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config = DEFAULT_CONFIG.copy()
    config_path = CONFIG_PATH
    legacy_loaded = False
    if not config_path.exists():
        legacy_path = next((path for path in legacy_config_paths() if path.exists()), None)
        if legacy_path:
            config_path = legacy_path
            legacy_loaded = True
    if config_path.exists():
        try:
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
        except (OSError, json.JSONDecodeError):
            pass
    if legacy_loaded:
        config = migrate_legacy_runtime_paths(config)
    config = migrate_ssh_config(config)
    config["connection_mode"] = infer_connection_mode(config)
    config["auto_start_tunnel"] = config["connection_mode"] == "private_ssh"
    if config.get("source_type") not in {"file", "stream"}:
        config["source_type"] = "file"
    config["video_file"] = str(config.get("video_file", ""))
    config["stream_url"] = str(config.get("stream_url", ""))
    config["rtsp_username"] = str(config.get("rtsp_username", ""))
    config["rtsp_password"] = str(config.get("rtsp_password", ""))
    config["rtsp_use_tls"] = bool(config.get("rtsp_use_tls", False))
    config["api_url"] = str(config.get("api_url", "")).strip()
    config["api_key"] = sanitize_api_key(
        config.get("api_key", ""),
        config["api_url"],
    )
    config["model"] = str(config.get("model", "")).strip()
    if config.get("stream_format") not in STREAM_FORMAT_PRESETS:
        config["stream_format"] = DEFAULT_STREAM_FORMAT
    config["rtsp_transport_mode"] = rtsp_transport_mode_value(config.get("rtsp_transport_mode", "auto"))
    config["image_dir"] = normalize_app_path(config.get("image_dir"), DEFAULT_IMAGE_DIR)
    config["results_dir"] = normalize_app_path(
        config.get("results_dir"),
        DEFAULT_RESULTS_DIR,
    )
    config["custom_prompt_templates"] = sanitize_custom_prompt_templates(
        config.get("custom_prompt_templates", {})
    )
    config["log_retention_days"] = int_from(
        config.get("log_retention_days"),
        30,
        1,
        365,
    )
    config = sanitize_capture_config(config)
    if not isinstance(config.get("selected_prompt_preset"), str):
        config["selected_prompt_preset"] = "通用详细报告"
    config["update_url"] = str(config.get("update_url") or DEFAULT_UPDATE_INFO).strip()
    config["update_timeout"] = int_from(config.get("update_timeout"), 8, 3, 60)
    return config


def save_config(config):
    # 先写临时文件再替换正式文件，避免断电或程序退出时留下半个 JSON。
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            backup_path = CONFIG_PATH.with_suffix(".json.bak")
            backup_path.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    temp_path = CONFIG_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(CONFIG_PATH)


def save_config_with_notice(config, notice_callback, context="保存配置"):
    try:
        save_config(config)
        return True
    except OSError as exc:
        notice_callback(
            "配置保存失败",
            (
                f"{context}失败：无法写入本机配置文件。\n\n"
                f"配置位置：{CONFIG_PATH}\n"
                f"失败原因：{exc}\n\n"
                "请检查软件目录权限、磁盘空间，或关闭正在占用配置文件的程序后重试。"
            ),
            "error",
            True,
        )
        return False


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def normalize_app_path(value, default):
    path = Path(value or default)
    if not path.is_absolute():
        path = DATA_DIR / path
    return str(path)


def sanitize_custom_prompt_templates(value):
    # 自定义模板不能覆盖内置模板，名称也限制长度，避免异常配置撑坏下拉框。
    if not isinstance(value, dict):
        return {}
    templates = {}
    for raw_name, raw_prompt in value.items():
        name = str(raw_name).strip()
        prompt = str(raw_prompt).strip()
        if not name or not prompt:
            continue
        if name.startswith(CUSTOM_PROMPT_PREFIX):
            name = name[len(CUSTOM_PROMPT_PREFIX) :].strip()
        if name and name not in PROMPT_PRESETS:
            templates[name[:40]] = prompt
    return templates


def custom_prompt_display_name(name):
    return f"{CUSTOM_PROMPT_PREFIX}{name}"


def custom_prompt_name_from_display(display):
    text = str(display or "").strip()
    if text.startswith(CUSTOM_PROMPT_PREFIX):
        return text[len(CUSTOM_PROMPT_PREFIX) :].strip()
    return ""


def normalize_api_path(value):
    raw = (value or "/v1/chat/completions").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = urlparse(normalize_chat_url(raw)).path
    if not raw.startswith("/"):
        raw = f"/{raw}"
    normalized = normalize_chat_url(f"http://localhost{raw}")
    return urlparse(normalized).path


def build_local_tunnel_api_url(local_port, api_path):
    port = int_from(local_port, 8080, 1, 65535)
    return f"http://localhost:{port}{normalize_api_path(api_path)}"


def parse_ssh_tunnel_command(command_text):
    # 兼容用户从终端复制来的 ssh -L 命令，并回填到结构化配置项中。
    result = {}
    try:
        parts = shlex.split(command_text or "", posix=False)
    except ValueError:
        return result

    index = 0
    while index < len(parts):
        item = parts[index]
        if item == "-L" and index + 1 < len(parts):
            forward = parts[index + 1]
            pieces = forward.split(":")
            if len(pieces) >= 3:
                result["ssh_local_port"] = int_from(pieces[0], 8080, 1, 65535)
                result["ssh_remote_host"] = pieces[1]
                result["ssh_remote_port"] = int_from(pieces[2], 8000, 1, 65535)
            index += 2
            continue
        if item.startswith("-L") and len(item) > 2:
            pieces = item[2:].split(":")
            if len(pieces) >= 3:
                result["ssh_local_port"] = int_from(pieces[0], 8080, 1, 65535)
                result["ssh_remote_host"] = pieces[1]
                result["ssh_remote_port"] = int_from(pieces[2], 8000, 1, 65535)
        elif item == "-p" and index + 1 < len(parts):
            result["ssh_port"] = int_from(parts[index + 1], 22, 1, 65535)
            index += 1
        elif item == "-i" and index + 1 < len(parts):
            key_path = parts[index + 1].strip('"')
            if not key_path.lower().endswith(".pub"):
                result["ssh_key_path"] = key_path
            index += 1
        elif "@" in item and not item.startswith("-"):
            user, host = item.split("@", 1)
            result["ssh_user"] = user
            result["ssh_host"] = host
        index += 1

    return result


def migrate_ssh_config(config):
    # 旧版本只保存完整 SSH 命令，新版本改为分字段保存，因此启动时做一次无损迁移。
    command_values = parse_ssh_tunnel_command(config.get("ssh_tunnel_command", ""))
    for key, value in command_values.items():
        if not config.get(key):
            config[key] = value
    api_path = urlparse(normalize_chat_url(config.get("api_url", ""))).path
    if is_local_api(config.get("api_url", "")) and api_path:
        config["ssh_api_path"] = api_path
    raw_api_path = str(config.get("ssh_api_path") or "").strip()
    config["ssh_api_path"] = normalize_api_path(raw_api_path) if raw_api_path else ""
    return config


def infer_connection_mode(config):
    # connection_mode 不存在时，根据旧字段推断路线，保证升级后仍能使用原配置。
    mode = config.get("connection_mode")
    if mode in {"public", "private_ssh", "private_direct"}:
        return mode
    if config.get("auto_start_tunnel"):
        return "private_ssh"
    if (
        is_local_api(config.get("api_url", ""))
        and str(config.get("ssh_host", "")).strip()
        and str(config.get("ssh_remote_host", "")).strip()
    ):
        return "private_ssh"
    return "public"


def normalize_input_source(input_source):
    if not input_source:
        return None
    if isinstance(input_source, str):
        value = input_source.strip()
        return {"type": "file", "value": value} if value else None

    source_type = str(input_source.get("type") or "file").strip()
    value = str(input_source.get("value") or "").strip()
    if not value:
        return None
    if source_type not in {"file", "stream"}:
        source_type = "file"
    return {"type": source_type, "value": value}


def safe_parsed_port(parsed):
    try:
        return parsed.port
    except ValueError:
        return None


def host_port_for_url(parsed):
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = safe_parsed_port(parsed)
    if port:
        return f"{host}:{port}"
    return host


def build_rtsp_netloc(parsed, username="", password=""):
    host_port = host_port_for_url(parsed)
    username = str(username or "").strip()
    password = str(password or "")
    if not username and not password:
        return parsed.netloc
    user = quote(username, safe="")
    if password:
        return f"{user}:{quote(password, safe='')}@{host_port}"
    return f"{user}@{host_port}"


def build_runtime_stream_url(stream_url, config):
    # 用户界面保留脱敏地址，真正启动 FFmpeg 前才把认证信息拼进运行地址。
    raw = normalize_stream_url_for_user(stream_url)
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"rtsp", "rtsps"}:
        return raw

    runtime_scheme = "rtsps" if bool(config.get("rtsp_use_tls")) else scheme
    username = str(config.get("rtsp_username", "") or "").strip()
    password = str(config.get("rtsp_password", "") or "")
    if username or password:
        embedded_username = unquote(parsed.username or "")
        embedded_password = unquote(parsed.password or "")
        runtime_username = username or embedded_username
        runtime_password = password if password else embedded_password
        netloc = build_rtsp_netloc(parsed, runtime_username, runtime_password)
    else:
        netloc = parsed.netloc
    return urlunparse(
        (
            runtime_scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def runtime_input_source(input_source, config):
    source = normalize_input_source(input_source)
    if source and source["type"] == "stream":
        source["value"] = build_runtime_stream_url(source["value"], config)
    return source


def mask_sensitive_text(text):
    # 日志、弹窗和诊断结果共用同一套脱敏规则，避免某个出口遗漏凭据。
    masked = str(text or "")
    masked = re.sub(
        r"((?:rtsp|rtsps|rtmp|rtmps|http|https|srt)://)([^/\s@]+)@",
        r"\1***:******@",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(
        r"(?i)(passphrase|password|token|apikey|api_key)=([^&\s]+)",
        r"\1=******",
        masked,
    )
    masked = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1******", masked)
    masked = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-******", masked)
    masked = re.sub(
        r'(?i)("(?:api_key|rtsp_password)"\s*:\s*")[^"]*(")',
        r"\1******\2",
        masked,
    )
    return masked


def masked_stream_url(stream_url):
    return mask_sensitive_text(stream_url)


def redacted_config_copy(config):
    clean = {}
    for key in DEFAULT_CONFIG:
        value = config.get(key, DEFAULT_CONFIG[key])
        if key in SENSITIVE_CONFIG_KEYS:
            clean[key] = ""
        elif key == "stream_url":
            clean[key] = masked_stream_url(value)
        elif key == "ssh_tunnel_command":
            clean[key] = mask_sensitive_text(value)
        else:
            clean[key] = value
    return clean


def cleanup_runtime_records(retention_days=30):
    retention_days = int_from(retention_days, 30, 1, 365)
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for directory in (LOGS_DIR, CRASH_DIR):
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def write_persistent_log(message):
    paths = write_persistent_logs([message])
    return paths[0] if paths else None


def write_persistent_logs(messages):
    lines = []
    for message in messages:
        text = mask_sensitive_text(message).rstrip()
        if not text:
            continue
        if not re.match(r"^\d{2}:\d{2}:\d{2}\s", text):
            text = f"{datetime.now():%H:%M:%S} [APP] {text}"
        lines.append(text)
    if not lines:
        return []
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / f"app_{datetime.now():%Y%m%d}.log"
        with PERSISTENT_LOG_LOCK:
            with log_path.open("a", encoding="utf-8") as file:
                file.write("\n".join(lines) + "\n")
        return [log_path]
    except OSError:
        return []


def export_config_snapshot(path, config):
    payload = {
        "schema": CONFIG_SNAPSHOT_SCHEMA,
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "contains_secrets": False,
        "config": redacted_config_copy(config),
    }
    atomic_write_json(path, payload)
    return Path(path)


def import_config_snapshot(path, current_config=None):
    path = Path(path)
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("配置快照文件过大，已拒绝导入")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SNAPSHOT_SCHEMA:
        raise ValueError("不是本软件支持的配置快照")
    imported = payload.get("config")
    if not isinstance(imported, dict):
        raise ValueError("配置快照缺少config对象")
    merged = DEFAULT_CONFIG.copy()
    if isinstance(current_config, dict):
        merged.update(current_config)
    for key in DEFAULT_CONFIG:
        if key in SENSITIVE_CONFIG_KEYS:
            continue
        if key in imported:
            merged[key] = imported[key]
    merged["api_key"] = str(merged.get("api_key", ""))
    merged["rtsp_password"] = str(merged.get("rtsp_password", ""))
    merged = migrate_ssh_config(merged)
    merged["custom_prompt_templates"] = sanitize_custom_prompt_templates(
        merged.get("custom_prompt_templates", {})
    )
    merged["log_retention_days"] = int_from(
        merged.get("log_retention_days"),
        30,
        1,
        365,
    )
    merged = sanitize_capture_config(merged)
    return merged


def import_legacy_config(path, current_config=None):
    path = Path(path)
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("配置文件过大，已拒绝导入")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict) and payload.get("schema") == CONFIG_SNAPSHOT_SCHEMA:
        return import_config_snapshot(path, current_config)
    if not isinstance(payload, dict):
        raise ValueError("旧版配置不是有效的 JSON 对象")
    merged = DEFAULT_CONFIG.copy()
    if isinstance(current_config, dict):
        merged.update(current_config)
    recognized = 0
    for key in DEFAULT_CONFIG:
        if key in payload:
            merged[key] = payload[key]
            recognized += 1
    if recognized < 3:
        raise ValueError("没有识别到足够的旧版配置字段")
    merged = migrate_ssh_config(merged)
    merged["connection_mode"] = infer_connection_mode(merged)
    merged = sanitize_capture_config(merged)
    merged["auto_start_tunnel"] = merged["connection_mode"] == "private_ssh"
    merged["custom_prompt_templates"] = sanitize_custom_prompt_templates(
        merged.get("custom_prompt_templates", {})
    )
    merged["log_retention_days"] = int_from(
        merged.get("log_retention_days"),
        30,
        1,
        365,
    )
    return merged


def latest_log_tail(max_lines=1200):
    if not LOGS_DIR.exists():
        return ""
    log_files = sorted(LOGS_DIR.glob("app_*.log"), key=lambda item: item.stat().st_mtime)
    if not log_files:
        return ""
    try:
        lines = log_files[-1].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def create_support_bundle(destination, config):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "python": sys.version,
        "frozen": bool(getattr(sys, "frozen", False)),
        "app_dir": str(APP_DIR),
        "data_dir": str(DATA_DIR),
        "tools": {
            "ffmpeg": find_tool("ffmpeg") or "",
            "ssh": find_tool("ssh") or "",
        },
        "config": redacted_config_copy(config),
    }
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "diagnostics.json",
            json.dumps(report, ensure_ascii=False, indent=2),
        )
        log_tail = latest_log_tail()
        if log_tail:
            archive.writestr("latest_log_tail.txt", mask_sensitive_text(log_tail))
        manifests = sorted(
            Path(config.get("results_dir") or DEFAULT_RESULTS_DIR).glob("session_*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for manifest in manifests[:5]:
            try:
                content = mask_sensitive_text(manifest.read_text(encoding="utf-8"))
            except OSError:
                continue
            archive.writestr(f"recent_sessions/{manifest.name}", content)
    return destination


def list_recent_sessions(config, limit=30):
    results_dir = Path(config.get("results_dir") or DEFAULT_RESULTS_DIR)
    if not results_dir.exists():
        return []
    sessions = []
    manifests = sorted(
        results_dir.glob("session_*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for manifest in manifests[: max(1, int(limit))]:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        sessions.append(
            {
                "manifest": str(manifest),
                "session_id": str(payload.get("session_id") or manifest.stem),
                "status": str(payload.get("status") or "unknown"),
                "updated_at": str(payload.get("updated_at") or ""),
                "source_type": str(source.get("type") or ""),
                "source_value": str(source.get("value") or ""),
                "result_file": str(payload.get("result_file") or ""),
                "success": int_from(stats.get("success"), 0, 0),
                "failed": int_from(stats.get("failed"), 0, 0),
            }
        )
    return sessions


def runtime_storage_status():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(DATA_DIR)
    except OSError:
        return "数据目录不可写"
    free_gb = usage.free / (1024 ** 3)
    if free_gb < 1:
        return f"剩余 {free_gb:.2f} GB，空间不足"
    return f"剩余 {free_gb:.1f} GB"


def materialize_resource(relative_path):
    relative_path = Path(relative_path)
    source = RESOURCE_DIR / relative_path
    if not source.exists():
        return None
    target = DOCS_DIR / relative_path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not target.exists() or source.stat().st_size != target.stat().st_size:
            shutil.copy2(source, target)
    except OSError:
        return None
    return target


def write_crash_report(exc_type, exc_value, exc_traceback, context="main"):
    CRASH_DIR.mkdir(parents=True, exist_ok=True)
    report_path = CRASH_DIR / f"crash_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.txt"
    text = (
        f"{APP_DISPLAY_NAME}\n"
        f"时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        f"上下文：{context}\n"
        f"系统：{platform.platform()}\n"
        f"Python：{sys.version}\n\n"
        + "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    )
    report_path.write_text(mask_sensitive_text(text), encoding="utf-8")
    return report_path


def install_exception_hooks():
    default_sys_hook = sys.excepthook

    def sys_hook(exc_type, exc_value, exc_traceback):
        try:
            write_crash_report(exc_type, exc_value, exc_traceback, "sys.excepthook")
        finally:
            default_sys_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = sys_hook
    if hasattr(threading, "excepthook"):
        default_thread_hook = threading.excepthook

        def thread_hook(args):
            try:
                write_crash_report(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                    f"thread:{getattr(args.thread, 'name', 'unknown')}",
                )
            finally:
                default_thread_hook(args)

        threading.excepthook = thread_hook


def rtsp_security_summary(config, stream_url=""):
    runtime_url = build_runtime_stream_url(stream_url or config.get("stream_url", ""), config)
    parsed = urlparse(runtime_url)
    if parsed.scheme.lower() not in {"rtsp", "rtsps"}:
        return "当前视频流不是 RTSP/RTSPS，安全接入设置不会参与拉流。"

    auth_text = "已配置账号/密码或Token" if (config.get("rtsp_username") or config.get("rtsp_password")) else "未单独配置账号/密码"
    tls_text = "已启用 RTSPS/TLS" if parsed.scheme.lower() == "rtsps" else "未启用 RTSPS/TLS"
    return (
        f"{auth_text}；{tls_text}；运行地址 {masked_stream_url(runtime_url)}。"
        "标准 RTSP 认证、RTSPS/TLS 可以由软件调用 FFmpeg 完成；"
        "厂家私有加密码流需要厂家 SDK、解密密钥或平台转出的标准播放流。"
    )


SUPPORTED_STREAM_SCHEMES = {
    "rtsp",
    "rtmp",
    "rtmps",
    "http",
    "https",
    "udp",
    "tcp",
    "srt",
    "rtp",
    "rtsps",
}
DIRECT_SIGNALING_SCHEMES = {"gb28181", "gb", "sip", "webrtc", "ws", "wss"}


def describe_stream_url(stream_url):
    raw = (stream_url or "").strip()
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    lowered = raw.lower()

    gb_hint = "gb28181" in lowered or "340200" in lowered or "国标" in raw
    if scheme in {"rtsp", "rtsps"}:
        return "GB28181平台转RTSP" if gb_hint else "RTSP实时流"
    if scheme in {"rtmp", "rtmps"}:
        return "RTMP/RTMPS直播流"
    if scheme in {"http", "https"}:
        if ".m3u8" in lowered:
            return "HLS(m3u8)直播流"
        if ".flv" in lowered:
            return "HTTP-FLV直播流"
        if ".ts" in lowered:
            return "HTTP-TS直播流"
        return "HTTP/HTTPS视频流"
    if scheme == "srt":
        return "SRT低延迟流"
    if scheme == "rtp":
        return "RTP实时流"
    if scheme == "udp":
        return "UDP组播/单播流"
    if scheme == "tcp":
        return "TCP实时流"
    return "实时视频流"


def normalize_stream_url_for_user(stream_url):
    # 有些平台会把真实播放地址包在 target/url 参数中，这里只解出受支持的媒体协议。
    raw = (stream_url or "").strip()
    if not raw:
        return ""
    direct_parsed = urlparse(raw)
    direct_scheme = direct_parsed.scheme.lower()
    if direct_scheme in {"http", "https"}:
        query = parse_qs(direct_parsed.query)
        for key in ("target", "url", "u"):
            values = query.get(key)
            if values:
                target = unquote(values[0]).strip()
                target_scheme = urlparse(target).scheme.lower()
                if target_scheme in SUPPORTED_STREAM_SCHEMES:
                    return target
    if direct_scheme in SUPPORTED_STREAM_SCHEMES or direct_scheme in DIRECT_SIGNALING_SCHEMES:
        return raw
    if "://" in raw and direct_scheme:
        return raw
    if any(ch.isspace() for ch in raw):
        return raw

    parsed = urlparse(f"//{raw}")
    if not parsed.hostname:
        return raw
    try:
        port = parsed.port
    except ValueError:
        return raw

    lowered = raw.lower()
    if lowered.endswith((".m3u8", ".flv", ".ts")):
        return f"http://{raw}"
    if port in {80, 443, 8000, 8080, 8081, 8090, 18080}:
        return f"http://{raw}"
    if port == 554 or re.search(r"/(streaming/channels|channels|h264|h265)/?", lowered):
        return f"rtsp://{raw}"
    return raw


def is_hls_stream_url(stream_url):
    parsed = urlparse(stream_url or "")
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    lowered = (parsed.path + "?" + parsed.query).lower()
    return ".m3u8" in lowered


def is_progressive_http_video_url(stream_url):
    parsed = urlparse(stream_url or "")
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    path = parsed.path.lower()
    return path.endswith((".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"))


def detect_stream_format(stream_url):
    raw = normalize_stream_url_for_user(stream_url)
    if not raw:
        return AUTO_STREAM_FORMAT
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    lowered = raw.lower()
    gb_hint = "gb28181" in lowered or "340200" in lowered or "国标" in raw or "/rtp/" in lowered

    if scheme in {"rtsp", "rtsps"}:
        return "RTSP 摄像头 / 国标平台转RTSP"
    if scheme in {"rtmp", "rtmps"}:
        return "RTMP/RTMPS 直播流"
    if scheme in {"http", "https"}:
        if ".flv" in lowered:
            return "GB28181 国标平台转 HTTP-FLV" if gb_hint else "HTTP-FLV 直播流"
        if ".m3u8" in lowered:
            return "GB28181 国标平台转 HLS(m3u8)" if gb_hint else "HLS(m3u8) 直播流"
        return AUTO_STREAM_FORMAT
    if scheme == "srt":
        return "SRT 低延迟流"
    if scheme in {"rtp", "udp", "tcp"}:
        return "RTP/UDP 组播或单播"
    return AUTO_STREAM_FORMAT


def validate_stream_url(stream_url):
    # 这里只校验地址是否具备“可尝试播放”的结构，真正可用性由后面的 FFmpeg 探测确认。
    raw = (stream_url or "").strip()
    suggested = normalize_stream_url_for_user(raw)
    if suggested and suggested != raw:
        if urlparse(raw).scheme.lower() in {"http", "https"}:
            return False, f"检测到网页跳转链接。可以点击“自动识别”提取真实播放地址：{suggested}"
        return False, f"地址缺少协议。可以点击“自动识别”自动补全为：{suggested}"
    parsed = urlparse(raw)
    if not parsed.scheme:
        return False, "实时视频流地址需要带协议，例如 rtsp://、rtsps://、rtmp://、http://、https://、srt://、udp:// 或 rtp://"
    scheme = parsed.scheme.lower()
    if scheme in DIRECT_SIGNALING_SCHEMES:
        return (
            False,
            "GB28181/SIP/WebRTC 属于信令或浏览器播放协议，FFmpeg 不能直接抽帧。"
            "请在国标平台、视频网关或流媒体服务中生成 RTSP、HTTP-FLV、HLS(m3u8)、RTP、UDP、SRT 等可播放地址后填入。",
        )
    if scheme not in SUPPORTED_STREAM_SCHEMES:
        return (
            False,
            "暂不支持该协议。请使用 RTSP/RTSPS、RTMP/RTMPS、HLS(m3u8)、HTTP-FLV、RTP、UDP、TCP 或 SRT；"
            "GB28181 请填写平台转出的这些播放地址。",
        )
    if any(
        placeholder in raw
        for placeholder in (
            "用户名",
            "密码",
            "摄像头IP",
            "国标平台IP",
            "设备国标编号",
            "通道编号",
            "服务器地址",
            "组播地址",
            "端口",
        )
    ):
        return False, "请先把示例地址里的用户名、密码、IP、设备编号或通道编号改成真实信息"
    if scheme in {"rtsp", "rtmp", "rtmps", "http", "https", "srt"} and not parsed.netloc:
        return False, "视频流地址缺少服务器地址，请检查 IP、域名和端口"
    return True, ""


def signed_exit_code(code):
    if code is None:
        return None
    if isinstance(code, int) and code > 0x7FFFFFFF:
        return code - 0x100000000
    return code


def format_exit_code(code):
    signed = signed_exit_code(code)
    if signed is None:
        return "None"
    if signed != code:
        return f"{code}（{signed}）"
    return str(code)


def kill_process_tree(process):
    # FFmpeg 和 SSH 可能派生子进程，Windows 下必须结束整棵进程树才能可靠停止任务。
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def compact_ffmpeg_output(output, limit=420):
    text = " ".join(str(output or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def ffmpeg_stream_error_hint(output, stream_url=""):
    # 将常见的 FFmpeg 原始错误翻译成用户可以直接排查的网络、鉴权或协议提示。
    lower = str(output or "").lower()
    parsed = urlparse(stream_url or "")
    scheme = parsed.scheme.lower()
    host = parsed.hostname or "视频流服务器"
    hints = []

    if "option rw_timeout not found" in lower or (
        "option not found" in lower and "rw_timeout" in lower
    ):
        return "当前 FFmpeg 不支持软件传入的 RTSP 超时参数，属于软件兼容性问题；请使用修复后的版本重新连接"

    if any(
        token in lower
        for token in (
            "error number -138",
            "connection timed out",
            "timed out",
            "连接超时",
            "无法连接",
            "端口不通",
            "i/o error",
        )
    ):
        hints.append(f"无法连接到 {host}，常见原因是地址失效、网络不可达、防火墙拦截或端口未开放")
    if any(token in lower for token in ("cannot open connection", "connection refused", "no route to host", "network is unreachable")):
        hints.append(f"{host} 连接失败，请确认摄像头/流媒体服务在线，且本机能访问对应端口")
    if any(token in lower for token in ("401", "unauthorized", "forbidden", "403")):
        hints.append("鉴权失败，请检查用户名、密码、Token、白名单或平台播放权限")
    if any(token in lower for token in ("404", "not found")):
        hints.append("播放路径不存在，请检查设备编号、通道编号、应用名或流名")
    if scheme in {"rtsp", "rtsps"} and any(
        token in lower
        for token in (
            "method describe failed: 500",
            "server returned 5xx",
            "internal server error",
        )
    ):
        hints.append("RTSP 服务器在 DESCRIBE 阶段返回 500；常见原因是播放 URL 已过期、平台只允许单连接、摄像头通道离线或账号密码/Token 被覆盖")
    if any(
        token in lower
        for token in (
            "could not find ref with poc",
            "error constructing the frame rps",
            "cu_qp_delta",
            "non-existing pps",
            "missing picture in access unit",
            "decode error rate",
            "error submitting packet to decoder",
        )
    ):
        hints.append(
            "已连接到视频流但 H.265/H.264 解码未获得完整关键帧或码流存在损伤；软件会优先使用稳定模式、UDP/HTTP 隧道和关键帧救援重新尝试"
        )
    if is_progressive_http_video_url(stream_url) and any(
        token in lower
        for token in (
            "invalid data found",
            "nothing was written",
            "could not open encoder before eof",
            "could not find codec parameters",
        )
    ):
        hints.append(
            "该地址更像普通 HTTP 视频文件，不是稳定实时流；建议下载后按本地视频分析，或使用 RTSP、HTTP-FLV、HLS(m3u8)、SRT 等实时播放地址"
        )
    if any(token in lower for token in ("invalid data found", "protocol not found", "invalid argument")) or (
        "server returned" in lower and "5xx" not in lower and "internal server error" not in lower
    ):
        hints.append("地址格式或协议可能不对，请确认填写的是平台转出的可播放地址，不是 GB28181/SIP/WebRTC 信令地址")
    if "method setup failed" in lower or "461" in lower:
        hints.append("RTSP 传输方式不兼容，软件会尝试 TCP/UDP；仍失败时请在摄像头或平台侧开启对应传输方式")
    if scheme in {"rtmp", "rtmps"}:
        hints.append("RTMP 服务经常被公网或防火墙限制 1935 端口；建议优先使用 RTSP、HTTP-FLV 或 HLS 地址")
    if scheme in DIRECT_SIGNALING_SCHEMES:
        hints.append("该地址是信令地址，不能直接抽帧；请先在国标平台生成 RTSP、HTTP-FLV 或 HLS 播放地址")

    unique_hints = []
    for hint in hints:
        if hint not in unique_hints:
            unique_hints.append(hint)
    if unique_hints:
        return "；".join(unique_hints)
    return "FFmpeg 未能从该地址读取到视频帧，请确认该地址能被 VLC 或 FFmpeg 正常播放"


def stream_probe_timed_out(output):
    return "读取 1 帧超时" in str(output or "")


def is_ffmpeg_nonfatal_noise(text):
    lowered = str(text or "").lower()
    noise_patterns = (
        "co located pocs unavailable",
        "mmco: unref short failure",
        "reference picture missing",
        "decode_slice_header error",
        "concealing ",
        "corrupt decoded frame",
        "non-existing pps",
        "no frame!",
        "error while decoding mb",
        "could not find ref with poc",
        "error constructing the frame rps",
        "cu_qp_delta",
        "missing picture in access unit",
        "decode error rate",
        "error submitting packet to decoder",
        "decoding error: invalid data found",
    )
    return any(pattern in lowered for pattern in noise_patterns)


def rtsp_transport_mode_label(value):
    return RTSP_TRANSPORT_MODE_LABELS.get(str(value or "auto").lower(), RTSP_TRANSPORT_MODE_LABELS["auto"])


def rtsp_transport_mode_value(label):
    text = str(label or "").strip()
    for value, display in RTSP_TRANSPORT_MODE_LABELS.items():
        if text == display or text.lower() == value:
            return value
    return "auto"


def build_ssh_tunnel_parts(config, ssh_path="ssh"):
    # 使用参数列表而不是拼接 shell 字符串，路径中有空格时也能正确传给 subprocess。
    local_port = int_from(config.get("ssh_local_port"), 8080, 1, 65535)
    remote_host = (config.get("ssh_remote_host") or "").strip()
    remote_port = int_from(config.get("ssh_remote_port"), 8000, 1, 65535)
    ssh_host = (config.get("ssh_host") or "").strip()
    ssh_user = (config.get("ssh_user") or "").strip()
    ssh_port = int_from(config.get("ssh_port"), 22, 1, 65535)
    key_path = (config.get("ssh_key_path") or "").strip()

    if not remote_host or not ssh_host or not ssh_user:
        return []

    parts = [
        ssh_path,
        "-N",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-L",
        f"{local_port}:{remote_host}:{remote_port}",
        "-p",
        str(ssh_port),
    ]
    if key_path:
        parts.extend(["-i", key_path])
    parts.append(f"{ssh_user}@{ssh_host}")
    return parts


def command_preview(parts):
    if not parts:
        return ""
    return " ".join(f'"{part}"' if " " in str(part) else str(part) for part in parts)


def int_from(value, default, minimum=None, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def float_from(value, default, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def find_tool(name):
    # 发布包优先使用 tools 目录内的固定版本，开发环境才回退到系统 PATH。
    exe_name = f"{name}.exe" if os.name == "nt" else name
    for local_tool in (TOOLS_DIR / exe_name, RESOURCE_TOOLS_DIR / exe_name):
        if local_tool.exists():
            return str(local_tool)
    return shutil.which(name)


def ffmpeg_smoke_test():
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return False, "未找到 FFmpeg"
    with tempfile.TemporaryDirectory(prefix="video_analyzer_ffmpeg_") as temp_dir:
        output_pattern = str(Path(temp_dir) / "frame_%03d.jpg")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=2",
            "-vf",
            "fps=1",
            "-q:v",
            "4",
            "-y",
            output_pattern,
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"FFmpeg 自检启动失败：{exc}"
        frames = list(Path(temp_dir).glob("frame_*.jpg"))
        if completed.returncode != 0 or len(frames) < 2:
            detail = compact_ffmpeg_output(completed.stdout)
            return False, f"FFmpeg 自检未生成预期帧：{detail or completed.returncode}"
    return True, f"FFmpeg 实际抽帧通过：{ffmpeg}"


def release_acceptance_command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def update_system_self_test():
    details = {}
    ok = True
    with tempfile.TemporaryDirectory(prefix="traffic_light_update_test_") as temp_dir:
        root = Path(temp_dir)
        package = root / "Traffic Light_V2.2_Setup.exe"
        package.write_bytes(b"traffic-light-update-package")
        package_sha = hashlib.sha256(package.read_bytes()).hexdigest()
        update_json = root / "update.json"
        update_json.write_text(
            json.dumps(
                {
                    "app_name": APP_NAME,
                    "latest_version": "2.2.0",
                    "release_date": "2026-06-23",
                    "download_url": package.name,
                    "file_size": package.stat().st_size,
                    "sha256": package_sha,
                    "notes": ["update self test"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        info = check_for_update(str(update_json), APP_VERSION, base_dirs=[root])
        details["new_version_detected"] = bool(info.get("has_update"))
        ok = ok and details["new_version_detected"]
        target = download_update_file(info, root / "downloads", base_dirs=[root])
        details["download_ok"] = target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == package_sha
        ok = ok and details["download_ok"]

        latest_json = root / "latest.json"
        latest_json.write_text(
            json.dumps({"latest_version": APP_VERSION, "download_url": ""}, ensure_ascii=False),
            encoding="utf-8",
        )
        latest_info = check_for_update(str(latest_json), APP_VERSION, base_dirs=[root])
        details["latest_no_update"] = not latest_info.get("has_update")
        ok = ok and details["latest_no_update"]

        bad_json = root / "bad.json"
        bad_json.write_text("{", encoding="utf-8")
        try:
            check_for_update(str(bad_json), APP_VERSION, base_dirs=[root])
            details["bad_json_rejected"] = False
        except UpdateError:
            details["bad_json_rejected"] = True
        ok = ok and details["bad_json_rejected"]

        missing_json = root / "missing.json"
        try:
            check_for_update(str(missing_json), APP_VERSION, base_dirs=[root])
            details["missing_json_rejected"] = False
        except UpdateError:
            details["missing_json_rejected"] = True
        ok = ok and details["missing_json_rejected"]

        bad_sha_info = dict(info)
        bad_sha_info["sha256"] = "0" * 64
        try:
            download_update_file(bad_sha_info, root / "bad_sha", base_dirs=[root])
            details["bad_sha_rejected"] = False
        except UpdateError:
            details["bad_sha_rejected"] = True
        ok = ok and details["bad_sha_rejected"]

        cancel_event = threading.Event()
        cancel_event.set()
        try:
            download_update_file(info, root / "cancelled", base_dirs=[root], cancel_event=cancel_event)
            details["cancel_supported"] = False
        except UpdateCancelled:
            details["cancel_supported"] = True
        ok = ok and details["cancel_supported"]

    print(json.dumps({"passed": ok, "details": details}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def release_site_candidates():
    candidates = [
        APP_DIR / RELEASE_SITE_DIR_NAME,
        RESOURCE_DIR / RELEASE_SITE_DIR_NAME,
        Path(__file__).resolve().parent / RELEASE_SITE_DIR_NAME,
    ]
    unique = []
    seen = set()
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_release_site_dir():
    for site_dir in release_site_candidates():
        if (site_dir / "index.html").exists() and (site_dir / LATEST_UPDATE_JSON).exists():
            return site_dir
    return None


def release_site_self_test():
    import functools
    import http.server

    details = {}
    temp_files = []
    server = None
    thread = None
    site_dir = find_release_site_dir()
    ok = site_dir is not None
    details["release_site_found"] = bool(site_dir)
    if not site_dir:
        print(json.dumps({"passed": False, "details": details}, ensure_ascii=False, indent=2))
        return 1

    try:
        invalid_json = site_dir / "releases" / "latest" / "_invalid_update_test.json"
        missing_field_json = site_dir / "releases" / "latest" / "_missing_field_update_test.json"
        invalid_json.write_text("{", encoding="utf-8")
        missing_field_json.write_text(json.dumps({"app_name": APP_NAME}, ensure_ascii=False), encoding="utf-8")
        temp_files.extend([invalid_json, missing_field_json])

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, _format, *args):
                return

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    return

            def copyfile(self, source, outputfile):
                try:
                    return super().copyfile(source, outputfile)
                except (BrokenPipeError, ConnectionResetError):
                    return

        handler = functools.partial(QuietHandler, directory=str(site_dir))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, name="release-site-test", daemon=True)
        thread.start()

        base_url = f"http://127.0.0.1:{port}/"
        update_url = base_url + LATEST_UPDATE_JSON
        index_response = requests.get(base_url, timeout=8)
        details["index_accessible"] = index_response.status_code == 200 and "Traffic Light" in index_response.text
        ok = ok and details["index_accessible"]

        update_response = requests.get(update_url, timeout=8)
        details["update_json_accessible"] = update_response.status_code == 200
        ok = ok and details["update_json_accessible"]

        info = check_for_update(update_url, "2.0.0", timeout=8)
        details["detects_v21_from_v20"] = bool(info.get("has_update")) and info.get("latest_version") == APP_VERSION
        details["extended_fields_parsed"] = all(
            key in info
            for key in (
                "version_code",
                "channel",
                "minimum_supported_version",
                "force_update",
                "package_type",
                "manual_download_url",
            )
        )
        ok = ok and details["detects_v21_from_v20"] and details["extended_fields_parsed"]

        with tempfile.TemporaryDirectory(prefix="traffic_light_release_site_") as temp_dir:
            download_dir = Path(temp_dir) / "下载 测试"
            local_info = dict(info)
            download_name = Path(unquote(urlparse(str(info.get("download_url") or "")).path)).name
            local_info["download_url"] = base_url + "downloads/" + quote(download_name or f"Traffic_Light_v{APP_VERSION}.zip")
            target = download_update_file(local_info, download_dir, timeout=20)
            details["download_ok"] = target.exists() and target.stat().st_size == int(info.get("file_size") or 0)
            details["sha256_ok"] = details["download_ok"] and hashlib.sha256(target.read_bytes()).hexdigest() == info.get("sha256")
            ok = ok and details["download_ok"] and details["sha256_ok"]

            bad_sha_info = dict(local_info)
            bad_sha_info["sha256"] = "0" * 64
            try:
                download_update_file(bad_sha_info, Path(temp_dir) / "bad_sha", timeout=20)
                details["bad_sha_rejected"] = False
            except UpdateError as exc:
                details["bad_sha_rejected"] = "校验失败" in str(exc)
            ok = ok and details["bad_sha_rejected"]

            missing_download_info = dict(info)
            missing_download_info["download_url"] = "../../downloads/not-found-update-test.exe"
            try:
                download_update_file(missing_download_info, Path(temp_dir) / "missing_download", timeout=8)
                details["missing_download_rejected"] = False
            except UpdateError:
                details["missing_download_rejected"] = True
            ok = ok and details["missing_download_rejected"]

            cancel_event = threading.Event()
            cancel_event.set()
            try:
                download_update_file(local_info, Path(temp_dir) / "cancelled", cancel_event=cancel_event, timeout=20)
                details["cancel_supported"] = False
            except UpdateCancelled:
                details["cancel_supported"] = True
            ok = ok and details["cancel_supported"]

        try:
            check_for_update(base_url + "releases/latest/_invalid_update_test.json", "2.0.0", timeout=8)
            details["invalid_json_rejected"] = False
        except UpdateError:
            details["invalid_json_rejected"] = True
        ok = ok and details["invalid_json_rejected"]

        try:
            check_for_update(base_url + "releases/latest/_missing_field_update_test.json", "2.0.0", timeout=8)
            details["missing_field_rejected"] = False
        except UpdateError:
            details["missing_field_rejected"] = True
        ok = ok and details["missing_field_rejected"]

        try:
            check_for_update(base_url + "releases/latest/not-found-update-test.json", "2.0.0", timeout=8)
            details["missing_update_json_rejected"] = False
        except UpdateError:
            details["missing_update_json_rejected"] = True
        ok = ok and details["missing_update_json_rejected"]

        server.shutdown()
        server.server_close()
        server = None
        try:
            check_for_update(update_url, "2.0.0", timeout=1)
            details["server_down_rejected"] = False
        except UpdateError:
            details["server_down_rejected"] = True
        ok = ok and details["server_down_rejected"]
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        for path in temp_files:
            try:
                path.unlink()
            except OSError:
                pass

    print(json.dumps({"passed": ok, "details": details}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def report_image_self_test():
    details = {}
    ok = True
    with tempfile.TemporaryDirectory(prefix="traffic_light_report_image_") as temp_dir:
        root = Path(temp_dir)
        results = root / "结果 目录"
        config = DEFAULT_CONFIG.copy()
        config.update(
            {
                "results_dir": str(results),
                "image_dir": str(root / "frames"),
                "capture_mode": "interval",
                "frame_interval": 5,
            }
        )
        engine = AnalysisEngine(config, record_session=False)
        engine.current_input_source = {"type": "file", "value": str(root / "测试 视频.mp4")}
        image_path = root / "frame_20260623_000001.jpg"
        Image.new("RGB", (640, 360), color=(30, 120, 210)).save(image_path, "JPEG")
        result = engine.record_result(image_path, "测试分析结果")
        markdown = engine.result_file.read_text(encoding="utf-8")
        asset_path = Path(result.get("frame_image_path") or "")
        details["asset_saved"] = asset_path.exists()
        details["markdown_has_image"] = "![抽帧图片]" in markdown and "测试分析结果" in markdown
        details["frame_time_recorded"] = result.get("frame_time") == "视频时间轴 00:00:00"
        missing_result = engine.record_result(root / "missing_000002.jpg", "缺失图片结果")
        markdown = engine.result_file.read_text(encoding="utf-8")
        details["missing_image_safe"] = not missing_result.get("frame_image_path") and "对应抽帧图片缺失" in markdown
        ok = all(details.values())
    print(json.dumps({"passed": ok, "details": details}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def verify_jpeg_file(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def free_local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def free_udp_port_pair():
    for _attempt in range(60):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port % 2:
            port += 1
        sockets = []
        try:
            for candidate in (port, port + 1):
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.bind(("127.0.0.1", candidate))
                sockets.append(udp)
            return port
        except OSError:
            pass
        finally:
            for udp in sockets:
                udp.close()
    return free_local_port()


def run_release_subprocess(command, timeout=60):
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
        return {
            "passed": completed.returncode == 0,
            "returncode": completed.returncode,
            "output": compact_ffmpeg_output(completed.stdout, 800),
        }
    except subprocess.TimeoutExpired as exc:
        return {"passed": False, "returncode": None, "output": f"超时：{exc}"}
    except OSError as exc:
        return {"passed": False, "returncode": None, "output": f"启动失败：{exc}"}


def ffmpeg_protocols(ffmpeg):
    result = run_release_subprocess([ffmpeg, "-hide_banner", "-protocols"], timeout=20)
    text = result.get("output", "")
    protocols = set()
    for line in text.split():
        token = line.strip()
        if re.fullmatch(r"[a-z0-9_]+", token):
            protocols.add(token)
    return protocols


def generate_release_media(ffmpeg, temp_root):
    temp_root = Path(temp_root)
    mp4_path = temp_root / "sample.mp4"
    flv_path = temp_root / "sample.flv"
    hls_dir = temp_root / "hls"
    hls_dir.mkdir(parents=True, exist_ok=True)
    hls_path = hls_dir / "index.m3u8"
    commands = {
        "mp4": [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=5:size=320x240:rate=8",
            "-an",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            "-movflags",
            "+faststart",
            str(mp4_path),
        ],
        "flv": [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=5:size=320x240:rate=8",
            "-an",
            "-c:v",
            "flv",
            "-f",
            "flv",
            str(flv_path),
        ],
        "hls": [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=5:size=320x240:rate=8",
            "-an",
            "-c:v",
            "mpeg2video",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            str(hls_path),
        ],
    }
    results = {}
    for name, command in commands.items():
        results[name] = run_release_subprocess(command, timeout=45)
    return {
        "results": results,
        "mp4": mp4_path,
        "flv": flv_path,
        "hls": hls_path,
    }


def pull_one_frame(ffmpeg, source, output_path, input_options=None, timeout=25):
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *(input_options or []),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-frames:v",
        "1",
        "-q:v",
        "4",
        str(output_path),
    ]
    result = run_release_subprocess(command, timeout=timeout)
    result["frame_ok"] = verify_jpeg_file(output_path)
    result["passed"] = bool(result["passed"] and result["frame_ok"])
    return result


def start_release_http_server(directory):
    import functools
    import http.server

    class QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, _format, *args):
            return

    handler = functools.partial(
        QuietHTTPRequestHandler,
        directory=str(directory),
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def pull_one_frame_with_sender(
    ffmpeg,
    input_url,
    sender_url,
    output_path,
    input_options=None,
    timeout=18,
    sender_format="mpegts",
):
    pull_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *(input_options or []),
        "-i",
        input_url,
        "-map",
        "0:v:0",
        "-an",
        "-frames:v",
        "1",
        "-q:v",
        "4",
        str(output_path),
    ]
    sender_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=6:size=320x240:rate=8",
        "-an",
        "-c:v",
        "mpeg2video",
        "-f",
        sender_format,
        sender_url,
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    pull = None
    sender = None
    try:
        pull = subprocess.Popen(
            pull_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        time.sleep(0.8)
        sender = subprocess.Popen(
            sender_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        try:
            output, _ = pull.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(pull)
            output = "读取 1 帧超时"
        passed = pull.poll() == 0 and verify_jpeg_file(output_path)
        return {
            "passed": passed,
            "returncode": pull.poll(),
            "frame_ok": verify_jpeg_file(output_path),
            "output": compact_ffmpeg_output(output, 800),
        }
    except OSError as exc:
        return {"passed": False, "returncode": None, "frame_ok": False, "output": str(exc)}
    finally:
        if sender is not None:
            kill_process_tree(sender)
        if pull is not None:
            kill_process_tree(pull)


def release_acceptance_test():
    checks = {}
    skipped = {}

    def record(name, passed, detail=None):
        checks[name] = {"passed": bool(passed)}
        if detail is not None:
            checks[name]["detail"] = detail

    ffmpeg = find_tool("ffmpeg")
    record("ffmpeg_available", bool(ffmpeg), ffmpeg or "未找到 FFmpeg")
    if not ffmpeg:
        result = {"passed": False, "checks": checks, "skipped": skipped}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    ok, message = ffmpeg_smoke_test()
    record("ffmpeg_smoke", ok, message)
    protocols = ffmpeg_protocols(ffmpeg)
    record(
        "ffmpeg_protocols",
        all(protocol in protocols for protocol in ("file", "http", "https", "rtmp", "rtmps", "udp", "tcp", "srt")),
        sorted(protocols),
    )

    config = DEFAULT_CONFIG.copy()
    config.update({"image_dir": "", "results_dir": ""})
    record(
        "fresh_sensitive_fields_blank",
        not DEFAULT_CONFIG["api_url"] and not DEFAULT_CONFIG["api_key"] and not DEFAULT_CONFIG["model"],
        {"api_url": DEFAULT_CONFIG["api_url"], "model": DEFAULT_CONFIG["model"]},
    )

    stream_cases = {
        "rtsp": ("rtsp://127.0.0.1:8554/live", "RTSP 摄像头 / 国标平台转RTSP"),
        "rtsps": ("rtsps://127.0.0.1:8554/live", "RTSP 摄像头 / 国标平台转RTSP"),
        "rtmp": ("rtmp://127.0.0.1/live/cam", "RTMP/RTMPS 直播流"),
        "rtmps": ("rtmps://127.0.0.1/live/cam", "RTMP/RTMPS 直播流"),
        "http_flv": ("http://127.0.0.1/live/cam.flv", "HTTP-FLV 直播流"),
        "gb_http_flv": ("http://127.0.0.1/rtp/34020000001320000001.live.flv", "GB28181 国标平台转 HTTP-FLV"),
        "hls": ("http://127.0.0.1/live/cam.m3u8", "HLS(m3u8) 直播流"),
        "gb_hls": ("http://127.0.0.1/rtp/34020000001320000001/hls.m3u8", "GB28181 国标平台转 HLS(m3u8)"),
        "srt": ("srt://127.0.0.1:9000?mode=caller", "SRT 低延迟流"),
        "udp": ("udp://239.0.0.1:1234", "RTP/UDP 组播或单播"),
        "rtp": ("rtp://239.0.0.1:5004", "RTP/UDP 组播或单播"),
        "tcp": ("tcp://127.0.0.1:9001", "RTP/UDP 组播或单播"),
    }
    format_details = {}
    format_ok = True
    for name, (url, expected) in stream_cases.items():
        valid, valid_message = validate_stream_url(url)
        detected = detect_stream_format(url)
        format_details[name] = {
            "valid": valid,
            "message": valid_message,
            "detected": detected,
            "description": describe_stream_url(url),
        }
        format_ok = format_ok and valid and detected == expected
    record("stream_format_detection_and_validation", format_ok, format_details)
    invalid_ok, invalid_message = validate_stream_url("gb28181://34020000001320000001")
    record("direct_signal_protocol_rejected", not invalid_ok and "信令" in invalid_message, invalid_message)

    engine = AnalysisEngine(DEFAULT_CONFIG.copy(), record_session=False)
    option_details = {}
    option_ok = True
    for name, (url, _expected) in stream_cases.items():
        options = engine.build_ffmpeg_input_options("stream", url, low_latency=False)
        option_details[name] = options
        option_ok = option_ok and "-rw_timeout" not in options
    option_ok = option_ok and "-rtsp_transport" in option_details["rtsp"]
    option_ok = option_ok and "-rtmp_live" in option_details["rtmp"]
    option_ok = option_ok and "-connect_timeout" in option_details["srt"]
    option_ok = option_ok and "-overrun_nonfatal" in option_details["udp"]
    option_ok = option_ok and "-reconnect" in option_details["hls"]
    record("stream_input_options", option_ok, option_details)
    engine.prepare_rtsp_runtime_plan({"type": "stream", "value": "rtsp://admin:pass@127.0.0.1/live"})
    rtsp_labels = [candidate["label"] for candidate in engine.rtsp_runtime_candidates]
    record(
        "rtsp_retry_plan",
        {"RTSP TCP稳定", "RTSP UDP稳定", "RTSP HTTP隧道", "RTSP TCP关键帧救援"}.issubset(set(rtsp_labels)),
        rtsp_labels,
    )

    capture_details = {}
    capture_ok = True
    for source_type in ("file", "stream"):
        for mode, values in (
            ("interval", {"capture_mode": "interval", "frame_interval": 5}),
            ("point", {"capture_mode": "point", "capture_point_time": "00:00:02"}),
            ("range", {"capture_mode": "range", "capture_start_time": "00:00:01", "capture_end_time": "00:00:04", "frame_interval": 2}),
        ):
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(values)
            try:
                plan = build_capture_plan(cfg, source_type)
                capture_details[f"{source_type}_{mode}"] = plan
                capture_ok = capture_ok and bool(plan.get("summary"))
            except ValueError as exc:
                capture_details[f"{source_type}_{mode}"] = str(exc)
                capture_ok = False
    record("capture_modes", capture_ok, capture_details)

    child_checks = {
        "ui_workflow": "--ui-workflow-test",
        "ui_tabs": "--ui-tab-switch-test",
        "ui_video_preview": "--ui-video-preview-test",
        "ui_resize": "--ui-resize-smooth-test",
        "ui_buttons": "--ui-button-audit-test",
        "update_system": "--update-system-test",
        "release_site": "--release-site-test",
        "report_images": "--report-image-test",
        "health_check": "--check",
    }
    child_details = {}
    child_ok = True
    for name, arg in child_checks.items():
        detail = run_release_subprocess(release_acceptance_command(arg), timeout=180)
        child_details[name] = detail
        child_ok = child_ok and detail["passed"]
    record("ui_and_health_checks", child_ok, child_details)

    with tempfile.TemporaryDirectory(prefix="video_analyzer_release_") as temp_dir:
        temp_root = Path(temp_dir)
        media = generate_release_media(ffmpeg, temp_root)
        media_ok = all(item["passed"] for item in media["results"].values())
        record("generated_test_media", media_ok, media["results"])

        http_server = None
        try:
            http_server, base_url = start_release_http_server(temp_root)
            pull_details = {
                "local_mp4": pull_one_frame(ffmpeg, media["mp4"], temp_root / "local_mp4.jpg"),
                "http_mp4": pull_one_frame(ffmpeg, f"{base_url}/sample.mp4", temp_root / "http_mp4.jpg"),
                "http_flv": pull_one_frame(ffmpeg, f"{base_url}/sample.flv", temp_root / "http_flv.jpg"),
                "hls_m3u8": pull_one_frame(ffmpeg, f"{base_url}/hls/index.m3u8", temp_root / "hls.jpg"),
            }
            record("file_http_hls_flv_real_pull", all(item["passed"] for item in pull_details.values()), pull_details)
        finally:
            if http_server is not None:
                http_server.shutdown()
                http_server.server_close()

        live_details = {}
        udp_port = free_local_port()
        live_details["udp_mpegts"] = pull_one_frame_with_sender(
            ffmpeg,
            f"udp://127.0.0.1:{udp_port}?overrun_nonfatal=1&fifo_size=5000000",
            f"udp://127.0.0.1:{udp_port}?pkt_size=1316",
            temp_root / "udp.jpg",
            input_options=["-timeout", "8000000", "-overrun_nonfatal", "1"],
        )
        rtp_port = free_udp_port_pair()
        rtp_url = f"rtp://127.0.0.1:{rtp_port}"
        live_details["rtp_mpegts"] = pull_one_frame_with_sender(
            ffmpeg,
            rtp_url,
            rtp_url,
            temp_root / "rtp.jpg",
            input_options=engine.build_ffmpeg_input_options(
                "stream",
                rtp_url,
                low_latency=False,
            ),
            sender_format="rtp_mpegts",
        )
        tcp_port = free_local_port()
        live_details["tcp_mpegts"] = pull_one_frame_with_sender(
            ffmpeg,
            f"tcp://127.0.0.1:{tcp_port}?listen=1",
            f"tcp://127.0.0.1:{tcp_port}",
            temp_root / "tcp.jpg",
            input_options=["-timeout", "8000000"],
        )
        if "srt" in protocols:
            srt_port = free_local_port()
            live_details["srt_mpegts"] = pull_one_frame_with_sender(
                ffmpeg,
                f"srt://127.0.0.1:{srt_port}?mode=listener&transtype=live",
                f"srt://127.0.0.1:{srt_port}?mode=caller&transtype=live",
                temp_root / "srt.jpg",
                input_options=["-connect_timeout", "8000"],
            )
        else:
            skipped["srt_real_pull"] = "当前 FFmpeg 不支持 srt 协议"
        record(
            "udp_tcp_srt_real_pull",
            all(item["passed"] for item in live_details.values()),
            live_details,
        )

    service_dependent = {
        "rtsp_real_pull": "需要现场 RTSP 摄像头或 RTSP 服务端；本机已验证 RTSP URL、鉴权拼接、FFmpeg 参数和自动重试候选。",
        "rtsps_real_pull": "需要支持 RTSPS/TLS 的摄像头或流媒体服务端。",
        "rtmp_real_pull": "需要 RTMP 服务端。",
        "rtmps_real_pull": "需要带 TLS 证书的 RTMPS 服务端。",
    }
    skipped.update(service_dependent)

    failed = {name: payload for name, payload in checks.items() if not payload.get("passed")}
    result = {
        "passed": not failed,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "checks": checks,
        "skipped_service_dependent": skipped,
        "failed": failed,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["passed"] else 1


def parse_ffmpeg_duration(output):
    match = re.search(
        r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)",
        str(output or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return 0.0
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return max(0.0, hours * 3600 + minutes * 60 + seconds)


def probe_video_duration(video_path, timeout=20):
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return 0.0
    path = Path(video_path)
    if not path.exists():
        return 0.0
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    return parse_ffmpeg_duration(completed.stdout)


def build_preview_ffmpeg_command(
    ffmpeg,
    source_type,
    source_value,
    input_options=None,
    start_time=0.0,
    fps=8,
    max_width=960,
    single_frame=False,
):
    input_options = list(input_options or [])
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if source_type != "stream" and float_from(start_time, 0.0, 0.0, 86400.0) > 0:
        command.extend(["-ss", format_ffmpeg_seconds(start_time)])
    command.extend(input_options)
    command.extend(["-i", str(source_value), "-map", "0:v:0", "-an"])
    if single_frame:
        command.extend(["-frames:v", "1"])
    else:
        command.extend(["-vf", f"fps={max(1, int(fps))},scale='min({int(max_width)},iw)':-2"])
    command.extend(["-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "pipe:1"])
    return command


def wait_for_file_ready(image_path, timeout=10):
    # watchdog 的 created 事件可能早于文件写完；大小稳定且 Pillow 能校验后才允许入模。
    deadline = time.time() + timeout
    last_size = -1

    while time.time() < deadline:
        path = Path(image_path)
        if not path.exists():
            time.sleep(0.2)
            continue

        try:
            size = path.stat().st_size
        except OSError:
            time.sleep(0.2)
            continue

        if size > 0 and size == last_size:
            try:
                with Image.open(path) as img:
                    img.verify()
                return True
            except Exception:
                pass

        last_size = size
        time.sleep(0.3)

    return False


def optimize_and_encode_image(image_path, max_image_size):
    # 在客户端统一缩放并转成 JPEG，控制请求体大小，同时兼容带透明通道的 PNG。
    with Image.open(image_path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")

        img.thumbnail((max_image_size, max_image_size), PIL_LANCZOS)

        buffered = BytesIO()
        img.save(buffered, format="JPEG", quality=85)
        return base64.b64encode(buffered.getvalue()).decode("utf-8")


def build_payload(config, base64_image):
    # 请求格式保持 OpenAI 兼容，公网服务和私有化模型可以复用同一条分析链路。
    return {
        "model": config["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}",
                        },
                    },
                    {
                        "type": "text",
                        "text": config["prompt"],
                    },
                ],
            }
        ],
        "max_tokens": int_from(config.get("max_tokens"), 1500, 1),
        "temperature": float_from(config.get("temperature"), 0.3, 0, 2),
    }


def normalize_chat_url(api_url):
    parsed = urlparse((api_url or "").strip())
    path = parsed.path.rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return api_url
    if path.endswith("/chat/completions"):
        return parsed._replace(path=path, query="", fragment="").geturl()
    if path.endswith("/models"):
        path = path[: -len("/models")]
    if path.endswith("/v1"):
        path = f"{path}/chat/completions"
    else:
        path = f"{path}/chat/completions" if path else "/v1/chat/completions"
    return parsed._replace(path=path, query="", fragment="").geturl()


def is_local_api(api_url):
    host = urlparse(api_url).hostname
    return host in {"localhost", "127.0.0.1", "::1"}


def is_private_network_host(host):
    if not host:
        return False
    lowered = host.lower()
    if lowered in {"localhost", "local"}:
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def validate_api_url(api_url, connection_mode):
    normalized = normalize_chat_url(api_url)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "接口地址需要是完整的 http 或 https 地址，例如 https://xxx/v1/chat/completions"
    if connection_mode == "public" and is_private_network_host(parsed.hostname):
        return False, "公网大模型模式不要填写 localhost 或内网 IP；如果是私有化模型，请选择对应的私有化路线"
    if connection_mode == "private_ssh" and not is_local_api(normalized):
        return False, "SSH 跳板机模式会通过本机 localhost 访问，请先点击“生成本机接口地址”"
    return True, normalized


def looks_like_vision_model(model_name):
    lowered = (model_name or "").lower()
    return any(hint in lowered for hint in VISION_MODEL_HINTS)


def model_id_from_display(value):
    text = (value or "").strip()
    for prefix in ("✓ 图像分析", "⚠ 可能仅文本", "？需确认"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def model_display_name(model_name):
    model = model_id_from_display(model_name)
    if not model:
        return ""
    if looks_like_vision_model(model):
        return f"✓ 图像分析  {model}"
    return f"⚠ 可能仅文本  {model}"


CONNECTION_MODE_NAMES = {
    "public": "公网大模型",
    "private_ssh": "SSH跳板机私有化",
    "private_direct": "私有化直连",
}


def connection_mode_name(mode):
    return CONNECTION_MODE_NAMES.get(str(mode or "public"), "公网大模型")


def evaluate_workflow_readiness(config, path_exists=None):
    """Return syntax-level readiness for the single-page task workflow."""
    path_exists = path_exists or (lambda value: Path(value).is_file())
    source_type = str(config.get("source_type") or "file")
    source_ready = False
    source_message = "请选择视频来源"

    if source_type == "stream":
        stream_url = normalize_stream_url_for_user(config.get("stream_url", ""))
        if not stream_url:
            source_message = "请填写视频流地址"
        else:
            source_ready, source_message = validate_stream_url(stream_url)
            if source_ready:
                source_message = describe_stream_url(stream_url)
    else:
        video_file = str(config.get("video_file") or "").strip()
        if not video_file:
            source_message = "请选择本地视频"
        elif not path_exists(video_file):
            source_message = "视频文件不存在"
        else:
            source_ready = True
            source_message = Path(video_file).name

    connection_mode = str(config.get("connection_mode") or "public")
    server_ready, server_message = validate_api_url(
        config.get("api_url", ""),
        connection_mode,
    )
    if server_ready and connection_mode == "public" and not str(config.get("api_key") or "").strip():
        server_ready = False
        server_message = "请填写 API 密钥"
    elif server_ready and api_key_looks_like_url(
        config.get("api_key", ""),
        config.get("api_url", ""),
    ):
        server_ready = False
        server_message = "API 密钥不能填写接口地址"
    if server_ready and connection_mode == "private_ssh":
        missing = [
            label
            for key, label in (
                ("ssh_host", "SSH服务器"),
                ("ssh_user", "用户名"),
                ("ssh_remote_host", "模型服务地址"),
            )
            if not str(config.get(key) or "").strip()
        ]
        if missing:
            server_ready = False
            server_message = "缺少" + "、".join(missing)
    model = str(config.get("model") or "").strip()
    if server_ready and not model:
        server_ready = False
        server_message = "请选择视觉模型"
    elif server_ready and not looks_like_vision_model(model):
        server_ready = False
        server_message = "当前模型可能不支持图像"
    elif server_ready:
        server_message = f"{connection_mode_name(connection_mode)} / {model}"

    prompt_ready = bool(str(config.get("prompt") or "").strip())
    prompt_message = (
        str(config.get("selected_prompt_preset") or "自定义分析规则")
        if prompt_ready
        else "请选择模板或填写分析目标"
    )
    return {
        "source_ready": source_ready,
        "source_message": source_message,
        "server_ready": server_ready,
        "server_message": server_message,
        "prompt_ready": prompt_ready,
        "prompt_message": prompt_message,
        "ready": source_ready and server_ready and prompt_ready,
    }


def choose_best_model(models, current_model=""):
    current_model = model_id_from_display(current_model)
    if current_model in models and looks_like_vision_model(current_model):
        return current_model
    for hint in VISION_MODEL_HINTS:
        for model in models:
            if hint in model.lower():
                return model
    if current_model in models:
        return current_model
    return models[0] if models else current_model


def format_model_summary(models, selected_model="", limit=12):
    if not models:
        return "未读取到模型"
    vision_models = [model for model in models if looks_like_vision_model(model)]
    visible = vision_models[:limit] if vision_models else models[:limit]
    suffix = "" if len(visible) <= limit and len(models) <= limit else f" 等 {len(models)} 个模型"
    selected = f"；已选择：{selected_model}" if selected_model else ""
    if vision_models:
        return (
            f"读取到 {len(models)} 个模型，其中 {len(vision_models)} 个疑似支持图像分析："
            f"{', '.join(visible)}{suffix}{selected}"
        )
    return f"读取到 {len(models)} 个模型；未发现名称明显带 VL/vision 的图像模型{selected}"


def api_host_is_reachable(api_url, timeout=3):
    parsed = urlparse(api_url)
    host = parsed.hostname
    if not host:
        return False, "接口地址不正确"

    if parsed.port:
        port = parsed.port
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} 可以连接"
    except OSError as exc:
        return False, f"{host}:{port} 无法连接：{exc}"


def rtsp_control_endpoint(stream_url):
    parsed = urlparse(stream_url or "")
    if parsed.scheme.lower() not in {"rtsp", "rtsps"} or not parsed.hostname:
        return None
    if parsed.port:
        port = parsed.port
    elif parsed.scheme.lower() == "rtsps":
        port = 322
    else:
        port = 554
    return parsed.hostname, port


def rtsp_control_port_is_reachable(stream_url, timeout=4):
    endpoint = rtsp_control_endpoint(stream_url)
    if not endpoint:
        return True, ""
    host, port = endpoint
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} 可以连接"
    except socket.gaierror as exc:
        return False, f"{host}:{port} 域名解析失败：{exc}"
    except TimeoutError as exc:
        return False, f"{host}:{port} 连接超时：{exc}"
    except OSError as exc:
        return False, f"{host}:{port} 无法连接：{exc}"


def wait_for_api_ready(api_url, timeout=15, interval=0.5):
    deadline = time.time() + timeout
    last_message = ""
    while time.time() < deadline:
        ok, message = api_host_is_reachable(api_url, timeout=2)
        if ok:
            return True, message
        last_message = message
        time.sleep(interval)
    return False, last_message or "接口未就绪"


def explain_api_error(status_code, detail):
    if status_code == 400:
        return "请求格式不被服务端接受，请优先检查模型是否支持图片输入、接口路径是否正确。"
    if status_code in {401, 403}:
        return "认证失败，请检查 API 密钥是否正确、是否有该模型权限。"
    if status_code == 404:
        return "接口或模型不存在，请检查接口地址是否以 /chat/completions 结尾，并确认模型名可用。"
    if status_code == 429:
        return "请求过于频繁或额度不足，请降低抽帧频率、减小并发，稍后重试。"
    if status_code >= 500:
        return "模型服务端暂时异常，请稍后重试或检查远端服务状态。"
    return f"服务端返回异常：{detail}"


def models_url_from_chat_url(api_url):
    parsed = urlparse(normalize_chat_url(api_url))
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    if not path.endswith("/v1"):
        path = path.rstrip("/")
    models_path = f"{path}/models" if path else "/v1/models"
    return parsed._replace(path=models_path, query="", fragment="").geturl()


def fetch_available_models(api_url, api_key, timeout=5):
    models_url = models_url_from_chat_url(api_url)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.get(models_url, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return [], f"模型列表读取失败：{exc}"

    if response.status_code != 200:
        if response.status_code in {401, 403}:
            return [], "模型列表鉴权失败，请检查 API 密钥是否正确，并确认密钥具有读取模型列表的权限"
        detail = response.text.strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        return [], f"模型列表接口返回 {response.status_code}: {detail}"

    try:
        data = response.json().get("data", [])
    except ValueError as exc:
        return [], f"模型列表不是合法 JSON：{exc}"

    models = [item.get("id") for item in data if isinstance(item, dict) and item.get("id")]
    return models, f"模型列表接口：{models_url}"


def response_detail(response, limit=800):
    text = response.text.strip()
    if not text:
        return "无返回内容"
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def open_path(path):
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def open_file(path):
    target = Path(path)
    if os.name == "nt":
        os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


class FrameHandler(FileSystemEventHandler):
    # FFmpeg 可能直接创建文件，也可能先写临时文件再移动，因此两个事件都要接住。
    def __init__(self, engine):
        self.engine = engine

    def on_created(self, event):
        if not event.is_directory:
            self.engine.enqueue_image(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.engine.enqueue_image(event.dest_path)


def safe_report_asset_name(value, fallback="frame"):
    text = str(value or "").strip() or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text).strip("._ ")
    return (text or fallback)[:80]


def markdown_relative_image_path(markdown_file, image_file):
    try:
        relative = os.path.relpath(Path(image_file), Path(markdown_file).parent)
    except ValueError:
        relative = str(image_file)
    return quote(relative.replace(os.sep, "/"), safe="/%._-()")


def parse_frame_sequence(image_name, fallback=1):
    match = re.search(r"_(\d{1,8})(?:\.[^.]+)?$", str(image_name))
    if match:
        return int(match.group(1))
    return max(1, int_from(fallback, 1, 1))


class AnalysisEngine:
    def __init__(self, config, event_queue=None, record_session=True):
        self.config = config
        self.event_queue = event_queue
        self.record_session = bool(record_session)
        self.session_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
        self.session_started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.session_terminal = False

        # queued_paths 用于防止重复入队，terminal_paths 记录已经得到最终结果的图片。
        self.task_queue = queue.Queue()
        self.queued_paths = set()
        self.terminal_paths = set()
        self.terminal_path_order = deque()
        self.successful_paths = set()
        self.failed_paths = set()
        self.batch_discovered_paths = set()

        # 这些集合和统计值会被监听线程、工作线程及主控线程同时访问。
        self.queued_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.observer = None
        self.worker_threads = []
        self.ffmpeg_process = None
        self.quick_frame_process = None
        self.ssh_process = None
        self.reader_threads = []
        self.current_input_source = None
        self.realtime_source = False

        # 以下状态只属于当前一次 FFmpeg 运行，重连或切换传输方式时会重新设置。
        self.ffmpeg_started_at = None
        self.ffmpeg_restart_attempts = 0
        self.stream_transport_override = None
        self.stream_low_latency_override = None
        self.stream_rtsp_prefer_tcp_override = False
        self.stream_keyframe_only_override = False
        self.ffmpeg_recent_output = []
        self.ffmpeg_noise_count = 0
        self.ffmpeg_output_prefix = ""
        self.stream_first_frame_seen = False
        self.stream_frame_interval = 10
        self.stream_finite_capture = False
        self.stream_last_accepted_at = 0.0
        self.stream_throttled_frames = 0
        self.stream_last_throttle_log = 0.0
        self.rtsp_runtime_candidates = []
        self.rtsp_runtime_index = 0
        self.switching_rtsp_transport = False
        self.ignored_ffmpeg_exit_pids = set()
        self.batch_ffmpeg_finished = False
        self.batch_ffmpeg_failed = False
        self.auto_stop_started = False

        # 重连、自动收尾和结果写入分别加锁，避免无关流程互相阻塞。
        self.restart_lock = threading.Lock()
        self.auto_stop_lock = threading.Lock()
        self.result_lock = threading.Lock()
        self.last_backlog_log = 0
        self.last_stats_emit = 0
        self.running = False
        self.stats = {
            "queued": 0,
            "processing": 0,
            "success": 0,
            "failed": 0,
        }
        results_dir = Path(self.config.get("results_dir") or DEFAULT_RESULTS_DIR)
        results_dir.mkdir(parents=True, exist_ok=True)
        self.result_file = results_dir / f"analysis_{datetime.now():%Y%m%d_%H%M%S}.md"
        self.report_assets_dir = results_dir / f"{self.result_file.stem}_assets"
        self.result_sequence = 0
        self.result_records = []
        self.session_manifest_file = results_dir / f"session_{self.session_id}.json"
        self.update_session_manifest("created")

    def session_source(self):
        source = normalize_input_source(self.current_input_source)
        if not source:
            return None
        value = source["value"]
        if source["type"] == "stream":
            value = masked_stream_url(value)
        return {
            "type": source["type"],
            "value": value,
        }

    def update_session_manifest(self, status, terminal=False, **extra):
        if not self.record_session:
            return
        payload = {
            "schema": "video-stream-analyzer-session-v1",
            "session_id": self.session_id,
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "status": status,
            "started_at": self.session_started_at,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": self.session_source(),
            "result_file": str(self.result_file),
            "report_assets_dir": str(getattr(self, "report_assets_dir", "")),
            "result_records": list(getattr(self, "result_records", []))[-1000:],
            "stats": self.stats_snapshot(),
            "config": redacted_config_copy(self.config),
        }
        payload.update(extra)
        try:
            atomic_write_json(self.session_manifest_file, payload)
            if terminal:
                self.session_terminal = True
        except OSError as exc:
            self.log(f"任务档案写入失败：{exc}", "WARN")

    def emit(self, event_type, payload):
        # 后台线程不直接操作 Tk 控件，只把事件交给 GUI 主线程统一处理。
        if self.event_queue is not None:
            try:
                self.event_queue.put_nowait((event_type, payload))
                return True
            except queue.Full:
                # 日志和统计允许在界面拥塞时降级；结果已先写入磁盘，不会因界面队列满而丢失。
                if event_type == "log":
                    write_persistent_log(payload.get("text", ""))
                elif event_type not in {"stats", "result"}:
                    try:
                        self.event_queue.put((event_type, payload), timeout=0.2)
                        return True
                    except queue.Full:
                        pass
                return False
        elif event_type == "log":
            print(payload["text"], flush=True)
        elif event_type == "result":
            print(payload["content"], flush=True)
        return True

    def log(self, message, level="INFO"):
        message = mask_sensitive_text(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"{timestamp} [{level}] {message}"
        if self.event_queue is None:
            write_persistent_log(formatted)
        self.emit(
            "log",
            {
                "level": level,
                "text": formatted,
            },
        )

    def notice(self, title, message, level="warning"):
        self.emit(
            "notice",
            {
                "title": str(title or "提示"),
                "message": mask_sensitive_text(message),
                "level": level,
            },
        )

    def emit_stats(self, force=False):
        # 高频抽帧时限制统计刷新频率，避免重复状态塞满界面事件队列。
        now = time.time()
        if (
            not force
            and self.event_queue is not None
            and now - self.last_stats_emit < 0.15
        ):
            return
        self.last_stats_emit = now
        with self.stats_lock:
            snapshot = self.stats.copy()
        self.emit("stats", snapshot)

    def stats_snapshot(self):
        with self.stats_lock:
            return self.stats.copy()

    def update_stat(self, key, delta):
        with self.stats_lock:
            self.stats[key] = max(0, self.stats.get(key, 0) + delta)
        self.emit_stats()

    def start_monitoring(self, skip_preflight=False):
        if self.running:
            message = "监听已经在运行。请先停止当前任务，再重新启动。"
            self.log(message)
            self.notice("监听已在运行", message, "warning")
            return True

        image_dir = Path(self.config["image_dir"])
        image_dir.mkdir(parents=True, exist_ok=True)
        Path(self.config.get("results_dir") or DEFAULT_RESULTS_DIR).mkdir(
            parents=True,
            exist_ok=True,
        )
        self.stop_event.clear()
        if not skip_preflight and not self.prepare_runtime():
            self.update_session_manifest("start_failed", terminal=True)
            return False

        self.worker_threads = []
        concurrency = int_from(self.config.get("concurrency"), 1, 1, 8)

        # 先启动消费者，再启动目录监听，确保最早生成的图片也能及时处理。
        for index in range(concurrency):
            thread = threading.Thread(
                target=self.worker_loop,
                name=f"llm-worker-{index + 1}",
                daemon=True,
            )
            thread.start()
            self.worker_threads.append(thread)

        self.observer = Observer()
        self.observer.schedule(FrameHandler(self), str(image_dir), recursive=False)
        self.observer.start()
        self.running = True
        self.update_session_manifest("running")

        self.log(f"已启动监听：{image_dir}")
        if self.config.get("process_existing"):
            self.enqueue_existing_images(image_dir)
        self.emit_stats(force=True)
        return True

    def prepare_runtime(self, input_source=None):
        # 按输入源、本机工具、视频首帧、模型服务的顺序检查，失败时尽早退出。
        source = normalize_input_source(input_source)
        if source:
            if source["type"] == "file":
                video_path = Path(source["value"])
                if not video_path.exists():
                    message = f"视频文件不存在：{video_path}"
                    self.log(message, "ERROR")
                    self.notice("视频文件不可用", message, "error")
                    self.emit("state", {"text": "启动失败"})
                    return False
            elif source["type"] == "stream":
                ok, message = validate_stream_url(source["value"])
                if not ok:
                    self.log(message, "ERROR")
                    self.notice("视频流地址不可用", message, "error")
                    self.emit("state", {"text": "启动失败"})
                    return False
            if not find_tool("ffmpeg"):
                message = "找不到 FFmpeg，请确认发布包里存在 tools\\ffmpeg.exe"
                self.log(message, "ERROR")
                self.notice("缺少 FFmpeg", message, "error")
                self.emit("state", {"text": "启动失败"})
                return False
            if source["type"] == "stream" and not self.probe_stream_before_start(source["value"]):
                self.notice("视频流不可用", "启动前验证未能读取到实时视频画面，请检查流地址、网络、权限或摄像头/平台状态。", "error")
                self.emit("state", {"text": "视频流不可用"})
                return False

        ok, api_url_or_message = validate_api_url(
            self.config.get("api_url", ""),
            self.config.get("connection_mode", "public"),
        )
        if not ok:
            self.log(api_url_or_message, "ERROR")
            self.notice("接口地址不可用", api_url_or_message, "error")
            self.emit("state", {"text": "启动失败"})
            return False
        api_url = api_url_or_message
        self.config["api_url"] = api_url

        connection_mode = self.config.get("connection_mode")
        if connection_mode == "private_ssh":
            self.log("正在准备私有化部署连接：先启动 SSH 隧道")
            if not self.start_ssh_tunnel():
                message = "SSH 隧道启动失败，已取消本次任务。请检查跳板机地址、端口、用户名、私钥和网络。"
                self.log(message, "ERROR")
                self.notice("SSH 隧道启动失败", message, "error")
                self.emit("state", {"text": "启动失败"})
                return False
            ok, message = wait_for_api_ready(api_url, timeout=20)
            if not ok:
                detail = f"SSH 已尝试启动，但本机接口仍不可用：{message}"
                self.log(detail, "ERROR")
                self.notice("私有化模型接口不可用", detail, "error")
                self.stop_process(self.ssh_process, "SSH 隧道")
                self.ssh_process = None
                self.emit("state", {"text": "启动失败"})
                return False
            self.log(f"本机模型接口已就绪：{message}")
        else:
            if connection_mode == "private_direct":
                self.log("正在检查私有化直连模型接口")
            else:
                self.log("正在检查公网模型接口")
            ok, message = wait_for_api_ready(api_url, timeout=6)
            if not ok:
                label = "私有化直连模型接口" if connection_mode == "private_direct" else "公网模型接口"
                detail = f"{label}不可用：{message}"
                self.log(detail, "ERROR")
                self.notice(f"{label}不可用", detail, "error")
                self.emit("state", {"text": "启动失败"})
                return False
            label = "私有化直连模型接口" if connection_mode == "private_direct" else "公网模型接口"
            self.log(f"{label}可连接：{message}")

        models_ok = self.sync_model_with_server()
        if self.config.get("connection_mode") == "private_ssh" and not models_ok:
            message = "私有化模型列表读取失败，已取消本次任务。请检查接口路径、远端服务端口和模型服务状态。"
            self.log(message, "ERROR")
            self.notice("模型列表读取失败", message, "error")
            self.stop_process(self.ssh_process, "SSH 隧道")
            self.ssh_process = None
            self.emit("state", {"text": "启动失败"})
            return False
        return True

    def build_ffmpeg_input_options(
        self,
        source_type,
        source_value,
        low_latency=None,
        rtsp_transport=None,
        rtsp_prefer_tcp=None,
        keyframe_only=None,
    ):
        # 各协议需要的输入参数不同，不能把 RTSP 传输选项混到本地文件或其他流协议。
        if source_type != "stream":
            if bool(self.config.get("ffmpeg_low_cpu", True)):
                return ["-readrate", "2.5"]
            return []

        scheme = urlparse(source_value).scheme.lower()
        hls_stream = is_hls_stream_url(source_value)
        if low_latency is None:
            low_latency = (
                bool(self.stream_low_latency_override)
                if self.stream_low_latency_override is not None
                else bool(self.config.get("stream_low_latency", True))
            )
        if hls_stream:
            low_latency = False
        open_timeout = int_from(self.config.get("stream_open_timeout"), 30, 3, 180)
        if scheme in {"rtsp", "rtsps"}:
            first_frame_timeout = int_from(self.config.get("stream_first_frame_timeout"), 60, 15, 300)
            open_timeout = max(open_timeout, 30, first_frame_timeout)
        open_timeout_us = open_timeout * 1000000
        open_timeout_ms = open_timeout * 1000

        input_options = []
        use_keyframe_only = (
            bool(keyframe_only)
            if keyframe_only is not None
            else bool(self.stream_keyframe_only_override)
        )
        if use_keyframe_only:
            input_options.extend(["-skip_frame", "nokey"])
        if hls_stream:
            input_options.extend(
                [
                    "-readrate",
                    "1",
                    "-timeout",
                    str(open_timeout_us),
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_on_network_error",
                    "1",
                    "-reconnect_on_http_error",
                    "5xx",
                    "-reconnect_delay_max",
                    "5",
                ]
            )
        elif scheme in {"http", "https", "tcp"}:
            input_options.extend(["-timeout", str(open_timeout_us)])
        elif scheme == "srt":
            input_options.extend(["-connect_timeout", str(open_timeout_ms)])
        elif scheme == "udp":
            input_options.extend(["-timeout", str(open_timeout_us), "-overrun_nonfatal", "1"])
        input_options.extend(["-err_detect", "ignore_err"])
        if low_latency:
            input_options.extend(
                [
                    "-fflags",
                    "+discardcorrupt+nobuffer",
                    "-flags",
                    "low_delay",
                    "-probesize",
                    "1000000",
                    "-analyzeduration",
                    "1000000",
                ]
            )
        elif source_type == "stream":
            input_options.extend(
                [
                    "-fflags",
                    "+genpts+discardcorrupt",
                    "-probesize",
                    "5000000",
                    "-analyzeduration",
                    "5000000",
                ]
            )
        if scheme in {"rtsp", "rtsps"}:
            transport = rtsp_transport or self.stream_transport_override or "tcp"
            if scheme == "rtsps":
                transport = "tcp"
            prefer_tcp = (
                bool(rtsp_prefer_tcp)
                if rtsp_prefer_tcp is not None
                else bool(self.stream_rtsp_prefer_tcp_override)
            )
            if prefer_tcp:
                input_options.extend(["-rtsp_flags", "prefer_tcp"])
            input_options.extend(
                [
                    "-rtsp_transport",
                    transport,
                    "-timeout",
                    str(open_timeout_us),
                    "-max_delay",
                    "500000",
                    "-allowed_media_types",
                    "video",
                ]
            )
        elif scheme in {"rtmp", "rtmps"}:
            input_options.extend(["-rtmp_live", "live"])
        elif scheme in {"http", "https"} and not hls_stream:
            input_options.extend(
                [
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_at_eof",
                    "1",
                    "-reconnect_delay_max",
                    "5",
                ]
            )
        return input_options

    def rtsp_transport_mode(self):
        mode = str(self.config.get("rtsp_transport_mode", "auto") or "auto").strip().lower()
        if mode not in {"auto", "tcp", "udp"}:
            return "auto"
        return mode

    def add_rtsp_candidate(
        self,
        candidates,
        low_latency,
        transport,
        label,
        prefer_tcp=False,
        keyframe_only=False,
    ):
        item = {
            "low_latency": bool(low_latency),
            "transport": transport,
            "label": label,
            "prefer_tcp": bool(prefer_tcp),
            "keyframe_only": bool(keyframe_only),
        }
        key = (
            item["low_latency"],
            item["transport"],
            item["prefer_tcp"],
            item["keyframe_only"],
        )
        if key not in {
            (
                candidate["low_latency"],
                candidate["transport"],
                candidate.get("prefer_tcp", False),
                candidate.get("keyframe_only", False),
            )
            for candidate in candidates
        }:
            candidates.append(item)

    def prepare_rtsp_runtime_plan(self, source):
        # 自动模式按稳定性优先生成候选：先 TCP，再 UDP；固定模式只保留指定项。
        self.rtsp_runtime_candidates = []
        self.rtsp_runtime_index = 0
        source = normalize_input_source(source)
        if not source or source["type"] != "stream":
            return
        source_value = source["value"]
        scheme = urlparse(source_value).scheme.lower()
        if scheme not in {"rtsp", "rtsps"}:
            return

        mode = self.rtsp_transport_mode()
        if scheme == "rtsps":
            mode = "tcp"
        configured_low_latency = bool(self.config.get("stream_low_latency", True))
        current_low_latency = (
            bool(self.stream_low_latency_override)
            if self.stream_low_latency_override is not None
            else configured_low_latency
        )
        current_transport = self.stream_transport_override
        if current_transport not in {"tcp", "udp", "http"}:
            current_transport = "tcp" if mode in {"auto", "tcp"} else "udp"

        candidates = []
        has_runtime_override = (
            self.stream_low_latency_override is not None
            or self.stream_transport_override is not None
            or self.stream_rtsp_prefer_tcp_override
            or self.stream_keyframe_only_override
        )
        if has_runtime_override:
            self.add_rtsp_candidate(
                candidates,
                current_low_latency,
                current_transport,
                f"RTSP {current_transport.upper()}{'低延迟' if current_low_latency else '兼容'}",
                prefer_tcp=self.stream_rtsp_prefer_tcp_override,
                keyframe_only=self.stream_keyframe_only_override,
            )
        if mode == "auto":
            self.add_rtsp_candidate(candidates, False, "tcp", "RTSP TCP稳定")
            self.add_rtsp_candidate(candidates, False, "udp", "RTSP UDP稳定")
            self.add_rtsp_candidate(candidates, False, "http", "RTSP HTTP隧道")
            self.add_rtsp_candidate(candidates, False, "tcp", "RTSP TCP关键帧救援", keyframe_only=True)
            if configured_low_latency:
                self.add_rtsp_candidate(candidates, True, "tcp", "RTSP TCP低延迟")
        elif mode == "tcp":
            self.add_rtsp_candidate(candidates, False, "tcp", "RTSP TCP稳定")
            self.add_rtsp_candidate(candidates, False, "tcp", "RTSP TCP关键帧救援", keyframe_only=True)
            if configured_low_latency:
                self.add_rtsp_candidate(candidates, True, "tcp", "RTSP TCP低延迟")
        elif mode == "udp":
            self.add_rtsp_candidate(candidates, False, "udp", "RTSP UDP稳定")
            self.add_rtsp_candidate(candidates, False, "udp", "RTSP UDP关键帧救援", keyframe_only=True)

        self.rtsp_runtime_candidates = candidates
        if candidates:
            self.apply_rtsp_runtime_candidate(0)

    def apply_rtsp_runtime_candidate(self, index):
        if not self.rtsp_runtime_candidates:
            return False
        index = max(0, min(index, len(self.rtsp_runtime_candidates) - 1))
        candidate = self.rtsp_runtime_candidates[index]
        self.rtsp_runtime_index = index
        self.stream_low_latency_override = candidate["low_latency"]
        self.stream_transport_override = candidate["transport"]
        self.stream_rtsp_prefer_tcp_override = candidate.get("prefer_tcp", False)
        self.stream_keyframe_only_override = candidate.get("keyframe_only", False)
        return True

    def try_next_rtsp_runtime_candidate(self, source_value, reason):
        # 切换候选时复用当前任务和监听目录，只重启 FFmpeg 的输入链路。
        if urlparse(source_value or "").scheme.lower() not in {"rtsp", "rtsps"}:
            return False
        if not self.rtsp_runtime_candidates:
            return False
        next_index = self.rtsp_runtime_index + 1
        if next_index >= len(self.rtsp_runtime_candidates):
            return False

        self.apply_rtsp_runtime_candidate(next_index)
        candidate = self.rtsp_runtime_candidates[self.rtsp_runtime_index]
        self.log(
            f"{reason}，自动切换到 {candidate['label']} 重新拉流。",
            "WARN",
        )

        def restart_worker():
            self.switching_rtsp_transport = True
            try:
                old_process = self.ffmpeg_process
                if old_process is not None:
                    self.ignored_ffmpeg_exit_pids.add(old_process.pid)
                self.stop_process(old_process, "FFmpeg")
                if self.ffmpeg_process is old_process:
                    self.ffmpeg_process = None
            finally:
                self.switching_rtsp_transport = False
            if self.stop_event.is_set() or not self.running:
                return
            source = normalize_input_source(self.current_input_source)
            if source and source["type"] == "stream":
                self.start_ffmpeg(source)
                self.emit("state", {"text": "运行中"})

        thread = threading.Thread(target=restart_worker, name="rtsp-transport-switch", daemon=True)
        thread.start()
        self.reader_threads.append(thread)
        return True

    def run_stream_probe_attempt(
        self,
        ffmpeg,
        source_value,
        low_latency,
        rtsp_transport,
        timeout,
        rtsp_prefer_tcp=False,
        keyframe_only=False,
    ):
        # 探测只读取一帧并设置硬超时，避免无响应的摄像头卡住整个启动流程。
        with tempfile.TemporaryDirectory(prefix="stream_probe_") as temp_dir:
            probe_path = Path(temp_dir) / "probe.jpg"
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                *self.build_ffmpeg_input_options(
                    "stream",
                    source_value,
                    low_latency=low_latency,
                    rtsp_transport=rtsp_transport,
                    rtsp_prefer_tcp=rtsp_prefer_tcp,
                    keyframe_only=keyframe_only,
                ),
                "-i",
                source_value,
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-an",
                "-q:v",
                "5",
                str(probe_path),
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = None
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(APP_DIR),
                    creationflags=creationflags,
                )
                output, _ = process.communicate(timeout=timeout + 5)
            except subprocess.TimeoutExpired as exc:
                output = (exc.stdout or "") + "\n" + (exc.stderr or "")
                kill_process_tree(process)
                return False, f"读取 1 帧超时（超过 {timeout} 秒）。{output}".strip()
            except OSError as exc:
                return False, f"FFmpeg 探测启动失败：{exc}"

            output = output or ""
            if probe_path.exists() and probe_path.stat().st_size > 0:
                return True, output
            if process.returncode == 0:
                return False, output or "FFmpeg 已退出但没有生成探测图片"
            return False, output or f"FFmpeg 探测失败，退出码 {format_exit_code(process.returncode)}"

    def probe_stream_before_start(self, source_value):
        # RTSP 自动模式逐个验证候选，首个能产生画面的组合会沿用到正式拉流。
        if not bool(self.config.get("stream_probe_before_start", True)):
            return True

        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            message = "找不到 FFmpeg，请确认发布包里存在 tools\\ffmpeg.exe"
            self.log(message, "ERROR")
            self.notice("缺少 FFmpeg", message, "error")
            return False

        timeout = int_from(self.config.get("stream_probe_timeout"), 12, 3, 60)
        scheme = urlparse(source_value).scheme.lower()
        configured_low_latency = bool(self.config.get("stream_low_latency", True))
        candidates = []

        def add_candidate(label, low_latency, transport=None, prefer_tcp=False, keyframe_only=False):
            item = (label, bool(low_latency), transport, bool(prefer_tcp), bool(keyframe_only))
            if item not in candidates:
                candidates.append(item)

        if scheme in {"rtsp", "rtsps"}:
            port_ok, port_message = rtsp_control_port_is_reachable(
                source_value,
                timeout=min(6, max(3, timeout // 3)),
            )
            if not port_ok:
                message = (
                    f"RTSP 地址无法连接：{port_message}。"
                    "本机当前网络到摄像头/国标平台的 RTSP 端口不通，软件无法抽帧。"
                    "请确认地址、端口、账号权限、VPN/内网、路由和防火墙；"
                    "如果这是公网测试地址，可能该摄像头已经关闭或禁止外网访问。"
                )
                self.log(message, "ERROR")
                self.notice("RTSP 端口不可连接", message, "error")
                return False
            self.log(f"RTSP 控制端口可连接：{port_message}")

        if scheme in {"rtsp", "rtsps"}:
            add_candidate("RTSP TCP 稳定", False, "tcp")
            if scheme == "rtsp":
                add_candidate("RTSP UDP 稳定", False, "udp")
                add_candidate("RTSP HTTP 隧道", False, "http")
            add_candidate("RTSP TCP 关键帧救援", False, "tcp", keyframe_only=True)
            if configured_low_latency:
                add_candidate("RTSP TCP 低延迟", True, "tcp")
        else:
            add_candidate("低延迟模式", configured_low_latency, None)
            if configured_low_latency:
                add_candidate("兼容模式", False, None)

        self.log(f"正在验证实时视频流：尝试读取 1 帧，最长 {timeout} 秒")
        last_output = ""
        probe_outputs = []
        for label, low_latency, transport, prefer_tcp, keyframe_only in candidates:
            ok, output = self.run_stream_probe_attempt(
                ffmpeg,
                source_value,
                low_latency,
                transport,
                timeout,
                rtsp_prefer_tcp=prefer_tcp,
                keyframe_only=keyframe_only,
            )
            if ok:
                self.stream_low_latency_override = low_latency
                self.stream_transport_override = transport
                self.stream_rtsp_prefer_tcp_override = prefer_tcp
                self.stream_keyframe_only_override = keyframe_only
                detail = label
                if scheme in {"rtsp", "rtsps"} and transport:
                    detail = f"{label}（传输：{transport.upper()}）"
                self.log(f"实时视频流验证通过：已读取到 1 帧，后续使用 {detail}")
                return True
            last_output = output
            probe_outputs.append(output)
            self.log(f"实时视频流验证失败：{label} 未读取到画面", "WARN")

        if scheme in {"rtsp", "rtsps"} and probe_outputs and all(stream_probe_timed_out(output) for output in probe_outputs):
            self.stream_low_latency_override = False
            self.stream_transport_override = "tcp"
            self.log(
                "RTSP 启动前探测均为超时，没有收到明确的鉴权/路径/协议错误。"
                "部分摄像头首帧慢或关键帧间隔长，软件将进入正式持续抽帧，并监控首帧是否生成。",
                "WARN",
            )
            return True

        hint = ffmpeg_stream_error_hint(last_output, source_value)
        detail = compact_ffmpeg_output(last_output)
        if detail:
            message = f"实时视频流不可用：{hint}。FFmpeg 输出：{detail}"
        else:
            message = f"实时视频流不可用：{hint}"
        self.log(message, "ERROR")
        self.notice("实时视频流不可用", message, "error")
        return False

    def sync_model_with_server(self):
        # 接口可达不代表历史模型仍可用，启动前读取列表并纠正失效选择。
        models, message = fetch_available_models(
            self.config["api_url"],
            self.config.get("api_key", ""),
        )
        if not models:
            self.log(message, "WARN")
            return False

        current_model = self.config.get("model", "")
        best_model = choose_best_model(models, current_model)
        if current_model == best_model:
            self.log(f"模型可用：{current_model}")
            return True

        replacement = best_model
        self.config["model"] = replacement
        self.log(
            f"模型名 {current_model or '空'} 不适合或不在服务端列表，已自动改用：{replacement}",
            "WARN",
        )
        try:
            saved = load_config()
            saved["model"] = replacement
            save_config(saved)
        except OSError as exc:
            self.log(f"保存模型配置失败：{exc}", "WARN")
        return True

    def start_all(self, input_source=None):
        # 完整任务先准备监听和工作线程，再启动 FFmpeg，避免首张图片漏处理。
        source = runtime_input_source(input_source, self.config)
        self.current_input_source = source
        self.realtime_source = bool(source and source["type"] == "stream")
        self.update_session_manifest("preparing")
        self.stream_frame_interval = int_from(self.config.get("frame_interval"), 10, 1, 3600)
        self.stream_finite_capture = False
        self.stream_last_accepted_at = 0.0
        self.stream_throttled_frames = 0
        self.stream_last_throttle_log = 0.0
        self.ffmpeg_restart_attempts = 0
        self.stream_transport_override = None
        self.stream_low_latency_override = None
        self.stream_rtsp_prefer_tcp_override = False
        self.stream_keyframe_only_override = False
        self.batch_ffmpeg_finished = False
        self.batch_ffmpeg_failed = False
        self.auto_stop_started = False
        with self.queued_lock:
            self.queued_paths.clear()
            self.terminal_paths.clear()
            self.terminal_path_order.clear()
            self.successful_paths.clear()
            self.failed_paths.clear()
            self.batch_discovered_paths.clear()
        if not self.prepare_runtime(source):
            self.update_session_manifest("start_failed", terminal=True)
            self.current_input_source = None
            self.realtime_source = False
            return
        self.prepare_rtsp_runtime_plan(source)

        if not self.start_monitoring(skip_preflight=True):
            self.emit("state", {"text": "启动失败"})
            return

        if source:
            if not self.start_ffmpeg(source):
                self.emit("state", {"text": "抽帧启动失败"})
                self.stop()

    def stop(self, cleanup_unprocessed=False):
        # 先阻断新的图片来源，再停止消费者，最后按当前批次范围清理残留。
        self.log("正在停止...")
        self.stop_event.set()

        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None

        self.stop_process(self.ffmpeg_process, "FFmpeg")
        self.ffmpeg_process = None

        self.stop_process(self.quick_frame_process, "快速首帧")
        self.quick_frame_process = None

        self.stop_process(self.ssh_process, "SSH 隧道")
        self.ssh_process = None

        self.clear_pending_tasks()

        for thread in self.worker_threads:
            thread.join(timeout=2)
        self.worker_threads = []
        if cleanup_unprocessed:
            self.cleanup_current_batch_unprocessed_images()
        self.running = False
        self.current_input_source = None
        self.realtime_source = False
        self.ffmpeg_started_at = None
        self.ffmpeg_output_prefix = ""
        self.stream_first_frame_seen = False
        self.stream_finite_capture = False
        self.stream_last_accepted_at = 0.0
        self.stream_throttled_frames = 0
        self.stream_last_throttle_log = 0.0
        self.rtsp_runtime_candidates = []
        self.rtsp_runtime_index = 0
        self.switching_rtsp_transport = False
        self.stream_transport_override = None
        self.stream_low_latency_override = None
        self.stream_rtsp_prefer_tcp_override = False
        self.stream_keyframe_only_override = False
        self.ignored_ffmpeg_exit_pids.clear()
        if not self.session_terminal:
            self.update_session_manifest("stopped", terminal=True)
        self.log("已停止")
        self.emit_stats(force=True)

    def current_generated_batch_paths(self):
        # 每次 FFmpeg 运行使用独立文件名前缀，历史图片不会进入本批次清理范围。
        if not self.ffmpeg_output_prefix:
            return []
        image_dir = Path(self.config["image_dir"])
        if not image_dir.exists():
            return []
        paths = set(image_dir.glob(f"{self.ffmpeg_output_prefix}_*"))
        paths.update(image_dir.glob(f"quick_{self.ffmpeg_output_prefix}_*"))
        return sorted(
            path
            for path in paths
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )

    def cleanup_current_batch_unprocessed_images(self):
        # terminal_paths 中的图片已有成功或失败结果；这里只处理尚未终结的当前批次文件。
        if not bool(self.config.get("delete_processed", True)):
            remaining = self.current_generated_batch_paths()
            if remaining:
                self.log(
                    f"手动停止：当前未启用“完成后删除图片”，"
                    f"已按设置保留本批次图片 {len(remaining)} 张。"
                )
            return 0

        deleted = 0
        failed = []
        for path in self.current_generated_batch_paths():
            last_error = None
            for attempt in range(1, 9):
                if not path.exists():
                    deleted += 1
                    last_error = None
                    break
                try:
                    path.unlink()
                    deleted += 1
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    if attempt < 8:
                        time.sleep(min(0.12 * attempt, 0.6))
            if last_error is not None and path.exists():
                failed.append((path.name, str(last_error)))

        if deleted:
            self.log(f"手动停止已删除当前抽帧批次剩余图片：{deleted} 张")
        if failed:
            names = "、".join(name for name, _error in failed[:5])
            self.log(
                f"手动停止后仍有 {len(failed)} 张当前批次图片删除失败："
                f"{names}{' 等' if len(failed) > 5 else ''}",
                "ERROR",
            )
        return deleted

    def clear_pending_tasks(self):
        # Queue 没有批量清空接口，逐项取出时还要同步 unfinished_tasks 计数。
        cleared = 0
        while True:
            try:
                self.task_queue.get_nowait()
            except queue.Empty:
                break
            cleared += 1
            self.task_queue.task_done()
        with self.queued_lock:
            self.queued_paths.clear()
        if cleared:
            with self.stats_lock:
                self.stats["queued"] = 0
            self.log(f"已清理未处理排队任务：{cleared} 张")

    def stop_process(self, process, name):
        if process is None or process.poll() is not None:
            return
        self.log(f"正在关闭{name}...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_process_tree(process)

    def enqueue_existing_images(self, image_dir):
        # 启动监听时补扫目录，主要用于恢复上次未处理完的图片。
        count = 0
        for path in sorted(image_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                if self.enqueue_image(str(path), quiet=True):
                    count += 1
        if count:
            self.log(f"已加入已有图片：{count} 张")

    def should_drop_realtime_backlog(self):
        return self.realtime_source and bool(self.config.get("stream_drop_stale_frames", False))

    def trim_realtime_backlog(self):
        # 仅低延迟实时流允许丢弃旧帧，本地视频和完整留痕模式不会进入这里。
        if not self.should_drop_realtime_backlog():
            return 0

        max_pending = int_from(self.config.get("stream_max_pending_frames"), 3, 1, 50)
        dropped = []
        while self.task_queue.qsize() >= max_pending:
            try:
                stale_path = self.task_queue.get_nowait()
            except queue.Empty:
                break
            dropped.append(stale_path)
            self.task_queue.task_done()

        if not dropped:
            return 0

        with self.queued_lock:
            for stale_path in dropped:
                self.queued_paths.discard(stale_path)

        self.update_stat("queued", -len(dropped))
        for stale_path in dropped:
            try:
                Path(stale_path).unlink(missing_ok=True)
            except OSError:
                pass

        now = time.time()
        if now - self.last_backlog_log > 8:
            self.log(
                f"实时流分析队列已满，已丢弃旧帧 {len(dropped)} 张，优先分析最新画面。",
                "WARN",
            )
            self.last_backlog_log = now
        return len(dropped)

    def warn_realtime_backlog_if_needed(self):
        if not self.realtime_source:
            return
        threshold = max(10, int_from(self.config.get("stream_max_pending_frames"), 3, 1, 50))
        pending = self.task_queue.qsize()
        if pending < threshold:
            return
        now = time.time()
        if now - self.last_backlog_log <= 10:
            return
        self.last_backlog_log = now
        self.log(
            f"实时流分析队列积压 {pending} 张。软件会保留并继续分析已抽到的帧，"
            "不会自动丢帧；如需降低积压，请增大抽帧秒数、提高服务器处理能力或调高并发。",
            "WARN",
        )

    def is_current_realtime_frame(self, image_path):
        if not self.realtime_source or not self.ffmpeg_output_prefix:
            return False
        name = Path(image_path).name
        return name.startswith(self.ffmpeg_output_prefix) or name.startswith(
            f"quick_{self.ffmpeg_output_prefix}_"
        )

    def is_current_file_batch_frame(self, image_path):
        source = normalize_input_source(self.current_input_source)
        if not source or source["type"] != "file" or not self.ffmpeg_output_prefix:
            return False
        path = Path(image_path)
        try:
            expected_dir = Path(self.config["image_dir"]).resolve()
            actual_dir = path.resolve().parent
        except OSError:
            return False
        return (
            actual_dir == expected_dir
            and path.suffix.lower() in IMAGE_EXTENSIONS
            and path.name.startswith(f"{self.ffmpeg_output_prefix}_")
        )

    def should_enforce_realtime_interval(self, image_path):
        if not self.realtime_source or not self.is_current_realtime_frame(image_path):
            return False
        mode = capture_mode_value(self.config.get("capture_mode", "interval"))
        return mode in {"interval", "range"}

    def delete_unscheduled_realtime_frame(self, path, attempts=6):
        # 实时流可能因解码恢复一次性落盘多张旧帧；被节流拒绝的图片不进入分析队列。
        path = Path(path)
        for attempt in range(1, max(1, attempts) + 1):
            if not path.exists():
                return True
            try:
                path.unlink()
                return True
            except OSError:
                time.sleep(min(0.08 * attempt, 0.4))
        return not path.exists()

    def current_file_batch_paths(self):
        source = normalize_input_source(self.current_input_source)
        if not source or source["type"] != "file" or not self.ffmpeg_output_prefix:
            return []
        return self.current_generated_batch_paths()

    def reconcile_file_batch_frames(self):
        # FFmpeg 退出后再扫一次目录，补上进程收尾瞬间可能漏掉的文件事件。
        paths = self.current_file_batch_paths()
        added = 0
        for path in paths:
            if self.enqueue_image(str(path), quiet=True):
                added += 1
        if added:
            self.log(
                f"本地视频收尾对账：补充发现并加入队列 {added} 张图片，"
                "避免文件监听事件延迟造成漏分析。"
            )
        signature_items = []
        for path in paths:
            try:
                signature_items.append((str(path.resolve()), path.stat().st_size))
            except OSError:
                continue
        signature = tuple(signature_items)
        return added, signature

    def reconcile_stream_finite_batch_frames(self):
        paths = [
            path
            for path in self.current_generated_batch_paths()
            if self.is_current_realtime_frame(path)
        ]
        added = 0
        for path in paths:
            if self.enqueue_image(str(path), quiet=True):
                added += 1
        if added:
            self.log(
                f"实时流定时抽帧收尾对账：补充发现并加入队列 {added} 张图片，"
                "避免文件监听事件延迟造成漏分析。"
            )
        signature_items = []
        for path in paths:
            try:
                signature_items.append((str(path.resolve()), path.stat().st_size))
            except OSError:
                continue
        return added, tuple(signature_items)

    def enqueue_image(self, image_path, quiet=False):
        # 去重判断和集合写入必须处于同一临界区，否则 created/moved 事件会重复入队。
        path = str(Path(image_path).resolve())
        if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
            return False

        throttled = False
        throttled_count = 0
        should_log_throttle = False
        with self.queued_lock:
            if path in self.queued_paths or path in self.terminal_paths:
                return False
            if self.should_enforce_realtime_interval(path):
                now = time.monotonic()
                interval = max(1, int_from(self.stream_frame_interval, 10, 1, 3600))
                tolerance = min(0.25, interval * 0.1)
                if (
                    self.stream_last_accepted_at > 0
                    and now - self.stream_last_accepted_at < interval - tolerance
                ):
                    throttled = True
                    self.stream_throttled_frames += 1
                    throttled_count = self.stream_throttled_frames
                    if throttled_count == 1 or now - self.stream_last_throttle_log >= 5:
                        should_log_throttle = True
                        self.stream_last_throttle_log = now
                else:
                    self.stream_last_accepted_at = now
            if throttled:
                pass
            else:
                self.queued_paths.add(path)
                if self.is_current_file_batch_frame(path) or (
                    self.stream_finite_capture and self.is_current_realtime_frame(path)
                ):
                    self.batch_discovered_paths.add(path)

        if throttled:
            deleted = self.delete_unscheduled_realtime_frame(path)
            if should_log_throttle:
                self.log(
                    f"实时抽帧节流：已丢弃突发帧 {throttled_count} 张，"
                    f"继续按 {self.stream_frame_interval} 秒规则接收图片。"
                    f"{'' if deleted else ' 个别突发帧稍后会在停止清理中处理。'}",
                    "INFO",
                )
            return False

        self.trim_realtime_backlog()
        self.task_queue.put(path)
        self.update_stat("queued", 1)
        self.warn_realtime_backlog_if_needed()
        if (
            self.realtime_source
            and not self.stream_first_frame_seen
            and self.ffmpeg_output_prefix
            and Path(path).name.startswith(self.ffmpeg_output_prefix)
        ):
            self.stream_first_frame_seen = True
            self.log(f"实时视频流已收到首帧：{Path(path).name}")
        if not quiet:
            self.log(f"新图片入队：{Path(path).name}，当前排队 {self.task_queue.qsize()} 张")
        return True

    def worker_loop(self):
        # 每个工作线程独立消费图片，停止事件负责让空闲线程及时退出。
        http_session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=1,
            pool_maxsize=1,
            max_retries=0,
            pool_block=True,
        )
        http_session.mount("http://", adapter)
        http_session.mount("https://", adapter)
        try:
            while not self.stop_event.is_set():
                try:
                    image_path = self.task_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                self.update_stat("queued", -1)
                self.update_stat("processing", 1)
                ok = False
                try:
                    try:
                        ok = self.process_image(image_path, http_session=http_session)
                    except Exception as exc:
                        self.log(
                            f"分析线程已拦截意外异常：{Path(image_path).name}，"
                            f"{type(exc).__name__}: {exc}。该线程将继续处理后续图片。",
                            "ERROR",
                        )
                    canceled = self.stop_event.is_set() and not ok
                    if ok:
                        self.update_stat("success", 1)
                    elif canceled:
                        self.log(f"任务停止，已取消当前图片分析：{Path(image_path).name}")
                    else:
                        self.update_stat("failed", 1)
                finally:
                    self.update_stat("processing", -1)
                    with self.queued_lock:
                        self.queued_paths.discard(image_path)
                        if not (self.stop_event.is_set() and not ok):
                            self.remember_terminal_path(image_path, ok)
                    self.task_queue.task_done()
        finally:
            http_session.close()

    def remember_terminal_path(self, image_path, ok):
        self.terminal_paths.add(image_path)
        self.terminal_path_order.append(image_path)
        if ok:
            self.successful_paths.add(image_path)
            self.failed_paths.discard(image_path)
        else:
            self.failed_paths.add(image_path)
        if not self.realtime_source:
            return
        while len(self.terminal_path_order) > REALTIME_TERMINAL_CACHE_LIMIT:
            stale_path = self.terminal_path_order.popleft()
            self.terminal_paths.discard(stale_path)
            self.successful_paths.discard(stale_path)
            self.failed_paths.discard(stale_path)

    def process_image(self, image_path, http_session=None):
        # 只有模型结果成功写入结果文件后，这张图片才进入成功终态。
        path = Path(image_path)
        if not wait_for_file_ready(path):
            self.log(f"图片尚未写入完成或已损坏：{path.name}", "WARN")
            return False

        self.log(f"开始分析：{path.name}")

        try:
            max_image_size = int_from(self.config.get("max_image_size"), 1080, 128, 4096)
            base64_image = optimize_and_encode_image(path, max_image_size)
        except Exception as exc:
            self.log(f"图片处理失败：{path.name}，{exc}", "ERROR")
            return False

        payload = build_payload(self.config, base64_image)
        headers = {"Content-Type": "application/json"}
        api_key = (self.config.get("api_key") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        max_retries = int_from(self.config.get("max_retries"), 3, 1, 10)
        timeout = int_from(self.config.get("request_timeout"), 60, 5, 600)

        for attempt in range(1, max_retries + 1):
            if self.stop_event.is_set():
                return False

            try:
                client = http_session or requests
                response = client.post(
                    normalize_chat_url(self.config["api_url"]),
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
            except requests.exceptions.RequestException as exc:
                self.log(f"网络错误：{exc}，第 {attempt} 次重试", "WARN")
                if self.stop_event.wait(3):
                    return False
                continue

            if response.status_code == 200:
                if self.stop_event.is_set():
                    return False
                try:
                    result = response.json()
                    content = result["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    self.log(f"接口返回格式异常：{exc}", "ERROR")
                    return False

                result_info = self.record_result(path, content)
                self.emit("result", result_info)
                self.log(f"分析完成：{path.name}")
                if self.config.get("delete_processed"):
                    self.delete_processed_image(path)
                return True

            detail = response_detail(response)
            reason = explain_api_error(response.status_code, detail)
            if response.status_code in {400, 401, 403, 404}:
                self.log(
                    f"API 配置错误：状态码 {response.status_code}。{reason} 返回内容：{detail}",
                    "ERROR",
                )
                return False

            self.log(
                f"API 异常：状态码 {response.status_code}，第 {attempt} 次重试。"
                f"{reason} 返回内容：{detail}",
                "WARN",
            )
            if self.stop_event.wait(min(2 + attempt * 2, 10)):
                return False

        self.log(f"分析失败：{path.name}，已重试 {max_retries} 次", "ERROR")
        return False

    def delete_processed_image(self, path, attempts=8):
        # Windows 文件句柄可能短暂未释放，有限次数退避可避免偶发的删除失败。
        path = Path(path)
        last_error = None
        for attempt in range(1, max(1, attempts) + 1):
            if not path.exists():
                return True
            try:
                path.unlink()
                self.log(f"已删除处理完成的图片：{path.name}")
                return True
            except OSError as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(min(0.12 * attempt, 0.6))
        self.log(
            f"删除图片失败：{path.name}，已重试 {attempts} 次，{last_error}",
            "ERROR",
        )
        return False

    def report_source_name(self):
        source = normalize_input_source(self.current_input_source)
        if not source:
            return "外部图片目录"
        if source["type"] == "file":
            return Path(source["value"]).name
        return masked_stream_url(source["value"])

    def estimate_frame_time_text(self, image_name, sequence):
        source = normalize_input_source(self.current_input_source)
        source_type = source["type"] if source else self.config.get("source_type", "file")
        index = parse_frame_sequence(image_name, sequence)
        mode = capture_mode_value(self.config.get("capture_mode", "interval"))
        interval = int_from(self.config.get("frame_interval"), 10, 1, 3600)
        try:
            if mode == "point":
                seconds = parse_capture_time(self.config.get("capture_point_time"), "抽帧时间点")
            elif mode == "range":
                start = parse_capture_time(self.config.get("capture_start_time"), "抽帧开始时间")
                seconds = start + max(0, index - 1) * interval
            else:
                seconds = max(0, index - 1) * interval
        except ValueError:
            seconds = None
        if seconds is None:
            return "未知"
        prefix = "任务启动后" if source_type == "stream" else "视频时间轴"
        if str(image_name).startswith("quick_"):
            return "实时快速首帧"
        return f"{prefix} {format_capture_seconds(seconds)}"

    def save_report_frame_asset(self, image_path, sequence):
        source = Path(image_path)
        if not source.exists():
            self.log(f"报告图片缺失：{source.name}", "WARN")
            return None, "对应抽帧图片缺失"
        self.report_assets_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = safe_report_asset_name(source.stem)
        target = self.report_assets_dir / f"{self.session_id}_{sequence:04d}_{safe_stem}.jpg"
        try:
            with Image.open(source) as image:
                image = image.convert("RGB")
                image.thumbnail((1280, 960), PIL_LANCZOS)
                image.save(target, "JPEG", quality=88, optimize=True)
        except Exception as exc:
            self.log(f"报告图片保存失败：{source.name}，{exc}", "WARN")
            try:
                shutil.copy2(source, target)
            except OSError as copy_exc:
                self.log(f"报告图片复制失败：{source.name}，{copy_exc}", "ERROR")
                return None, "对应抽帧图片保存失败"
        return target, ""

    def record_result(self, image_path, content):
        # 多个工作线程共享一个 Markdown 文件，写入必须串行，保证结果块不交叉。
        image_path = Path(image_path)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.result_lock:
            self.result_sequence += 1
            sequence = self.result_sequence
            frame_asset, frame_warning = self.save_report_frame_asset(image_path, sequence)
            image_name = image_path.name
            frame_time = self.estimate_frame_time_text(image_name, sequence)
            source_name = self.report_source_name()
            if frame_asset:
                image_markdown = (
                    f"![抽帧图片]({markdown_relative_image_path(self.result_file, frame_asset)})\n\n"
                    f"> 抽帧时间：{frame_time}；视频来源：{source_name}；"
                    f"分析编号：{sequence:04d}；分析时间：{timestamp}\n"
                )
                frame_path = str(frame_asset)
            else:
                image_markdown = (
                    f"> {frame_warning or '对应抽帧图片缺失'}；抽帧时间：{frame_time}；"
                    f"视频来源：{source_name}；分析编号：{sequence:04d}；分析时间：{timestamp}\n"
                )
                frame_path = ""
            section = (
                f"\n\n## 分析 {sequence:04d}  {timestamp}  {image_name}\n\n"
                f"{image_markdown}\n"
                f"{content.strip()}\n"
            )
            if not self.result_file.exists():
                header = (
                    f"# {APP_DISPLAY_NAME} 分析结果\n\n"
                    f"- 开始时间：{timestamp}\n"
                    f"- 连接路线：{connection_mode_name(self.config.get('connection_mode'))}\n"
                    f"- 接口：{mask_sensitive_text(self.config.get('api_url'))}\n"
                    f"- 模型：{self.config.get('model')}\n"
                    f"- 报告图片：每条分析记录下方均保存对应抽帧图片；历史记录如无图片会显示缺失说明。\n"
                )
                self.result_file.write_text(header, encoding="utf-8")
            with self.result_file.open("a", encoding="utf-8") as file:
                file.write(section)
            record = {
                "index": sequence,
                "time": timestamp,
                "image": image_name,
                "frame_image_path": frame_path,
                "frame_time": frame_time,
                "source": source_name,
                "result_file": str(self.result_file),
            }
            self.result_records.append(record)
            self.update_session_manifest("running")

        return {
            "time": timestamp,
            "image": image_name,
            "content": content.strip(),
            "file": str(self.result_file),
            "frame_image_path": frame_path,
            "frame_time": frame_time,
            "index": sequence,
        }

    def should_capture_quick_first_frame(self, source_value):
        if not bool(self.config.get("stream_fast_first_frame", True)):
            return False
        scheme = urlparse(source_value or "").scheme.lower()
        return scheme in {"rtmp", "rtmps", "http", "https", "srt"}

    def start_quick_first_frame_capture(self, source_value, image_dir, prefix, interval):
        # 快速首帧使用独立进程改善启动反馈，不替代后续持续抽帧进程。
        if not self.should_capture_quick_first_frame(source_value):
            return

        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            return

        timeout = max(10, min(30, int_from(interval, 10, 1, 3600) + 10))
        image_dir = Path(image_dir)
        output_name = f"quick_{prefix}_000001.jpg"

        def worker():
            temp_dir = Path(tempfile.mkdtemp(prefix="stream_quick_frame_"))
            temp_path = temp_dir / output_name

            def cleanup_temp():
                shutil.rmtree(temp_dir, ignore_errors=True)

            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                *self.build_ffmpeg_input_options(
                    "stream",
                    source_value,
                    low_latency=False,
                    rtsp_transport=self.stream_transport_override,
                ),
                "-i",
                source_value,
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-an",
                "-q:v",
                "3",
                str(temp_path),
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = None
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(APP_DIR),
                    creationflags=creationflags,
                )
                self.quick_frame_process = process
                process.communicate(timeout=timeout + 5)
            except subprocess.TimeoutExpired:
                kill_process_tree(process)
                cleanup_temp()
                return
            except OSError:
                cleanup_temp()
                return
            finally:
                if self.quick_frame_process is process:
                    self.quick_frame_process = None

            if process is None or process.returncode != 0:
                cleanup_temp()
                return
            if not temp_path.exists() or temp_path.stat().st_size <= 0:
                cleanup_temp()
                return
            if self.stop_event.is_set() or not self.running:
                cleanup_temp()
                return

            dest_path = image_dir / output_name
            try:
                counter = 1
                while dest_path.exists():
                    counter += 1
                    dest_path = image_dir / f"quick_{prefix}_{counter:06d}.jpg"
                shutil.move(str(temp_path), str(dest_path))
            except OSError:
                cleanup_temp()
                return

            if wait_for_file_ready(dest_path, timeout=3) and self.enqueue_image(str(dest_path), quiet=True):
                self.log(f"快速首帧已入队：{dest_path.name}")

            cleanup_temp()

        thread = threading.Thread(target=worker, name="stream-quick-first-frame", daemon=True)
        thread.start()
        self.reader_threads.append(thread)

    def start_ffmpeg(self, input_source):
        # 本地视频按媒体时间轴抽帧，实时流按真实时间节流，两者的参数策略不同。
        if self.ffmpeg_process is not None and self.ffmpeg_process.poll() is None:
            self.log("FFmpeg 已经在运行")
            return True

        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            message = "找不到 ffmpeg，请把 ffmpeg.exe 放到 tools 文件夹或安装到 PATH"
            self.log(message, "ERROR")
            self.notice("缺少 FFmpeg", message, "error")
            return False

        source = normalize_input_source(input_source)
        if not source:
            return

        if source["type"] == "file":
            video_path = Path(source["value"])
            if not video_path.exists():
                message = f"视频文件不存在：{video_path}"
                self.log(message, "ERROR")
                self.notice("视频文件不可用", message, "error")
                return False
            source_value = str(video_path)
        else:
            ok, message = validate_stream_url(source["value"])
            if not ok:
                self.log(message, "ERROR")
                self.notice("视频流地址不可用", message, "error")
                return False
            source_value = source["value"]

        image_dir = Path(self.config["image_dir"])
        image_dir.mkdir(parents=True, exist_ok=True)
        try:
            capture_plan = build_capture_plan(self.config, source["type"])
        except ValueError as exc:
            message = f"抽帧设置错误：{exc}"
            self.log(message, "ERROR")
            self.notice("抽帧设置错误", message, "error")
            return False
        interval = capture_plan["interval"]
        low_cpu = bool(self.config.get("ffmpeg_low_cpu", True))
        low_latency = bool(self.config.get("stream_low_latency", True))
        ffmpeg_threads = int_from(self.config.get("ffmpeg_threads"), 1, 1, 8)
        prefix = datetime.now().strftime("frame_%Y%m%d_%H%M%S")
        self.ffmpeg_output_prefix = prefix
        self.stream_finite_capture = source["type"] == "stream" and bool(capture_plan["finite"])
        output_pattern = str(image_dir / f"{prefix}_%06d.jpg")
        input_options = self.build_ffmpeg_input_options(source["type"], source_value)
        if capture_plan["disable_local_readrate"]:
            input_options = remove_ffmpeg_option_with_value(input_options, "-readrate")
        input_options = [*capture_plan["input_options"], *input_options]

        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-threads",
            str(ffmpeg_threads),
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            *input_options,
            "-i",
            source_value,
            "-map",
            "0:v:0",
            "-an",
        ]
        if capture_plan["video_filter"]:
            command.extend(["-vf", capture_plan["video_filter"]])
        command.extend(capture_plan["output_options"])
        command.extend(
            [
                "-threads",
                str(ffmpeg_threads),
                "-q:v",
                "2",
                output_pattern,
            ]
        )

        if source["type"] == "stream":
            stream_label = describe_stream_url(source_value)
            effective_low_latency = (
                bool(self.stream_low_latency_override)
                if self.stream_low_latency_override is not None
                else low_latency
            )
            latency_text = "低延迟模式" if effective_low_latency else "兼容模式"
            extra_labels = []
            if self.stream_transport_override:
                extra_labels.append(f"RTSP传输 {self.stream_transport_override.upper()}")
            if self.stream_rtsp_prefer_tcp_override:
                extra_labels.append("优先TCP")
            if self.stream_keyframe_only_override:
                extra_labels.append("关键帧救援")
            extra_text = f"，{'，'.join(extra_labels)}" if extra_labels else ""
            self.log(
                f"启动 FFmpeg：{stream_label}，{capture_plan['summary']}，"
                f"完整分析不丢帧，{latency_text}{extra_text}，线程 {ffmpeg_threads}"
            )
        else:
            mode_text = "低CPU模式" if low_cpu else "高速模式"
            self.log(
                f"启动 FFmpeg：本地视频{capture_plan['summary']}，{mode_text}，线程 {ffmpeg_threads}"
            )
        self.ffmpeg_process = self.launch_process(command, "FFmpeg")
        if self.ffmpeg_process is not None:
            self.ffmpeg_started_at = time.time()
            self.ffmpeg_recent_output = []
            self.ffmpeg_noise_count = 0
            if source["type"] == "stream":
                self.stream_first_frame_seen = False
                self.start_stream_first_frame_watchdog(
                    source_value,
                    interval,
                    expected_delay=capture_plan.get("first_frame_wait", 0),
                )
                if not capture_plan["finite"]:
                    self.start_quick_first_frame_capture(source_value, image_dir, prefix, interval)
            self.read_process_output(self.ffmpeg_process, "FFmpeg")
            return True
        return False

    def start_stream_first_frame_watchdog(self, source_value, interval, expected_delay=0):
        # 探测成功后正式进程仍可能卡住，因此再用当前前缀确认首帧确实落盘。
        timeout = int_from(self.config.get("stream_first_frame_timeout"), 60, 15, 300)
        if urlparse(source_value or "").scheme.lower() in {"rtsp", "rtsps"}:
            timeout = max(timeout, 120)
        timeout = max(timeout, min(300, int_from(interval, 10, 1, 3600) + 20))
        timeout += int(math.ceil(max(0.0, float(expected_delay or 0))))
        expected_prefix = self.ffmpeg_output_prefix

        def watcher():
            if self.stop_event.wait(timeout):
                return
            if not self.running or self.stop_event.is_set():
                return
            if self.current_input_source is None or not self.realtime_source:
                return
            if self.stream_first_frame_seen or expected_prefix != self.ffmpeg_output_prefix:
                return
            if self.ffmpeg_process is None or self.ffmpeg_process.poll() is not None:
                return

            if self.try_next_rtsp_runtime_candidate(source_value, f"RTSP 正式拉流 {timeout} 秒仍未收到首帧"):
                return

            recent_output = "\n".join(self.ffmpeg_recent_output)
            if recent_output:
                detail = compact_ffmpeg_output(recent_output)
                hint = ffmpeg_stream_error_hint(recent_output, source_value)
                message = f"实时视频流启动后 {timeout} 秒仍未生成任何抽帧图片：{hint}。最近输出：{detail}"
                self.log(message, "ERROR")
            else:
                message = (
                    f"实时视频流启动后 {timeout} 秒仍未生成任何抽帧图片。"
                    "如果 VLC 能播放，请把“验证超时秒”调大，或确认摄像头主码流/子码流有视频画面；"
                    "如果 VLC 也不能播放，请检查 RTSP 地址、账号密码、端口、网络和摄像头权限。"
                )
                self.log(message, "ERROR")
            self.notice("实时视频流无画面", message, "error")
            self.emit("state", {"text": "抽帧无画面"})
            self.stop_after_runtime_failure("已停止")

        thread = threading.Thread(target=watcher, name="stream-first-frame-watch", daemon=True)
        thread.start()
        self.reader_threads.append(thread)

    def start_ssh_tunnel(self):
        # SSH 只提供端口转发，模型请求仍统一访问本机 localhost 地址。
        if self.ssh_process is not None and self.ssh_process.poll() is None:
            self.log("SSH 隧道已经在运行")
            return True

        ok, message = api_host_is_reachable(self.config["api_url"], timeout=1)
        if ok:
            self.log(f"本机接口已经可连接，复用现有 SSH 隧道或本机服务：{message}")
            return True

        ssh = find_tool("ssh")
        if not ssh:
            self.log("找不到 ssh，请安装或启用 Windows OpenSSH", "ERROR")
            return False

        parts = build_ssh_tunnel_parts(self.config, ssh)
        if not parts:
            self.log("SSH 隧道信息不完整，请填写 SSH服务器、用户名、模型服务地址和端口", "ERROR")
            return False

        self.log(
            "启动 SSH 隧道："
            f"localhost:{self.config.get('ssh_local_port')} -> "
            f"{self.config.get('ssh_remote_host')}:{self.config.get('ssh_remote_port')}，"
            f"经 {self.config.get('ssh_user')}@{self.config.get('ssh_host')}"
        )
        self.ssh_process = self.launch_process(
            parts,
            "SSH 隧道",
            visible=bool(self.config.get("ssh_open_terminal")),
        )
        if self.ssh_process is not None and self.ssh_process.stdout is not None:
            self.read_process_output(self.ssh_process, "SSH 隧道")
        elif self.ssh_process is not None:
            self.watch_process(self.ssh_process, "SSH 隧道")
        return self.ssh_process is not None

    def launch_process(self, command, name, visible=False):
        # 子进程输出由独立线程持续读取，避免管道缓冲区写满后反向阻塞进程。
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_CONSOLE if visible else subprocess.CREATE_NO_WINDOW

        stdout = None if visible else subprocess.PIPE
        stderr = None if visible else subprocess.STDOUT

        try:
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(APP_DIR),
                creationflags=creationflags,
            )
        except OSError as exc:
            self.log(f"{name} 启动失败：{exc}", "ERROR")
            return None

        self.log(f"{name} 已启动，PID {process.pid}")
        return process

    def watch_process(self, process, name):
        def watcher():
            code = process.wait()
            self.log(f"{name} 已退出，退出码 {format_exit_code(code)}")
            self.handle_process_exit(name, code, process)

        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        self.reader_threads.append(thread)

    def read_process_output(self, process, name):
        def reader():
            assert process.stdout is not None
            for line in process.stdout:
                text = line.strip()
                if text:
                    if name == "FFmpeg":
                        if is_ffmpeg_nonfatal_noise(text):
                            self.ffmpeg_noise_count += 1
                            if self.ffmpeg_noise_count == 1:
                                self.ffmpeg_recent_output.append(text)
                            elif self.ffmpeg_noise_count % 100 == 0:
                                self.log(
                                    f"FFmpeg 已抑制非致命解码噪声 {self.ffmpeg_noise_count} 条，"
                                    "实时流仍会继续等待可用画面。",
                                    "DEBUG",
                                )
                            continue
                        self.ffmpeg_recent_output.append(text)
                        if len(self.ffmpeg_recent_output) > 40:
                            self.ffmpeg_recent_output = self.ffmpeg_recent_output[-40:]
                    self.log(f"{name}: {text}", "DEBUG")
            code = process.wait()
            self.log(f"{name} 已退出，退出码 {format_exit_code(code)}")
            self.handle_process_exit(name, code, process)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.reader_threads.append(thread)

    def schedule_ffmpeg_restart(self, code):
        # restart_lock 保证网络抖动时只存在一条重连链路，不会重复拉起 FFmpeg。
        source = normalize_input_source(self.current_input_source)
        if not source or source["type"] != "stream":
            return False
        if not bool(self.config.get("stream_auto_reconnect", True)):
            return False

        max_attempts = int_from(self.config.get("stream_reconnect_attempts"), 5, 0, 30)
        if max_attempts <= 0:
            return False

        with self.restart_lock:
            if self.ffmpeg_started_at and time.time() - self.ffmpeg_started_at > 60:
                self.ffmpeg_restart_attempts = 0
            if self.ffmpeg_restart_attempts >= max_attempts:
                return False
            self.ffmpeg_restart_attempts += 1
            attempt = self.ffmpeg_restart_attempts

        delay = min(3 * attempt, 15)
        self.log(
            f"实时流抽帧进程已退出，退出码 {format_exit_code(code)}。{delay} 秒后自动重连（{attempt}/{max_attempts}）。",
            "WARN",
        )
        self.emit("state", {"text": "抽帧重连中"})

        def restart_worker():
            if self.stop_event.wait(delay):
                return
            if not self.running or self.stop_event.is_set():
                return
            self.ffmpeg_process = None
            if self.start_ffmpeg(source):
                self.emit("state", {"text": "运行中"})
            else:
                self.log("实时流重连启动失败，请检查流地址、网络、国标平台或摄像头状态。", "ERROR")

        thread = threading.Thread(target=restart_worker, daemon=True)
        thread.start()
        self.reader_threads.append(thread)
        return True

    def begin_auto_stop_once(self):
        # 进程退出和队列完成都可能触发收尾，用标志保证整批任务只结束一次。
        with self.auto_stop_lock:
            if self.auto_stop_started:
                return False
            self.auto_stop_started = True
            return True

    def analysis_queue_idle(self):
        stats = self.stats_snapshot()
        return (
            stats.get("queued", 0) == 0
            and stats.get("processing", 0) == 0
            and self.task_queue.empty()
        ), stats

    def start_file_batch_completion_watcher(self):
        # 本地视频需同时满足 FFmpeg 已结束、队列为空、工作线程空闲后才能收尾。
        if not self.begin_auto_stop_once():
            return

        def watcher():
            stable_since = None
            last_signature = None
            while not self.stop_event.is_set():
                if not self.running:
                    return
                added, signature = self.reconcile_file_batch_frames()
                idle, stats = self.analysis_queue_idle()
                with self.queued_lock:
                    discovered = set(self.batch_discovered_paths)
                    terminal = set(self.terminal_paths)
                    unresolved = discovered - terminal
                directory_stable = signature == last_signature
                if (
                    idle
                    and not unresolved
                    and added == 0
                    and directory_stable
                ):
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= 2.0:
                        self.finish_file_batch(stats)
                        return
                else:
                    stable_since = None
                last_signature = signature
                time.sleep(0.25)

        thread = threading.Thread(target=watcher, name="batch-completion-watch", daemon=True)
        thread.start()
        self.reader_threads.append(thread)

    def finish_file_batch(self, stats):
        # 收尾阶段再次核对统计与目录，删除成功图片，失败图片保留用于排查。
        success = stats.get("success", 0)
        failed = stats.get("failed", 0)
        total = success + failed
        cleanup_failed = []
        if self.config.get("delete_processed"):
            with self.queued_lock:
                successful_paths = list(self.successful_paths)
            for image_path in successful_paths:
                path = Path(image_path)
                if path.exists() and not self.delete_processed_image(path, attempts=4):
                    cleanup_failed.append(path.name)

        remaining_batch_paths = self.current_file_batch_paths()
        with self.queued_lock:
            failed_paths = set(self.failed_paths)
        failed_remaining = [
            path.name for path in remaining_batch_paths if str(path.resolve()) in failed_paths
        ]
        unexpected_remaining = [
            path.name for path in remaining_batch_paths if str(path.resolve()) not in failed_paths
        ]

        if total == 0:
            self.log("本次任务没有产生可分析图片，已自动停止。", "WARN")
            final_state = "已停止"
        elif self.batch_ffmpeg_failed:
            self.log(
                f"本地视频抽帧异常结束；已处理 {total} 张，成功 {success} 张，失败 {failed} 张。已自动停止。",
                "ERROR",
            )
            final_state = "已停止"
        elif failed == 0:
            self.log(f"所有图片已分析完成：成功 {success} 张。已自动停止，状态回到就绪。")
            final_state = "就绪"
        elif success == 0:
            self.log(f"所有图片均分析失败：失败 {failed} 张。已自动停止。", "ERROR")
            final_state = "已停止"
        else:
            self.log(
                f"图片分析已结束：成功 {success} 张，失败 {failed} 张。请检查失败图片日志，任务已自动停止。",
                "WARN",
            )
            final_state = "已停止"

        if failed_remaining:
            self.log(
                f"保留分析失败图片 {len(failed_remaining)} 张用于排障："
                f"{'、'.join(failed_remaining[:5])}"
                f"{' 等' if len(failed_remaining) > 5 else ''}",
                "WARN",
            )
        if cleanup_failed or unexpected_remaining:
            names = cleanup_failed + unexpected_remaining
            self.log(
                f"本地批次仍有 {len(names)} 张非失败图片未能清理："
                f"{'、'.join(names[:5])}"
                f"{' 等' if len(names) > 5 else ''}",
                "ERROR",
            )
            final_state = "已停止"

        session_status = (
            "completed"
            if final_state == "就绪"
            else "completed_with_errors"
            if total > 0
            else "failed"
        )
        self.update_session_manifest(
            session_status,
            terminal=True,
            summary={
                "total": total,
                "success": success,
                "failed": failed,
                "remaining_failed_images": len(failed_remaining),
                "cleanup_failures": len(cleanup_failed) + len(unexpected_remaining),
            },
        )
        self.stop()
        self.emit("state", {"text": final_state})

    def start_stream_finite_completion_watcher(self):
        if not self.begin_auto_stop_once():
            return

        def watcher():
            stable_since = None
            last_signature = None
            while not self.stop_event.is_set():
                if not self.running:
                    return
                added, signature = self.reconcile_stream_finite_batch_frames()
                idle, stats = self.analysis_queue_idle()
                with self.queued_lock:
                    discovered = set(self.batch_discovered_paths)
                    terminal = set(self.terminal_paths)
                    unresolved = discovered - terminal
                directory_stable = signature == last_signature
                if idle and not unresolved and added == 0 and directory_stable:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= 2.0:
                        self.finish_stream_finite_batch(stats)
                        return
                else:
                    stable_since = None
                last_signature = signature
                time.sleep(0.25)

        thread = threading.Thread(
            target=watcher,
            name="stream-finite-completion-watch",
            daemon=True,
        )
        thread.start()
        self.reader_threads.append(thread)

    def finish_stream_finite_batch(self, stats):
        success = stats.get("success", 0)
        failed = stats.get("failed", 0)
        total = success + failed
        cleanup_failed = []
        if self.config.get("delete_processed"):
            with self.queued_lock:
                successful_paths = list(self.successful_paths)
            for image_path in successful_paths:
                path = Path(image_path)
                if path.exists() and not self.delete_processed_image(path, attempts=4):
                    cleanup_failed.append(path.name)

        remaining_paths = self.current_generated_batch_paths()
        with self.queued_lock:
            failed_paths = set(self.failed_paths)
        failed_remaining = [
            path.name for path in remaining_paths if str(path.resolve()) in failed_paths
        ]
        unexpected_remaining = [
            path.name for path in remaining_paths if str(path.resolve()) not in failed_paths
        ]

        if total == 0:
            self.log("实时流定时抽帧未产生可分析图片，已自动停止。", "WARN")
            final_state = "已停止"
        elif self.batch_ffmpeg_failed:
            self.log(
                f"实时流定时抽帧异常结束；已处理 {total} 张，成功 {success} 张，失败 {failed} 张。已自动停止。",
                "ERROR",
            )
            final_state = "已停止"
        elif failed == 0:
            self.log(f"实时流定时抽帧已完成：成功 {success} 张。已自动停止，状态回到就绪。")
            final_state = "就绪"
        elif success == 0:
            self.log(f"实时流定时抽帧全部分析失败：失败 {failed} 张。已自动停止。", "ERROR")
            final_state = "已停止"
        else:
            self.log(
                f"实时流定时抽帧已结束：成功 {success} 张，失败 {failed} 张。请检查失败图片日志。",
                "WARN",
            )
            final_state = "已停止"

        if failed_remaining:
            self.log(
                f"保留分析失败图片 {len(failed_remaining)} 张用于排障："
                f"{'、'.join(failed_remaining[:5])}"
                f"{' 等' if len(failed_remaining) > 5 else ''}",
                "WARN",
            )
        if cleanup_failed or unexpected_remaining:
            names = cleanup_failed + unexpected_remaining
            self.log(
                f"实时流定时抽帧仍有 {len(names)} 张非失败图片未能清理："
                f"{'、'.join(names[:5])}"
                f"{' 等' if len(names) > 5 else ''}",
                "ERROR",
            )
            final_state = "已停止"

        session_status = (
            "completed"
            if final_state == "就绪"
            else "completed_with_errors"
            if total > 0
            else "failed"
        )
        self.update_session_manifest(
            session_status,
            terminal=True,
            summary={
                "total": total,
                "success": success,
                "failed": failed,
                "remaining_failed_images": len(failed_remaining),
                "cleanup_failures": len(cleanup_failed) + len(unexpected_remaining),
            },
        )
        self.stop()
        self.emit("state", {"text": final_state})

    def stop_after_runtime_failure(self, state_text="已停止"):
        if not self.begin_auto_stop_once():
            return

        def worker():
            if self.stop_event.is_set():
                return
            self.update_session_manifest(
                "runtime_failed",
                terminal=True,
                final_state=state_text,
            )
            self.stop()
            self.emit("state", {"text": state_text})

        thread = threading.Thread(target=worker, name="runtime-failure-stop", daemon=True)
        thread.start()
        self.reader_threads.append(thread)

    def handle_process_exit(self, name, code, process=None):
        # 实时流异常退出优先重连，本地文件退出则转入批次完成检查。
        if name == "FFmpeg" and process is not None:
            if process.pid in self.ignored_ffmpeg_exit_pids:
                self.ignored_ffmpeg_exit_pids.discard(process.pid)
                return
        if name == "FFmpeg" and self.switching_rtsp_transport:
            return
        if name == "FFmpeg" and not self.stop_event.is_set():
            source = normalize_input_source(self.current_input_source)
            if source and source["type"] == "file":
                self.batch_ffmpeg_finished = True
                self.batch_ffmpeg_failed = code not in (0, None)
                if self.batch_ffmpeg_failed:
                    self.log("本地视频抽帧进程异常退出，将等待已入队图片处理完成后自动停止。", "ERROR")
                    self.emit("state", {"text": "抽帧异常"})
                else:
                    self.log("本地视频抽帧已结束，正在等待剩余图片分析完成。")
                    self.emit("state", {"text": "收尾中"})
                self.start_file_batch_completion_watcher()
                return
            if source and source["type"] == "stream" and self.stream_finite_capture:
                self.batch_ffmpeg_finished = True
                self.batch_ffmpeg_failed = code not in (0, None)
                if (
                    self.batch_ffmpeg_failed
                    and not self.stream_first_frame_seen
                    and self.try_next_rtsp_runtime_candidate(
                        source.get("value", ""),
                        f"RTSP 定时抽帧进程退出且尚未收到首帧，退出码 {format_exit_code(code)}",
                    )
                ):
                    return
                if self.batch_ffmpeg_failed:
                    self.log("实时流定时抽帧进程异常退出，将等待已入队图片处理完成后自动停止。", "ERROR")
                    self.emit("state", {"text": "抽帧异常"})
                else:
                    self.log("实时流定时抽帧已到达设定时间，正在等待剩余图片分析完成。")
                    self.emit("state", {"text": "收尾中"})
                self.start_stream_finite_completion_watcher()
                return
            if (
                source
                and source["type"] == "stream"
                and not self.stream_first_frame_seen
                and self.try_next_rtsp_runtime_candidate(
                    source.get("value", ""),
                    f"RTSP 拉流进程退出且尚未收到首帧，退出码 {format_exit_code(code)}",
                )
            ):
                return
            if self.schedule_ffmpeg_restart(code):
                return
            if code not in (0, None):
                recent_output = "\n".join(self.ffmpeg_recent_output)
                source_value = source.get("value", "") if source else ""
                if source and source["type"] == "stream" and recent_output:
                    message = (
                        "FFmpeg 异常退出："
                        f"{ffmpeg_stream_error_hint(recent_output, source_value)}。"
                        f"最近输出：{compact_ffmpeg_output(recent_output)}"
                    )
                else:
                    message = "FFmpeg 异常退出，请检查视频文件、实时流地址、网络、国标平台或摄像头权限。"
                self.log(message, "ERROR")
                self.notice("抽帧进程异常", message, "error")
                self.emit("state", {"text": "抽帧异常"})
                self.stop_after_runtime_failure("已停止")
            elif self.realtime_source:
                message = "实时流抽帧进程已退出，且未继续重连。请检查流地址、国标平台或摄像头状态。"
                self.log(message, "WARN")
                self.notice("实时流抽帧已停止", message, "warning")
                self.emit("state", {"text": "抽帧已停止"})
                self.stop_after_runtime_failure("已停止")
        elif name == "SSH 隧道" and code not in (0, None) and not self.stop_event.is_set():
            message = "SSH 隧道异常退出，请检查跳板机地址、端口、用户名、密码或私钥。"
            self.log(message, "ERROR")
            self.notice("SSH 隧道异常", message, "error")
            self.emit("state", {"text": "SSH异常"})
            if self.running or self.current_input_source:
                self.stop_after_runtime_failure("已停止")


def run_cli():
    # 命令行模式只保留目录监听和分析引擎，便于无界面部署或快速排障。
    config = load_config()
    engine = AnalysisEngine(config)
    try:
        engine.start_monitoring()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        engine.stop(cleanup_unprocessed=True)


def run_health_check():
    # 健康检查不启动正式任务，只验证依赖、工具、目录和接口主机的基础条件。
    failed = False
    print(APP_DISPLAY_NAME)
    print(f"Python: {sys.version.split()[0]}")

    checks = [
        ("requests", "requests"),
        ("Pillow", "PIL"),
        ("watchdog", "watchdog"),
        ("tkinter", "tkinter"),
    ]
    for label, module_name in checks:
        try:
            __import__(module_name)
            print(f"[OK] {label}")
        except Exception as exc:
            failed = True
            print(f"[失败] {label}: {exc}")

    for tool in ("ffmpeg", "ssh"):
        path = find_tool(tool)
        if path:
            print(f"[OK] {tool}: {path}")
        else:
            print(f"[提示] {tool}: 未找到")

    config = load_config()
    runtime_directories = [
        ("图片目录", Path(config.get("image_dir") or DEFAULT_IMAGE_DIR)),
        ("结果目录", Path(config.get("results_dir") or DEFAULT_RESULTS_DIR)),
        ("持久日志目录", LOGS_DIR),
        ("崩溃报告目录", CRASH_DIR),
        ("技术支持包目录", SUPPORT_DIR),
        ("升级下载目录", UPDATE_DIR),
    ]
    for label, directory in runtime_directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            print(f"[OK] {label}: {directory}")
        except OSError as exc:
            failed = True
            print(f"[失败] {label}不可用: {exc}")

    ok, message = api_host_is_reachable(config["api_url"], timeout=1)
    if ok:
        print(f"[OK] 大模型接口: {message}")
    else:
        print(f"[提示] 大模型接口当前未连通: {message}")

    return 1 if failed else 0


def run_gui(
    smoke_test=False,
    layout_report=False,
    workflow_test=False,
    tab_switch_test=False,
    video_preview_test=False,
    resize_smooth_test=False,
    button_audit_test=False,
    preview_tab=None,
):
    # Tkinter 只在 GUI 模式导入，命令行模式和环境自检不必初始化图形组件。
    enable_windows_dpi_awareness()
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, scrolledtext, ttk
    from PIL import ImageTk

    def attach_tooltip(widget, text, delay=450):
        if not text:
            return
        state = {"after_id": None, "window": None}

        def cancel():
            after_id = state.get("after_id")
            if after_id is not None:
                try:
                    widget.after_cancel(after_id)
                except tk.TclError:
                    pass
                state["after_id"] = None

        def hide(_event=None):
            cancel()
            tip = state.get("window")
            if tip is not None:
                try:
                    tip.destroy()
                except tk.TclError:
                    pass
                state["window"] = None

        def show():
            state["after_id"] = None
            if state.get("window") is not None:
                return
            try:
                if not widget.winfo_exists():
                    return
                screen_width = max(320, int(widget.winfo_screenwidth()))
                screen_height = max(240, int(widget.winfo_screenheight()))
                x = widget.winfo_rootx() + 8
                y = widget.winfo_rooty() + widget.winfo_height() + 6
                tip = tk.Toplevel(widget)
                tip.wm_overrideredirect(True)
                label = tk.Label(
                    tip,
                    text=text,
                    justify=tk.LEFT,
                    background="#fffdf3",
                    foreground="#0f1720",
                    relief=tk.SOLID,
                    borderwidth=1,
                    font=("Microsoft YaHei UI", 9),
                    padx=8,
                    pady=5,
                    wraplength=280,
                )
                label.pack()
                tip.update_idletasks()
                tip_width = max(1, tip.winfo_reqwidth())
                tip_height = max(1, tip.winfo_reqheight())
                margin = 8
                if x + tip_width + margin > screen_width:
                    x = max(margin, screen_width - tip_width - margin)
                if y + tip_height + margin > screen_height:
                    y = widget.winfo_rooty() - tip_height - 6
                if y < margin:
                    y = max(margin, screen_height - tip_height - margin)
                tip.wm_geometry(f"+{int(x)}+{int(y)}")
                state["window"] = tip
            except tk.TclError:
                state["window"] = None

        def schedule(_event=None):
            hide()
            try:
                state["after_id"] = widget.after(delay, show)
            except tk.TclError:
                state["after_id"] = None

        widget.bind("<Enter>", schedule, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress>", hide, add="+")
        widget._tooltip_state = state
        widget._tooltip_text = text
        widget._tooltip_show = show
        widget._tooltip_hide = hide

    class VideoPreviewWindow:
        def __init__(self, app, source_type, source_value, config):
            self.app = app
            self.source_type = "stream" if source_type == "stream" else "file"
            self.source_value = source_value
            self.config = config.copy()
            self.embedded = hasattr(app, "video_preview_content")
            self.closed = False
            self.host = getattr(app, "video_preview_content", None)
            if not self.embedded:
                raise RuntimeError("内嵌视频预览区域尚未初始化")
            for child in self.host.winfo_children():
                child.destroy()
            self.window = ttk.Frame(self.host)
            self.window.grid(row=0, column=0, sticky=tk.NSEW)
            self.preview_max_size = (560, 120)
            self.video_min_height = 82

            self.frame_queue = queue.Queue(maxsize=3)
            self.process = None
            self.reader_thread = None
            self.stop_event = threading.Event()
            self.photo = None
            self.last_frame_bytes = None
            self.preview_resize_after_id = None
            self.preview_resize_pending = False
            self.playing = False
            self.dragging = False
            self.play_start_position = 0.0
            self.play_started_at = 0.0
            self.stream_started_at = time.monotonic()
            self.duration = (
                probe_video_duration(source_value)
                if self.source_type == "file"
                else 0.0
            )
            self.position_var = tk.DoubleVar(value=0.0)
            self.time_var = tk.StringVar(value="00:00:00")
            self.status_var = tk.StringVar(value="准备预览")
            self.point_var = tk.StringVar(value="未选择")
            self.start_var = tk.StringVar(value="未选择")
            self.end_var = tk.StringVar(value="未选择")
            self.point_time = None
            self.range_start = None
            self.range_end = None
            self.preview_action_buttons = []

            self.build_ui()
            self.window.after(80, self.poll_frames)
            if self.source_type == "file":
                self.load_single_frame(0.0)
            else:
                self.start_playback(0.0)

        def build_ui(self):
            if self.embedded:
                self.build_embedded_ui()
                return
            self.window.columnconfigure(0, weight=1)
            self.window.rowconfigure(0, weight=1)
            button_style = "Compact.TButton" if self.embedded else "Tool.TButton"
            accent_style = "CompactAccent.TButton" if self.embedded else "Accent.TButton"

            video_shell = tk.Frame(
                self.window,
                background="#0f1720",
                padx=6 if self.embedded else 8,
                pady=6 if self.embedded else 8,
            )
            self.video_shell = video_shell
            video_shell.grid(row=0, column=0, sticky=tk.NSEW)
            video_shell.columnconfigure(0, weight=1)
            video_shell.rowconfigure(0, weight=1)
            video_shell.configure(height=self.video_min_height)
            video_shell.grid_propagate(False)
            video_shell.bind("<Configure>", self.on_video_shell_resize, add="+")
            self.video_label = tk.Label(
                video_shell,
                text="正在准备视频预览",
                fg="#d9e6f2",
                bg="#0f1720",
                anchor=tk.CENTER,
                compound=tk.CENTER,
            )
            self.video_label.grid(row=0, column=0, sticky=tk.NSEW)

            controls = ttk.Frame(self.window, padding=(0, 5) if self.embedded else (10, 8))
            controls.grid(row=1, column=0, sticky=tk.EW)
            controls.columnconfigure(1, weight=1)

            self.play_button = ttk.Button(
                controls,
                text="播放" if self.source_type == "file" else "重新连接",
                command=self.toggle_playback,
                style=button_style,
            )
            self.play_button.grid(row=0, column=0, sticky=tk.W)
            ttk.Label(controls, textvariable=self.time_var, style="Route.TLabel").grid(
                row=0, column=1, sticky=tk.W, padx=(12, 0)
            )
            ttk.Label(controls, textvariable=self.status_var, style="Muted.TLabel").grid(
                row=0, column=2, sticky=tk.E, padx=(12, 0)
            )

            self.timeline = ttk.Scale(
                controls,
                from_=0,
                to=max(self.duration, 1.0),
                variable=self.position_var,
                command=self.on_timeline_drag,
            )
            self.timeline.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
            self.timeline.bind("<ButtonPress-1>", self.on_timeline_press, add="+")
            self.timeline.bind("<ButtonRelease-1>", self.on_timeline_release, add="+")
            if self.source_type == "stream":
                self.timeline.state(["disabled"])

            labels = ttk.Frame(self.window, padding=(0, 0, 0, 5) if self.embedded else (10, 0, 10, 8))
            labels.grid(row=2, column=0, sticky=tk.EW)
            labels.columnconfigure(1, weight=1)
            ttk.Label(labels, text="时间点", style="Form.TLabel").grid(row=0, column=0, sticky=tk.W)
            ttk.Label(labels, textvariable=self.point_var).grid(row=0, column=1, sticky=tk.W, padx=(6, 20))
            ttk.Label(labels, text="时间段", style="Form.TLabel").grid(row=0, column=2, sticky=tk.W)
            ttk.Label(labels, textvariable=self.start_var).grid(row=0, column=3, sticky=tk.W, padx=(6, 4))
            ttk.Label(labels, text="到").grid(row=0, column=4, sticky=tk.W, padx=4)
            ttk.Label(labels, textvariable=self.end_var).grid(row=0, column=5, sticky=tk.W, padx=(4, 0))

            actions = ttk.Frame(self.window, padding=(0, 0, 0, 0) if self.embedded else (10, 0, 10, 10))
            actions.grid(row=3, column=0, sticky=tk.EW)
            for column in range(8):
                actions.columnconfigure(column, weight=0)
            ttk.Button(
                actions,
                text="设为时间点",
                command=self.mark_point,
                style=accent_style,
            ).grid(row=0, column=0, sticky=tk.W)
            ttk.Button(
                actions,
                text="设为开始",
                command=self.mark_start,
                style=button_style,
            ).grid(row=0, column=1, sticky=tk.W, padx=(8, 0))
            ttk.Button(
                actions,
                text="设为结束",
                command=self.mark_end,
                style=button_style,
            ).grid(row=0, column=2, sticky=tk.W, padx=(8, 0))
            ttk.Button(
                actions,
                text="应用时间点",
                command=self.apply_point,
                style=accent_style,
            ).grid(row=0, column=3, sticky=tk.W, padx=(20, 0))
            ttk.Button(
                actions,
                text="应用时间段",
                command=self.apply_range,
                style=accent_style,
            ).grid(row=0, column=4, sticky=tk.W, padx=(8, 0))
            ttk.Button(
                actions,
                text="停止预览" if self.embedded else "关闭",
                command=self.close,
                style=button_style,
            ).grid(row=0, column=5, sticky=tk.W, padx=(20, 0))

            if not self.embedded:
                hint = (
                    "本地视频可拖动进度条定位；实时流显示的是从打开预览开始的真实时间。"
                    "点击设为时间点、开始或结束后，再应用到抽帧规则。"
                )
                ttk.Label(self.window, text=hint, style="Muted.TLabel", wraplength=980).grid(
                    row=4, column=0, sticky=tk.W, padx=10, pady=(0, 10)
                )

        def build_embedded_ui(self):
            self.window.columnconfigure(0, weight=1)
            self.window.rowconfigure(0, weight=1)
            self.window.rowconfigure(1, weight=0)

            body = ttk.Frame(self.window)
            body.grid(row=0, column=0, sticky=tk.NSEW)
            body.columnconfigure(0, weight=1)
            body.columnconfigure(1, weight=0)
            body.rowconfigure(0, weight=1)

            video_shell = tk.Frame(
                body,
                background="#0f1720",
                padx=6,
                pady=6,
            )
            self.video_shell = video_shell
            video_shell.grid(row=0, column=0, sticky=tk.NSEW)
            video_shell.columnconfigure(0, weight=1)
            video_shell.rowconfigure(0, weight=1)
            video_shell.configure(height=210)
            video_shell.grid_propagate(False)
            video_shell.bind("<Configure>", self.on_video_shell_resize, add="+")
            self.video_label = tk.Label(
                video_shell,
                text="正在准备视频预览",
                fg="#d9e6f2",
                bg="#0f1720",
                anchor=tk.CENTER,
                compound=tk.CENTER,
            )
            self.video_label.grid(row=0, column=0, sticky=tk.NSEW)

            side = ttk.Frame(body, width=126)
            side.grid(row=0, column=1, sticky=tk.NS, padx=(6, 0))
            side.grid_propagate(False)
            side.columnconfigure(0, weight=1)
            self.play_button = ttk.Button(
                side,
                text="播放" if self.source_type == "file" else "重新连接",
                command=self.toggle_playback,
                style="CompactAccent.TButton",
            )
            self.play_button.grid(row=0, column=0, sticky=tk.EW)
            attach_tooltip(
                self.play_button,
                "播放或暂停当前本地视频预览"
                if self.source_type == "file"
                else "重新连接当前实时视频流并刷新预览画面",
            )
            self.preview_action_buttons.append(self.play_button)
            ttk.Label(side, textvariable=self.time_var, style="Route.TLabel").grid(
                row=1, column=0, sticky=tk.EW, pady=(6, 0)
            )
            ttk.Label(side, textvariable=self.status_var, style="Muted.TLabel", wraplength=118).grid(
                row=2, column=0, sticky=tk.EW, pady=(2, 8)
            )
            ttk.Label(side, text="取时结果", style="Form.TLabel").grid(
                row=3, column=0, sticky=tk.W
            )
            ttk.Label(side, textvariable=self.point_var, style="Muted.TLabel").grid(
                row=4, column=0, sticky=tk.EW, pady=(2, 0)
            )
            ttk.Label(side, textvariable=self.start_var, style="Muted.TLabel").grid(
                row=5, column=0, sticky=tk.EW, pady=(2, 0)
            )
            ttk.Label(side, textvariable=self.end_var, style="Muted.TLabel").grid(
                row=6, column=0, sticky=tk.EW, pady=(2, 8)
            )
            compact_actions = ttk.Frame(side)
            compact_actions.grid(row=7, column=0, sticky=tk.EW)
            compact_actions.columnconfigure(0, weight=1, uniform="preview_actions", minsize=58)
            compact_actions.columnconfigure(1, weight=1, uniform="preview_actions", minsize=58)

            def add_preview_button(row, column, text, command, style, tooltip):
                button = ttk.Button(
                    compact_actions,
                    text=text,
                    command=command,
                    style=style,
                    width=5,
                )
                button.grid(
                    row=row,
                    column=column,
                    sticky=tk.EW,
                    padx=(0, 3) if column == 0 else (3, 0),
                    pady=(0, 4) if row < 2 else (0, 0),
                )
                attach_tooltip(button, tooltip)
                self.preview_action_buttons.append(button)
                return button

            add_preview_button(
                0,
                0,
                "取点",
                self.mark_point,
                "PreviewMini.TButton",
                "记录当前画面时间，作为单点抽帧位置。",
            )
            add_preview_button(
                0,
                1,
                "起点",
                self.mark_start,
                "PreviewMini.TButton",
                "记录当前画面时间，作为时间段抽帧的开始位置。",
            )
            add_preview_button(
                1,
                0,
                "终点",
                self.mark_end,
                "PreviewMini.TButton",
                "记录当前画面时间，作为时间段抽帧的结束位置。",
            )
            add_preview_button(
                1,
                1,
                "应用点",
                self.apply_point,
                "PreviewMiniAccent.TButton",
                "把已记录的时间点写入分析规则。",
            )
            add_preview_button(
                2,
                0,
                "应用段",
                self.apply_range,
                "PreviewMiniAccent.TButton",
                "把已记录的开始和结束时间写入分析规则。",
            )
            add_preview_button(
                2,
                1,
                "停止",
                self.close,
                "PreviewMini.TButton",
                "停止视频预览并释放当前拉流进程。",
            )

            timeline_row = ttk.Frame(self.window)
            timeline_row.grid(row=1, column=0, sticky=tk.EW, pady=(6, 0))
            timeline_row.columnconfigure(0, weight=1)
            self.timeline = ttk.Scale(
                timeline_row,
                from_=0,
                to=max(self.duration, 1.0),
                variable=self.position_var,
                command=self.on_timeline_drag,
            )
            self.timeline.grid(row=0, column=0, sticky=tk.EW)
            self.timeline.bind("<ButtonPress-1>", self.on_timeline_press, add="+")
            self.timeline.bind("<ButtonRelease-1>", self.on_timeline_release, add="+")
            if self.source_type == "stream":
                self.timeline.state(["disabled"])

        def current_time(self):
            if self.source_type == "stream":
                return max(0.0, time.monotonic() - self.stream_started_at)
            if self.playing:
                value = self.play_start_position + max(0.0, time.monotonic() - self.play_started_at)
                if self.duration > 0:
                    value = min(value, self.duration)
                return value
            return float_from(self.position_var.get(), 0.0, 0.0, max(self.duration, 86400.0))

        def set_status(self, text):
            self.status_var.set(text)

        def preview_input_options(self):
            if self.source_type != "stream":
                return []
            engine = AnalysisEngine.__new__(AnalysisEngine)
            engine.config = self.config.copy()
            engine.stream_low_latency_override = True
            engine.stream_transport_override = None
            engine.stream_rtsp_prefer_tcp_override = False
            engine.stream_keyframe_only_override = False
            return engine.build_ffmpeg_input_options("stream", self.source_value, low_latency=True)

        def start_process(self, start_time=0.0, single_frame=False):
            ffmpeg = find_tool("ffmpeg")
            if not ffmpeg:
                self.set_status("未找到 FFmpeg")
                return None
            self.stop_process()
            command = build_preview_ffmpeg_command(
                ffmpeg,
                self.source_type,
                self.source_value,
                input_options=self.preview_input_options(),
                start_time=start_time,
                fps=8 if self.source_type == "stream" else 10,
                max_width=960,
                single_frame=single_frame,
            )
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except OSError as exc:
                self.set_status(f"预览启动失败：{exc}")
                return None
            self.process = process
            self.stop_event.clear()
            self.reader_thread = threading.Thread(
                target=self.read_jpeg_frames,
                args=(process, single_frame),
                name="video-preview-reader",
                daemon=True,
            )
            self.reader_thread.start()
            return process

        def start_playback(self, start_time=None):
            if start_time is None:
                start_time = self.current_time()
            self.playing = True
            self.play_start_position = max(0.0, float(start_time))
            self.play_started_at = time.monotonic()
            if self.source_type == "stream":
                self.stream_started_at = time.monotonic()
            self.play_button.configure(text="暂停" if self.source_type == "file" else "重新连接")
            self.set_status("预览中")
            self.start_process(self.play_start_position, single_frame=False)

        def pause_playback(self):
            if self.source_type == "stream":
                self.start_playback(0.0)
                return
            position = self.current_time()
            self.playing = False
            self.play_button.configure(text="播放")
            self.stop_process()
            self.position_var.set(position)
            self.load_single_frame(position)

        def toggle_playback(self):
            if self.source_type == "stream":
                self.start_playback(0.0)
            elif self.playing:
                self.pause_playback()
            else:
                self.start_playback(self.current_time())

        def load_single_frame(self, position):
            self.playing = False
            self.play_button.configure(text="播放")
            self.position_var.set(max(0.0, float(position)))
            self.set_status("定位中")
            self.start_process(position, single_frame=True)

        def stop_process(self):
            self.stop_event.set()
            process = self.process
            self.process = None
            if process is not None and process.poll() is None:
                kill_process_tree(process)

        def read_jpeg_frames(self, process, single_frame=False):
            buffer = b""
            stream = process.stdout
            if stream is None:
                return
            try:
                while not self.stop_event.is_set():
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while True:
                        start = buffer.find(b"\xff\xd8")
                        end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                        if start < 0:
                            buffer = buffer[-2:]
                            break
                        if end < 0:
                            buffer = buffer[start:]
                            if len(buffer) > 4_000_000:
                                buffer = buffer[-256_000:]
                            break
                        frame = buffer[start : end + 2]
                        buffer = buffer[end + 2 :]
                        self.enqueue_frame(frame)
                        if single_frame:
                            return
            finally:
                try:
                    process.wait(timeout=1)
                except Exception:
                    pass

        def enqueue_frame(self, frame):
            while True:
                try:
                    self.frame_queue.put_nowait(frame)
                    return
                except queue.Full:
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        return

        def poll_frames(self):
            if self.closed:
                return
            try:
                if not self.window.winfo_exists():
                    return
            except tk.TclError:
                return
            latest = None
            while True:
                try:
                    latest = self.frame_queue.get_nowait()
                except queue.Empty:
                    break
            if latest:
                self.display_frame(latest)
                self.set_status("画面已更新")
            current = self.current_time()
            self.time_var.set(
                f"{format_capture_seconds(current)}"
                + (f" / {format_capture_seconds(self.duration)}" if self.duration > 0 else "")
            )
            if self.source_type == "file" and not self.dragging:
                self.position_var.set(current)
                if self.playing and self.duration > 0 and current >= self.duration:
                    self.pause_playback()
            self.window.after(90, self.poll_frames)

        def display_frame(self, frame_bytes):
            self.last_frame_bytes = frame_bytes
            if self.photo is not None and getattr(self.app, "is_root_resizing", lambda: False)():
                self.preview_resize_pending = True
                return
            try:
                with Image.open(BytesIO(frame_bytes)) as image:
                    image = image.convert("RGB")
                    available_width = (
                        self.video_shell.winfo_width() - 14
                        if hasattr(self, "video_shell")
                        else self.preview_max_size[0]
                    )
                    available_height = (
                        self.video_shell.winfo_height() - 14
                        if hasattr(self, "video_shell")
                        else self.preview_max_size[1]
                    )
                    max_width = max(320, available_width)
                    max_height = max(120, available_height)
                    image.thumbnail((max_width, max_height), PIL_LANCZOS)
                    photo = ImageTk.PhotoImage(image)
            except Exception as exc:
                self.set_status(f"画面解码失败：{exc}")
                return
            self.photo = photo
            self.preview_resize_pending = False
            self.video_label.configure(image=photo, text="")

        def on_video_shell_resize(self, _event=None):
            if self.closed or not self.last_frame_bytes:
                return
            if self.preview_resize_after_id is not None:
                try:
                    self.window.after_cancel(self.preview_resize_after_id)
                except tk.TclError:
                    pass
            self.preview_resize_after_id = self.window.after(120, self.redisplay_last_frame)

        def redisplay_last_frame(self):
            self.preview_resize_after_id = None
            if self.closed or not self.last_frame_bytes:
                return
            if getattr(self.app, "is_root_resizing", lambda: False)():
                self.preview_resize_pending = True
                try:
                    self.preview_resize_after_id = self.window.after(180, self.redisplay_last_frame)
                except tk.TclError:
                    self.preview_resize_after_id = None
                return
            self.display_frame(self.last_frame_bytes)

        def on_timeline_press(self, _event=None):
            if self.source_type == "file":
                self.dragging = True

        def on_timeline_drag(self, value):
            if self.source_type == "file" and self.dragging:
                self.time_var.set(
                    f"{format_capture_seconds(float_from(value, 0.0, 0.0, max(self.duration, 1.0)))}"
                    + (f" / {format_capture_seconds(self.duration)}" if self.duration > 0 else "")
                )

        def on_timeline_release(self, _event=None):
            if self.source_type != "file":
                return
            self.dragging = False
            position = float_from(self.position_var.get(), 0.0, 0.0, max(self.duration, 1.0))
            if self.playing:
                self.start_playback(position)
            else:
                self.load_single_frame(position)

        def mark_point(self):
            self.point_time = self.current_time()
            self.point_var.set(format_capture_seconds(self.point_time))

        def mark_start(self):
            self.range_start = self.current_time()
            self.start_var.set(format_capture_seconds(self.range_start))

        def mark_end(self):
            self.range_end = self.current_time()
            self.end_var.set(format_capture_seconds(self.range_end))

        def apply_point(self):
            if self.point_time is None:
                self.mark_point()
            self.app.capture_mode_var.set(capture_mode_display("point"))
            self.app.capture_point_var.set(format_capture_seconds(self.point_time))
            self.app.update_capture_field_states()
            self.app.on_analysis_config_changed()
            self.set_status("已写入抽帧时间点")

        def apply_range(self):
            if self.range_start is None:
                self.mark_start()
            if self.range_end is None:
                self.mark_end()
            start = min(self.range_start, self.range_end)
            end = max(self.range_start, self.range_end)
            if end <= start:
                messagebox.showwarning("时间段无效", "结束时间必须大于开始时间。")
                return
            self.app.capture_mode_var.set(capture_mode_display("range"))
            self.app.capture_start_var.set(format_capture_seconds(start))
            self.app.capture_end_var.set(format_capture_seconds(end))
            self.app.update_capture_field_states()
            self.app.on_analysis_config_changed()
            self.set_status("已写入抽帧时间段")

        def close(self):
            self.closed = True
            if self.preview_resize_after_id is not None:
                try:
                    self.window.after_cancel(self.preview_resize_after_id)
                except tk.TclError:
                    pass
                self.preview_resize_after_id = None
            self.stop_process()
            if getattr(self.app, "video_preview_window", None) is self:
                self.app.video_preview_window = None
            if self.embedded:
                try:
                    for child in self.host.winfo_children():
                        child.destroy()
                    self.app.show_video_preview_placeholder()
                except tk.TclError:
                    pass
                return
            try:
                if self.window.winfo_exists():
                    self.window.destroy()
            except tk.TclError:
                pass

    class StreamApp:
        def __init__(self, root):
            self.root = root
            self.ui_thread_ident = threading.get_ident()
            self.config = load_config()
            cleanup_runtime_records(self.config.get("log_retention_days", 30))

            # 引擎线程通过 events 与界面通信，所有控件更新最终都回到 Tk 主线程。
            self.events = queue.Queue(maxsize=GUI_EVENT_QUEUE_LIMIT)
            self.engine = None
            self.current_result_file = None
            self.model_values = []
            self.api_key_entries = []
            self.rtsp_password_entries = []
            self.model_combos = []
            self.server_panels = {}
            self.workflow_server_panels = {}
            self.source_panels = {}
            self.workflow_step_labels = {}
            self.workflow_sections = {}
            self.choice_groups = []
            self.log_line_count = 0
            self.result_line_count = 0
            self.video_preview_window = None
            self.starting = False
            self.testing = False
            self.stopping = False
            self.update_checking = False
            self.update_downloading = False
            self.update_cancel_event = None
            self.update_progress_window = None
            self.closing = False
            self.config_dirty = False
            self.analysis_config_dirty = False
            self.advanced_config_dirty = False
            self.server_trace_handles = []
            self.analysis_trace_handles = []
            self.advanced_trace_handles = []
            self.analysis_resize_after_id = None
            self.root_resize_after_id = None
            self.root_resize_active_until = 0.0
            self.resize_text_redraw_locked = False
            self.resize_redraw_locked_widgets = []
            self.smooth_resize_text_widgets = []
            self.task_action_bar_compact = None
            self.last_root_size = None
            self.main_tab_after_id = None
            self.overview_refresh_in_progress = False
            self.last_overview_refresh_at = 0.0
            self.last_log_autoscroll = 0.0
            self.last_result_autoscroll = 0.0
            self.pending_log_lines = []
            self.log_flush_after_id = None
            self.custom_prompt_templates = sanitize_custom_prompt_templates(
                self.config.get("custom_prompt_templates", {})
            )

            self.root.title(APP_DISPLAY_NAME)
            if APP_ICON_PATH.exists():
                try:
                    self.root.iconbitmap(default=str(APP_ICON_PATH))
                except tk.TclError:
                    pass
            width, height, x, y = calculate_initial_window_geometry(
                self.root.winfo_screenwidth(),
                self.root.winfo_screenheight(),
            )
            self.initial_window_size = (width, height)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
            self.root.resizable(True, True)
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

            self.source_type_var = tk.StringVar(value=self.config.get("source_type", "file"))
            self.video_var = tk.StringVar(value=self.config.get("video_file", ""))
            self.stream_url_var = tk.StringVar(value=self.config.get("stream_url", ""))
            self.rtsp_username_var = tk.StringVar(value=self.config.get("rtsp_username", ""))
            self.rtsp_password_var = tk.StringVar(value=self.config.get("rtsp_password", ""))
            self.rtsp_tls_var = tk.BooleanVar(value=bool(self.config.get("rtsp_use_tls", False)))
            self.show_rtsp_password_var = tk.BooleanVar(value=False)
            self.rtsp_security_status_var = tk.StringVar(value="")
            self.stream_format_var = tk.StringVar(
                value=self.config.get("stream_format", DEFAULT_STREAM_FORMAT)
            )
            self.stream_format_hint_var = tk.StringVar(value="")
            self.stream_url_status_var = tk.StringVar(value="")
            self.source_hint_var = tk.StringVar(value="")
            self.connection_mode_var = tk.StringVar(
                value=self.config.get("connection_mode", "public")
            )
            self.image_dir_var = tk.StringVar(value=self.config["image_dir"])
            self.results_dir_var = tk.StringVar(
                value=self.config.get("results_dir", str(DEFAULT_RESULTS_DIR))
            )
            self.api_url_var = tk.StringVar(value=self.config["api_url"])
            self.api_key_var = tk.StringVar(value=self.config["api_key"])
            self.model_var = tk.StringVar(value=self.config["model"])
            self.interval_var = tk.StringVar(value=str(self.config["frame_interval"]))
            self.capture_mode_var = tk.StringVar(
                value=capture_mode_display(self.config.get("capture_mode", "interval"))
            )
            self.capture_point_var = tk.StringVar(
                value=str(self.config.get("capture_point_time", "00:00:00"))
            )
            self.capture_start_var = tk.StringVar(
                value=str(self.config.get("capture_start_time", "00:00:00"))
            )
            self.capture_end_var = tk.StringVar(
                value=str(self.config.get("capture_end_time", "00:01:00"))
            )
            self.size_var = tk.StringVar(value=str(self.config["max_image_size"]))
            self.tokens_var = tk.StringVar(value=str(self.config["max_tokens"]))
            self.concurrency_var = tk.StringVar(value=str(self.config["concurrency"]))
            self.retries_var = tk.StringVar(value=str(self.config["max_retries"]))
            self.timeout_var = tk.StringVar(value=str(self.config["request_timeout"]))
            self.temperature_var = tk.StringVar(value=str(self.config["temperature"]))
            self.log_retention_var = tk.StringVar(
                value=str(self.config.get("log_retention_days", 30))
            )
            self.low_cpu_var = tk.BooleanVar(value=bool(self.config.get("ffmpeg_low_cpu", True)))
            self.ffmpeg_threads_var = tk.StringVar(value=str(self.config.get("ffmpeg_threads", 1)))
            self.stream_low_latency_var = tk.BooleanVar(
                value=bool(self.config.get("stream_low_latency", True))
            )
            self.stream_fast_first_frame_var = tk.BooleanVar(
                value=bool(self.config.get("stream_fast_first_frame", True))
            )
            self.stream_drop_stale_var = tk.BooleanVar(
                value=bool(self.config.get("stream_drop_stale_frames", False))
            )
            self.stream_max_pending_var = tk.StringVar(
                value=str(self.config.get("stream_max_pending_frames", 3))
            )
            self.stream_auto_reconnect_var = tk.BooleanVar(
                value=bool(self.config.get("stream_auto_reconnect", True))
            )
            self.stream_reconnect_attempts_var = tk.StringVar(
                value=str(self.config.get("stream_reconnect_attempts", 5))
            )
            self.stream_probe_var = tk.BooleanVar(
                value=bool(self.config.get("stream_probe_before_start", True))
            )
            self.stream_probe_timeout_var = tk.StringVar(
                value=str(self.config.get("stream_probe_timeout", 12))
            )
            self.rtsp_transport_mode_var = tk.StringVar(
                value=rtsp_transport_mode_label(self.config.get("rtsp_transport_mode", "auto"))
            )
            self.update_url_var = tk.StringVar(
                value=str(self.config.get("update_url") or DEFAULT_UPDATE_INFO)
            )
            self.update_timeout_var = tk.StringVar(
                value=str(self.config.get("update_timeout", 8))
            )
            self.delete_var = tk.BooleanVar(value=bool(self.config["delete_processed"]))
            self.existing_var = tk.BooleanVar(value=bool(self.config["process_existing"]))
            self.tunnel_var = tk.BooleanVar(value=bool(self.config["auto_start_tunnel"]))
            self.ssh_host_var = tk.StringVar(value=str(self.config.get("ssh_host", "")))
            self.ssh_port_var = tk.StringVar(value=str(self.config.get("ssh_port", 22)))
            self.ssh_user_var = tk.StringVar(value=str(self.config.get("ssh_user", "")))
            self.ssh_key_var = tk.StringVar(value=str(self.config.get("ssh_key_path", "")))
            self.ssh_terminal_var = tk.BooleanVar(
                value=bool(self.config.get("ssh_open_terminal"))
            )
            self.ssh_local_port_var = tk.StringVar(
                value=str(self.config.get("ssh_local_port", 8080))
            )
            self.ssh_remote_host_var = tk.StringVar(
                value=str(self.config.get("ssh_remote_host", ""))
            )
            self.ssh_remote_port_var = tk.StringVar(
                value=str(self.config.get("ssh_remote_port", 8000))
            )
            self.ssh_api_path_var = tk.StringVar(
                value=str(self.config.get("ssh_api_path", ""))
            )
            self.ssh_preview_var = tk.StringVar()
            self.status_var = tk.StringVar(value="就绪")
            self.summary_var = tk.StringVar(value="排队 0 | 分析中 0 | 成功 0 | 失败 0")
            self.stats_queue_var = tk.StringVar(value="0")
            self.stats_processing_var = tk.StringVar(value="0")
            self.stats_success_var = tk.StringVar(value="0")
            self.stats_failed_var = tk.StringVar(value="0")
            self.overview_ffmpeg_var = tk.StringVar(value="检查中")
            self.overview_route_var = tk.StringVar(value="读取中")
            self.overview_storage_var = tk.StringVar(value="检查中")
            self.overview_sessions_var = tk.StringVar(value="0 个任务")
            self.overview_latest_var = tk.StringVar(value="暂无任务记录")
            self.overview_refresh_after_id = None
            self.overview_session_items = {}
            self.connection_var = tk.StringVar(value="未测试")
            self.connection_hint_var = tk.StringVar(value="")
            self.active_server_summary_var = tk.StringVar(value="")
            self.server_save_state_var = tk.StringVar(value="")
            self.active_analysis_summary_var = tk.StringVar(value="")
            self.analysis_save_state_var = tk.StringVar(value="")
            self.active_advanced_summary_var = tk.StringVar(value="")
            self.advanced_save_state_var = tk.StringVar(value="")
            self.status_blink_after_id = None
            self.status_breath_phase = 0
            self.last_status_light_state = None
            self.next_step_var = tk.StringVar(value="")
            self.model_hint_var = tk.StringVar(value="")
            self.preset_var = tk.StringVar(value=self.initial_prompt_preset())
            self.prompt_template_name_var = tk.StringVar(value="")
            self.show_key_var = tk.BooleanVar(value=False)

            self.build_ui()
            self.update_capture_field_states()
            self.root.report_callback_exception = self.report_callback_exception
            self.setup_ssh_traces()
            self.refresh_ssh_preview()
            self.on_source_type_change()
            self.on_stream_format_change()
            self.on_stream_url_change()
            self.stream_url_var.trace_add("write", self.on_stream_url_change)
            self.rtsp_username_var.trace_add("write", self.on_stream_url_change)
            self.rtsp_password_var.trace_add("write", self.on_stream_url_change)
            self.rtsp_tls_var.trace_add("write", self.on_stream_url_change)
            self.on_connection_mode_change(initial=True)
            self.update_model_hint()
            self.setup_server_config_traces()
            self.setup_analysis_config_traces()
            self.setup_advanced_config_traces()
            self.mark_server_config_saved("已保存：下次任务和下次打开软件都会使用当前服务器配置")
            self.mark_analysis_config_saved("已保存：下次任务和下次打开软件都会使用当前开始分析配置")
            self.mark_advanced_config_saved("已保存：下次任务和下次打开软件都会使用当前任务参数")
            self.root.after(90, self.poll_events)

        def prompt_template_values(self):
            values = list(PROMPT_PRESETS.keys())
            values.extend(custom_prompt_display_name(name) for name in self.custom_prompt_templates)
            return values

        def initial_prompt_preset(self):
            selected = str(self.config.get("selected_prompt_preset") or "").strip()
            values = self.prompt_template_values()
            if selected in values:
                return selected
            prompt = str(self.config.get("prompt") or "").strip()
            for name, preset_prompt in PROMPT_PRESETS.items():
                if preset_prompt and preset_prompt.strip() == prompt:
                    return name
            for name, custom_prompt in self.custom_prompt_templates.items():
                if custom_prompt.strip() == prompt:
                    return custom_prompt_display_name(name)
            if not prompt:
                return PROMPT_NONE_NAME
            return PROMPT_NONE_NAME

        def refresh_prompt_template_combo(self):
            values = self.prompt_template_values()
            if hasattr(self, "preset_combo"):
                self.preset_combo.configure(values=values)
            if self.preset_var.get() not in values:
                self.preset_var.set(PROMPT_NONE_NAME)

        def block_combobox_mousewheel(self, combo):
            def block(_event):
                return "break"

            combo.bind("<MouseWheel>", block)
            combo.bind("<Button-4>", block)
            combo.bind("<Button-5>", block)

        def setup_server_config_traces(self):
            variables = [
                self.connection_mode_var,
                self.api_url_var,
                self.api_key_var,
                self.model_var,
                self.ssh_host_var,
                self.ssh_port_var,
                self.ssh_user_var,
                self.ssh_key_var,
                self.ssh_terminal_var,
                self.ssh_local_port_var,
                self.ssh_remote_host_var,
                self.ssh_remote_port_var,
                self.ssh_api_path_var,
            ]
            for variable in variables:
                self.server_trace_handles.append(
                    variable.trace_add("write", self.on_server_config_changed)
                )

        def on_server_config_changed(self, *_args):
            if not hasattr(self, "active_server_summary_var"):
                return
            self.config_dirty = True
            self.refresh_server_effective_status()

        def mark_server_config_saved(self, message=None):
            self.config_dirty = False
            self.refresh_server_effective_status(
                message or "已保存：下次任务和下次打开软件都会使用当前服务器配置"
            )

        def current_server_route_summary(self):
            mode = self.connection_mode_var.get()
            model = model_id_from_display(self.model_var.get()) or "未选择"
            if mode == "private_ssh":
                tunnel_config = self.current_tunnel_config()
                api_url = build_local_tunnel_api_url(
                    tunnel_config["ssh_local_port"],
                    tunnel_config["ssh_api_path"],
                )
                ssh_target = (
                    f"{tunnel_config['ssh_user']}@{tunnel_config['ssh_host']}:{tunnel_config['ssh_port']}"
                    if tunnel_config.get("ssh_user") or tunnel_config.get("ssh_host")
                    else "未填写跳板机"
                )
                remote_target = (
                    f"{tunnel_config['ssh_remote_host']}:{tunnel_config['ssh_remote_port']}"
                    if tunnel_config.get("ssh_remote_host")
                    else "未填写远端模型服务"
                )
                return (
                    f"下次任务将使用：{connection_mode_name(mode)} | 本机接口 {api_url} | "
                    f"跳板机 {ssh_target} | 远端 {remote_target} | 模型 {model}"
                )
            if mode == "private_direct":
                return (
                    f"下次任务将使用：{connection_mode_name(mode)} | 接口 {self.api_url_var.get().strip() or '未填写'} | "
                    f"不会启动 SSH | 模型 {model}"
                )
            return (
                f"下次任务将使用：{connection_mode_name(mode)} | 接口 {self.api_url_var.get().strip() or '未填写'} | "
                f"不会启动 SSH | 模型 {model}"
            )

        def refresh_server_effective_status(self, saved_message=None):
            self.active_server_summary_var.set(self.current_server_route_summary())
            if saved_message:
                self.server_save_state_var.set(saved_message)
            elif self.config_dirty:
                self.server_save_state_var.set(
                    "未保存：界面已修改。点击“保存连接配置”会写入本机配置；点击“开始分析”也会自动保存。"
                )
            else:
                self.server_save_state_var.set("已保存：下次任务和下次打开软件都会使用当前服务器配置")
            self.refresh_next_step()

        def server_route_log_text(self, config):
            mode = config.get("connection_mode", "public")
            text = (
                f"{connection_mode_name(mode)} | 接口 {config.get('api_url', '')} | "
                f"模型 {config.get('model', '')}"
            )
            if mode == "private_ssh":
                text += (
                    f" | 跳板机 {config.get('ssh_user', '')}@{config.get('ssh_host', '')}:{config.get('ssh_port', '')}"
                    f" | 远端 {config.get('ssh_remote_host', '')}:{config.get('ssh_remote_port', '')}"
                )
            elif mode == "private_direct":
                text += " | 不启动 SSH"
            else:
                text += " | 不启动 SSH"
            return text

        def short_display(self, value, limit=46):
            text = str(value or "").strip()
            if len(text) <= limit:
                return text or "未填写"
            keep = max(8, (limit - 3) // 2)
            return f"{text[:keep]}...{text[-keep:]}"

        def setup_analysis_config_traces(self):
            variables = [
                self.source_type_var,
                self.video_var,
                self.stream_url_var,
                self.rtsp_username_var,
                self.rtsp_password_var,
                self.rtsp_tls_var,
                self.stream_format_var,
                self.interval_var,
                self.capture_mode_var,
                self.capture_point_var,
                self.capture_start_var,
                self.capture_end_var,
                self.tokens_var,
                self.preset_var,
                self.delete_var,
                self.existing_var,
                self.prompt_template_name_var,
            ]
            for variable in variables:
                self.analysis_trace_handles.append(
                    variable.trace_add("write", self.on_analysis_config_changed)
                )
            if hasattr(self, "prompt_text"):
                self.prompt_text.bind("<<Modified>>", self.on_prompt_text_modified, add="+")
                self.prompt_text.edit_modified(False)

        def setup_advanced_config_traces(self):
            variables = [
                self.image_dir_var,
                self.results_dir_var,
                self.size_var,
                self.concurrency_var,
                self.retries_var,
                self.timeout_var,
                self.temperature_var,
                self.low_cpu_var,
                self.ffmpeg_threads_var,
                self.stream_low_latency_var,
                self.stream_fast_first_frame_var,
                self.stream_drop_stale_var,
                self.stream_max_pending_var,
                self.stream_auto_reconnect_var,
                self.stream_reconnect_attempts_var,
                self.stream_probe_var,
                self.stream_probe_timeout_var,
                self.rtsp_transport_mode_var,
                self.log_retention_var,
                self.update_url_var,
                self.update_timeout_var,
            ]
            for variable in variables:
                self.advanced_trace_handles.append(
                    variable.trace_add("write", self.on_advanced_config_changed)
                )

        def on_analysis_config_changed(self, *_args):
            if not hasattr(self, "active_analysis_summary_var"):
                return
            self.update_capture_field_states()
            self.analysis_config_dirty = True
            self.refresh_analysis_effective_status()

        def update_capture_field_states(self):
            mode = capture_mode_value(self.capture_mode_var.get())
            states = {
                "interval": tk.NORMAL if mode in {"interval", "range"} else tk.DISABLED,
                "point": tk.NORMAL if mode == "point" else tk.DISABLED,
                "range": tk.NORMAL if mode == "range" else tk.DISABLED,
            }
            for widget in getattr(self, "capture_interval_widgets", []):
                widget.configure(state=states["interval"])
            for widget in getattr(self, "capture_point_widgets", []):
                widget.configure(state=states["point"])
            for widget in getattr(self, "capture_range_widgets", []):
                widget.configure(state=states["range"])

        def on_prompt_text_modified(self, _event=None):
            if not hasattr(self, "prompt_text") or not self.prompt_text.edit_modified():
                return
            self.prompt_text.edit_modified(False)
            self.on_analysis_config_changed()
            self.refresh_next_step()

        def on_advanced_config_changed(self, *_args):
            if not hasattr(self, "active_advanced_summary_var"):
                return
            self.advanced_config_dirty = True
            self.refresh_advanced_effective_status()

        def current_analysis_summary(self):
            source_type = self.source_type_var.get()
            tokens = int_from(self.tokens_var.get(), 1500, 1, 32768)
            preset = self.preset_var.get() or PROMPT_NONE_NAME
            prompt_len = len(self.prompt_text.get("1.0", tk.END).strip()) if hasattr(self, "prompt_text") else 0
            delete_text = "完成后删除图片" if self.delete_var.get() else "保留抽帧图片"
            existing_text = "会处理已有图片" if self.existing_var.get() else "只处理新图片"
            capture_config = {
                "capture_mode": capture_mode_value(self.capture_mode_var.get()),
                "frame_interval": self.interval_var.get(),
                "capture_point_time": self.capture_point_var.get(),
                "capture_start_time": self.capture_start_var.get(),
                "capture_end_time": self.capture_end_var.get(),
            }
            interval_text = capture_summary_from_config(capture_config, source_type)
            if source_type == "stream":
                stream_url = normalize_stream_url_for_user(self.stream_url_var.get().strip())
                detected = describe_stream_url(stream_url) if stream_url else "未填写"
                source_text = f"实时视频流 | {detected} | 地址 {self.short_display(masked_stream_url(stream_url))}"
                if urlparse(stream_url).scheme.lower() in {"rtsp", "rtsps"}:
                    rtsp_config = {
                        "stream_url": stream_url,
                        "rtsp_username": self.rtsp_username_var.get(),
                        "rtsp_password": self.rtsp_password_var.get(),
                        "rtsp_use_tls": self.rtsp_tls_var.get(),
                    }
                    source_text += f" | {rtsp_security_summary(rtsp_config, stream_url)}"
            else:
                source_text = f"本地视频文件 | 文件 {self.short_display(self.video_var.get())}"
            return (
                f"下次任务将使用：{source_text} | {interval_text} | 输出字数 {tokens} | "
                f"提示词模板 {preset} | 提示词 {prompt_len} 字 | {delete_text} | {existing_text}"
            )

        def current_advanced_summary(self):
            low_cpu = "低CPU开" if self.low_cpu_var.get() else "高速抽帧"
            low_latency = "低延迟开" if self.stream_low_latency_var.get() else "兼容模式"
            quick_first = "快速首帧开" if self.stream_fast_first_frame_var.get() else "快速首帧关"
            drop_stale = "低延迟优先会丢旧帧" if self.stream_drop_stale_var.get() else "完整分析不丢帧"
            reconnect = "自动重连" if self.stream_auto_reconnect_var.get() else "不自动重连"
            probe = "启动前验证" if self.stream_probe_var.get() else "不预验证"
            queue_limit = int_from(self.stream_max_pending_var.get(), 3, 1, 50)
            queue_text = (
                f"丢帧阈值 {queue_limit} 帧"
                if self.stream_drop_stale_var.get()
                else f"积压提醒 {max(10, queue_limit)} 帧"
            )
            return (
                f"下次任务参数：图片目录 {self.short_display(self.image_dir_var.get(), 34)} | "
                f"结果目录 {self.short_display(self.results_dir_var.get(), 34)} | "
                f"图片上限 {int_from(self.size_var.get(), 1080, 128, 4096)} | "
                f"并发 {int_from(self.concurrency_var.get(), 1, 1, 8)} | "
                f"重试 {int_from(self.retries_var.get(), 3, 1, 10)} | "
                f"接口超时 {int_from(self.timeout_var.get(), 60, 5, 600)} 秒 | "
                f"{low_cpu}，FFmpeg线程 {int_from(self.ffmpeg_threads_var.get(), 1, 1, 8)} | "
                f"实时流：{low_latency}，{quick_first}，{drop_stale}，{reconnect} "
                f"{int_from(self.stream_reconnect_attempts_var.get(), 5, 0, 30)} 次，"
                f"{queue_text}，{probe}，"
                f"RTSP传输 {rtsp_transport_mode_label(rtsp_transport_mode_value(self.rtsp_transport_mode_var.get()))} | "
                f"日志保留 {int_from(self.log_retention_var.get(), 30, 1, 365)} 天"
            )

        def refresh_analysis_effective_status(self, saved_message=None):
            self.active_analysis_summary_var.set(self.current_analysis_summary())
            if saved_message:
                self.analysis_save_state_var.set(saved_message)
            elif self.analysis_config_dirty:
                self.analysis_save_state_var.set(
                    "未保存：任务配置已修改。点击“保存当前配置”会写入本机配置；点击“开始分析”也会自动保存。"
                )
            else:
                self.analysis_save_state_var.set("已保存：下次任务和下次打开软件都会使用当前开始分析配置")
            self.refresh_next_step()

        def refresh_advanced_effective_status(self, saved_message=None):
            self.active_advanced_summary_var.set(self.current_advanced_summary())
            if saved_message:
                self.advanced_save_state_var.set(saved_message)
            elif self.advanced_config_dirty:
                self.advanced_save_state_var.set(
                    "未保存：参数已修改。点击“保存参数”会写入本机配置；点击“开始分析”也会自动保存。"
                )
            else:
                self.advanced_save_state_var.set("已保存：下次任务和下次打开软件都会使用当前任务参数")
            self.refresh_next_step()

        def mark_analysis_config_saved(self, message=None):
            self.analysis_config_dirty = False
            self.refresh_analysis_effective_status(
                message or "已保存：下次任务和下次打开软件都会使用当前开始分析配置"
            )

        def mark_advanced_config_saved(self, message=None):
            self.advanced_config_dirty = False
            self.refresh_advanced_effective_status(
                message or "已保存：下次任务和下次打开软件都会使用当前任务参数"
            )

        def analysis_config_log_text(self, config):
            if config.get("source_type") == "stream":
                stream_url = config.get("stream_url", "")
                source = f"实时视频流 {describe_stream_url(stream_url)} {masked_stream_url(stream_url)}"
                if urlparse(normalize_stream_url_for_user(stream_url)).scheme.lower() in {"rtsp", "rtsps"}:
                    source += f" | {rtsp_security_summary(config, stream_url)}"
            else:
                source = f"本地视频 {config.get('video_file', '')}"
            interval_text = capture_summary_from_config(config, config.get("source_type"))
            return (
                f"{source} | {interval_text} | "
                f"输出字数 {config.get('max_tokens')} | 模板 {config.get('selected_prompt_preset')} | "
                f"提示词 {len(str(config.get('prompt', '')).strip())} 字"
            )

        def advanced_config_log_text(self, config):
            drop_text = (
                f"低延迟优先丢旧帧，阈值 {config.get('stream_max_pending_frames')}"
                if bool(config.get("stream_drop_stale_frames"))
                else f"完整分析不丢帧，积压提醒 {max(10, int_from(config.get('stream_max_pending_frames'), 3, 1, 50))}"
            )
            return (
                f"图片目录 {config.get('image_dir')} | 结果目录 {config.get('results_dir')} | "
                f"图片上限 {config.get('max_image_size')} | 并发 {config.get('concurrency')} | "
                f"重试 {config.get('max_retries')} | 超时 {config.get('request_timeout')} 秒 | "
                f"FFmpeg线程 {config.get('ffmpeg_threads')} | 快速首帧 {bool(config.get('stream_fast_first_frame'))} | "
                f"{drop_text} | RTSP传输 {config.get('rtsp_transport_mode')}"
            )

        def safe_after(self, delay, callback, *args):
            # root.after 不是跨线程接口，后台线程先投递 callback 事件再由主线程执行。
            if threading.get_ident() != self.ui_thread_ident:
                def enqueue_callback():
                    self.queue_ui_event(
                        "callback",
                        {
                            "callback": callback,
                            "args": args,
                        },
                    )

                if delay <= 0:
                    enqueue_callback()
                else:
                    timer = threading.Timer(delay / 1000.0, enqueue_callback)
                    timer.daemon = True
                    timer.start()
                return
            try:
                self.root.after(delay, callback, *args)
            except (tk.TclError, RuntimeError):
                pass

        def queue_ui_event(self, event_type, payload):
            try:
                self.events.put_nowait((event_type, payload))
                return True
            except queue.Full:
                if event_type == "log":
                    write_persistent_log(payload.get("text", ""))
                    return False
                if event_type in {"stats", "result"}:
                    return False
                try:
                    self.events.put((event_type, payload), timeout=0.2)
                    return True
                except queue.Full:
                    return False

        def show_notice(self, title, message, level="warning", log=True):
            title = str(title or "提示")
            message = mask_sensitive_text(message)
            if log:
                self.append_log(f"{title}：{message}")
            if level == "error":
                self.status_var.set("操作失败")
                messagebox.showerror(title, message)
            elif level == "info":
                self.status_var.set(title)
                messagebox.showinfo(title, message)
            else:
                self.status_var.set(title)
                messagebox.showwarning(title, message)

        def action_blocked(self, title, message):
            self.show_notice(title, message, "warning", log=True)

        def is_root_resizing(self):
            return time.time() < float(getattr(self, "root_resize_active_until", 0.0))

        def on_root_configure(self, event):
            # 拖动时合并布局事件，并暂停大文本框重绘，避免文字随每个像素变化反复重排。
            if event.widget is not self.root:
                return
            size = (int(event.width), int(event.height))
            if self.last_root_size == size:
                return
            if self.last_root_size is not None:
                width_delta = abs(size[0] - self.last_root_size[0])
                height_delta = abs(size[1] - self.last_root_size[1])
                if width_delta < 4 and height_delta < 4:
                    return
            self.last_root_size = size
            self.freeze_text_wrapping_for_resize()
            self.root_resize_active_until = time.time() + 0.22
            if self.root_resize_after_id is not None:
                try:
                    self.root.after_cancel(self.root_resize_after_id)
                except tk.TclError:
                    pass
            self.root_resize_after_id = self.root.after(140, self.finish_root_resize)

        def finish_root_resize(self):
            self.root_resize_after_id = None
            if self.is_root_resizing():
                self.root_resize_after_id = self.root.after(90, self.finish_root_resize)
                return
            try:
                if hasattr(self, "workbench_paned"):
                    self.clamp_workbench_split(force=True)
                if hasattr(self, "output_paned"):
                    self.clamp_output_split(force=True)
                if hasattr(self, "task_actions_frame"):
                    self.layout_task_action_bar()
                self.schedule_resize_analysis_controls()
                self.refresh_preview_after_resize()
            finally:
                self.restore_text_wrapping_after_resize()
                self.repair_visible_tab_layout()

        def freeze_text_wrapping_for_resize(self):
            if getattr(self, "resize_text_redraw_locked", False):
                return
            self.resize_text_redraw_locked = True
            self.resize_redraw_locked_widgets = []
            for widget in self.resize_redraw_freeze_targets():
                if self.set_widget_redraw(widget, False):
                    self.resize_redraw_locked_widgets.append(widget)

        def resize_redraw_freeze_targets(self):
            targets = []
            for name in ("result_text", "log_text", "prompt_text"):
                widget = getattr(self, name, None)
                if widget is not None:
                    targets.append(widget)
            targets.extend(getattr(self, "smooth_resize_text_widgets", []))

            unique = []
            seen = set()
            for widget in targets:
                try:
                    if not widget.winfo_exists():
                        continue
                    widget_id = str(widget)
                except tk.TclError:
                    continue
                if widget_id in seen:
                    continue
                seen.add(widget_id)
                unique.append(widget)
            return unique

        def restore_text_wrapping_after_resize(self):
            if not getattr(self, "resize_text_redraw_locked", False):
                return
            self.resize_text_redraw_locked = False
            for widget in reversed(getattr(self, "resize_redraw_locked_widgets", [])):
                self.set_widget_redraw(widget, True)
                try:
                    if widget in (getattr(self, "result_text", None), getattr(self, "log_text", None)):
                        widget.see(tk.END)
                except tk.TclError:
                    pass
            self.resize_redraw_locked_widgets = []

        def set_redraw_tree(self, widget, enabled, locked_ids):
            try:
                if not widget.winfo_exists():
                    return
                widget_id = str(widget)
                if widget_id not in locked_ids and self.set_widget_redraw(widget, enabled):
                    locked_ids.add(widget_id)
                    if not enabled:
                        self.resize_redraw_locked_widgets.append(widget)
                for child in widget.winfo_children():
                    self.set_redraw_tree(child, enabled, locked_ids)
            except tk.TclError:
                return

        def set_widget_redraw(self, widget, enabled):
            if os.name != "nt":
                return False
            try:
                if not widget.winfo_exists():
                    return False
                hwnd = int(widget.winfo_id())
                user32 = ctypes.windll.user32
                user32.SendMessageW.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.c_size_t,
                    ctypes.c_void_p,
                ]
                user32.SendMessageW.restype = ctypes.c_void_p
                user32.SendMessageW(ctypes.c_void_p(hwnd), 0x000B, 1 if enabled else 0, None)
                if enabled:
                    # RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW
                    user32.RedrawWindow.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.c_uint,
                    ]
                    user32.RedrawWindow.restype = ctypes.c_bool
                    user32.RedrawWindow(
                        ctypes.c_void_p(hwnd),
                        None,
                        None,
                        0x0001 | 0x0080 | 0x0100,
                    )
                return True
            except (tk.TclError, OSError, AttributeError, ValueError):
                return False

        def repair_visible_tab_layout(self):
            if getattr(self, "closing", False) or not hasattr(self, "notebook"):
                return
            try:
                selected = self.notebook.select()
                if not selected:
                    return
                page = self.root.nametowidget(selected)
                self.notebook.update_idletasks()

                if page is getattr(self, "analysis_tab", None):
                    self.repair_workbench_layout()
                elif page is getattr(self, "overview_tab_container", None):
                    child = getattr(self, "overview_tab", None)
                    if child is not None and not child.winfo_ismapped():
                        child.pack(fill=tk.BOTH, expand=True)
                    if time.time() - self.last_overview_refresh_at >= 2.0:
                        self.refresh_overview()
                elif page is getattr(self, "server_tab_container", None):
                    child = getattr(self, "server_tab", None)
                    if child is not None and not child.winfo_ismapped():
                        child.pack(fill=tk.BOTH, expand=True)
                elif page is getattr(self, "advanced_tab_container", None):
                    child = getattr(self, "advanced_tab", None)
                    if child is not None and not child.winfo_ismapped():
                        child.pack(fill=tk.BOTH, expand=True)

                self.root.update_idletasks()
            except tk.TclError:
                return

        def repair_workbench_layout(self):
            if not hasattr(self, "analysis_tab"):
                return
            try:
                self.analysis_tab.columnconfigure(0, weight=1)
                self.analysis_tab.rowconfigure(0, weight=0)
                self.analysis_tab.rowconfigure(1, weight=1)

                actions = getattr(self, "task_actions_frame", None)
                if actions is not None and not actions.winfo_ismapped():
                    actions.grid(row=0, column=0, sticky=tk.EW, pady=(0, 7))

                paned = getattr(self, "workbench_paned", None)
                if paned is not None and not paned.winfo_ismapped():
                    paned.grid(row=1, column=0, sticky=tk.NSEW)

                self.layout_task_action_bar()
                self.resize_analysis_controls()

                rules_canvas = getattr(self, "workbench_rules_scroll_canvas", None)
                if rules_canvas is not None and rules_canvas.winfo_exists():
                    rules_canvas.configure(scrollregion=rules_canvas.bbox("all"))

                controls_canvas = getattr(self, "analysis_controls_canvas", None)
                if controls_canvas is not None and controls_canvas.winfo_exists():
                    controls_canvas.configure(scrollregion=controls_canvas.bbox("all"))

                if paned is not None:
                    self.clamp_workbench_split(force=True)
                if hasattr(self, "output_paned"):
                    self.clamp_output_split(force=True)
                self.refresh_preview_after_resize()
            except tk.TclError:
                return

        def lock_paned_sash(self, paned):
            def block_sash_drag(_event=None):
                return "break"

            for sequence in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>"):
                paned.bind(sequence, block_sash_drag, add="+")

        def refresh_preview_after_resize(self):
            preview = getattr(self, "video_preview_window", None)
            if preview is None or getattr(preview, "closed", True):
                return
            if getattr(preview, "last_frame_bytes", None):
                try:
                    preview.redisplay_last_frame()
                except tk.TclError:
                    pass

        def on_main_tab_changed(self, _event=None):
            if self.main_tab_after_id is not None:
                try:
                    self.root.after_cancel(self.main_tab_after_id)
                except tk.TclError:
                    pass
            # 先让新页面完成一次绘制，再执行非必要维护，避免切页时出现空白块或布局跳动。
            self.main_tab_after_id = self.root.after(80, self.finish_main_tab_change)

        def finish_main_tab_change(self):
            self.main_tab_after_id = None
            self.repair_visible_tab_layout()
            selected = self.notebook.select()
            if selected == str(self.overview_tab_container):
                if time.time() - self.last_overview_refresh_at >= 2.0:
                    self.refresh_overview()

        def current_beginner_next_step(self):
            status = self.status_var.get()
            if self.starting:
                return "正在启动任务，请等待状态变成“运行中”或按弹窗提示处理失败项。"
            if self.testing:
                return "正在测试当前模型路线，请等待测试结果弹窗。"
            if self.engine and self.engine.running:
                return "任务正在运行：结果会显示在工作台下方；需要结束时点击“停止任务”。"
            if self.connection_mode_var.get() == "public" and not self.api_key_var.get().strip():
                return "请在工作台的“模型服务”区域填写 API 密钥，再点击“测试连接并读取模型”。"
            if self.connection_mode_var.get() == "private_ssh":
                missing = []
                for var, label in (
                    (self.ssh_host_var, "SSH服务器"),
                    (self.ssh_user_var, "用户名"),
                    (self.ssh_remote_host_var, "模型服务地址"),
                ):
                    if not var.get().strip():
                        missing.append(label)
                if missing:
                    return "请在工作台补齐 SSH 跳板机信息：" + "、".join(missing)
            if not looks_like_vision_model(model_id_from_display(self.model_var.get())):
                return "请读取模型列表，并选择带“图像分析”、VL 或 vision 标识的模型。"
            if self.source_type_var.get() == "stream":
                stream_url = self.stream_url_var.get().strip()
                if not stream_url:
                    return "请在工作台的“视频来源”区域粘贴实时视频流地址。"
                normalized = normalize_stream_url_for_user(stream_url)
                if normalized != stream_url:
                    return "视频流地址缺少协议，请点击“自动识别”。"
                ok, message = validate_stream_url(stream_url)
                if not ok:
                    return "请修正实时流地址：" + message
                if urlparse(stream_url).scheme.lower() in {"rtsp", "rtsps"} and self.rtsp_password_var.get() and not self.rtsp_username_var.get().strip():
                    return "RTSP 已填写密码或 Token，请同时填写 RTSP 账号。"
            else:
                video = self.video_var.get().strip()
                if not video:
                    return "请选择本地视频；如果只分析已有图片，可点击“监听图片目录”。"
                if not Path(video).exists():
                    return "当前视频文件不存在，请重新选择。"
            prompt = ""
            if hasattr(self, "prompt_text"):
                prompt = self.prompt_text.get("1.0", tk.END).strip()
            if not prompt:
                return "请选择分析模板，或填写自己的分析目标。"
            if "失败" in status or "错误" in status or "不可用" in status:
                return "当前状态需要处理：请查看提示，或点击“配置检查”定位问题。"
            return "配置已具备启动条件：建议先测试模型连接，通过后点击“开始分析”。"

        def refresh_next_step(self):
            if hasattr(self, "next_step_var"):
                self.next_step_var.set(self.current_beginner_next_step())
            self.refresh_workflow_status()
            self.update_action_states()

        def current_workflow_config(self):
            prompt = ""
            if hasattr(self, "prompt_text"):
                prompt = self.prompt_text.get("1.0", tk.END).strip()
            return {
                "source_type": self.source_type_var.get(),
                "video_file": self.video_var.get().strip(),
                "stream_url": self.stream_url_var.get().strip(),
                "connection_mode": self.connection_mode_var.get(),
                "api_url": self.api_url_var.get().strip(),
                "api_key": self.api_key_var.get().strip(),
                "model": model_id_from_display(self.model_var.get()),
                "ssh_host": self.ssh_host_var.get().strip(),
                "ssh_user": self.ssh_user_var.get().strip(),
                "ssh_remote_host": self.ssh_remote_host_var.get().strip(),
                "prompt": prompt,
                "selected_prompt_preset": self.preset_var.get(),
            }

        def refresh_workflow_status(self):
            if not self.workflow_step_labels:
                return
            readiness = evaluate_workflow_readiness(self.current_workflow_config())
            values = {
                "server": (
                    "模型",
                    readiness["server_message"],
                    readiness["server_ready"],
                ),
                "source": (
                    "来源",
                    readiness["source_message"],
                    readiness["source_ready"],
                ),
                "prompt": (
                    "规则",
                    readiness["prompt_message"],
                    readiness["prompt_ready"],
                ),
            }
            running = bool(self.engine and self.engine.running)
            values["ready"] = (
                "启动",
                "任务运行中" if running else ("可以开始" if readiness["ready"] else "等待配置"),
                readiness["ready"] or running,
            )
            for key, (title, detail, ready) in values.items():
                label = self.workflow_step_labels.get(key)
                if label is None:
                    continue
                label.configure(
                    text=f"{title}: {self.short_display(detail, 12)}",
                    foreground="#166534" if ready else "#9a3412",
                    background="#dcfce7" if ready else "#fff7ed",
                )

        def update_action_states(self, *_args):
            if not hasattr(self, "start_task_button"):
                return
            running = bool(self.engine and self.engine.running)
            busy = self.starting or self.testing or self.stopping or running
            editable_state = tk.DISABLED if busy else tk.NORMAL
            for button in (
                self.save_task_button,
                self.test_task_button,
                self.start_task_button,
                self.listen_task_button,
            ):
                button.configure(state=editable_state)
            self.stop_task_button.configure(
                state=tk.NORMAL if (self.starting or running) and not self.stopping else tk.DISABLED
            )
            self.schedule_resize_analysis_controls()

        def reveal_workflow_section(self, section, focus_widget=None):
            target = self.workflow_sections.get(section)
            if target is None:
                return
            self.notebook.select(self.analysis_tab)
            self.root.update_idletasks()
            tab = getattr(self, "workbench_section_tabs", {}).get(section)
            if tab is not None and hasattr(self, "workbench_notebook"):
                self.workbench_notebook.select(tab)
            elif hasattr(self, "analysis_controls_canvas"):
                content_height = max(1, self.analysis_controls_content.winfo_height())
                y = max(0, target.winfo_y() - 8)
                self.analysis_controls_canvas.yview_moveto(min(1.0, y / content_height))
            if focus_widget is not None:
                try:
                    focus_widget.focus_set()
                except tk.TclError:
                    pass

        def show_beginner_next_step(self):
            next_step = self.current_beginner_next_step()
            detail = [
                "当前建议：",
                next_step,
                "",
                "任务工作台使用顺序：",
                "1. 在“模型服务”选择连接方式并测试连接。",
                "2. 在“视频来源”选择本地视频或填写实时流地址。",
                "3. 在“分析规则”选择抽帧方式、时间范围和分析模板。",
                "4. 点击“开始分析”；实时流结束时点击“停止任务”。",
                "",
                "所有高频配置都在任务工作台，不需要来回切换页面。",
            ]
            messagebox.showinfo("下一步建议", "\n".join(detail))

        def style_text_widget(self, widget, mono=False):
            font = ("Consolas", 10) if mono else ("Microsoft YaHei UI", 10)
            try:
                try:
                    widget._smooth_resize_restore_wrap = widget.cget("wrap")
                    if widget not in self.smooth_resize_text_widgets:
                        self.smooth_resize_text_widgets.append(widget)
                except tk.TclError:
                    pass
                widget.configure(
                    background=self.ui["surface"],
                    foreground=self.ui["text"],
                    insertbackground=self.ui["primary"],
                    selectbackground=self.ui["selection"],
                    selectforeground=self.ui["text"],
                    borderwidth=1,
                    relief=tk.SOLID,
                    highlightthickness=1,
                    highlightbackground=self.ui["border"],
                    highlightcolor=self.ui["primary"],
                    font=font,
                    padx=8,
                    pady=8,
                )
            except tk.TclError:
                pass

        def bind_dynamic_wraplength(self, label, container=None, margin=18, minimum=120, maximum=720):
            target = container or label.master

            def update(_event=None):
                try:
                    width = max(1, int(target.winfo_width()))
                    wrap = max(minimum, min(maximum, width - margin))
                    label.configure(wraplength=wrap)
                except tk.TclError:
                    return

            try:
                target.bind("<Configure>", update, add="+")
                self.root.after_idle(update)
            except tk.TclError:
                pass

        def remove_ttk_focus_indicators(self, style, style_names):
            def strip_focus_nodes(nodes):
                cleaned = []
                for element, options in nodes:
                    opts = dict(options)
                    children = opts.get("children")
                    if children:
                        stripped_children = strip_focus_nodes(children)
                        if stripped_children:
                            opts["children"] = stripped_children
                        else:
                            opts.pop("children", None)
                    if "focus" in element.lower():
                        cleaned.extend(opts.get("children", []))
                    else:
                        cleaned.append((element, opts))
                return cleaned

            for style_name in style_names:
                try:
                    layout = style.layout(style_name)
                    stripped = strip_focus_nodes(layout)
                    if stripped and stripped != layout:
                        style.layout(style_name, stripped)
                    style.configure(style_name, focuscolor=self.ui["bg"], focusthickness=0)
                except tk.TclError:
                    pass

        def suppress_non_input_focus(self, widget=None):
            if widget is None:
                widget = self.root
            no_focus_classes = {
                "Button",
                "TButton",
                "Radiobutton",
                "TRadiobutton",
                "Checkbutton",
                "TCheckbutton",
                "Notebook",
                "TNotebook",
            }
            try:
                widget_class = widget.winfo_class()
            except tk.TclError:
                return
            if widget_class in no_focus_classes:
                try:
                    widget.configure(takefocus=0)
                except tk.TclError:
                    pass
                try:
                    widget.configure(highlightthickness=0)
                except tk.TclError:
                    pass
                try:
                    widget.bind("<ButtonRelease-1>", lambda _event: self.root.after_idle(self.root.focus_set), add="+")
                except tk.TclError:
                    pass
            try:
                children = widget.winfo_children()
            except tk.TclError:
                return
            for child in children:
                self.suppress_non_input_focus(child)

        def build_ui(self):
            # 颜色、字号和间距集中定义，后续页面只引用语义名称，避免局部样式漂移。
            self.ui = {
                "bg": "#edf1f5",
                "surface": "#ffffff",
                "surface_alt": "#f7f9fc",
                "border": "#b8c4d2",
                "panel_border": "#7f8b99",
                "text": "#111827",
                "muted": "#536273",
                "primary": "#0b5cad",
                "primary_dark": "#063f7d",
                "primary_soft": "#eaf2fb",
                "selection": "#c8ddf3",
                "danger": "#d71920",
                "danger_dark": "#9f1117",
                "success": "#00a651",
                "success_dark": "#007a3d",
                "warning": "#ffbf00",
                "warning_dark": "#9a6400",
                "field": "#ffffff",
                "footer": "#f8fafc",
            }
            self.root.configure(bg=self.ui["bg"])
            style = ttk.Style()
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure("TFrame", background=self.ui["bg"])
            style.configure(
                "TLabel",
                background=self.ui["bg"],
                foreground=self.ui["text"],
                font=("Microsoft YaHei UI", 10),
            )
            style.configure(
                "Form.TLabel",
                background=self.ui["bg"],
                foreground=self.ui["text"],
                font=("Microsoft YaHei UI", 10, "bold"),
            )
            style.configure("Muted.TLabel", background=self.ui["bg"], foreground=self.ui["muted"], font=("Microsoft YaHei UI", 9))
            style.configure(
                "TCheckbutton",
                background=self.ui["bg"],
                foreground=self.ui["text"],
                font=("Microsoft YaHei UI", 10),
                focuscolor=self.ui["bg"],
            )
            style.map(
                "TCheckbutton",
                background=[("active", self.ui["bg"]), ("pressed", self.ui["bg"])],
                foreground=[("active", self.ui["text"])],
            )
            style.configure(
                "TRadiobutton",
                background=self.ui["bg"],
                foreground=self.ui["text"],
                focuscolor=self.ui["bg"],
            )
            style.configure(
                "TLabelframe",
                background=self.ui["bg"],
                borderwidth=1,
                relief="solid",
                bordercolor=self.ui["panel_border"],
                lightcolor=self.ui["panel_border"],
                darkcolor=self.ui["panel_border"],
            )
            style.configure(
                "TLabelframe.Label",
                background=self.ui["bg"],
                foreground=self.ui["text"],
                font=("Microsoft YaHei UI", 11, "bold"),
            )
            style.configure(
                "TNotebook",
                background=self.ui["bg"],
                borderwidth=0,
                tabmargins=(0, 4, 0, 0),
            )
            style.configure(
                "TNotebook.Tab",
                padding=(16, 8),
                background="#dfe6ee",
                foreground=self.ui["muted"],
                font=("Microsoft YaHei UI", 10, "bold"),
                bordercolor=self.ui["panel_border"],
                lightcolor=self.ui["panel_border"],
                darkcolor=self.ui["panel_border"],
                focuscolor=self.ui["bg"],
            )
            style.map(
                "TNotebook.Tab",
                background=[("selected", self.ui["surface"]), ("active", "#eef3f8")],
                foreground=[("selected", self.ui["primary_dark"]), ("active", self.ui["text"])],
            )
            style.configure(
                "TButton",
                padding=(14, 8),
                foreground=self.ui["text"],
                font=("Microsoft YaHei UI", 10),
                focuscolor=self.ui["bg"],
                focusthickness=0,
            )
            style.configure(
                "TEntry",
                fieldbackground=self.ui["field"],
                foreground=self.ui["text"],
                bordercolor=self.ui["border"],
                lightcolor=self.ui["border"],
                darkcolor=self.ui["border"],
                padding=7,
                font=("Microsoft YaHei UI", 10),
                selectbackground=self.ui["selection"],
                selectforeground=self.ui["text"],
            )
            style.configure(
                "TCombobox",
                fieldbackground=self.ui["field"],
                foreground=self.ui["text"],
                bordercolor=self.ui["border"],
                lightcolor=self.ui["border"],
                darkcolor=self.ui["border"],
                padding=7,
                font=("Microsoft YaHei UI", 10),
                selectbackground=self.ui["selection"],
                selectforeground=self.ui["text"],
            )
            style.configure(
                "Tool.TButton",
                padding=(15, 9),
                font=("Microsoft YaHei UI", 10, "bold"),
                background=self.ui["surface"],
                foreground=self.ui["text"],
                bordercolor=self.ui["border"],
                lightcolor=self.ui["border"],
                darkcolor=self.ui["border"],
                focuscolor=self.ui["surface"],
                focusthickness=0,
            )
            style.map(
                "Tool.TButton",
                background=[("active", "#eef3f8"), ("pressed", "#dbe8f6"), ("disabled", "#eef1f5")],
                foreground=[("active", self.ui["primary_dark"]), ("pressed", self.ui["primary_dark"]), ("disabled", "#9aa6b2")],
                bordercolor=[("active", self.ui["primary"]), ("pressed", self.ui["primary_dark"]), ("disabled", "#c9d2dd")],
            )
            style.configure(
                "Accent.TButton",
                padding=(20, 11),
                font=("Microsoft YaHei UI", 10, "bold"),
                background=self.ui["primary"],
                foreground="#ffffff",
                bordercolor=self.ui["primary"],
                lightcolor=self.ui["primary"],
                darkcolor=self.ui["primary_dark"],
                focuscolor=self.ui["primary"],
                focusthickness=0,
            )
            style.map(
                "Accent.TButton",
                background=[("active", self.ui["primary_dark"]), ("pressed", "#052f60"), ("disabled", "#a9bdd1")],
                foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("disabled", "#eef6ff")],
            )
            style.configure(
                "Danger.TButton",
                padding=(15, 9),
                foreground="#ffffff",
                background=self.ui["danger"],
                bordercolor=self.ui["danger"],
                lightcolor=self.ui["danger"],
                darkcolor=self.ui["danger_dark"],
                font=("Microsoft YaHei UI", 10, "bold"),
                focuscolor=self.ui["danger"],
                focusthickness=0,
            )
            style.map(
                "Danger.TButton",
                background=[("active", self.ui["danger_dark"]), ("pressed", "#6f160f"), ("disabled", "#d9aaa5")],
                foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("disabled", "#fff5f4")],
            )
            style.configure(
                "Compact.TButton",
                padding=(9, 5),
                font=("Microsoft YaHei UI", 9),
                background=self.ui["surface"],
                foreground=self.ui["text"],
                bordercolor=self.ui["border"],
                lightcolor=self.ui["border"],
                darkcolor=self.ui["border"],
                focuscolor=self.ui["surface"],
                focusthickness=0,
            )
            style.map(
                "Compact.TButton",
                background=[("active", "#eef3f8"), ("pressed", "#dbe8f6"), ("disabled", "#eef1f5")],
                foreground=[("active", self.ui["primary_dark"]), ("pressed", self.ui["primary_dark"]), ("disabled", "#9aa6b2")],
                bordercolor=[("active", self.ui["primary"]), ("pressed", self.ui["primary_dark"]), ("disabled", "#c9d2dd")],
            )
            style.configure(
                "CompactAccent.TButton",
                padding=(11, 6),
                font=("Microsoft YaHei UI", 9, "bold"),
                background=self.ui["primary"],
                foreground="#ffffff",
                bordercolor=self.ui["primary"],
                lightcolor=self.ui["primary"],
                darkcolor=self.ui["primary_dark"],
                focuscolor=self.ui["primary"],
                focusthickness=0,
            )
            style.map(
                "CompactAccent.TButton",
                background=[("active", self.ui["primary_dark"]), ("pressed", "#052f60"), ("disabled", "#a9bdd1")],
                foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("disabled", "#eef6ff")],
            )
            style.configure(
                "PreviewMini.TButton",
                padding=(4, 4),
                font=("Microsoft YaHei UI", 9),
                background=self.ui["surface"],
                foreground=self.ui["text"],
                bordercolor=self.ui["border"],
                lightcolor=self.ui["border"],
                darkcolor=self.ui["border"],
                focuscolor=self.ui["surface"],
                focusthickness=0,
            )
            style.map(
                "PreviewMini.TButton",
                background=[("active", "#eef3f8"), ("pressed", "#dbe8f6"), ("disabled", "#eef1f5")],
                foreground=[("active", self.ui["primary_dark"]), ("pressed", self.ui["primary_dark"]), ("disabled", "#9aa6b2")],
                bordercolor=[("active", self.ui["primary"]), ("pressed", self.ui["primary_dark"]), ("disabled", "#c9d2dd")],
            )
            style.configure(
                "PreviewMiniAccent.TButton",
                padding=(4, 4),
                font=("Microsoft YaHei UI", 9, "bold"),
                background=self.ui["primary"],
                foreground="#ffffff",
                bordercolor=self.ui["primary"],
                lightcolor=self.ui["primary"],
                darkcolor=self.ui["primary_dark"],
                focuscolor=self.ui["primary"],
                focusthickness=0,
            )
            style.map(
                "PreviewMiniAccent.TButton",
                background=[("active", self.ui["primary_dark"]), ("pressed", "#052f60"), ("disabled", "#a9bdd1")],
                foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("disabled", "#eef6ff")],
            )
            style.configure(
                "CompactDanger.TButton",
                padding=(11, 6),
                font=("Microsoft YaHei UI", 9, "bold"),
                background=self.ui["danger"],
                foreground="#ffffff",
                bordercolor=self.ui["danger"],
                lightcolor=self.ui["danger"],
                darkcolor=self.ui["danger_dark"],
                focuscolor=self.ui["danger"],
                focusthickness=0,
            )
            style.map(
                "CompactDanger.TButton",
                background=[("active", self.ui["danger_dark"]), ("pressed", "#6f160f"), ("disabled", "#d9aaa5")],
                foreground=[("active", "#ffffff"), ("pressed", "#ffffff"), ("disabled", "#fff5f4")],
            )
            style.configure(
                "Workbench.TNotebook",
                background=self.ui["bg"],
                borderwidth=1,
                tabmargins=(0, 0, 0, 0),
            )
            style.configure(
                "Workbench.TNotebook.Tab",
                padding=(12, 6),
                font=("Microsoft YaHei UI", 9, "bold"),
                background="#e4eaf1",
                foreground=self.ui["muted"],
                focuscolor=self.ui["bg"],
            )
            style.map(
                "Workbench.TNotebook.Tab",
                background=[("selected", self.ui["surface"]), ("active", "#eef3f8")],
                foreground=[("selected", self.ui["primary_dark"]), ("active", self.ui["text"])],
            )
            style.configure("Header.TLabel", background=self.ui["surface"], font=("Microsoft YaHei UI", 21, "bold"), foreground=self.ui["text"])
            style.configure("Status.TLabel", background=self.ui["surface"], font=("Microsoft YaHei UI", 18, "bold"), foreground=self.ui["text"])
            style.configure("Route.TLabel", background=self.ui["bg"], font=("Microsoft YaHei UI", 11, "bold"), foreground=self.ui["text"])
            style.configure("SaveState.TLabel", background=self.ui["bg"], font=("Microsoft YaHei UI", 10), foreground=self.ui["primary_dark"])
            style.configure("Footer.TFrame", background=self.ui["footer"])
            style.configure("Summary.TLabel", background=self.ui["footer"], foreground=self.ui["muted"], font=("Microsoft YaHei UI", 10, "bold"))
            style.configure(
                "Overview.Treeview",
                background=self.ui["surface"],
                fieldbackground=self.ui["surface"],
                foreground=self.ui["text"],
                rowheight=30,
                bordercolor=self.ui["border"],
                font=("Microsoft YaHei UI", 9),
            )
            style.configure(
                "Overview.Treeview.Heading",
                background="#dfe7ef",
                foreground=self.ui["text"],
                font=("Microsoft YaHei UI", 9, "bold"),
                padding=(8, 7),
            )
            style.map(
                "Overview.Treeview",
                background=[("selected", self.ui["selection"])],
                foreground=[("selected", self.ui["text"])],
            )
            style.configure("Vertical.TScrollbar", background="#e6edf5", troughcolor=self.ui["bg"], bordercolor=self.ui["bg"], arrowcolor=self.ui["muted"])
            style.configure("Horizontal.TProgressbar", troughcolor="#e9eef5", background=self.ui["primary"], bordercolor="#e9eef5", lightcolor=self.ui["primary"], darkcolor=self.ui["primary"])
            self.remove_ttk_focus_indicators(
                style,
                (
                    "TButton",
                    "Tool.TButton",
                    "Accent.TButton",
                    "Danger.TButton",
                    "TCheckbutton",
                    "TRadiobutton",
                    "TNotebook.Tab",
                    "TNotebook",
                ),
            )

            root_frame = ttk.Frame(self.root, padding=10)
            root_frame.pack(fill=tk.BOTH, expand=True)
            self.root_frame = root_frame

            header = tk.Frame(
                root_frame,
                background="#17212b",
                highlightbackground="#17212b",
                highlightthickness=1,
                padx=14,
                pady=7,
            )
            header.pack(fill=tk.X)
            self.header_frame = header
            brand = tk.Frame(header, background="#17212b")
            brand.pack(side=tk.LEFT)
            tk.Label(
                brand,
                text=APP_DISPLAY_NAME,
                font=("Microsoft YaHei UI", 18, "bold"),
                foreground="#ffffff",
                background="#17212b",
                anchor=tk.W,
            ).pack(anchor=tk.W)
            tk.Label(
                brand,
                text="视频抽帧  ·  图像分析  ·  结果管理",
                font=("Microsoft YaHei UI", 9, "bold"),
                foreground="#79c0ff",
                background="#17212b",
                anchor=tk.W,
            ).pack(anchor=tk.W, pady=(2, 0))
            status_label_font = ("Microsoft YaHei UI", 15, "bold")
            status_label_samples = (
                "就绪",
                "正在测试",
                "正在检查更新",
                "检查更新完成",
                "检查更新失败",
                "正在停止",
            )
            try:
                status_font = tkfont.Font(root=self.root, font=status_label_font)
                status_text_width = max(status_font.measure(text) for text in status_label_samples)
            except tk.TclError:
                status_text_width = 116
            status_header_width = max(292, 10 + 108 + 10 + status_text_width + 18)
            status_header = tk.Frame(
                header,
                background=self.ui["surface"],
                borderwidth=0,
                highlightbackground=self.ui["border"],
                highlightthickness=1,
                padx=10,
                pady=5,
                width=status_header_width,
                height=50,
            )
            self.status_header_frame = status_header
            status_header.pack(side=tk.RIGHT, before=brand)
            status_header.pack_propagate(False)
            self.status_light = tk.Canvas(
                status_header,
                width=108,
                height=36,
                borderwidth=0,
                highlightthickness=0,
                background=self.ui["surface"],
            )
            self.status_light.pack(side=tk.LEFT, padx=(0, 10))
            self.status_lamp_bezels = {}
            self.status_lamp_wells = {}
            self.status_lamp_glows = {}
            self.status_lamps = {}
            self.status_lamp_highlights = {}
            for name, center_x in (("red", 18), ("yellow", 54), ("green", 90)):
                self.status_lamp_bezels[name] = self.status_light.create_oval(
                    center_x - 15,
                    3,
                    center_x + 15,
                    33,
                    fill="#d9e0e7",
                    outline="#5f6b78",
                    width=1,
                )
                self.status_lamp_wells[name] = self.status_light.create_oval(
                    center_x - 12,
                    6,
                    center_x + 12,
                    30,
                    fill="#1d2630",
                    outline="#0b1118",
                    width=1,
                )
                self.status_lamp_glows[name] = self.status_light.create_oval(
                    center_x - 10,
                    8,
                    center_x + 10,
                    28,
                    fill="#26313d",
                    outline="",
                )
                self.status_lamps[name] = self.status_light.create_oval(
                    center_x - 8,
                    10,
                    center_x + 8,
                    26,
                    fill="#3a4652",
                    outline="#111827",
                    width=1,
                )
                self.status_lamp_highlights[name] = self.status_light.create_oval(
                    center_x - 5,
                    12,
                    center_x - 1,
                    16,
                    fill="#8f9aa6",
                    outline="",
                )
            self.status_label = tk.Label(
                status_header,
                textvariable=self.status_var,
                font=status_label_font,
                foreground=self.ui["text"],
                background=self.ui["surface"],
                anchor=tk.W,
            )
            self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.status_var.trace_add("write", self.update_status_light)
            self.status_var.trace_add("write", self.update_action_states)
            self.update_status_light()

            next_step = tk.Frame(
                root_frame,
                background=self.ui["surface"],
                highlightbackground=self.ui["panel_border"],
                highlightthickness=1,
                padx=10,
                pady=5,
                height=58,
            )
            next_step.pack(fill=tk.X, pady=(6, 7))
            self.next_step_frame = next_step
            next_step.pack_propagate(False)
            next_step.rowconfigure(0, weight=1)
            next_step.columnconfigure(2, weight=1)
            tk.Frame(next_step, background=self.ui["primary"], width=4).grid(
                row=0,
                column=0,
                sticky=tk.NS,
                padx=(0, 10),
            )
            tk.Label(
                next_step,
                text="下一步",
                font=("Microsoft YaHei UI", 10, "bold"),
                foreground=self.ui["primary_dark"],
                background=self.ui["surface"],
                width=6,
                anchor=tk.W,
            ).grid(row=0, column=1, sticky=tk.W)
            self.next_step_text_label = tk.Label(
                next_step,
                textvariable=self.next_step_var,
                font=("Microsoft YaHei UI", 10, "bold"),
                foreground=self.ui["text"],
                background=self.ui["surface"],
                anchor=tk.W,
                justify=tk.LEFT,
                wraplength=680,
                height=2,
            )
            self.next_step_text_label.grid(row=0, column=2, sticky=tk.EW)
            ttk.Button(
                next_step,
                text="检查配置",
                command=lambda: self.review_industrial_readiness("新手检查"),
                style="Compact.TButton",
            ).grid(row=0, column=3, sticky=tk.E, padx=(8, 0))
            ttk.Button(
                next_step,
                text="查看指引",
                command=self.show_beginner_next_step,
                style="Compact.TButton",
            ).grid(row=0, column=4, sticky=tk.E, padx=(8, 0))

            self.notebook = ttk.Notebook(root_frame)
            self.notebook.pack(fill=tk.BOTH, expand=True)

            self.overview_tab_container = ttk.Frame(self.notebook)
            self.overview_tab = ttk.Frame(self.overview_tab_container, padding=8)
            self.overview_tab.pack(fill=tk.BOTH, expand=True)
            self.analysis_tab = ttk.Frame(self.notebook, padding=8)
            self.server_tab_container = ttk.Frame(self.notebook)
            self.server_tab = ttk.Frame(self.server_tab_container, padding=8)
            self.server_tab.pack(fill=tk.BOTH, expand=True)
            self.advanced_tab_container = ttk.Frame(self.notebook)
            self.advanced_tab = ttk.Frame(self.advanced_tab_container, padding=8)
            self.advanced_tab.pack(fill=tk.BOTH, expand=True)
            self.log_tab = ttk.Frame(self.notebook, padding=12)

            self.notebook.add(self.analysis_tab, text="任务工作台")
            self.notebook.add(self.overview_tab_container, text="任务记录")
            self.notebook.add(self.server_tab_container, text="模型连接")
            self.notebook.add(self.advanced_tab_container, text="参数设置")
            self.notebook.add(self.log_tab, text="运行日志")

            self.build_overview_tab()
            self.build_analysis_tab()
            self.build_server_tab()
            self.build_advanced_tab()
            self.build_log_tab()
            self.notebook.select(self.analysis_tab)
            self.notebook.bind("<<NotebookTabChanged>>", self.on_main_tab_changed, add="+")
            self.suppress_non_input_focus()
            self.root.bind("<Configure>", self.on_root_configure, add="+")
            self.refresh_next_step()
            self.refresh_overview()
            self.schedule_overview_refresh()

        def status_light_state(self, text):
            text = str(text or "")
            if any(keyword in text for keyword in ("停止", "失败", "错误", "异常", "不可用")):
                return "red"
            if any(keyword in text for keyword in ("运行", "监听", "分析", "启动", "测试", "正在", "重试", "收尾")):
                return "yellow"
            return "green"

        def status_light_should_blink(self, active):
            return active == "yellow"

        def cancel_status_blink(self):
            after_id = getattr(self, "status_blink_after_id", None)
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
                self.status_blink_after_id = None
            self.status_breath_phase = 0

        def schedule_status_blink(self):
            if getattr(self, "status_blink_after_id", None):
                return
            try:
                self.status_blink_after_id = self.root.after(55, self.tick_status_blink)
            except tk.TclError:
                self.status_blink_after_id = None

        def tick_status_blink(self):
            self.status_blink_after_id = None
            if not hasattr(self, "status_light"):
                return
            active = self.status_light_state(self.status_var.get())
            if not self.status_light_should_blink(active):
                self.status_breath_phase = 0
                self.paint_status_light(active)
                return
            self.status_breath_phase = (self.status_breath_phase + 1) % 40
            self.paint_status_light(active)
            self.schedule_status_blink()

        def blend_hex_color(self, start, end, ratio):
            ratio = max(0.0, min(1.0, float(ratio)))
            start = start.lstrip("#")
            end = end.lstrip("#")
            channels = []
            for index in (0, 2, 4):
                first = int(start[index:index + 2], 16)
                second = int(end[index:index + 2], 16)
                channels.append(round(first + (second - first) * ratio))
            return "#" + "".join(f"{channel:02x}" for channel in channels)

        def status_breath_intensity(self):
            phase = (self.status_breath_phase % 40) / 40.0
            return 0.5 - 0.5 * math.cos(phase * math.tau)

        def paint_status_light(self, active):
            # 三层灯腔和高光模拟工业指示灯，运行状态使用平滑呼吸光。
            if not hasattr(self, "status_light") or not hasattr(self, "status_lamps"):
                return
            lamp_colors = {
                "red": "#ff2938",
                "yellow": "#ffc400",
                "green": "#00d46a",
            }
            label_colors = {
                "red": "#a50f1a",
                "yellow": "#8a5a00",
                "green": "#007a3d",
            }
            inactive_lens = {
                "red": "#5a242a",
                "yellow": "#5c4b18",
                "green": "#173f2b",
            }
            inactive_glow = {
                "red": "#352126",
                "yellow": "#38331f",
                "green": "#1c3128",
            }
            breath_intensity = self.status_breath_intensity()
            breath_fill = self.blend_hex_color("#8f7100", "#ffe45c", breath_intensity)
            breath_glow = self.blend_hex_color("#4a4018", "#ffcf28", breath_intensity)
            breath_highlight = self.blend_hex_color("#b9a45b", "#fffdf0", breath_intensity)
            for name, item in self.status_lamps.items():
                bezel = self.status_lamp_bezels.get(name)
                well = self.status_lamp_wells.get(name)
                glow = self.status_lamp_glows.get(name)
                highlight = self.status_lamp_highlights.get(name)
                is_active = name == active
                is_breathing = is_active and self.status_light_should_blink(active)
                if is_breathing:
                    fill = breath_fill
                    glow_fill = breath_glow
                    highlight_fill = breath_highlight
                elif is_active:
                    fill = lamp_colors[name]
                    glow_fill = self.blend_hex_color("#26313d", lamp_colors[name], 0.72)
                    highlight_fill = "#ffffff"
                else:
                    fill = inactive_lens[name]
                    glow_fill = inactive_glow[name]
                    highlight_fill = "#737d87"
                self.status_light.itemconfigure(
                    bezel,
                    fill="#eef2f6" if is_active else "#d2d9e0",
                    outline="#3f4b57" if is_active else "#697582",
                )
                self.status_light.itemconfigure(
                    well,
                    fill="#111820" if is_active else "#202a34",
                    outline="#080d12",
                )
                self.status_light.itemconfigure(glow, fill=glow_fill)
                self.status_light.itemconfigure(
                    item,
                    fill=fill,
                    outline="#0c1117",
                    width=1,
                )
                self.status_light.itemconfigure(highlight, fill=highlight_fill)
            self.status_label.configure(foreground=label_colors[active])

        def update_status_light(self, *_args):
            if not hasattr(self, "status_light") or not hasattr(self, "status_lamps"):
                return
            active = self.status_light_state(self.status_var.get())
            if self.status_light_should_blink(active):
                if self.last_status_light_state != active:
                    self.status_breath_phase = 20
                self.schedule_status_blink()
            else:
                self.cancel_status_blink()
            self.last_status_light_state = active
            self.paint_status_light(active)
            self.refresh_next_step()

        def update_stats_dashboard(self, payload):
            def safe_count(name):
                try:
                    return max(0, int(payload.get(name, 0)))
                except (TypeError, ValueError):
                    return 0

            self.stats_queue_var.set(str(safe_count("queued")))
            self.stats_processing_var.set(str(safe_count("processing")))
            self.stats_success_var.set(str(safe_count("success")))
            self.stats_failed_var.set(str(safe_count("failed")))

        def build_stats_dashboard(self, parent):
            shell = tk.Frame(
                parent,
                background=self.ui["panel_border"],
                borderwidth=0,
                highlightthickness=0,
            )
            body = tk.Frame(shell, background=self.ui["surface"], padx=4, pady=4)
            body.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

            metrics = (
                ("排队", self.stats_queue_var, "#1b78d0", "#eef6ff"),
                ("分析中", self.stats_processing_var, "#a66a00", "#fff7df"),
                ("成功", self.stats_success_var, "#1b7f3a", "#edf9f1"),
                ("失败", self.stats_failed_var, "#b42318", "#fff1ef"),
            )
            for index, item in enumerate(metrics):
                self.build_stats_tile(body, *item, pad_left=0 if index == 0 else 4)
            return shell

        def build_stats_tile(self, parent, label, value_var, color, background, pad_left=0):
            tile = tk.Frame(
                parent,
                background=background,
                highlightbackground=self.ui["panel_border"],
                highlightcolor=self.ui["panel_border"],
                highlightthickness=1,
                width=70,
                height=46,
            )
            tile.pack(side=tk.LEFT, padx=(pad_left, 0))
            tile.pack_propagate(False)
            tk.Frame(tile, background=color).place(x=0, y=0, width=4, relheight=1)
            tk.Label(
                tile,
                textvariable=value_var,
                font=("Microsoft YaHei UI", 14, "bold"),
                foreground=self.ui["text"],
                background=background,
                anchor=tk.CENTER,
            ).place(x=6, y=1, width=59, height=25)
            tk.Label(
                tile,
                text=label,
                font=("Microsoft YaHei UI", 8),
                foreground=self.ui["muted"],
                background=background,
                anchor=tk.CENTER,
            ).place(x=6, y=27, width=59, height=16)

        def schedule_resize_analysis_controls(self, _event=None):
            if self.analysis_resize_after_id is not None:
                try:
                    self.root.after_cancel(self.analysis_resize_after_id)
                except tk.TclError:
                    pass
            self.analysis_resize_after_id = self.root.after(160, self.resize_analysis_controls)

        def resize_analysis_controls(self):
            self.analysis_resize_after_id = None
            if not hasattr(self, "analysis_controls_canvas"):
                return
            total_height = max(420, self.analysis_tab.winfo_height())
            running = bool(self.engine and self.engine.running)
            compact = self.analysis_tab.winfo_width() < 1180
            if compact:
                reserved_height = 260 if running else 230
                ratio = 0.42 if running else 0.48
                minimum_height = 180
            else:
                reserved_height = 330 if running else 170
                ratio = 0.48 if running else 0.76
                minimum_height = 210
            target_height = max(minimum_height, min(650, int(total_height * ratio)))
            if total_height - target_height < reserved_height:
                target_height = max(minimum_height, total_height - reserved_height)
            try:
                current_height = int(float(self.analysis_controls_canvas.cget("height")))
            except (tk.TclError, ValueError):
                current_height = 0
            if abs(current_height - target_height) >= 12:
                self.analysis_controls_canvas.configure(height=target_height)

        def create_analysis_controls_area(self):
            shell = ttk.Frame(self.analysis_tab)
            shell.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
            shell.columnconfigure(0, weight=1)

            scroll_shell = tk.Frame(
                shell,
                background=self.ui["panel_border"],
                borderwidth=0,
                highlightthickness=0,
            )
            scroll_shell.grid(row=0, column=0, sticky=tk.EW)
            scroll_shell.columnconfigure(0, weight=1)
            scroll_shell.rowconfigure(0, weight=1)

            canvas = tk.Canvas(
                scroll_shell,
                borderwidth=0,
                highlightthickness=0,
                background=self.ui["bg"],
                height=420,
            )
            scrollbar = ttk.Scrollbar(scroll_shell, orient=tk.VERTICAL, command=canvas.yview)
            content = ttk.Frame(canvas, padding=(10, 10, 10, 2))
            window_id = canvas.create_window((0, 0), window=content, anchor=tk.NW)

            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.grid(row=0, column=0, sticky=tk.NSEW, padx=(1, 0), pady=1)
            scrollbar.grid(row=0, column=1, sticky=tk.NS, padx=(0, 1), pady=1)
            content.columnconfigure(0, weight=1)
            refresh_state = {"after_id": None, "last_width": 0}

            def refresh_scrollbar():
                first, last = canvas.yview()
                if first <= 0 and last >= 1:
                    try:
                        scrollbar.state(["disabled"])
                    except tk.TclError:
                        pass
                else:
                    try:
                        scrollbar.state(["!disabled"])
                    except tk.TclError:
                        pass

            def refresh_layout():
                refresh_state["after_id"] = None
                canvas.configure(scrollregion=canvas.bbox("all"))
                refresh_scrollbar()

            def schedule_refresh_layout(_event=None):
                after_id = refresh_state.get("after_id")
                if after_id is not None:
                    try:
                        canvas.after_cancel(after_id)
                    except tk.TclError:
                        pass
                refresh_state["after_id"] = canvas.after(120, refresh_layout)

            def on_canvas_configure(event):
                if abs(int(event.width) - int(refresh_state.get("last_width", 0))) >= 8:
                    refresh_state["last_width"] = int(event.width)
                    canvas.itemconfigure(window_id, width=event.width)
                schedule_refresh_layout()

            def on_mousewheel(event):
                delta = -1 * int(event.delta / 120) if event.delta else 0
                if delta:
                    canvas.yview_scroll(delta, "units")
                return "break"

            content.bind("<Configure>", schedule_refresh_layout)
            canvas.bind("<Configure>", on_canvas_configure)
            canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_mousewheel))
            canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

            self.analysis_controls_canvas = canvas
            self.analysis_controls_scrollbar = scrollbar
            self.analysis_controls_content = content
            return content

        def create_scrollable_content(self, parent):
            # Canvas 负责滚动，内部 Frame 负责正常网格布局；宽度变化时同步二者尺寸。
            canvas = tk.Canvas(parent, borderwidth=0, highlightthickness=0, background=self.ui["bg"])
            scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
            content = ttk.Frame(canvas, padding=12)
            window_id = canvas.create_window((0, 0), window=content, anchor=tk.NW)
            scroll_state = {"needed": True}
            refresh_state = {"after_id": None, "last_width": 0}
            scroll_tolerance = 18

            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            def set_scrollbar_visible(visible):
                if scroll_state["needed"] == visible:
                    return
                scroll_state["needed"] = visible
                try:
                    scrollbar.state(["!disabled"] if visible else ["disabled"])
                except tk.TclError:
                    pass

            def get_metrics():
                bbox = canvas.bbox(window_id)
                if not bbox:
                    return 1, 1, canvas.winfo_width()
                content_height = max(1, bbox[3] - bbox[1])
                viewport_height = max(1, canvas.winfo_height())
                width = max(canvas.winfo_width(), bbox[2] - bbox[0])
                return content_height, viewport_height, width

            def clamp_scroll():
                content_height, viewport_height, _width = get_metrics()
                needs_scroll = content_height > viewport_height + scroll_tolerance
                set_scrollbar_visible(needs_scroll)
                if not needs_scroll:
                    canvas.yview_moveto(0)
                    return
                first, last = canvas.yview()
                if first <= 0:
                    canvas.yview_moveto(0)
                elif last >= 1:
                    canvas.yview_moveto(1)

            def refresh_layout():
                refresh_state["after_id"] = None
                content_height, viewport_height, width = get_metrics()
                needs_scroll = content_height > viewport_height + scroll_tolerance
                height = content_height if needs_scroll else viewport_height
                canvas.configure(scrollregion=(0, 0, width, height))
                clamp_scroll()

            def schedule_refresh_layout(_event=None):
                after_id = refresh_state.get("after_id")
                if after_id is not None:
                    try:
                        canvas.after_cancel(after_id)
                    except tk.TclError:
                        pass
                refresh_state["after_id"] = canvas.after(140, refresh_layout)

            def on_canvas_configure(event):
                if abs(int(event.width) - int(refresh_state.get("last_width", 0))) >= 8:
                    refresh_state["last_width"] = int(event.width)
                    canvas.coords(window_id, 0, 0)
                    canvas.itemconfigure(window_id, width=event.width)
                schedule_refresh_layout()

            def on_mousewheel(event):
                content_height, viewport_height, _width = get_metrics()
                if content_height <= viewport_height + scroll_tolerance:
                    canvas.yview_moveto(0)
                    return "break"
                delta = -1 * int(event.delta / 120) if event.delta else 0
                if delta == 0:
                    return "break"
                first, last = canvas.yview()
                if (delta < 0 and first <= 0) or (delta > 0 and last >= 1):
                    clamp_scroll()
                    return "break"
                canvas.yview_scroll(delta, "units")
                clamp_scroll()
                return "break"

            content.bind("<Configure>", schedule_refresh_layout)
            canvas.bind("<Configure>", on_canvas_configure)
            canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_mousewheel))
            canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
            content.scroll_canvas = canvas
            content.scrollbar = scrollbar
            content.refresh_scroll_region = schedule_refresh_layout
            return content

        def build_footer(self, parent):
            footer = ttk.Frame(parent, padding=(12, 10), style="Footer.TFrame")
            footer.pack(fill=tk.X, pady=(10, 0))

            actions = ttk.Frame(footer, style="Footer.TFrame")
            actions.pack(fill=tk.X)
            ttk.Button(
                actions,
                text="保存配置",
                command=self.save_settings,
                style="Accent.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                actions,
                text="测试连接",
                command=lambda: self.test_api("底部工具栏"),
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                actions,
                text="开始分析",
                command=self.start_all,
                style="Accent.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                actions,
                text="停止任务",
                command=self.stop_engine,
                style="Danger.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                actions,
                text="打开结果",
                command=self.open_results_dir,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                actions,
                text="使用说明",
                command=self.open_tutorial,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            self.build_stats_dashboard(footer).pack(anchor=tk.W, pady=(8, 0))

        def build_overview_tab(self):
            page = self.overview_tab
            self.overview_tab_container.pack_propagate(False)
            page.columnconfigure(0, weight=1)
            page.rowconfigure(2, weight=1)
            page.grid_propagate(False)
            command_band = tk.Frame(
                page,
                background=self.ui["surface"],
                highlightbackground=self.ui["panel_border"],
                highlightthickness=1,
                padx=12,
                pady=8,
            )
            command_band.grid(row=0, column=0, sticky=tk.EW)
            command_band.columnconfigure(0, weight=1)
            command_text = tk.Frame(command_band, background=self.ui["surface"])
            command_text.grid(row=0, column=0, sticky=tk.EW)
            tk.Label(
                command_text,
                text="最近运行",
                font=("Microsoft YaHei UI", 9, "bold"),
                foreground=self.ui["muted"],
                background=self.ui["surface"],
                anchor=tk.W,
            ).pack(anchor=tk.W)
            tk.Label(
                command_text,
                textvariable=self.overview_latest_var,
                font=("Microsoft YaHei UI", 10, "bold"),
                foreground=self.ui["text"],
                background=self.ui["surface"],
                anchor=tk.W,
                width=1,
                wraplength=540,
                justify=tk.LEFT,
            ).pack(fill=tk.X, anchor=tk.W, pady=(2, 0))

            command_actions = tk.Frame(command_band, background=self.ui["surface"])
            command_actions.grid(row=0, column=1, sticky=tk.E)
            ttk.Button(
                command_actions,
                text="新建本地视频任务",
                command=self.overview_choose_video,
                style="CompactAccent.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                command_actions,
                text="新建视频流任务",
                command=self.overview_select_stream,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                command_actions,
                text="监听图片目录",
                command=self.start_monitoring,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))

            metrics = tk.Frame(page, background=self.ui["bg"])
            metrics.grid(row=1, column=0, sticky=tk.EW, pady=(8, 8))
            metrics.rowconfigure(0, minsize=76)
            self.overview_metric_frames = []
            for column in range(4):
                metrics.columnconfigure(column, weight=1, uniform="overview")
            self.build_overview_metric(
                metrics,
                0,
                "抽帧组件",
                self.overview_ffmpeg_var,
                "#0b5cad",
            )
            self.build_overview_metric(
                metrics,
                1,
                "模型服务",
                self.overview_route_var,
                "#7a4f01",
            )
            self.build_overview_metric(
                metrics,
                2,
                "可用空间",
                self.overview_storage_var,
                "#087f5b",
            )
            self.build_overview_metric(
                metrics,
                3,
                "任务数量",
                self.overview_sessions_var,
                "#9f2f2f",
            )

            history = ttk.LabelFrame(page, text="任务记录", padding=8)
            history.grid(row=2, column=0, sticky=tk.NSEW)
            history.columnconfigure(0, weight=1)
            history.rowconfigure(0, weight=1)
            table_shell = ttk.Frame(history)
            table_shell.grid(row=0, column=0, columnspan=2, sticky=tk.NSEW)
            table_shell.columnconfigure(0, weight=1)
            table_shell.rowconfigure(0, weight=1)
            columns = ("time", "status", "source", "success", "failed")
            self.session_tree = ttk.Treeview(
                table_shell,
                columns=columns,
                show="headings",
                height=10,
                style="Overview.Treeview",
                selectmode="browse",
            )
            headings = {
                "time": "更新时间",
                "status": "任务状态",
                "source": "输入来源",
                "success": "成功",
                "failed": "失败",
            }
            widths = {
                "time": 142,
                "status": 105,
                "source": 300,
                "success": 58,
                "failed": 58,
            }
            for column in columns:
                self.session_tree.heading(column, text=headings[column])
                self.session_tree.column(
                    column,
                    width=widths[column],
                    minwidth=55,
                    anchor=tk.CENTER if column in {"status", "success", "failed"} else tk.W,
                    stretch=column == "source",
                )
            self.session_tree.grid(row=0, column=0, sticky=tk.NSEW)
            history_scroll = ttk.Scrollbar(
                table_shell,
                orient=tk.VERTICAL,
                command=self.session_tree.yview,
            )
            history_scroll.grid(row=0, column=1, sticky=tk.NS)
            self.session_tree.configure(yscrollcommand=history_scroll.set)
            self.session_tree.bind("<Double-1>", self.open_selected_session_result)
            self.session_tree.bind("<<TreeviewSelect>>", self.update_overview_selection)
            self.overview_empty_label = tk.Label(
                table_shell,
                text="暂无任务记录",
                font=("Microsoft YaHei UI", 12, "bold"),
                foreground=self.ui["muted"],
                background=self.ui["surface"],
                padx=18,
                pady=10,
            )

            history_actions = ttk.Frame(history)
            history_actions.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(7, 0))
            self.open_session_button = ttk.Button(
                history_actions,
                text="打开所选结果",
                command=self.open_selected_session_result,
                style="CompactAccent.TButton",
                state=tk.DISABLED,
            )
            self.open_session_button.pack(side=tk.LEFT)
            ttk.Button(
                history_actions,
                text="打开结果目录",
                command=self.open_results_dir,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(
                history_actions,
                text="刷新",
                command=self.refresh_overview,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(6, 0))

        def update_overview_selection(self, _event=None):
            if not hasattr(self, "open_session_button"):
                return
            state = tk.NORMAL if self.session_tree.selection() else tk.DISABLED
            self.open_session_button.configure(state=state)

        def build_overview_metric(self, parent, column, title, value_var, accent):
            shell = tk.Frame(
                parent,
                background=self.ui["surface"],
                highlightbackground=self.ui["border"],
                highlightthickness=1,
                padx=10,
                pady=8,
            )
            shell.grid(
                row=0,
                column=column,
                sticky=tk.NSEW,
                padx=(0 if column == 0 else 4, 0 if column == 3 else 4),
            )
            shell.columnconfigure(1, weight=1)
            shell.rowconfigure(1, weight=1)
            self.overview_metric_frames.append(shell)
            tk.Frame(shell, background=accent, width=5).grid(
                row=0,
                column=0,
                rowspan=2,
                sticky=tk.NS,
                padx=(0, 10),
            )
            tk.Label(
                shell,
                text=title,
                font=("Microsoft YaHei UI", 9, "bold"),
                foreground=self.ui["muted"],
                background=self.ui["surface"],
                anchor=tk.W,
                width=1,
            ).grid(row=0, column=1, sticky=tk.EW, pady=(0, 3))
            tk.Label(
                shell,
                textvariable=value_var,
                font=("Microsoft YaHei UI", 11, "bold"),
                foreground=self.ui["text"],
                background=self.ui["surface"],
                anchor=tk.W,
                width=1,
            ).grid(row=1, column=1, sticky=tk.EW)

        def overview_choose_video(self):
            self.notebook.select(self.analysis_tab)
            self.source_type_var.set("file")
            self.on_source_type_change()
            self.choose_video()

        def overview_select_stream(self):
            self.notebook.select(self.analysis_tab)
            self.source_type_var.set("stream")
            self.on_source_type_change()
            try:
                self.stream_url_entry.focus_set()
            except (AttributeError, tk.TclError):
                pass

        def schedule_overview_refresh(self):
            if self.closing:
                return
            try:
                self.overview_refresh_after_id = self.root.after(
                    5000,
                    self.periodic_overview_refresh,
                )
            except tk.TclError:
                self.overview_refresh_after_id = None

        def periodic_overview_refresh(self):
            self.overview_refresh_after_id = None
            if self.closing:
                return
            if self.notebook.select() == str(self.overview_tab_container):
                self.refresh_overview()
            self.schedule_overview_refresh()

        def refresh_overview(self):
            if not hasattr(self, "session_tree") or self.overview_refresh_in_progress:
                return
            mode = connection_mode_name(self.connection_mode_var.get())
            model = model_id_from_display(self.model_var.get()) or "未选择模型"
            route_text = f"{mode} / {self.short_display(model, 20)}"
            config = self.config.copy()
            config["results_dir"] = self.results_dir_var.get().strip() or str(DEFAULT_RESULTS_DIR)
            self.overview_refresh_in_progress = True

            def worker():
                try:
                    payload = {
                        "ffmpeg": "可用" if find_tool("ffmpeg") else "未找到",
                        "route": route_text,
                        "storage": runtime_storage_status(),
                        "sessions": list_recent_sessions(config, limit=30),
                    }
                except Exception as exc:
                    payload = {
                        "ffmpeg": "检查失败",
                        "route": route_text,
                        "storage": "检查失败",
                        "sessions": [],
                        "error": str(exc),
                    }
                self.safe_after(0, self.apply_overview_refresh, payload)

            threading.Thread(target=worker, daemon=True).start()

        def apply_overview_refresh(self, payload):
            self.overview_refresh_in_progress = False
            if self.closing or not hasattr(self, "session_tree"):
                return
            self.last_overview_refresh_at = time.time()
            self.overview_ffmpeg_var.set(payload.get("ffmpeg", "未找到"))
            self.overview_route_var.set(payload.get("route", "未选择模型"))
            self.overview_storage_var.set(payload.get("storage", "检查失败"))
            sessions = payload.get("sessions") or []
            self.overview_sessions_var.set(f"{len(sessions)} 个")

            for item in self.session_tree.get_children():
                self.session_tree.delete(item)
            self.overview_session_items.clear()
            self.update_overview_selection()
            status_labels = {
                "created": "已创建",
                "preparing": "准备中",
                "running": "运行中",
                "completed": "已完成",
                "completed_with_errors": "完成但有失败",
                "stopped": "手动停止",
                "start_failed": "启动失败",
                "runtime_failed": "运行失败",
                "failed": "失败",
            }
            for index, session in enumerate(sessions):
                source_type = "实时视频流" if session["source_type"] == "stream" else "本地视频"
                source_value = self.short_display(session["source_value"], 58)
                updated = session["updated_at"].replace("T", " ")[:19] or "-"
                item_id = f"session_{index}"
                self.overview_session_items[item_id] = session
                self.session_tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    values=(
                        updated,
                        status_labels.get(session["status"], session["status"]),
                        f"{source_type} | {source_value}",
                        session["success"],
                        session["failed"],
                    ),
                )
            if sessions:
                self.overview_empty_label.place_forget()
                latest = sessions[0]
                latest_status = status_labels.get(latest["status"], latest["status"])
                self.overview_latest_var.set(
                    f"最近任务：{latest_status}  |  成功 {latest['success']}  |  失败 {latest['failed']}"
                )
            else:
                self.overview_empty_label.place(relx=0.5, rely=0.52, anchor=tk.CENTER)
                self.overview_latest_var.set("暂无任务记录")

        def open_selected_session_result(self, _event=None):
            if not hasattr(self, "session_tree"):
                return
            selection = self.session_tree.selection()
            if not selection:
                messagebox.showinfo("最近任务", "请先选择一条任务记录。")
                return
            session = self.overview_session_items.get(selection[0], {})
            result_file = Path(session.get("result_file") or "")
            manifest = Path(session.get("manifest") or "")
            target = result_file if result_file.is_file() else manifest
            if not target.is_file():
                messagebox.showwarning("文件不存在", "所选任务的结果文件和任务档案均不存在。")
                return
            try:
                open_file(target)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc))

        def build_workflow_header(self, parent):
            header = tk.Frame(
                parent,
                background="#17212b",
                highlightbackground="#17212b",
                highlightthickness=1,
                padx=8,
                pady=6,
            )
            header.grid(row=0, column=0, sticky=tk.EW, pady=(0, 5))
            header.columnconfigure(1, weight=1)
            tk.Label(
                header,
                text="任务状态",
                font=("Microsoft YaHei UI", 9, "bold"),
                foreground="#ffffff",
                background="#17212b",
                anchor=tk.W,
                padx=2,
            ).grid(row=0, column=0, sticky=tk.W, padx=(0, 8))

            steps = tk.Frame(header, background="#17212b")
            steps.grid(row=0, column=1, sticky=tk.EW)
            step_specs = (
                ("server", "模型"),
                ("source", "来源"),
                ("prompt", "规则"),
                ("ready", "启动"),
            )
            for index, (key, title) in enumerate(step_specs):
                row = 0
                column = index
                steps.columnconfigure(column, weight=1, uniform="workflow-step")
                label = tk.Label(
                    steps,
                    text=f"{title}: 待检查",
                    font=("Microsoft YaHei UI", 8, "bold"),
                    foreground="#334155",
                    background="#e2e8f0",
                    anchor=tk.W,
                    justify=tk.LEFT,
                    width=1,
                    padx=6,
                    pady=4,
                    borderwidth=1,
                    relief=tk.SOLID,
                )
                label.grid(
                    row=row,
                    column=column,
                    sticky=tk.EW,
                    padx=(0 if column == 0 else 3, 0),
                )
                self.workflow_step_labels[key] = label

        def add_workflow_api_fields(self, parent, api_label="接口地址"):
            parent.columnconfigure(1, weight=1)
            self.add_form_label(parent, 0, api_label, width=9, pady=3)
            ttk.Entry(parent, textvariable=self.api_url_var).grid(
                row=0,
                column=1,
                columnspan=3,
                sticky=tk.EW,
                padx=(6, 0),
                pady=3,
            )
            self.add_form_label(parent, 1, "API 密钥", width=9, pady=3)
            key_entry = ttk.Entry(parent, textvariable=self.api_key_var, show="*")
            key_entry.grid(row=1, column=1, sticky=tk.EW, padx=(6, 8), pady=3)
            self.api_key_entries.append(key_entry)
            ttk.Checkbutton(
                parent,
                text="显示密钥",
                variable=self.show_key_var,
                command=self.toggle_api_key,
            ).grid(row=1, column=2, sticky=tk.W, pady=3)
            ttk.Button(
                parent,
                text="测试并读取模型",
                command=lambda: self.test_api("任务工作台"),
                style="Compact.TButton",
            ).grid(row=1, column=3, sticky=tk.E, pady=3)

            self.add_form_label(parent, 2, "视觉模型", width=9, pady=3)
            combo = ttk.Combobox(
                parent,
                textvariable=self.model_var,
                values=[model_display_name(model) for model in self.model_values],
            )
            combo.grid(row=2, column=1, sticky=tk.EW, padx=(6, 8), pady=3)
            combo.bind("<<ComboboxSelected>>", lambda _event: self.update_model_hint())
            combo.bind("<FocusOut>", lambda _event: self.update_model_hint())
            self.model_combos.append(combo)
            ttk.Button(
                parent,
                text="刷新列表",
                command=lambda: self.test_api("任务工作台"),
                style="Compact.TButton",
            ).grid(row=2, column=2, sticky=tk.W, pady=3)
            ttk.Label(
                parent,
                textvariable=self.model_hint_var,
                wraplength=180,
                style="Muted.TLabel",
            ).grid(row=2, column=3, sticky=tk.W, padx=(8, 0), pady=3)

        def build_workflow_connection(self, parent, row):
            connection = ttk.Frame(parent, padding=8)
            connection.grid(row=row, column=0, sticky=tk.NSEW)
            connection.columnconfigure(0, weight=1)
            self.workflow_sections["server"] = connection

            route_choice = ttk.Frame(connection)
            route_choice.grid(row=0, column=0, sticky=tk.EW)
            self.add_choice_group(
                route_choice,
                self.connection_mode_var,
                [
                    ("public", "公网 API"),
                    ("private_direct", "私有直连"),
                    ("private_ssh", "SSH 跳板机"),
                ],
                self.on_connection_mode_change,
                columns=3,
                width=12,
                compact=True,
            )
            ttk.Label(
                connection,
                textvariable=self.connection_hint_var,
                wraplength=520,
                style="Muted.TLabel",
            ).grid(row=1, column=0, sticky=tk.W, pady=(0, 4))

            panel_host = ttk.Frame(connection)
            panel_host.grid(row=2, column=0, sticky=tk.EW)
            panel_host.columnconfigure(0, weight=1)

            public = ttk.Frame(panel_host)
            self.add_workflow_api_fields(public)
            examples = ttk.Frame(public)
            examples.grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(4, 0))
            ttk.Button(
                examples,
                text="DashScope",
                command=self.apply_dashscope_public,
                style="Compact.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                examples,
                text="OpenAI 兼容",
                command=self.apply_openai_public,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                examples,
                text="硅基流动",
                command=self.apply_siliconflow_public,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))

            direct = ttk.Frame(panel_host)
            self.add_workflow_api_fields(direct, "内网接口")
            direct_examples = ttk.Frame(direct)
            direct_examples.grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(4, 0))
            ttk.Button(
                direct_examples,
                text="内网示例",
                command=self.apply_private_direct_example,
                style="Compact.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                direct_examples,
                text="本机示例",
                command=self.apply_private_direct_local_example,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                direct_examples,
                text="标准化路径",
                command=self.apply_private_direct_standard_path,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))

            tunnel = ttk.Frame(panel_host)
            tunnel.columnconfigure(1, weight=1)
            tunnel.columnconfigure(3, weight=1)
            self.add_form_label(tunnel, 0, "SSH服务器", width=11)
            ttk.Entry(tunnel, textvariable=self.ssh_host_var).grid(
                row=0, column=1, sticky=tk.EW, padx=(6, 12), pady=5
            )
            self.add_form_label(tunnel, 0, "端口", column=2, width=7)
            ttk.Entry(tunnel, textvariable=self.ssh_port_var, width=8).grid(
                row=0, column=3, sticky=tk.W, padx=(6, 0), pady=5
            )
            self.add_form_label(tunnel, 1, "SSH用户名", width=11)
            ttk.Entry(tunnel, textvariable=self.ssh_user_var).grid(
                row=1, column=1, sticky=tk.EW, padx=(6, 12), pady=5
            )
            self.add_form_label(tunnel, 1, "本机端口", column=2, width=7)
            ttk.Entry(tunnel, textvariable=self.ssh_local_port_var, width=8).grid(
                row=1, column=3, sticky=tk.W, padx=(6, 0), pady=5
            )
            self.add_form_label(tunnel, 2, "模型服务地址", width=11)
            ttk.Entry(tunnel, textvariable=self.ssh_remote_host_var).grid(
                row=2, column=1, sticky=tk.EW, padx=(6, 12), pady=5
            )
            self.add_form_label(tunnel, 2, "服务端口", column=2, width=7)
            ttk.Entry(tunnel, textvariable=self.ssh_remote_port_var, width=8).grid(
                row=2, column=3, sticky=tk.W, padx=(6, 0), pady=5
            )
            self.add_form_label(tunnel, 3, "接口路径", width=11)
            ttk.Entry(tunnel, textvariable=self.ssh_api_path_var).grid(
                row=3, column=1, columnspan=3, sticky=tk.EW, padx=(6, 0), pady=5
            )
            self.add_form_label(tunnel, 4, "私钥文件", width=11)
            ttk.Entry(tunnel, textvariable=self.ssh_key_var).grid(
                row=4, column=1, sticky=tk.EW, padx=(6, 8), pady=5
            )
            ttk.Button(
                tunnel,
                text="选择私钥",
                command=self.choose_ssh_key,
                style="Compact.TButton",
            ).grid(row=4, column=2, sticky=tk.W, pady=5)
            ttk.Checkbutton(
                tunnel,
                text="密码登录时打开终端",
                variable=self.ssh_terminal_var,
            ).grid(row=4, column=3, sticky=tk.W, pady=5)
            self.add_form_label(tunnel, 5, "API 密钥", width=11)
            tunnel_key_entry = ttk.Entry(tunnel, textvariable=self.api_key_var, show="*")
            tunnel_key_entry.grid(row=5, column=1, sticky=tk.EW, padx=(6, 8), pady=5)
            self.api_key_entries.append(tunnel_key_entry)
            ttk.Checkbutton(
                tunnel,
                text="显示密钥",
                variable=self.show_key_var,
                command=self.toggle_api_key,
            ).grid(row=5, column=2, sticky=tk.W, pady=5)
            self.add_form_label(tunnel, 6, "视觉模型", width=11)
            tunnel_model = ttk.Combobox(
                tunnel,
                textvariable=self.model_var,
                values=[model_display_name(model) for model in self.model_values],
            )
            tunnel_model.grid(row=6, column=1, sticky=tk.EW, padx=(6, 8), pady=5)
            tunnel_model.bind("<<ComboboxSelected>>", lambda _event: self.update_model_hint())
            tunnel_model.bind("<FocusOut>", lambda _event: self.update_model_hint())
            self.model_combos.append(tunnel_model)
            tunnel_actions = ttk.Frame(tunnel)
            tunnel_actions.grid(row=6, column=2, columnspan=2, sticky=tk.W, pady=5)
            ttk.Button(
                tunnel_actions,
                text="生成并测试",
                command=lambda: (self.apply_tunnel_api_url(), self.test_api("任务工作台")),
                style="Compact.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                tunnel_actions,
                text="刷新模型",
                command=lambda: self.test_api("任务工作台"),
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))

            self.workflow_server_panels = {
                "public": public,
                "private_direct": direct,
                "private_ssh": tunnel,
            }
            self.show_connection_panel()

        def _legacy_build_analysis_tab(self):
            self.analysis_tab.columnconfigure(0, weight=1)
            self.analysis_tab.rowconfigure(0, weight=0)
            self.analysis_tab.rowconfigure(1, weight=0)
            self.analysis_tab.rowconfigure(2, weight=1)
            controls = self.create_analysis_controls_area()

            self.build_workflow_header(controls)

            self.build_workflow_connection(controls, 1)

            source = ttk.LabelFrame(controls, text="2. 视频来源", padding=10)
            source.grid(row=2, column=0, sticky=tk.EW)
            source.columnconfigure(0, weight=1)
            self.workflow_sections["source"] = source

            ttk.Label(
                source,
                text="先选要分析的来源。选本地视频就点“选择视频”；选实时视频流就粘贴摄像头或流媒体地址。",
                wraplength=900,
                style="Muted.TLabel",
            ).grid(row=0, column=0, sticky=tk.W, pady=(0, 8))

            source_choice = ttk.Frame(source)
            source_choice.grid(row=1, column=0, sticky=tk.W, pady=(0, 4))
            self.add_choice_group(
                source_choice,
                self.source_type_var,
                [
                    ("file", "本地视频文件\nMP4 / MOV / MKV 等"),
                    ("stream", "实时视频流\nRTSP / HLS / RTMP / SRT 等"),
                ],
                self.on_source_type_change,
                columns=2,
                width=28,
            )

            file_panel = ttk.Frame(source)
            file_panel.grid(row=2, column=0, sticky=tk.EW, pady=(8, 0))
            file_panel.columnconfigure(1, weight=1)
            self.add_form_label(file_panel, 0, "视频文件")
            self.video_entry = ttk.Entry(file_panel, textvariable=self.video_var)
            self.video_entry.grid(
                row=0, column=1, sticky=tk.EW, padx=(6, 8), pady=5
            )
            ttk.Button(file_panel, text="选择视频文件", command=self.choose_video, style="Tool.TButton").grid(
                row=0, column=2, sticky=tk.E, pady=5
            )
            ttk.Button(
                file_panel,
                text="打开预览",
                command=self.open_video_preview,
                style="Tool.TButton",
            ).grid(row=0, column=3, sticky=tk.E, padx=(8, 0), pady=5)
            ttk.Label(
                file_panel,
                text="适合离线视频文件，例如 mp4、mov、mkv。短视频建议每 5-10 秒抽 1 帧。",
                wraplength=850,
                style="Muted.TLabel",
            ).grid(row=1, column=0, columnspan=4, sticky=tk.W, pady=(2, 0))

            stream_panel = ttk.Frame(source)
            stream_panel.grid(row=2, column=0, sticky=tk.EW, pady=(8, 0))
            stream_panel.columnconfigure(1, weight=1)
            self.add_form_label(stream_panel, 0, "视频流地址")
            self.stream_url_entry = ttk.Entry(stream_panel, textvariable=self.stream_url_var)
            self.stream_url_entry.grid(
                row=0, column=1, sticky=tk.EW, padx=(6, 8), pady=5
            )
            stream_url_buttons = ttk.Frame(stream_panel)
            stream_url_buttons.grid(row=0, column=2, sticky=tk.E, pady=5)
            ttk.Button(
                stream_url_buttons,
                text="粘贴并识别",
                command=self.paste_and_detect_stream_url,
                style="Tool.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                stream_url_buttons,
                text="自动识别",
                command=self.auto_detect_stream_format,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                stream_url_buttons,
                text="校验地址",
                command=self.check_stream_url_format,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                stream_url_buttons,
                text="打开预览",
                command=self.open_video_preview,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Label(
                stream_panel,
                textvariable=self.stream_url_status_var,
                wraplength=850,
                style="Muted.TLabel",
            ).grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(2, 6))
            self.add_form_label(stream_panel, 2, "地址类型")
            stream_format_combo = ttk.Combobox(
                stream_panel,
                textvariable=self.stream_format_var,
                values=list(STREAM_FORMAT_PRESETS.keys()),
                state="readonly",
                width=34,
            )
            self.stream_format_combo = stream_format_combo
            stream_format_combo.grid(row=2, column=1, sticky=tk.W, padx=(6, 8), pady=5)
            stream_format_combo.bind("<<ComboboxSelected>>", self.on_stream_format_change)
            self.block_combobox_mousewheel(stream_format_combo)
            stream_buttons = ttk.Frame(stream_panel)
            stream_buttons.grid(row=2, column=2, sticky=tk.E, pady=5)
            ttk.Button(
                stream_buttons,
                text="填入所选格式示例",
                command=self.apply_stream_format_example,
                style="Tool.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                stream_buttons,
                text="RTSP 示例",
                command=self.apply_rtsp_example,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                stream_buttons,
                text="国标平台转流示例",
                command=self.apply_gb28181_example,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Label(
                stream_panel,
                textvariable=self.stream_format_hint_var,
                wraplength=850,
                style="Muted.TLabel",
            ).grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(2, 6))

            security = ttk.LabelFrame(stream_panel, text="安全接入 / 加密RTSP", padding=8)
            security.grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(2, 8))
            security.columnconfigure(1, weight=1)
            security.columnconfigure(3, weight=1)
            ttk.Label(
                security,
                text=(
                    "适用于标准 RTSP 账号密码、Token 和 RTSPS/TLS。"
                    "厂家私有加密码流不能直接破解，需要厂家 SDK、解密密钥，或由国标平台/视频网关转成标准播放流。"
                ),
                wraplength=850,
                style="Muted.TLabel",
            ).grid(row=0, column=0, columnspan=6, sticky=tk.W, pady=(0, 6))
            self.add_form_label(security, 1, "RTSP账号", width=10)
            ttk.Entry(security, textvariable=self.rtsp_username_var).grid(
                row=1, column=1, sticky=tk.EW, padx=(6, 12), pady=4
            )
            self.add_form_label(security, 1, "密码/Token", column=2, width=10)
            rtsp_password_entry = ttk.Entry(security, textvariable=self.rtsp_password_var, show="*")
            rtsp_password_entry.grid(row=1, column=3, sticky=tk.EW, padx=(6, 8), pady=4)
            self.rtsp_password_entries.append(rtsp_password_entry)
            ttk.Checkbutton(
                security,
                text="显示",
                variable=self.show_rtsp_password_var,
                command=self.toggle_rtsp_password,
            ).grid(row=1, column=4, sticky=tk.W, pady=4)
            ttk.Checkbutton(
                security,
                text="使用RTSPS/TLS",
                variable=self.rtsp_tls_var,
            ).grid(row=1, column=5, sticky=tk.W, padx=(12, 0), pady=4)
            ttk.Button(
                security,
                text="检查安全接入",
                command=self.check_rtsp_security_settings,
                style="Tool.TButton",
            ).grid(row=2, column=0, sticky=tk.W, pady=(6, 0))
            ttk.Label(
                security,
                textvariable=self.rtsp_security_status_var,
                wraplength=720,
                style="Muted.TLabel",
            ).grid(row=2, column=1, columnspan=5, sticky=tk.W, padx=(8, 0), pady=(6, 0))

            ttk.Label(
                stream_panel,
                text=(
                    "最简单用法：直接粘贴播放地址，点“自动识别”或“校验格式”。"
                    "不会判断格式时，不需要手动选择地址类型。"
                    "GB28181 不能直接填 gb28181:// 或 sip://，要填平台转出的播放流地址。"
                ),
                wraplength=850,
                style="Muted.TLabel",
            ).grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=(2, 0))

            self.source_panels = {
                "file": file_panel,
                "stream": stream_panel,
            }
            ttk.Label(source, textvariable=self.source_hint_var, wraplength=860).grid(
                row=3, column=0, sticky=tk.W, pady=(8, 0)
            )

            quick = ttk.LabelFrame(controls, text="3. 分析规则", padding=10)
            quick.grid(row=3, column=0, sticky=tk.EW, pady=(10, 8))
            self.workflow_sections["rules"] = quick
            for column in range(10):
                quick.columnconfigure(column, weight=0)
            quick.columnconfigure(7, weight=1)
            self.add_form_label(quick, 0, "抽帧方式", width=8, pady=2)
            capture_mode_combo = ttk.Combobox(
                quick,
                textvariable=self.capture_mode_var,
                values=CAPTURE_MODE_VALUES,
                state="readonly",
                width=12,
            )
            capture_mode_combo.grid(row=0, column=1, sticky=tk.W, padx=(6, 14), pady=2)
            self.block_combobox_mousewheel(capture_mode_combo)
            self.add_form_label(quick, 0, "间隔(秒)", column=2, width=8, pady=2)
            interval_entry = ttk.Entry(quick, textvariable=self.interval_var, width=8)
            interval_entry.grid(row=0, column=3, sticky=tk.W, padx=(6, 14), pady=2)
            self.add_form_label(quick, 0, "时间点", column=4, width=7, pady=2)
            point_entry = ttk.Entry(quick, textvariable=self.capture_point_var, width=12)
            point_entry.grid(row=0, column=5, sticky=tk.W, padx=(6, 14), pady=2)
            ttk.Label(
                quick,
                text="格式：秒数 / MM:SS / HH:MM:SS",
                style="Muted.TLabel",
            ).grid(row=0, column=6, columnspan=2, sticky=tk.W, pady=2)
            ttk.Button(
                quick,
                text="打开预览",
                command=self.open_video_preview,
                style="Tool.TButton",
            ).grid(row=0, column=8, columnspan=2, sticky=tk.W, pady=2)

            self.add_form_label(quick, 1, "开始", width=8, pady=2)
            start_entry = ttk.Entry(quick, textvariable=self.capture_start_var, width=12)
            start_entry.grid(row=1, column=1, sticky=tk.W, padx=(6, 14), pady=2)
            self.add_form_label(quick, 1, "结束", column=2, width=8, pady=2)
            end_entry = ttk.Entry(quick, textvariable=self.capture_end_var, width=12)
            end_entry.grid(row=1, column=3, sticky=tk.W, padx=(6, 14), pady=2)
            self.add_form_label(quick, 1, "输出上限", column=4, width=7, pady=2)
            ttk.Entry(quick, textvariable=self.tokens_var, width=8).grid(
                row=1, column=5, sticky=tk.W, padx=(6, 14), pady=2
            )
            self.add_form_label(quick, 1, "分析模板", column=6, width=8, pady=2)
            preset = ttk.Combobox(
                quick,
                textvariable=self.preset_var,
                values=self.prompt_template_values(),
                state="readonly",
                width=24,
            )
            self.preset_combo = preset
            preset.grid(row=1, column=7, sticky=tk.EW, padx=(6, 14), pady=2)
            preset.bind("<<ComboboxSelected>>", self.apply_prompt_preset)
            self.block_combobox_mousewheel(preset)
            ttk.Checkbutton(quick, text="完成后删除图片", variable=self.delete_var).grid(
                row=1,
                column=8,
                sticky=tk.W,
                padx=(0, 12),
                pady=2,
            )
            ttk.Checkbutton(quick, text="处理已有图片", variable=self.existing_var).grid(
                row=1,
                column=9,
                sticky=tk.W,
                pady=2,
            )
            self.capture_interval_widgets = getattr(self, "capture_interval_widgets", []) + [interval_entry]
            self.capture_point_widgets = getattr(self, "capture_point_widgets", []) + [point_entry]
            self.capture_range_widgets = getattr(self, "capture_range_widgets", []) + [start_entry, end_entry]

            prompt_frame = ttk.LabelFrame(controls, text="具体分析目标", padding=10)
            prompt_frame.grid(row=4, column=0, sticky=tk.EW, pady=(0, 8))
            self.workflow_sections["prompt"] = prompt_frame
            prompt_toolbar = ttk.Frame(prompt_frame)
            prompt_toolbar.pack(fill=tk.X, pady=(0, 6))
            prompt_toolbar.columnconfigure(0, weight=1)
            ttk.Label(
                prompt_toolbar,
                text="选择“无（自行填写）”后可直接输入自己的分析目标；填写模板名称并保存后，下次打开仍可使用。",
                style="Muted.TLabel",
                wraplength=560,
            ).grid(row=0, column=0, sticky=tk.W)
            prompt_buttons = ttk.Frame(prompt_toolbar)
            prompt_buttons.grid(row=0, column=1, sticky=tk.E, padx=(10, 0))
            prompt_name = ttk.Frame(prompt_toolbar)
            prompt_name.grid(row=1, column=0, sticky=tk.EW, pady=(8, 0))
            ttk.Label(prompt_name, text="我的模板名称", style="Form.TLabel").pack(side=tk.LEFT)
            self.prompt_template_name_entry = ttk.Entry(
                prompt_name,
                textvariable=self.prompt_template_name_var,
                width=28,
            )
            self.prompt_template_name_entry.pack(side=tk.LEFT, padx=(8, 10))
            ttk.Label(
                prompt_name,
                text="例如：工地安全巡检、国标摄像头异常告警",
                style="Muted.TLabel",
            ).pack(side=tk.LEFT)
            ttk.Button(
                prompt_buttons,
                text="保存为模板",
                command=self.save_current_prompt_template,
                style="Tool.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                prompt_buttons,
                text="删除自定义模板",
                command=self.delete_current_prompt_template,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                prompt_buttons,
                text="清空内容",
                command=self.clear_prompt_text,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            self.prompt_text = scrolledtext.ScrolledText(prompt_frame, height=5, wrap=tk.WORD)
            self.prompt_text.pack(fill=tk.X)
            self.style_text_widget(self.prompt_text)
            self.prompt_text.insert("1.0", self.config["prompt"])

            actions = ttk.Frame(self.analysis_tab)
            actions.grid(row=1, column=0, sticky=tk.EW, pady=(0, 8))
            actions.columnconfigure(0, weight=1)
            self.task_actions_frame = actions
            command_actions = ttk.Frame(actions)
            self.task_command_actions = command_actions
            command_actions.grid(row=0, column=0, sticky=tk.W)
            self.save_task_button = ttk.Button(
                command_actions,
                text="保存当前配置",
                command=self.save_settings,
                style="Tool.TButton",
            )
            self.save_task_button.pack(side=tk.LEFT)
            self.test_task_button = ttk.Button(
                command_actions,
                text="测试模型连接",
                command=lambda: self.test_api("任务工作台"),
                style="Tool.TButton",
            )
            self.test_task_button.pack(side=tk.LEFT, padx=(8, 0))
            self.start_task_button = ttk.Button(
                command_actions,
                text="▶ 开始分析",
                command=self.start_all,
                style="Accent.TButton",
            )
            self.start_task_button.pack(side=tk.LEFT, padx=(8, 0))
            self.listen_task_button = ttk.Button(
                command_actions,
                text="监听图片目录",
                command=self.start_monitoring,
                style="Tool.TButton",
            )
            self.listen_task_button.pack(side=tk.LEFT, padx=(8, 0))
            self.stop_task_button = ttk.Button(
                command_actions,
                text="■ 停止任务",
                command=self.stop_engine,
                style="Danger.TButton",
            )
            self.stop_task_button.pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                command_actions,
                text="打开结果目录",
                command=self.open_results_dir,
                style="Tool.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            self.task_stats_dashboard = self.build_stats_dashboard(actions)
            self.task_stats_dashboard.grid(row=0, column=1, sticky=tk.E, padx=(12, 0))
            actions.bind("<Configure>", self.layout_task_action_bar, add="+")

            result_frame = ttk.LabelFrame(self.analysis_tab, text="分析结果与运行消息", padding=10)
            result_frame.grid(row=2, column=0, sticky=tk.NSEW)
            self.result_text = scrolledtext.ScrolledText(
                result_frame,
                height=12,
                wrap=tk.NONE,
                state=tk.DISABLED,
                font=("Microsoft YaHei UI", 10),
            )
            self.result_text.pack(fill=tk.BOTH, expand=True)
            result_xscroll = ttk.Scrollbar(
                result_frame,
                orient=tk.HORIZONTAL,
                command=self.result_text.xview,
            )
            result_xscroll.pack(fill=tk.X)
            self.result_text.configure(xscrollcommand=result_xscroll.set)
            self.style_text_widget(self.result_text)
            self.analysis_tab.bind("<Configure>", self.schedule_resize_analysis_controls, add="+")
            self.root.after(120, self.resize_analysis_controls)
            self.update_action_states()

        def build_analysis_tab(self):
            self.analysis_tab.columnconfigure(0, weight=1)
            self.analysis_tab.rowconfigure(0, weight=0)
            self.analysis_tab.rowconfigure(1, weight=1)
            self.analysis_tab.grid_propagate(False)

            workspace = tk.PanedWindow(
                self.analysis_tab,
                orient=tk.HORIZONTAL,
                background=self.ui["panel_border"],
                borderwidth=0,
                sashwidth=4,
                sashrelief=tk.FLAT,
                showhandle=False,
                cursor="arrow",
            )
            workspace.grid(row=1, column=0, sticky=tk.NSEW)
            self.workbench_paned = workspace

            config_shell = ttk.Frame(
                workspace,
                padding=(0, 0, 4, 0),
                width=520,
                height=300,
            )
            config_shell.columnconfigure(0, weight=1)
            config_shell.rowconfigure(1, weight=1)
            config_shell.grid_propagate(False)
            output_shell = ttk.Frame(
                workspace,
                padding=(4, 0, 0, 0),
                width=520,
                height=300,
            )
            output_shell.columnconfigure(0, weight=1)
            output_shell.rowconfigure(1, weight=1)
            output_shell.grid_propagate(False)
            workspace.add(config_shell, minsize=480, stretch="always")
            workspace.add(output_shell, minsize=500, stretch="always")
            workspace.bind("<Configure>", self.clamp_workbench_split, add="+")
            workspace.bind("<ButtonRelease-1>", self.clamp_workbench_split, add="+")
            self.lock_paned_sash(workspace)

            config_header = tk.Frame(
                config_shell,
                background="#243241",
                padx=10,
                pady=7,
            )
            config_header.grid(row=0, column=0, sticky=tk.EW)
            tk.Label(
                config_header,
                text="任务配置",
                font=("Microsoft YaHei UI", 12, "bold"),
                foreground="#ffffff",
                background="#243241",
                anchor=tk.W,
            ).pack(side=tk.LEFT)
            tk.Label(
                config_header,
                text="按顺序完成三个页签",
                font=("Microsoft YaHei UI", 8),
                foreground="#cbd5e1",
                background="#243241",
            ).pack(side=tk.RIGHT)

            config_notebook = ttk.Notebook(
                config_shell,
                style="Workbench.TNotebook",
            )
            config_notebook.grid(row=1, column=0, sticky=tk.NSEW)
            self.workbench_notebook = config_notebook
            model_tab = ttk.Frame(config_notebook)
            source_tab = ttk.Frame(config_notebook)
            rules_tab = ttk.Frame(config_notebook)
            for tab in (model_tab, source_tab, rules_tab):
                tab.columnconfigure(0, weight=1)
                tab.rowconfigure(0, weight=1)
            config_notebook.add(model_tab, text="1  模型服务")
            config_notebook.add(source_tab, text="2  视频来源")
            config_notebook.add(rules_tab, text="3  分析规则")
            self.workbench_section_tabs = {
                "server": model_tab,
                "source": source_tab,
                "rules": rules_tab,
                "prompt": rules_tab,
            }

            self.build_workflow_connection(model_tab, 0)
            self.build_workbench_source_tab(source_tab)
            self.build_workbench_rules_tab(rules_tab)

            self.build_workflow_header(output_shell)
            output_paned = tk.PanedWindow(
                output_shell,
                orient=tk.VERTICAL,
                background=self.ui["panel_border"],
                borderwidth=0,
                sashwidth=4,
                sashrelief=tk.FLAT,
                showhandle=False,
                cursor="arrow",
            )
            output_paned.grid(row=1, column=0, sticky=tk.NSEW, pady=(7, 0))
            self.output_paned = output_paned
            output_paned.bind("<Configure>", self.clamp_output_split, add="+")
            output_paned.bind("<ButtonRelease-1>", self.clamp_output_split, add="+")
            self.lock_paned_sash(output_paned)

            preview_container = ttk.Frame(output_paned)
            preview_container.columnconfigure(0, weight=1)
            preview_container.rowconfigure(0, weight=1)
            preview_frame = ttk.LabelFrame(preview_container, text="视频预览与抽帧取时", padding=6)
            preview_frame.grid(row=0, column=0, sticky=tk.NSEW)
            preview_frame.columnconfigure(0, weight=1)
            preview_frame.rowconfigure(0, weight=1)
            self.video_preview_frame = preview_frame
            self.video_preview_content = ttk.Frame(preview_frame)
            self.video_preview_content.grid(row=0, column=0, sticky=tk.NSEW)
            self.video_preview_content.columnconfigure(0, weight=1)
            self.video_preview_content.rowconfigure(0, weight=1)
            self.show_video_preview_placeholder()

            result_frame = ttk.LabelFrame(output_paned, text="实时结果与运行消息", padding=7)
            result_frame.columnconfigure(0, weight=1)
            result_frame.rowconfigure(1, weight=1)
            result_toolbar = ttk.Frame(result_frame)
            result_toolbar.grid(row=0, column=0, sticky=tk.EW, pady=(0, 6))
            result_toolbar.columnconfigure(0, weight=1)
            ttk.Label(
                result_toolbar,
                textvariable=self.summary_var,
                style="Muted.TLabel",
            ).grid(row=0, column=0, sticky=tk.W)
            ttk.Button(
                result_toolbar,
                text="清空显示",
                command=self.clear_results,
                style="Compact.TButton",
            ).grid(row=0, column=1, padx=(6, 0))
            ttk.Button(
                result_toolbar,
                text="打开结果目录",
                command=self.open_results_dir,
                style="Compact.TButton",
            ).grid(row=0, column=2, padx=(6, 0))
            self.result_text = scrolledtext.ScrolledText(
                result_frame,
                height=14,
                wrap=tk.NONE,
                state=tk.DISABLED,
                font=("Microsoft YaHei UI", 10),
                spacing1=2,
                spacing3=4,
            )
            self.result_text.grid(row=1, column=0, sticky=tk.NSEW)
            result_xscroll = ttk.Scrollbar(
                result_frame,
                orient=tk.HORIZONTAL,
                command=self.result_text.xview,
            )
            result_xscroll.grid(row=2, column=0, sticky=tk.EW)
            self.result_text.configure(xscrollcommand=result_xscroll.set)
            self.style_text_widget(self.result_text)
            output_paned.add(preview_container, minsize=190, stretch="always")
            output_paned.add(result_frame, minsize=145, stretch="always")

            actions = ttk.Frame(self.analysis_tab)
            actions.grid(row=0, column=0, sticky=tk.EW, pady=(0, 7))
            actions.columnconfigure(0, weight=1)
            self.task_actions_frame = actions
            command_actions = ttk.Frame(actions)
            self.task_command_actions = command_actions
            command_actions.grid(row=0, column=0, sticky=tk.W)
            self.save_task_button = ttk.Button(
                command_actions,
                text="保存配置",
                command=self.save_settings,
                style="Compact.TButton",
            )
            self.save_task_button.pack(side=tk.LEFT)
            self.test_task_button = ttk.Button(
                command_actions,
                text="测试连接",
                command=lambda: self.test_api("任务工作台"),
                style="Compact.TButton",
            )
            self.test_task_button.pack(side=tk.LEFT, padx=(6, 0))
            self.start_task_button = ttk.Button(
                command_actions,
                text="▶ 开始分析",
                command=self.start_all,
                style="CompactAccent.TButton",
            )
            self.start_task_button.pack(side=tk.LEFT, padx=(6, 0))
            self.listen_task_button = ttk.Button(
                command_actions,
                text="监听目录",
                command=self.start_monitoring,
                style="Compact.TButton",
            )
            self.listen_task_button.pack(side=tk.LEFT, padx=(6, 0))
            self.stop_task_button = ttk.Button(
                command_actions,
                text="■ 停止任务",
                command=self.stop_engine,
                style="CompactDanger.TButton",
            )
            self.stop_task_button.pack(side=tk.LEFT, padx=(6, 0))
            ttk.Button(
                command_actions,
                text="结果目录",
                command=self.open_results_dir,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(6, 0))
            self.workbench_split_initialized = False
            self.output_split_initialized = False
            self.root.after(180, self.set_initial_workbench_split)
            self.root.after(220, self.set_initial_output_split)
            self.update_action_states()

        def set_initial_workbench_split(self):
            if (
                not hasattr(self, "workbench_paned")
                or getattr(self, "workbench_split_initialized", False)
            ):
                return
            try:
                width = self.workbench_paned.winfo_width()
                if width < 800:
                    self.root.after(120, self.set_initial_workbench_split)
                    return
                self.workbench_paned.sash_place(0, max(500, int(width * 0.43)), 0)
                self.workbench_split_initialized = True
                self.root.after_idle(self.clamp_workbench_split)
            except tk.TclError:
                pass

        def clamp_workbench_split(self, _event=None, force=False):
            if not hasattr(self, "workbench_paned"):
                return
            if (
                not force
                and _event is not None
                and "Configure" in str(getattr(_event, "type", ""))
                and self.is_root_resizing()
            ):
                return

            def apply_limits():
                try:
                    width = self.workbench_paned.winfo_width()
                    if width < 980:
                        return
                    position = self.workbench_paned.sash_coord(0)[0]
                    minimum_left = 500
                    maximum_left = width - 560
                    desired = max(500, int(width * 0.43)) if force else position
                    target = min(maximum_left, max(minimum_left, desired))
                    if abs(target - position) > 2:
                        self.workbench_paned.sash_place(0, target, 0)
                except (tk.TclError, IndexError):
                    pass

            try:
                self.root.after_idle(apply_limits)
            except tk.TclError:
                pass

        def set_initial_output_split(self):
            if (
                not hasattr(self, "output_paned")
                or getattr(self, "output_split_initialized", False)
            ):
                return
            try:
                height = self.output_paned.winfo_height()
                if height < 300:
                    self.root.after(120, self.set_initial_output_split)
                    return
                minimum_result = 170 if height >= 420 else 145
                minimum_preview = 240 if height >= 420 else 185
                desired = int(height * (0.58 if height >= 420 else 0.50))
                lower = min(minimum_preview, max(0, height - minimum_result))
                upper = max(lower, height - minimum_result)
                target = min(upper, max(lower, desired))
                self.output_paned.sash_place(0, 0, target)
                self.output_split_initialized = True
                self.root.after_idle(self.clamp_output_split)
            except (tk.TclError, IndexError):
                pass

        def clamp_output_split(self, _event=None, force=False):
            if not hasattr(self, "output_paned"):
                return
            if (
                not force
                and _event is not None
                and "Configure" in str(getattr(_event, "type", ""))
                and self.is_root_resizing()
            ):
                return

            def apply_limits():
                try:
                    height = self.output_paned.winfo_height()
                    if height < 280:
                        return
                    position = self.output_paned.sash_coord(0)[1]
                    minimum_preview = 230 if height >= 420 else 185
                    minimum_result = 165 if height >= 420 else 140
                    if minimum_preview + minimum_result > height:
                        minimum_result = max(90, min(minimum_result, int(height * 0.34)))
                        minimum_preview = max(
                            165,
                            min(minimum_preview, height - minimum_result),
                        )
                    lower = min(minimum_preview, max(0, height - minimum_result))
                    upper = max(lower, height - minimum_result)
                    desired = int(height * (0.58 if height >= 420 else 0.50)) if force else position
                    target = min(upper, max(lower, desired))
                    if abs(target - position) > 2:
                        self.output_paned.sash_place(0, 0, target)
                except (tk.TclError, IndexError):
                    pass

            try:
                self.root.after_idle(apply_limits)
            except tk.TclError:
                pass

        def build_workbench_source_tab(self, parent):
            source_shell = ttk.Frame(parent)
            source_shell.grid(row=0, column=0, sticky=tk.NSEW)
            source_shell.columnconfigure(0, weight=1)
            source_shell.rowconfigure(0, weight=1)

            source = self.create_scrollable_content(source_shell)
            source.columnconfigure(0, weight=1)
            self.workflow_sections["source"] = source
            self.workbench_source_content = source
            self.workbench_source_scroll_canvas = source.scroll_canvas

            source_choice = ttk.Frame(source)
            source_choice.grid(row=0, column=0, sticky=tk.EW, pady=(0, 5))
            self.add_choice_group(
                source_choice,
                self.source_type_var,
                [
                    ("file", "本地视频"),
                    ("stream", "实时视频流"),
                ],
                self.on_source_type_change,
                columns=2,
                width=16,
                compact=True,
            )

            panel_host = ttk.Frame(source)
            panel_host.grid(row=1, column=0, sticky=tk.EW)
            panel_host.columnconfigure(0, weight=1)

            file_panel = ttk.Frame(panel_host, padding=(2, 6))
            file_panel.grid(row=0, column=0, sticky=tk.EW)
            file_panel.columnconfigure(0, weight=1)
            ttk.Label(file_panel, text="视频文件", style="Form.TLabel").grid(
                row=0,
                column=0,
                sticky=tk.W,
            )
            self.video_entry = ttk.Entry(file_panel, textvariable=self.video_var)
            self.video_entry.grid(row=1, column=0, sticky=tk.EW, pady=(4, 4))
            file_actions = ttk.Frame(file_panel)
            file_actions.grid(row=2, column=0, sticky=tk.EW)
            file_actions.columnconfigure(0, weight=1, uniform="file_actions")
            file_actions.columnconfigure(1, weight=1, uniform="file_actions")
            ttk.Button(
                file_actions,
                text="选择文件",
                command=self.choose_video,
                style="CompactAccent.TButton",
            ).grid(row=0, column=0, sticky=tk.EW, padx=(0, 4))
            ttk.Button(
                file_actions,
                text="打开预览",
                command=self.open_video_preview,
                style="Compact.TButton",
            ).grid(row=0, column=1, sticky=tk.EW, padx=(4, 0))
            file_hint = ttk.Label(
                file_panel,
                text="支持 MP4、MOV、MKV、AVI、FLV、WMV、M4V。",
                style="Muted.TLabel",
            )
            file_hint.grid(row=3, column=0, sticky=tk.W, pady=(4, 0))
            self.bind_dynamic_wraplength(file_hint, file_panel, maximum=520)

            stream_panel = ttk.Frame(panel_host, padding=(2, 4))
            stream_panel.grid(row=0, column=0, sticky=tk.EW)
            stream_panel.columnconfigure(0, weight=1)
            ttk.Label(stream_panel, text="播放地址", style="Form.TLabel").grid(
                row=0,
                column=0,
                sticky=tk.W,
            )
            self.stream_url_entry = ttk.Entry(stream_panel, textvariable=self.stream_url_var)
            self.stream_url_entry.grid(row=1, column=0, sticky=tk.EW, pady=(4, 4))
            stream_url_buttons = ttk.Frame(stream_panel)
            stream_url_buttons.grid(row=2, column=0, sticky=tk.EW)
            for column in range(3):
                stream_url_buttons.columnconfigure(column, weight=1, uniform="stream_url_actions")
            ttk.Button(
                stream_url_buttons,
                text="粘贴识别",
                command=self.paste_and_detect_stream_url,
                style="Compact.TButton",
            ).grid(row=0, column=0, sticky=tk.EW, padx=(0, 4))
            ttk.Button(
                stream_url_buttons,
                text="校验格式",
                command=self.check_stream_url_format,
                style="Compact.TButton",
            ).grid(row=0, column=1, sticky=tk.EW, padx=4)
            ttk.Button(
                stream_url_buttons,
                text="打开预览",
                command=self.open_video_preview,
                style="Compact.TButton",
            ).grid(row=0, column=2, sticky=tk.EW, padx=(4, 0))
            stream_status = ttk.Label(
                stream_panel,
                textvariable=self.stream_url_status_var,
                style="Muted.TLabel",
            )
            stream_status.grid(row=3, column=0, sticky=tk.W, pady=(4, 7))
            self.bind_dynamic_wraplength(stream_status, stream_panel, maximum=520)

            ttk.Label(stream_panel, text="地址类型", style="Form.TLabel").grid(
                row=4,
                column=0,
                sticky=tk.W,
            )
            stream_format_combo = ttk.Combobox(
                stream_panel,
                textvariable=self.stream_format_var,
                values=list(STREAM_FORMAT_PRESETS.keys()),
                state="readonly",
                width=18,
            )
            self.stream_format_combo = stream_format_combo
            stream_format_combo.grid(row=5, column=0, sticky=tk.EW, pady=(4, 4))
            stream_format_combo.bind("<<ComboboxSelected>>", self.on_stream_format_change)
            self.block_combobox_mousewheel(stream_format_combo)
            format_actions = ttk.Frame(stream_panel)
            format_actions.grid(row=6, column=0, sticky=tk.EW)
            format_actions.columnconfigure(0, weight=1, uniform="format_actions")
            format_actions.columnconfigure(1, weight=1, uniform="format_actions")
            ttk.Button(
                format_actions,
                text="自动识别",
                command=self.auto_detect_stream_format,
                style="Compact.TButton",
            ).grid(row=0, column=0, sticky=tk.EW, padx=(0, 4))
            ttk.Button(
                format_actions,
                text="RTSP 示例",
                command=self.apply_rtsp_example,
                style="Compact.TButton",
            ).grid(row=0, column=1, sticky=tk.EW, padx=(4, 0))
            format_hint = ttk.Label(
                stream_panel,
                textvariable=self.stream_format_hint_var,
                style="Muted.TLabel",
            )
            format_hint.grid(row=7, column=0, sticky=tk.W, pady=(4, 6))
            self.bind_dynamic_wraplength(format_hint, stream_panel, maximum=520)

            security = ttk.LabelFrame(stream_panel, text="账号与安全接入", padding=6)
            security.grid(row=8, column=0, sticky=tk.EW, pady=(5, 3))
            security.columnconfigure(0, weight=1)
            ttk.Label(security, text="账号", style="Form.TLabel").grid(
                row=0,
                column=0,
                sticky=tk.W,
            )
            ttk.Entry(security, textvariable=self.rtsp_username_var).grid(
                row=1,
                column=0,
                sticky=tk.EW,
                pady=(3, 6),
            )
            ttk.Label(security, text="密码/Token", style="Form.TLabel").grid(
                row=2,
                column=0,
                sticky=tk.W,
            )
            password_row = ttk.Frame(security)
            password_row.grid(row=3, column=0, sticky=tk.EW, pady=(3, 6))
            password_row.columnconfigure(0, weight=1)
            rtsp_password_entry = ttk.Entry(
                password_row,
                textvariable=self.rtsp_password_var,
                show="*",
            )
            rtsp_password_entry.grid(row=0, column=0, sticky=tk.EW, padx=(0, 6))
            self.rtsp_password_entries.append(rtsp_password_entry)
            ttk.Checkbutton(
                password_row,
                text="显示",
                variable=self.show_rtsp_password_var,
                command=self.toggle_rtsp_password,
            ).grid(row=0, column=1, sticky=tk.W)
            security_actions = ttk.Frame(security)
            security_actions.grid(row=4, column=0, sticky=tk.EW)
            security_actions.columnconfigure(1, weight=1)
            ttk.Checkbutton(
                security_actions,
                text="RTSPS/TLS",
                variable=self.rtsp_tls_var,
            ).grid(row=0, column=0, sticky=tk.W)
            ttk.Button(
                security_actions,
                text="检查安全接入",
                command=self.check_rtsp_security_settings,
                style="Compact.TButton",
            ).grid(row=0, column=1, sticky=tk.E, padx=(8, 0))
            security_status = ttk.Label(
                security,
                textvariable=self.rtsp_security_status_var,
                style="Muted.TLabel",
            )
            security_status.grid(row=5, column=0, sticky=tk.W, pady=(5, 0))
            self.bind_dynamic_wraplength(security_status, security, maximum=520)

            source_note = ttk.Label(
                stream_panel,
                text=(
                    "标准 RTSP 认证、RTSPS/TLS 可以由软件调用 FFmpeg 完成；"
                    "厂家私有加密码流需要厂家 SDK、解密密钥或平台转成标准播放流。"
                ),
                style="Muted.TLabel",
            )
            source_note.grid(row=9, column=0, sticky=tk.W, pady=(4, 0))
            self.bind_dynamic_wraplength(source_note, stream_panel, maximum=520)

            self.source_panels = {"file": file_panel, "stream": stream_panel}
            self.show_source_panel()

        def build_workbench_rules_tab(self, parent):
            parent.columnconfigure(0, weight=1)
            parent.rowconfigure(0, weight=1)
            rules_shell = ttk.Frame(parent)
            rules_shell.grid(row=0, column=0, sticky=tk.NSEW)
            rules_shell.columnconfigure(0, weight=1)
            rules_shell.rowconfigure(0, weight=1)

            rules = self.create_scrollable_content(rules_shell)
            rules.columnconfigure(0, weight=1)
            rules.rowconfigure(3, weight=1)
            self.workbench_rules_scroll_canvas = rules.scroll_canvas
            self.workflow_sections["rules"] = rules
            self.workflow_sections["prompt"] = rules

            settings = ttk.LabelFrame(rules, text="抽帧参数", padding=(8, 6))
            settings.grid(row=0, column=0, sticky=tk.EW)
            settings.columnconfigure(1, weight=1)
            settings.columnconfigure(3, weight=1)
            self.workbench_rules_settings_frame = settings
            self.add_form_label(settings, 0, "抽帧方式", width=8, pady=3)
            capture_mode_combo = ttk.Combobox(
                settings,
                textvariable=self.capture_mode_var,
                values=CAPTURE_MODE_VALUES,
                state="readonly",
                width=14,
            )
            capture_mode_combo.grid(row=0, column=1, sticky=tk.W, padx=(4, 10), pady=3)
            self.block_combobox_mousewheel(capture_mode_combo)
            ttk.Button(
                settings,
                text="打开预览",
                command=self.open_video_preview,
                style="Compact.TButton",
            ).grid(row=0, column=2, columnspan=2, sticky=tk.E, padx=(8, 0), pady=3)
            self.add_form_label(settings, 1, "间隔秒", width=8, pady=3)
            interval_entry = ttk.Entry(settings, textvariable=self.interval_var, width=8)
            interval_entry.grid(row=1, column=1, sticky=tk.W, padx=(4, 10), pady=3)
            self.add_form_label(settings, 1, "输出上限", column=2, width=8, pady=3)
            ttk.Entry(settings, textvariable=self.tokens_var, width=7).grid(
                row=1, column=3, sticky=tk.W, padx=(4, 0), pady=3
            )
            self.add_form_label(settings, 2, "时间点", width=8, pady=3)
            point_entry = ttk.Entry(settings, textvariable=self.capture_point_var, width=12)
            point_entry.grid(row=2, column=1, sticky=tk.W, padx=(4, 10), pady=3)
            ttk.Label(
                settings,
                text="从右侧预览区取时后可自动写回。",
                style="Muted.TLabel",
            ).grid(row=2, column=2, columnspan=2, sticky=tk.W, padx=(4, 0), pady=3)
            self.add_form_label(settings, 3, "开始", width=8, pady=3)
            start_entry = ttk.Entry(settings, textvariable=self.capture_start_var, width=12)
            start_entry.grid(row=3, column=1, sticky=tk.W, padx=(4, 10), pady=3)
            self.add_form_label(settings, 3, "结束", column=2, width=8, pady=3)
            end_entry = ttk.Entry(settings, textvariable=self.capture_end_var, width=12)
            end_entry.grid(row=3, column=3, sticky=tk.W, padx=(4, 0), pady=3)
            self.add_form_label(settings, 4, "分析模板", width=8, pady=3)
            preset = ttk.Combobox(
                settings,
                textvariable=self.preset_var,
                values=self.prompt_template_values(),
                state="readonly",
                width=20,
            )
            self.preset_combo = preset
            preset.grid(row=4, column=1, columnspan=3, sticky=tk.EW, padx=(4, 0), pady=3)
            preset.bind("<<ComboboxSelected>>", self.apply_prompt_preset)
            self.block_combobox_mousewheel(preset)
            self.capture_interval_widgets = getattr(self, "capture_interval_widgets", []) + [interval_entry]
            self.capture_point_widgets = getattr(self, "capture_point_widgets", []) + [point_entry]
            self.capture_range_widgets = getattr(self, "capture_range_widgets", []) + [start_entry, end_entry]

            options = ttk.LabelFrame(rules, text="处理选项", padding=(8, 6))
            options.grid(row=1, column=0, sticky=tk.EW, pady=(6, 6))
            ttk.Checkbutton(
                options,
                text="完成后删除图片",
                variable=self.delete_var,
            ).pack(side=tk.LEFT)
            ttk.Checkbutton(
                options,
                text="处理已有图片",
                variable=self.existing_var,
            ).pack(side=tk.LEFT, padx=(12, 0))

            prompt_toolbar = ttk.LabelFrame(rules, text="自定义模板", padding=(8, 6))
            prompt_toolbar.grid(row=2, column=0, sticky=tk.EW, pady=(0, 6))
            prompt_toolbar.columnconfigure(1, weight=1)
            ttk.Label(prompt_toolbar, text="模板名称", style="Form.TLabel").grid(
                row=0, column=0, sticky=tk.W, pady=(0, 5)
            )
            self.prompt_template_name_entry = ttk.Entry(
                prompt_toolbar,
                textvariable=self.prompt_template_name_var,
            )
            self.prompt_template_name_entry.grid(
                row=0, column=1, columnspan=3, sticky=tk.EW, padx=(6, 0), pady=(0, 5)
            )
            ttk.Button(
                prompt_toolbar,
                text="保存模板",
                command=self.save_current_prompt_template,
                style="Compact.TButton",
            ).grid(row=1, column=1, sticky=tk.W)
            ttk.Button(
                prompt_toolbar,
                text="删除",
                command=self.delete_current_prompt_template,
                style="Compact.TButton",
            ).grid(row=1, column=2, sticky=tk.W, padx=(5, 0))
            ttk.Button(
                prompt_toolbar,
                text="清空",
                command=self.clear_prompt_text,
                style="Compact.TButton",
            ).grid(row=1, column=3, sticky=tk.W, padx=(5, 0))

            prompt_frame = ttk.LabelFrame(rules, text="具体分析目标", padding=6)
            prompt_frame.grid(row=3, column=0, sticky=tk.NSEW)
            prompt_frame.columnconfigure(0, weight=1)
            prompt_frame.rowconfigure(0, weight=1)
            self.workbench_rules_prompt_frame = prompt_frame
            self.prompt_text = scrolledtext.ScrolledText(
                prompt_frame,
                height=8,
                wrap=tk.WORD,
            )
            self.prompt_text.grid(row=0, column=0, sticky=tk.NSEW)
            self.style_text_widget(self.prompt_text)
            self.prompt_text.insert("1.0", self.config["prompt"])

        def show_video_preview_placeholder(self):
            if not hasattr(self, "video_preview_content"):
                return
            try:
                for child in self.video_preview_content.winfo_children():
                    child.destroy()
                shell = ttk.Frame(self.video_preview_content)
                shell.grid(row=0, column=0, sticky=tk.NSEW)
                shell.columnconfigure(0, weight=1)
                shell.rowconfigure(0, weight=1)

                video_shell = tk.Frame(
                    shell,
                    background="#0f1720",
                    highlightbackground="#253445",
                    highlightthickness=1,
                    height=82,
                )
                video_shell.grid(row=0, column=0, sticky=tk.NSEW)
                video_shell.grid_propagate(False)
                video_shell.columnconfigure(0, weight=1)
                video_shell.rowconfigure(0, weight=1)
                tk.Label(
                    video_shell,
                    text="未打开视频预览",
                    font=("Microsoft YaHei UI", 11, "bold"),
                    foreground="#dbe7f3",
                    background="#0f1720",
                    anchor=tk.CENTER,
                ).grid(row=0, column=0, sticky=tk.NSEW)

                actions = ttk.Frame(shell)
                actions.grid(row=1, column=0, sticky=tk.EW, pady=(7, 0))
                actions.columnconfigure(1, weight=1)
                ttk.Button(
                    actions,
                    text="打开预览",
                    command=self.open_video_preview,
                    style="CompactAccent.TButton",
                ).grid(row=0, column=0, sticky=tk.W)
                ttk.Label(
                    actions,
                    text="本地视频可拖动进度条取时，实时流显示打开后的当前时间。",
                    style="Muted.TLabel",
                ).grid(row=0, column=1, sticky=tk.W, padx=(10, 0))
            except tk.TclError:
                pass

        def layout_task_action_bar(self, event=None):
            if not hasattr(self, "task_stats_dashboard"):
                return
            width = int(event.width) if event is not None else self.task_actions_frame.winfo_width()
            compact = width < 900
            if self.is_root_resizing() and self.task_action_bar_compact is not None:
                return
            if self.task_action_bar_compact == compact:
                return
            self.task_action_bar_compact = compact
            self.task_stats_dashboard.grid_forget()
            if compact:
                self.task_stats_dashboard.grid(
                    row=1,
                    column=0,
                    sticky=tk.W,
                    pady=(6, 0),
                )
            else:
                self.task_stats_dashboard.grid(
                    row=0,
                    column=1,
                    sticky=tk.E,
                    padx=(12, 0),
                )

        def build_server_tab(self):
            page = self.server_tab
            page.columnconfigure(0, weight=1)
            page.rowconfigure(2, weight=1)

            status_band = tk.Frame(
                page,
                background=self.ui["surface"],
                highlightbackground=self.ui["panel_border"],
                highlightthickness=1,
                padx=10,
                pady=7,
            )
            status_band.grid(row=0, column=0, sticky=tk.EW)
            status_band.columnconfigure(0, weight=1)
            ttk.Label(
                status_band,
                textvariable=self.active_server_summary_var,
                wraplength=700,
                style="Route.TLabel",
            ).grid(row=0, column=0, sticky=tk.EW)
            ttk.Label(
                status_band,
                textvariable=self.server_save_state_var,
                wraplength=700,
                style="SaveState.TLabel",
            ).grid(row=1, column=0, sticky=tk.EW, pady=(3, 0))
            status_actions = ttk.Frame(status_band)
            status_actions.grid(row=0, column=1, rowspan=2, sticky=tk.E, padx=(12, 0))
            ttk.Button(
                status_actions,
                text="保存连接",
                command=self.save_settings,
                style="CompactAccent.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                status_actions,
                text="测试连接",
                command=lambda: self.test_api("模型连接"),
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(6, 0))

            mode_frame = ttk.LabelFrame(page, text="连接方式", padding=7)
            mode_frame.grid(row=1, column=0, sticky=tk.EW, pady=(7, 7))
            route_choice = ttk.Frame(mode_frame)
            route_choice.pack(side=tk.LEFT)
            self.add_choice_group(
                route_choice,
                self.connection_mode_var,
                [
                    ("public", "公网 API"),
                    ("private_direct", "内网直连"),
                    ("private_ssh", "SSH 跳板机"),
                ],
                self.on_connection_mode_change,
                columns=3,
                width=18,
                compact=True,
            )
            ttk.Label(
                mode_frame,
                textvariable=self.connection_hint_var,
                wraplength=560,
                style="Muted.TLabel",
            ).pack(side=tk.LEFT, padx=(12, 0))

            panel_host = ttk.Frame(page)
            panel_host.grid(row=2, column=0, sticky=tk.NSEW)

            public = ttk.LabelFrame(panel_host, text="公网 API", padding=10)
            public.columnconfigure(1, weight=1)
            ttk.Label(
                public,
                text="填写兼容 OpenAI Chat Completions 的接口地址、API 密钥和视觉模型。",
                wraplength=880,
                style="Muted.TLabel",
            ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 8))
            self.add_form_label(public, 1, "接口地址", pady=6)
            ttk.Entry(public, textvariable=self.api_url_var).grid(
                row=1, column=1, columnspan=3, sticky=tk.EW, padx=(6, 0), pady=6
            )
            self.add_api_key_row(public, 2, "API 密钥", "必填")
            self.add_model_row(public, 3)
            public_examples = ttk.Frame(public)
            public_examples.grid(row=4, column=1, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(4, 0))
            ttk.Button(
                public_examples,
                text="DashScope",
                command=self.apply_dashscope_public,
                style="Compact.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                public_examples,
                text="OpenAI 兼容",
                command=self.apply_openai_public,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                public_examples,
                text="硅基流动",
                command=self.apply_siliconflow_public,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                public_examples,
                text="测试并读取模型",
                command=lambda: self.test_api("公网 API"),
                style="CompactAccent.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            self.add_connection_status(public, 5)

            direct = ttk.LabelFrame(panel_host, text="内网直连", padding=10)
            direct.columnconfigure(1, weight=1)
            ttk.Label(
                direct,
                text="当前电脑能够直接访问内网或本机模型服务时使用，不启动 SSH。",
                wraplength=880,
                style="Muted.TLabel",
            ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 8))
            self.add_form_label(direct, 1, "内网接口地址", pady=6)
            ttk.Entry(direct, textvariable=self.api_url_var).grid(
                row=1, column=1, columnspan=3, sticky=tk.EW, padx=(6, 0), pady=6
            )
            ttk.Label(
                direct,
                text="示例：http://192.168.1.50:8000/v1/chat/completions",
                wraplength=820,
                style="Muted.TLabel",
            ).grid(row=2, column=1, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(0, 6))
            self.add_api_key_row(direct, 3, "API 密钥", "可空，按你的私有化服务要求填写")
            self.add_model_row(direct, 4)
            direct_examples = ttk.Frame(direct)
            direct_examples.grid(row=5, column=1, columnspan=3, sticky=tk.W, padx=(6, 0), pady=(4, 0))
            ttk.Button(
                direct_examples,
                text="内网示例",
                command=self.apply_private_direct_example,
                style="Compact.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                direct_examples,
                text="本机示例",
                command=self.apply_private_direct_local_example,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                direct_examples,
                text="整理接口路径",
                command=self.apply_private_direct_standard_path,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                direct_examples,
                text="测试并读取模型",
                command=lambda: self.test_api("内网直连"),
                style="CompactAccent.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            self.add_connection_status(direct, 6)

            tunnel = ttk.LabelFrame(panel_host, text="SSH 跳板机", padding=10)
            tunnel.columnconfigure(1, weight=1)
            tunnel.columnconfigure(3, weight=1)
            tunnel.columnconfigure(5, weight=1)
            ttk.Label(
                tunnel,
                text="通过 SSH 隧道访问内网模型服务。请填写跳板机、远端服务和本机转发端口。",
                style="Muted.TLabel",
            ).grid(row=0, column=0, columnspan=6, sticky=tk.W, pady=(0, 7))

            self.add_form_label(tunnel, 1, "SSH服务器", width=10)
            ttk.Entry(tunnel, textvariable=self.ssh_host_var).grid(
                row=1, column=1, sticky=tk.EW, padx=(6, 10), pady=4
            )
            self.add_form_label(tunnel, 1, "端口", column=2, width=6)
            ttk.Entry(tunnel, textvariable=self.ssh_port_var, width=8).grid(
                row=1, column=3, sticky=tk.W, padx=(6, 10), pady=4
            )
            self.add_form_label(tunnel, 1, "用户名", column=4, width=7)
            ttk.Entry(tunnel, textvariable=self.ssh_user_var).grid(
                row=1, column=5, sticky=tk.EW, padx=(6, 0), pady=4
            )

            self.add_form_label(tunnel, 2, "私钥文件", width=10)
            ttk.Entry(tunnel, textvariable=self.ssh_key_var).grid(
                row=2, column=1, columnspan=3, sticky=tk.EW, padx=(6, 8), pady=4
            )
            ttk.Button(tunnel, text="选择私钥", command=self.choose_ssh_key, style="Compact.TButton").grid(
                row=2, column=4, sticky=tk.W, pady=4
            )
            ttk.Checkbutton(
                tunnel,
                text="密码登录时打开终端",
                variable=self.ssh_terminal_var,
            ).grid(row=2, column=5, sticky=tk.W, padx=(6, 0), pady=4)

            self.add_form_label(tunnel, 3, "模型服务", width=10)
            ttk.Entry(tunnel, textvariable=self.ssh_remote_host_var).grid(
                row=3, column=1, sticky=tk.EW, padx=(6, 10), pady=4
            )
            self.add_form_label(tunnel, 3, "端口", column=2, width=6)
            ttk.Entry(tunnel, textvariable=self.ssh_remote_port_var, width=8).grid(
                row=3, column=3, sticky=tk.W, padx=(6, 10), pady=4
            )
            self.add_form_label(tunnel, 3, "接口路径", column=4, width=7)
            ttk.Entry(tunnel, textvariable=self.ssh_api_path_var).grid(
                row=3, column=5, sticky=tk.EW, padx=(6, 0), pady=4
            )

            self.add_form_label(tunnel, 4, "本机端口", width=10)
            ttk.Entry(tunnel, textvariable=self.ssh_local_port_var, width=8).grid(
                row=4, column=1, sticky=tk.W, padx=(6, 10), pady=4
            )
            self.add_form_label(tunnel, 4, "本机接口", column=2, width=8)
            ttk.Entry(tunnel, textvariable=self.api_url_var, state="readonly").grid(
                row=4, column=3, columnspan=3, sticky=tk.EW, padx=(6, 0), pady=4
            )

            self.add_form_label(tunnel, 5, "API 密钥", width=10)
            tunnel_key_entry = ttk.Entry(tunnel, textvariable=self.api_key_var, show="*")
            tunnel_key_entry.grid(row=5, column=1, sticky=tk.EW, padx=(6, 10), pady=4)
            self.api_key_entries.append(tunnel_key_entry)
            ttk.Checkbutton(
                tunnel,
                text="显示",
                variable=self.show_key_var,
                command=self.toggle_api_key,
            ).grid(row=5, column=2, sticky=tk.W, pady=4)
            self.add_form_label(tunnel, 5, "视觉模型", column=3, width=8)
            tunnel_model_combo = ttk.Combobox(
                tunnel,
                textvariable=self.model_var,
                values=[model_display_name(model) for model in self.model_values],
            )
            tunnel_model_combo.grid(row=5, column=4, columnspan=2, sticky=tk.EW, padx=(6, 0), pady=4)
            tunnel_model_combo.bind("<<ComboboxSelected>>", lambda _event: self.update_model_hint())
            tunnel_model_combo.bind("<FocusOut>", lambda _event: self.update_model_hint())
            self.model_combos.append(tunnel_model_combo)

            tunnel_actions = ttk.Frame(tunnel)
            tunnel_actions.grid(row=6, column=0, columnspan=6, sticky=tk.EW, pady=(8, 4))
            ttk.Button(
                tunnel_actions,
                text="生成本机接口",
                command=self.apply_tunnel_api_url,
                style="Compact.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                tunnel_actions,
                text="测试 SSH 并读取模型",
                command=lambda: self.test_api("SSH跳板机设置"),
                style="CompactAccent.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Label(
                tunnel_actions,
                textvariable=self.connection_var,
                style="Muted.TLabel",
                wraplength=680,
            ).pack(side=tk.LEFT, padx=(12, 0))

            self.server_panels = {
                "public": public,
                "private_direct": direct,
                "private_ssh": tunnel,
            }
            self.show_connection_panel()

        def build_advanced_tab(self):
            page = self.advanced_tab
            page.columnconfigure(0, weight=1)
            page.rowconfigure(1, weight=1)
            status_band = tk.Frame(
                page,
                background=self.ui["surface"],
                highlightbackground=self.ui["panel_border"],
                highlightthickness=1,
                padx=10,
                pady=7,
            )
            status_band.grid(row=0, column=0, sticky=tk.EW, pady=(0, 7))
            status_band.columnconfigure(0, weight=1)
            ttk.Label(
                status_band,
                textvariable=self.active_advanced_summary_var,
                wraplength=720,
                style="Route.TLabel",
            ).grid(row=0, column=0, sticky=tk.EW)
            ttk.Label(
                status_band,
                textvariable=self.advanced_save_state_var,
                wraplength=720,
                style="SaveState.TLabel",
            ).grid(row=1, column=0, sticky=tk.EW, pady=(3, 0))
            status_actions = ttk.Frame(status_band)
            status_actions.grid(row=0, column=1, rowspan=2, sticky=tk.E, padx=(12, 0))
            ttk.Button(
                status_actions,
                text="保存参数",
                command=self.save_settings,
                style="CompactAccent.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                status_actions,
                text="检查参数",
                command=lambda: self.review_industrial_readiness("参数设置"),
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(6, 0))

            settings_notebook = ttk.Notebook(page, style="Workbench.TNotebook")
            settings_notebook.grid(row=1, column=0, sticky=tk.NSEW)
            storage_tab = ttk.Frame(settings_notebook, padding=8)
            stream_tab = ttk.Frame(settings_notebook, padding=8)
            maintenance_tab = ttk.Frame(settings_notebook, padding=8)
            settings_notebook.add(storage_tab, text="存储与性能")
            settings_notebook.add(stream_tab, text="视频流稳定性")
            settings_notebook.add(maintenance_tab, text="运行维护")
            self.settings_notebook = settings_notebook

            advanced = ttk.LabelFrame(storage_tab, text="存储与处理", padding=10)
            advanced.pack(fill=tk.X)
            advanced.columnconfigure(1, weight=1)

            self.add_path_row(
                advanced,
                0,
                "图片目录",
                self.image_dir_var,
                self.choose_image_dir,
            )
            self.add_path_row(
                advanced,
                1,
                "结果目录",
                self.results_dir_var,
                self.choose_results_dir,
            )

            numbers = ttk.Frame(advanced)
            numbers.grid(row=2, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
            for column in range(8):
                numbers.columnconfigure(column, weight=0)
            numbers.columnconfigure(8, weight=1)
            self.add_small_entry(numbers, 0, "图片上限", self.size_var)
            self.add_small_entry(numbers, 2, "并发", self.concurrency_var)
            self.add_small_entry(numbers, 4, "重试", self.retries_var)
            self.add_small_entry(numbers, 6, "超时秒", self.timeout_var)

            temp = ttk.Frame(advanced)
            temp.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(10, 0))
            ttk.Label(temp, text="温度", width=8, anchor=tk.E, style="Form.TLabel").pack(side=tk.LEFT)
            ttk.Entry(temp, textvariable=self.temperature_var, width=10).pack(
                side=tk.LEFT,
                padx=(6, 16),
            )
            ttk.Label(
                temp,
                text="建议保持 0.2 到 0.5；并发一般保持 1，远端算力足够再调高。",
            ).pack(side=tk.LEFT)

            ffmpeg_perf = ttk.Frame(advanced)
            ffmpeg_perf.grid(row=4, column=0, columnspan=3, sticky=tk.EW, pady=(10, 0))
            ttk.Checkbutton(
                ffmpeg_perf,
                text="低CPU抽帧",
                variable=self.low_cpu_var,
            ).pack(side=tk.LEFT)
            ttk.Label(
                ffmpeg_perf,
                text="FFmpeg线程",
                width=11,
                anchor=tk.E,
                style="Form.TLabel",
            ).pack(side=tk.LEFT, padx=(18, 0))
            ttk.Entry(ffmpeg_perf, textvariable=self.ffmpeg_threads_var, width=8).pack(
                side=tk.LEFT,
                padx=(6, 14),
            )
            ttk.Label(
                ffmpeg_perf,
                text="低CPU模式会降低本地视频抽帧峰值占用；想更快可关闭或把线程调到 2-4。",
                style="Muted.TLabel",
            ).pack(side=tk.LEFT)

            stream_perf = ttk.LabelFrame(stream_tab, text="视频流处理策略", padding=10)
            stream_perf.pack(fill=tk.X)
            stream_row = ttk.Frame(stream_perf)
            stream_row.pack(fill=tk.X)
            ttk.Checkbutton(
                stream_row,
                text="实时流低延迟",
                variable=self.stream_low_latency_var,
            ).grid(row=0, column=0, sticky=tk.W)
            ttk.Checkbutton(
                stream_row,
                text="低延迟优先（队列满时丢旧帧）",
                variable=self.stream_drop_stale_var,
            ).grid(row=0, column=1, sticky=tk.W, padx=(18, 0))
            ttk.Checkbutton(
                stream_row,
                text="断线自动重连",
                variable=self.stream_auto_reconnect_var,
            ).grid(row=0, column=2, sticky=tk.W, padx=(18, 0))
            ttk.Checkbutton(
                stream_row,
                text="快速首帧",
                variable=self.stream_fast_first_frame_var,
            ).grid(row=0, column=3, sticky=tk.W, padx=(18, 0))
            ttk.Label(
                stream_row,
                text="最多排队帧",
                anchor=tk.E,
                style="Form.TLabel",
            ).grid(row=1, column=0, sticky=tk.W, pady=(10, 0))
            ttk.Entry(stream_row, textvariable=self.stream_max_pending_var, width=7).grid(
                row=1, column=1, sticky=tk.W, padx=(8, 18), pady=(10, 0)
            )
            ttk.Label(
                stream_row,
                text="重连次数",
                anchor=tk.E,
                style="Form.TLabel",
            ).grid(row=1, column=2, sticky=tk.W, pady=(10, 0))
            ttk.Entry(stream_row, textvariable=self.stream_reconnect_attempts_var, width=7).grid(
                row=1, column=3, sticky=tk.W, padx=(8, 0), pady=(10, 0)
            )
            probe_row = ttk.Frame(stream_perf)
            probe_row.pack(fill=tk.X, pady=(8, 0))
            ttk.Checkbutton(
                probe_row,
                text="启动前验证视频流",
                variable=self.stream_probe_var,
            ).grid(row=0, column=0, sticky=tk.W)
            ttk.Label(
                probe_row,
                text="验证超时秒",
                anchor=tk.E,
                style="Form.TLabel",
            ).grid(row=0, column=1, sticky=tk.W, padx=(18, 0))
            ttk.Entry(probe_row, textvariable=self.stream_probe_timeout_var, width=7).grid(
                row=0, column=2, sticky=tk.W, padx=(8, 18)
            )
            ttk.Label(
                probe_row,
                text="RTSP传输",
                anchor=tk.E,
                style="Form.TLabel",
            ).grid(row=0, column=3, sticky=tk.W)
            rtsp_transport_combo = ttk.Combobox(
                probe_row,
                textvariable=self.rtsp_transport_mode_var,
                values=RTSP_TRANSPORT_MODE_VALUES,
                state="readonly",
                width=24,
            )
            rtsp_transport_combo.grid(row=0, column=4, sticky=tk.W, padx=(8, 0))
            self.block_combobox_mousewheel(rtsp_transport_combo)
            ttk.Label(
                probe_row,
                text="默认自动：端口可连接后先试 TCP，再试 UDP；启动很慢的流可适当调大验证超时。",
                style="Muted.TLabel",
                wraplength=820,
            ).grid(row=1, column=0, columnspan=5, sticky=tk.W, pady=(8, 0))
            ttk.Label(
                stream_perf,
                text=(
                    "默认完整分析不丢帧：按设定抽到的帧都会进入服务器分析队列。"
                    "只有明确追求低延迟、不要求每帧都分析时，才勾选“低延迟优先”；"
                    "快速首帧会额外短时抓取 1 张图用于尽快开始分析，随后仍按设定间隔抽帧；"
                    "GB28181 请从国标平台输出 RTSP、HTTP-FLV、HLS 等播放地址。"
                ),
                style="Muted.TLabel",
                wraplength=900,
            ).pack(anchor=tk.W, pady=(8, 0))

            maintenance = ttk.LabelFrame(maintenance_tab, text="日志与任务文件", padding=10)
            maintenance.pack(fill=tk.X)
            retention_row = ttk.Frame(maintenance)
            retention_row.pack(fill=tk.X)
            ttk.Label(
                retention_row,
                text="日志保留天数",
                width=12,
                anchor=tk.E,
                style="Form.TLabel",
            ).pack(side=tk.LEFT)
            ttk.Entry(
                retention_row,
                textvariable=self.log_retention_var,
                width=8,
            ).pack(side=tk.LEFT, padx=(6, 12))
            ttk.Label(
                retention_row,
                text="范围 1-365 天；仅清理过期运行日志和崩溃报告，不删除分析结果。",
                style="Muted.TLabel",
            ).pack(side=tk.LEFT)
            maintenance_actions = ttk.Frame(maintenance)
            maintenance_actions.pack(fill=tk.X, pady=(10, 0))
            ttk.Button(
                maintenance_actions,
                text="打开日志目录",
                command=self.open_logs_dir,
                style="Compact.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                maintenance_actions,
                text="打开任务档案",
                command=self.open_session_records,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(
                maintenance_actions,
                text="运行本机检查",
                command=lambda: self.run_diagnostics("参数设置"),
                style="CompactAccent.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))

            update_box = ttk.LabelFrame(maintenance_tab, text="软件更新", padding=10)
            update_box.pack(fill=tk.X, pady=(10, 0))
            update_box.columnconfigure(1, weight=1)
            self.add_form_label(update_box, 0, "更新地址")
            ttk.Entry(update_box, textvariable=self.update_url_var).grid(
                row=0,
                column=1,
                sticky=tk.EW,
                padx=(6, 8),
                pady=5,
            )
            ttk.Button(
                update_box,
                text="选择本地文件",
                command=self.choose_update_json,
                style="Tool.TButton",
            ).grid(row=0, column=2, sticky=tk.E, pady=5)
            self.add_form_label(update_box, 1, "超时秒")
            ttk.Entry(update_box, textvariable=self.update_timeout_var, width=10).grid(
                row=1,
                column=1,
                sticky=tk.W,
                padx=(6, 8),
                pady=5,
            )
            update_actions = ttk.Frame(update_box)
            update_actions.grid(row=1, column=2, sticky=tk.E)
            ttk.Button(
                update_actions,
                text="检查更新",
                command=self.check_updates,
                style="CompactAccent.TButton",
            ).pack(side=tk.LEFT)
            ttk.Button(
                update_actions,
                text="打开下载目录",
                command=self.open_update_dir,
                style="Compact.TButton",
            ).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Label(
                update_box,
                text=(
                    "默认使用 Traffic Light 公网官网 update.json；本地联调可临时填写 "
                    f"{LOCAL_STATIC_UPDATE_URL}。下载的升级包只放入 updates 目录，不覆盖配置和结果。"
                ),
                style="Muted.TLabel",
                wraplength=900,
            ).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(8, 0))

        def build_log_tab(self):
            self.log_tab.columnconfigure(0, weight=1)
            self.log_tab.rowconfigure(2, weight=1)
            ttk.Label(
                self.log_tab,
                text="任务与系统日志",
                font=("Microsoft YaHei UI", 12, "bold"),
            ).grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
            actions = ttk.Frame(self.log_tab)
            actions.grid(row=1, column=0, sticky=tk.EW, pady=(0, 8))
            ttk.Button(actions, text="清空显示", command=self.clear_log, style="Compact.TButton").pack(side=tk.LEFT)
            ttk.Button(actions, text="运行本机检查", command=lambda: self.run_diagnostics("运行日志"), style="CompactAccent.TButton").pack(
                side=tk.LEFT,
                padx=(8, 0),
            )
            ttk.Button(actions, text="检查当前配置", command=lambda: self.review_industrial_readiness("运行日志"), style="Compact.TButton").pack(
                side=tk.LEFT,
                padx=(8, 0),
            )
            ttk.Button(actions, text="打开结果目录", command=self.open_results_dir, style="Compact.TButton").pack(
                side=tk.LEFT,
                padx=(8, 0),
            )
            ttk.Button(actions, text="打开日志目录", command=self.open_logs_dir, style="Compact.TButton").pack(
                side=tk.LEFT,
                padx=(8, 0),
            )

            log_body = ttk.Frame(self.log_tab)
            log_body.grid(row=2, column=0, sticky=tk.NSEW)
            log_body.columnconfigure(0, weight=1)
            log_body.rowconfigure(0, weight=1)
            self.log_text = scrolledtext.ScrolledText(
                log_body,
                wrap=tk.NONE,
                state=tk.DISABLED,
                font=("Consolas", 9),
            )
            self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
            log_xscroll = ttk.Scrollbar(
                log_body,
                orient=tk.HORIZONTAL,
                command=self.log_text.xview,
            )
            log_xscroll.grid(row=1, column=0, sticky=tk.EW)
            self.log_text.configure(xscrollcommand=log_xscroll.set)
            self.style_text_widget(self.log_text, mono=True)

        def add_form_label(self, parent, row, text, column=0, width=12, pady=5):
            ttk.Label(
                parent,
                text=text,
                width=width,
                anchor=tk.E,
                style="Form.TLabel",
            ).grid(row=row, column=column, sticky=tk.E, pady=pady)

        def add_path_row(self, parent, row, label, variable, command):
            self.add_form_label(parent, row, label)
            ttk.Entry(parent, textvariable=variable).grid(
                row=row,
                column=1,
                sticky=tk.EW,
                padx=(6, 8),
                pady=5,
            )
            ttk.Button(parent, text="+ 选择", command=command, style="Tool.TButton").grid(
                row=row,
                column=2,
                sticky=tk.E,
                pady=5,
            )

        def add_small_entry(self, parent, column, label, variable):
            self.add_form_label(parent, 0, label, column=column, width=8, pady=0)
            ttk.Entry(parent, textvariable=variable, width=10).grid(
                row=0,
                column=column + 1,
                sticky=tk.W,
                padx=(6, 18),
            )

        def add_choice_group(
            self,
            parent,
            variable,
            choices,
            command,
            columns=2,
            width=28,
            compact=False,
        ):
            widgets = []

            def refresh(*_args):
                for button, value in widgets:
                    selected = variable.get() == value
                    button.configure(
                        bg=self.ui["primary_soft"] if selected else self.ui["surface"],
                        fg=self.ui["primary_dark"] if selected else self.ui["text"],
                        activebackground="#dcecff" if selected else "#f3f7fb",
                        activeforeground=self.ui["primary_dark"],
                        relief=tk.SOLID,
                        bd=1,
                        highlightthickness=1,
                        highlightbackground=self.ui["primary"] if selected else self.ui["border"],
                        highlightcolor=self.ui["primary"] if selected else self.ui["border"],
                        selectcolor=self.ui["primary_soft"] if selected else self.ui["surface"],
                    )

            def on_choose():
                if command:
                    command()
                refresh()

            for index, (value, text) in enumerate(choices):
                button = tk.Radiobutton(
                    parent,
                    text=text,
                    variable=variable,
                    value=value,
                    command=on_choose,
                    indicatoron=False,
                    anchor=tk.W,
                    justify=tk.LEFT,
                    width=width,
                    padx=9 if compact else 14,
                    pady=6 if compact else 12,
                    cursor="hand2",
                    font=("Microsoft YaHei UI", 9 if compact else 10, "bold"),
                    bg=self.ui["surface"],
                    fg=self.ui["text"],
                    takefocus=0,
                    overrelief=tk.SOLID,
                    highlightthickness=1,
                    highlightbackground=self.ui["border"],
                    highlightcolor=self.ui["border"],
                    selectcolor=self.ui["surface"],
                )
                button.bind("<ButtonRelease-1>", lambda _event: self.root.after_idle(self.root.focus_set), add="+")
                row = index // columns
                column = index % columns
                button.grid(
                    row=row,
                    column=column,
                    sticky=tk.EW,
                    padx=(0, 6 if compact else 10),
                    pady=(0, 5 if compact else 8),
                )
                parent.columnconfigure(column, weight=1)
                widgets.append((button, value))

            self.choice_groups.append(widgets)
            variable.trace_add("write", refresh)
            refresh()

        def add_api_key_row(self, parent, row, label, hint):
            self.add_form_label(parent, row, label, pady=6)
            entry = ttk.Entry(parent, textvariable=self.api_key_var, show="*")
            entry.grid(row=row, column=1, sticky=tk.EW, padx=(6, 8), pady=6)
            self.api_key_entries.append(entry)
            self.api_key_entry = entry
            ttk.Checkbutton(
                parent,
                text="显示",
                variable=self.show_key_var,
                command=self.toggle_api_key,
            ).grid(row=row, column=2, sticky=tk.W, pady=6)
            ttk.Label(parent, text=hint, wraplength=260, style="Muted.TLabel").grid(
                row=row,
                column=3,
                sticky=tk.W,
                padx=(8, 0),
                pady=6,
            )

        def add_model_row(self, parent, row):
            self.add_form_label(parent, row, "模型", pady=6)
            combo = ttk.Combobox(
                parent,
                textvariable=self.model_var,
                values=[model_display_name(model) for model in self.model_values],
            )
            combo.grid(row=row, column=1, sticky=tk.EW, padx=(6, 8), pady=6)
            combo.bind("<<ComboboxSelected>>", lambda _event: self.update_model_hint())
            combo.bind("<FocusOut>", lambda _event: self.update_model_hint())
            self.model_combos.append(combo)
            self.model_combo = combo
            ttk.Button(parent, text="读取模型列表", command=lambda: self.test_api("模型设置区域"), style="Tool.TButton").grid(
                row=row,
                column=2,
                sticky=tk.W,
                pady=6,
            )
            ttk.Label(parent, textvariable=self.model_hint_var, wraplength=360, style="Muted.TLabel").grid(
                row=row,
                column=3,
                sticky=tk.W,
                padx=(8, 0),
                pady=6,
            )

        def update_model_hint(self):
            model = model_id_from_display(self.model_var.get())
            if not model:
                self.model_hint_var.set("读取模型后会自动标注哪些模型适合图像分析")
                return
            if looks_like_vision_model(model):
                self.model_hint_var.set(f"✓ 当前模型可用于图像分析：{model}")
            else:
                self.model_hint_var.set(
                    f"⚠ 当前模型名称不像图像模型：{model}。建议读取服务端列表后选择带 VL 或 vision 标识的模型。"
                )
            self.refresh_next_step()

        def add_connection_status(self, parent, row):
            self.add_form_label(parent, row, "连接状态", pady=6)
            ttk.Label(parent, textvariable=self.connection_var, wraplength=780).grid(
                row=row,
                column=1,
                columnspan=3,
                sticky=tk.W,
                padx=(6, 0),
                pady=6,
            )

        def show_connection_panel(self):
            for panel in self.server_panels.values():
                panel.pack_forget()
            for panel in self.workflow_server_panels.values():
                panel.pack_forget()
            mode = self.connection_mode_var.get()
            panel = self.server_panels.get(mode)
            if panel is not None:
                panel.pack(fill=tk.BOTH, expand=True)
            workflow_panel = self.workflow_server_panels.get(mode)
            if workflow_panel is not None:
                workflow_panel.pack(fill=tk.X)
            refresh = getattr(self.server_tab, "refresh_scroll_region", None)
            if refresh:
                self.server_tab.after_idle(refresh)
            if hasattr(self, "analysis_controls_canvas"):
                self.analysis_controls_canvas.after_idle(
                    lambda: self.analysis_controls_canvas.configure(
                        scrollregion=self.analysis_controls_canvas.bbox("all")
                    )
                )

        def show_source_panel(self):
            for panel in self.source_panels.values():
                panel.grid_remove()
            panel = self.source_panels.get(self.source_type_var.get())
            if panel is not None:
                panel.grid()
            refresh = getattr(getattr(self, "workbench_source_content", None), "refresh_scroll_region", None)
            if refresh:
                try:
                    self.workbench_source_content.after_idle(refresh)
                except tk.TclError:
                    pass

        def open_video_preview(self):
            config = self.collect_config()
            source_type = "stream" if self.source_type_var.get() == "stream" else "file"
            if source_type == "file":
                video_path = Path(self.video_var.get().strip())
                if not video_path.exists():
                    messagebox.showwarning("视频浏览", "请先选择一个存在的本地视频文件。")
                    self.reveal_workflow_section("source", getattr(self, "video_entry", None))
                    return
                source_value = str(video_path)
            else:
                stream_url = normalize_stream_url_for_user(self.stream_url_var.get().strip())
                if stream_url != self.stream_url_var.get().strip():
                    self.stream_url_var.set(stream_url)
                ok, message = validate_stream_url(stream_url)
                if not ok:
                    self.stream_url_status_var.set(message)
                    messagebox.showerror("视频浏览", message)
                    self.reveal_workflow_section("source", getattr(self, "stream_url_entry", None))
                    return
                config["stream_url"] = stream_url
                source_value = build_runtime_stream_url(stream_url, config)
            existing = getattr(self, "video_preview_window", None)
            if existing is not None:
                try:
                    if existing.window.winfo_exists():
                        existing.close()
                except tk.TclError:
                    pass
            self.video_preview_window = VideoPreviewWindow(self, source_type, source_value, config)

        def choose_video(self):
            path = filedialog.askopenfilename(
                title="选择视频文件",
                filetypes=[
                    ("视频文件", "*.mp4 *.mov *.mkv *.avi *.flv *.wmv *.m4v"),
                    ("所有文件", "*.*"),
                ],
            )
            if path:
                self.source_type_var.set("file")
                self.video_var.set(path)
                self.on_source_type_change()

        def apply_rtsp_example(self):
            self.source_type_var.set("stream")
            self.stream_format_var.set("RTSP 摄像头 / 国标平台转RTSP")
            self.stream_url_var.set("rtsp://摄像头IP:554/stream1")
            self.on_stream_format_change()
            self.on_source_type_change()

        def apply_gb28181_example(self):
            self.source_type_var.set("stream")
            self.stream_format_var.set("GB28181 国标平台转 HTTP-FLV")
            self.stream_url_var.set(STREAM_FORMAT_PRESETS[self.stream_format_var.get()]["example"])
            self.on_stream_format_change()
            self.on_source_type_change()

        def apply_stream_format_example(self):
            self.source_type_var.set("stream")
            selected = self.stream_format_var.get()
            preset = STREAM_FORMAT_PRESETS.get(selected)
            if not preset:
                selected = DEFAULT_STREAM_FORMAT
                preset = STREAM_FORMAT_PRESETS[selected]
                self.stream_format_var.set(selected)
            self.stream_url_var.set(preset["example"])
            self.on_stream_format_change()
            self.on_source_type_change()

        def paste_and_detect_stream_url(self):
            try:
                text = self.root.clipboard_get().strip()
            except tk.TclError:
                messagebox.showwarning("粘贴视频流地址", "剪贴板里没有可粘贴的文本。")
                return
            if not text:
                messagebox.showwarning("粘贴视频流地址", "剪贴板内容为空。")
                return
            self.source_type_var.set("stream")
            self.stream_url_var.set(text)
            self.auto_detect_stream_format(show_message=False)
            self.on_source_type_change()

        def auto_detect_stream_format(self, show_message=True):
            stream_url = self.stream_url_var.get().strip()
            if not stream_url:
                self.stream_format_var.set(AUTO_STREAM_FORMAT)
                self.on_stream_format_change()
                self.stream_url_status_var.set("请先粘贴视频流播放地址。")
                if show_message:
                    messagebox.showwarning("自动识别", "请先粘贴或填写视频流播放地址。")
                return False
            normalized_url = normalize_stream_url_for_user(stream_url)
            if normalized_url != stream_url:
                self.stream_url_var.set(normalized_url)
                stream_url = normalized_url
            detected = detect_stream_format(stream_url)
            self.stream_format_var.set(detected)
            self.on_stream_format_change()
            ok, message = validate_stream_url(stream_url)
            if ok:
                self.stream_url_status_var.set(
                    f"已自动识别：{describe_stream_url(stream_url)}。地址写法正常。"
                )
                if show_message:
                    messagebox.showinfo(
                        "自动识别",
                        f"已识别为：{describe_stream_url(stream_url)}。\n\n地址写法正常；实际能否播放，会在开始分析时由 FFmpeg 连接验证。",
                    )
                return True
            self.stream_url_status_var.set(message)
            if show_message:
                messagebox.showerror("视频流地址不正确", message)
            return False

        def check_stream_url_format(self):
            stream_url = self.stream_url_var.get().strip()
            if not stream_url:
                messagebox.showwarning("视频流地址", "请先填写实时视频流地址。")
                return
            normalized_url = normalize_stream_url_for_user(stream_url)
            if normalized_url != stream_url:
                self.stream_url_var.set(normalized_url)
                stream_url = normalized_url
            detected = detect_stream_format(stream_url)
            self.stream_format_var.set(detected)
            self.on_stream_format_change()
            ok, message = validate_stream_url(stream_url)
            if not ok:
                self.stream_url_status_var.set(message)
                messagebox.showerror("视频流地址不正确", message)
                return
            self.stream_url_status_var.set(
                f"格式正常，识别为：{describe_stream_url(stream_url)}。"
            )
            messagebox.showinfo(
                "视频流地址",
                f"格式正常，识别为：{describe_stream_url(stream_url)}。\n\n实际能否播放，还需要开始分析时由 FFmpeg 连接验证。",
            )

        def on_stream_format_change(self, _event=None):
            selected = self.stream_format_var.get()
            preset = STREAM_FORMAT_PRESETS.get(selected)
            if not preset:
                selected = DEFAULT_STREAM_FORMAT
                preset = STREAM_FORMAT_PRESETS[selected]
                self.stream_format_var.set(selected)
            self.stream_format_hint_var.set(f"当前格式：{selected}。{preset['hint']}")

        def on_stream_url_change(self, *_args):
            stream_url = self.stream_url_var.get().strip()
            if not stream_url:
                self.stream_url_status_var.set(
                    "支持 RTSP/RTSPS、HLS、HTTP-FLV、RTMP、SRT、RTP/UDP 播放地址。"
                )
                self.rtsp_security_status_var.set(
                    "RTSP 可填写账号、密码或 Token；加密通道勾选 RTSPS/TLS。"
                )
                self.refresh_next_step()
                return
            normalized_url = normalize_stream_url_for_user(stream_url)
            if normalized_url != stream_url:
                self.stream_url_status_var.set(f"地址缺少协议，可点击“自动识别”补全为：{normalized_url}")
                self.rtsp_security_status_var.set("点击“自动识别”补全协议后，再检查安全接入。")
                self.refresh_next_step()
                return
            ok, message = validate_stream_url(stream_url)
            if ok:
                self.stream_url_status_var.set(
                    f"自动判断：{describe_stream_url(stream_url)}。可以点击“校验格式”确认。"
                )
                if urlparse(stream_url).scheme.lower() in {"rtsp", "rtsps"}:
                    config = {
                        "stream_url": stream_url,
                        "rtsp_username": self.rtsp_username_var.get(),
                        "rtsp_password": self.rtsp_password_var.get(),
                        "rtsp_use_tls": self.rtsp_tls_var.get(),
                    }
                    self.rtsp_security_status_var.set(rtsp_security_summary(config, stream_url))
                else:
                    self.rtsp_security_status_var.set("当前不是 RTSP/RTSPS，安全接入设置不会参与拉流。")
            else:
                self.stream_url_status_var.set(message)
                self.rtsp_security_status_var.set("视频流地址格式未通过，先修正地址后再配置安全接入。")
            self.refresh_next_step()

        def on_source_type_change(self):
            if self.source_type_var.get() == "stream":
                self.source_hint_var.set(
                    "当前来源：实时视频流。支持 RTSP/RTSPS、RTMP/RTMPS、HLS(m3u8)、HTTP-FLV、RTP、UDP、TCP、SRT；"
                    "国标GB28181需填写平台转出的播放地址。"
                )
            else:
                self.source_hint_var.set("当前来源：本地视频文件。短视频可用 5-10 秒抽帧，长视频可用 30-60 秒抽帧。")
            self.show_source_panel()
            self.refresh_next_step()

        def selected_input_source(self):
            if self.source_type_var.get() == "stream":
                stream_url = self.stream_url_var.get().strip()
                if not stream_url:
                    self.reveal_workflow_section("source", self.stream_url_entry)
                    messagebox.showerror("缺少视频流地址", "请选择“本地视频文件”，或填写实时视频流地址。")
                    return None, False
                normalized_url = normalize_stream_url_for_user(stream_url)
                if normalized_url != stream_url:
                    self.stream_url_var.set(normalized_url)
                    stream_url = normalized_url
                detected = detect_stream_format(stream_url)
                if detected != self.stream_format_var.get():
                    self.stream_format_var.set(detected)
                    self.on_stream_format_change()
                ok, message = validate_stream_url(stream_url)
                if not ok:
                    self.reveal_workflow_section("source", self.stream_url_entry)
                    messagebox.showerror("视频流地址不正确", message)
                    return None, False
                self.stream_url_status_var.set(
                    f"准备使用：{describe_stream_url(stream_url)}。{rtsp_security_summary(self.collect_config(), stream_url) if urlparse(stream_url).scheme.lower() in {'rtsp', 'rtsps'} else ''}"
                )
                return {"type": "stream", "value": stream_url}, True

            video = self.video_var.get().strip()
            if not video:
                self.reveal_workflow_section("source", self.video_entry)
                ok = messagebox.askyesno("未选择视频", "没有选择视频文件，是否只启动监听？")
                if not ok:
                    return None, False
                return None, True
            if not Path(video).is_file():
                self.reveal_workflow_section("source", self.video_entry)
                messagebox.showerror("视频文件不存在", "当前选择的视频文件不存在，请重新选择。")
                return None, False
            return {"type": "file", "value": video}, True

        def choose_image_dir(self):
            path = filedialog.askdirectory(title="选择图片目录")
            if path:
                self.image_dir_var.set(path)

        def choose_results_dir(self):
            path = filedialog.askdirectory(title="选择结果目录")
            if path:
                self.results_dir_var.set(path)

        def choose_update_json(self):
            path = filedialog.askopenfilename(
                title="选择 update.json",
                initialdir=str(APP_DIR),
                filetypes=[("更新信息", "update.json *.json"), ("所有文件", "*.*")],
            )
            if path:
                self.update_url_var.set(path)

        def choose_ssh_key(self):
            path = filedialog.askopenfilename(
                title="选择 SSH 私钥文件",
                filetypes=[
                    ("私钥文件", "*.pem *.key *.ppk id_rsa id_ed25519 *.*"),
                    ("所有文件", "*.*"),
                ],
            )
            if path:
                self.ssh_key_var.set(path)

        def setup_ssh_traces(self):
            variables = [
                self.ssh_host_var,
                self.ssh_port_var,
                self.ssh_user_var,
                self.ssh_key_var,
                self.ssh_local_port_var,
                self.ssh_remote_host_var,
                self.ssh_remote_port_var,
                self.ssh_api_path_var,
            ]
            for variable in variables:
                variable.trace_add("write", lambda *_args: self.refresh_ssh_preview())

        def current_tunnel_config(self):
            raw_api_path = self.ssh_api_path_var.get().strip()
            return {
                "ssh_host": self.ssh_host_var.get().strip(),
                "ssh_port": int_from(self.ssh_port_var.get(), 22, 1, 65535),
                "ssh_user": self.ssh_user_var.get().strip(),
                "ssh_key_path": self.ssh_key_var.get().strip(),
                "ssh_open_terminal": bool(self.ssh_terminal_var.get()),
                "ssh_local_port": int_from(self.ssh_local_port_var.get(), 8080, 1, 65535),
                "ssh_remote_host": self.ssh_remote_host_var.get().strip(),
                "ssh_remote_port": int_from(
                    self.ssh_remote_port_var.get(),
                    8000,
                    1,
                    65535,
                ),
                "ssh_api_path": normalize_api_path(raw_api_path) if raw_api_path else "",
            }

        def refresh_ssh_preview(self):
            config = self.current_tunnel_config()
            parts = build_ssh_tunnel_parts(config, "ssh")
            self.ssh_preview_var.set(command_preview(parts) or "请先填写 SSH 和模型服务信息")

        def apply_tunnel_api_url(self, log=True):
            config = self.current_tunnel_config()
            config["ssh_api_path"] = normalize_api_path(config["ssh_api_path"])
            api_url = build_local_tunnel_api_url(
                config["ssh_local_port"],
                config["ssh_api_path"],
            )
            self.api_url_var.set(api_url)
            self.ssh_api_path_var.set(config["ssh_api_path"])
            self.connection_mode_var.set("private_ssh")
            self.tunnel_var.set(True)
            self.refresh_ssh_preview()
            self.connection_var.set("SSH 跳板机模式：将通过本机 localhost 接口访问私有化模型")
            self.show_connection_panel()
            if log:
                self.append_log(f"已生成本机接口地址：{api_url}")

        def apply_dashscope_public(self):
            self.connection_mode_var.set("public")
            self.tunnel_var.set(False)
            self.api_url_var.set(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
            )
            self.update_model_hint()
            self.connection_var.set("公网大模型模式：请填写 DashScope API Key 后读取模型")
            self.show_connection_panel()
            self.append_log("已切换为公网大模型模式，并填入 DashScope 兼容接口")

        def apply_openai_public(self):
            self.connection_mode_var.set("public")
            self.tunnel_var.set(False)
            self.api_url_var.set("https://api.openai.com/v1/chat/completions")
            self.update_model_hint()
            self.connection_var.set("公网大模型模式：请填写 OpenAI API Key 后读取模型确认权限")
            self.show_connection_panel()
            self.append_log("已切换为 OpenAI 兼容公网接口示例")

        def apply_siliconflow_public(self):
            self.connection_mode_var.set("public")
            self.tunnel_var.set(False)
            self.api_url_var.set("https://api.siliconflow.cn/v1/chat/completions")
            self.update_model_hint()
            self.connection_var.set("公网大模型模式：请填写硅基流动 API Key 后读取模型确认权限")
            self.show_connection_panel()
            self.append_log("已切换为硅基流动公网接口示例")

        def apply_private_direct_example(self):
            self.connection_mode_var.set("private_direct")
            self.tunnel_var.set(False)
            self.api_url_var.set("http://192.168.1.50:8000/v1/chat/completions")
            self.update_model_hint()
            self.connection_var.set("私有化直连模式：请改成实际内网接口后读取模型")
            self.show_connection_panel()
            self.append_log("已切换为私有化直连模式，并填入内网接口示例")

        def apply_private_direct_local_example(self):
            self.connection_mode_var.set("private_direct")
            self.tunnel_var.set(False)
            self.api_url_var.set("http://127.0.0.1:8000/v1/chat/completions")
            self.connection_var.set("私有化直连模式：适合本机直接运行模型服务")
            self.show_connection_panel()
            self.append_log("已切换为私有化直连模式，并填入本机 8000 示例")

        def apply_private_direct_standard_path(self):
            self.connection_mode_var.set("private_direct")
            self.tunnel_var.set(False)
            current = normalize_chat_url(self.api_url_var.get().strip())
            parsed = urlparse(current)
            if parsed.scheme and parsed.netloc:
                self.api_url_var.set(
                    parsed._replace(path="/v1/chat/completions", query="", fragment="").geturl()
                )
            else:
                self.api_url_var.set("http://192.168.1.50:8000/v1/chat/completions")
            self.connection_var.set("私有化直连模式：已整理为标准 /v1/chat/completions 路径")
            self.show_connection_panel()
            self.append_log("已把私有化直连接口整理为标准 /v1/chat/completions 路径")

        def on_tunnel_toggle(self):
            if self.tunnel_var.get():
                self.apply_tunnel_api_url()

        def on_connection_mode_change(self, initial=False):
            mode = self.connection_mode_var.get()
            self.show_connection_panel()
            if mode == "private_ssh":
                self.tunnel_var.set(True)
                self.connection_hint_var.set("当前路线：通过 SSH 跳板机连接私有化模型。只需要填写跳板机和远端模型服务信息。")
                self.connection_var.set("私有化部署模式：请填写 SSH 和模型服务信息后测试")
            elif mode == "private_direct":
                self.tunnel_var.set(False)
                self.connection_hint_var.set("当前路线：直接连接私有化模型。只填写本机能访问到的内网接口，不会启动 SSH。")
                current_api = self.api_url_var.get().strip()
                current_host = urlparse(current_api).hostname or ""
                tunnel_config = self.current_tunnel_config()
                remote_host = tunnel_config.get("ssh_remote_host")
                remote_port = tunnel_config.get("ssh_remote_port")
                api_path = tunnel_config.get("ssh_api_path")
                if is_local_api(current_api) and remote_host:
                    self.api_url_var.set(
                        f"http://{remote_host}:{remote_port}{normalize_api_path(api_path)}"
                    )
                self.connection_var.set("私有化直连模式：填写本机可直接访问的内网模型接口后测试")
            else:
                self.tunnel_var.set(False)
                self.connection_hint_var.set("当前路线：直接调用公网大模型。请填写公网接口和 API Key，不会启动 SSH。")
                current_api = self.api_url_var.get().strip()
                current_host = urlparse(current_api).hostname
                if is_private_network_host(current_host):
                    self.api_url_var.set("")
                    self.update_model_hint()
                self.connection_var.set("公网大模型模式：请填写公网接口和 API Key 后测试")
            self.show_connection_panel()

        def apply_prompt_preset(self, _event=None):
            name = self.preset_var.get()
            if name in PROMPT_PRESETS:
                prompt = PROMPT_PRESETS.get(name, "")
                custom_name = ""
            else:
                custom_name = custom_prompt_name_from_display(name)
                prompt = self.custom_prompt_templates.get(custom_name)
            if prompt is None:
                return
            self.prompt_text.delete("1.0", tk.END)
            if prompt:
                self.prompt_text.insert("1.0", prompt)
            if name == PROMPT_NONE_NAME:
                self.append_log("已选择无模板，可自行填写分析目标")
                self.prompt_template_name_var.set("")
            elif custom_name:
                self.prompt_template_name_var.set(custom_name)
            else:
                self.append_log(f"已应用提示词模板：{name}")
                self.prompt_template_name_var.set("")

        def clear_prompt_text(self):
            self.preset_var.set(PROMPT_NONE_NAME)
            self.prompt_template_name_var.set("")
            self.prompt_text.delete("1.0", tk.END)
            self.append_log("已清空提示词，请自行填写分析目标")

        def save_current_prompt_template(self):
            prompt = self.prompt_text.get("1.0", tk.END).strip()
            if not prompt:
                messagebox.showwarning("保存模板", "提示词为空。请先填写分析目标，再保存为模板。")
                return
            current_name = custom_prompt_name_from_display(self.preset_var.get())
            name = self.prompt_template_name_var.get().strip() or current_name
            name = name.strip()
            if not name:
                messagebox.showwarning("保存模板", "请先在“我的模板名称”里填写一个容易识别的名称。")
                try:
                    self.prompt_template_name_entry.focus_set()
                except tk.TclError:
                    pass
                return
            if name.startswith(CUSTOM_PROMPT_PREFIX):
                name = name[len(CUSTOM_PROMPT_PREFIX) :].strip()
            if name in PROMPT_PRESETS:
                messagebox.showwarning("保存模板", "这个名称是系统内置模板名称，请换一个名称。")
                return
            name = name[:40]
            display_name = custom_prompt_display_name(name)
            if name in self.custom_prompt_templates:
                if not messagebox.askyesno("覆盖模板", f"“{display_name}”已存在，是否覆盖？"):
                    return
            self.custom_prompt_templates[name] = prompt
            self.preset_var.set(display_name)
            self.prompt_template_name_var.set(name)
            self.refresh_prompt_template_combo()
            if not self.save_config_or_notice(self.collect_config(), "保存自定义提示词模板"):
                return
            self.mark_analysis_config_saved("已保存：自定义提示词模板和开始分析配置已写入本机配置")
            self.append_log(f"已保存自定义提示词模板：{display_name}")
            messagebox.showinfo("保存模板", f"已保存：{display_name}")

        def delete_current_prompt_template(self):
            display_name = self.preset_var.get()
            custom_name = custom_prompt_name_from_display(display_name)
            if not custom_name:
                messagebox.showinfo("删除模板", "系统内置模板不能删除。请选择“我的模板”后再删除。")
                return
            if custom_name not in self.custom_prompt_templates:
                self.refresh_prompt_template_combo()
                self.show_notice("删除模板", "没有找到这个自定义模板，列表已刷新。", "warning")
                return
            if not messagebox.askyesno("删除模板", f"确认删除“{display_name}”？"):
                return
            self.custom_prompt_templates.pop(custom_name, None)
            self.preset_var.set(PROMPT_NONE_NAME)
            self.prompt_template_name_var.set("")
            self.refresh_prompt_template_combo()
            if not self.save_config_or_notice(self.collect_config(), "删除自定义提示词模板"):
                return
            self.mark_analysis_config_saved("已保存：自定义提示词模板已删除，开始分析配置已更新")
            self.append_log(f"已删除自定义提示词模板：{display_name}")

        def toggle_api_key(self):
            show = "" if self.show_key_var.get() else "*"
            for entry in self.api_key_entries:
                entry.configure(show=show)

        def toggle_rtsp_password(self):
            show = "" if self.show_rtsp_password_var.get() else "*"
            for entry in self.rtsp_password_entries:
                entry.configure(show=show)

        def check_rtsp_security_settings(self):
            stream_url = normalize_stream_url_for_user(self.stream_url_var.get().strip())
            if not stream_url:
                messagebox.showwarning("安全接入", "请先填写 RTSP/RTSPS 视频流地址。")
                return
            parsed = urlparse(stream_url)
            if parsed.scheme.lower() not in {"rtsp", "rtsps"}:
                detail = "当前地址不是 RTSP/RTSPS，安全接入设置不会参与拉流。"
                self.rtsp_security_status_var.set(detail)
                messagebox.showinfo("安全接入", detail)
                return
            config = self.collect_config()
            runtime_url = build_runtime_stream_url(stream_url, config)
            detail = rtsp_security_summary(config, stream_url)
            self.rtsp_security_status_var.set(detail)
            messagebox.showinfo(
                "安全接入检查",
                (
                    f"{detail}\n\n"
                    f"实际交给 FFmpeg 的播放地址：{masked_stream_url(runtime_url)}\n\n"
                    "说明：软件能处理标准 RTSP Basic/Digest 鉴权、账号密码/Token 和 RTSPS/TLS；"
                    "如果摄像头输出的是厂家私有加密码流，必须通过厂家 SDK、解密密钥或平台转码获得标准播放地址。"
                ),
            )

        def collect_config(self):
            # 从各页控件汇总出一份完整配置，启动、保存和测试都使用同一数据来源。
            config = load_config()
            tunnel_config = self.current_tunnel_config()
            connection_mode = self.connection_mode_var.get()
            use_tunnel = connection_mode == "private_ssh"
            api_url = normalize_chat_url(self.api_url_var.get().strip())
            api_key = sanitize_api_key(self.api_key_var.get(), api_url)
            stream_url = self.stream_url_var.get().strip()
            if self.source_type_var.get() == "stream":
                stream_url = normalize_stream_url_for_user(stream_url)
                if stream_url != self.stream_url_var.get().strip():
                    self.stream_url_var.set(stream_url)
            if use_tunnel:
                api_url = build_local_tunnel_api_url(
                    tunnel_config["ssh_local_port"],
                    tunnel_config["ssh_api_path"],
                )
                self.api_url_var.set(api_url)
                self.ssh_api_path_var.set(tunnel_config["ssh_api_path"])
                self.tunnel_var.set(True)
            else:
                self.tunnel_var.set(False)
            ssh_command = command_preview(build_ssh_tunnel_parts(tunnel_config, "ssh"))
            config.update(
                {
                    "image_dir": self.image_dir_var.get().strip() or str(DEFAULT_IMAGE_DIR),
                    "results_dir": self.results_dir_var.get().strip()
                    or str(DEFAULT_RESULTS_DIR),
                    "source_type": self.source_type_var.get(),
                    "video_file": self.video_var.get().strip(),
                    "stream_url": stream_url,
                    "stream_format": self.stream_format_var.get()
                    if self.stream_format_var.get() in STREAM_FORMAT_PRESETS
                    else DEFAULT_STREAM_FORMAT,
                    "rtsp_username": self.rtsp_username_var.get().strip(),
                    "rtsp_password": self.rtsp_password_var.get(),
                    "rtsp_use_tls": bool(self.rtsp_tls_var.get()),
                    "connection_mode": connection_mode,
                    "api_url": api_url,
                    "api_key": api_key,
                    "model": model_id_from_display(self.model_var.get()),
                    "prompt": self.prompt_text.get("1.0", tk.END).strip(),
                    "selected_prompt_preset": self.preset_var.get(),
                    "custom_prompt_templates": sanitize_custom_prompt_templates(
                        self.custom_prompt_templates
                    ),
                    "frame_interval": int_from(self.interval_var.get(), 10, 1, 3600),
                    "capture_mode": capture_mode_value(self.capture_mode_var.get()),
                    "capture_point_time": self.capture_point_var.get().strip(),
                    "capture_start_time": self.capture_start_var.get().strip(),
                    "capture_end_time": self.capture_end_var.get().strip(),
                    "ffmpeg_low_cpu": bool(self.low_cpu_var.get()),
                    "ffmpeg_threads": int_from(self.ffmpeg_threads_var.get(), 1, 1, 8),
                    "stream_low_latency": bool(self.stream_low_latency_var.get()),
                    "stream_fast_first_frame": bool(self.stream_fast_first_frame_var.get()),
                    "stream_drop_stale_frames": bool(self.stream_drop_stale_var.get()),
                    "stream_max_pending_frames": int_from(
                        self.stream_max_pending_var.get(), 3, 1, 50
                    ),
                    "stream_auto_reconnect": bool(self.stream_auto_reconnect_var.get()),
                    "stream_reconnect_attempts": int_from(
                        self.stream_reconnect_attempts_var.get(), 5, 0, 30
                    ),
                    "stream_probe_before_start": bool(self.stream_probe_var.get()),
                    "stream_probe_timeout": int_from(self.stream_probe_timeout_var.get(), 12, 3, 60),
                    "stream_open_timeout": max(
                        30,
                        int_from(self.stream_probe_timeout_var.get(), 12, 3, 60),
                    ),
                    "stream_first_frame_timeout": max(
                        60,
                        int_from(self.stream_probe_timeout_var.get(), 12, 3, 60) * 3,
                    ),
                    "rtsp_transport_mode": rtsp_transport_mode_value(self.rtsp_transport_mode_var.get()),
                    "max_image_size": int_from(self.size_var.get(), 1080, 128, 4096),
                    "max_tokens": int_from(self.tokens_var.get(), 1500, 1, 32768),
                    "concurrency": int_from(self.concurrency_var.get(), 1, 1, 8),
                    "max_retries": int_from(self.retries_var.get(), 3, 1, 10),
                    "request_timeout": int_from(self.timeout_var.get(), 60, 5, 600),
                    "temperature": float_from(self.temperature_var.get(), 0.3, 0, 2),
                    "log_retention_days": int_from(
                        self.log_retention_var.get(),
                        30,
                        1,
                        365,
                    ),
                    "update_url": self.update_url_var.get().strip() or DEFAULT_UPDATE_INFO,
                    "update_timeout": int_from(
                        self.update_timeout_var.get(),
                        8,
                        3,
                        60,
                    ),
                    "delete_processed": bool(self.delete_var.get()),
                    "process_existing": bool(self.existing_var.get()),
                    "auto_start_tunnel": use_tunnel,
                    "ssh_tunnel_command": ssh_command,
                    **tunnel_config,
                }
            )
            return config

        def validate_config(self, config, check_model=True, check_prompt=True):
            # 校验按阻断程度排列，先处理必然失败的地址和凭据，再给出可继续的风险提醒。
            ok, message_or_url = validate_api_url(
                config.get("api_url", ""),
                config.get("connection_mode", "public"),
            )
            if not ok:
                self.reveal_workflow_section("server")
                messagebox.showerror("接口地址不正确", message_or_url)
                return False
            config["api_url"] = message_or_url
            self.api_url_var.set(message_or_url)
            raw_api_key = self.api_key_var.get().strip()
            if api_key_looks_like_url(raw_api_key, message_or_url):
                self.api_key_var.set("")
                config["api_key"] = ""
                focus_entry = self.api_key_entries[0] if self.api_key_entries else None
                self.reveal_workflow_section("server", focus_entry)
                messagebox.showerror(
                    "API 密钥填写错误",
                    "API 密钥输入框中不能填写接口地址。\n\n"
                    "软件已清空错误内容，请填写服务商提供的真实 API Key 后重新测试。",
                )
                return False
            if config.get("connection_mode") == "public" and not config["api_key"]:
                self.reveal_workflow_section("server")
                messagebox.showerror("缺少密钥", "公网大模型通常需要 API 密钥，请填写 API Key。")
                return False
            if check_prompt and not str(config.get("prompt", "")).strip():
                self.reveal_workflow_section("prompt", self.prompt_text)
                messagebox.showerror(
                    "缺少分析目标",
                    "提示词为空。请选择一个提示词模板，或选择“无（自行填写）”后输入自己的分析目标。",
                )
                return False
            if config.get("source_type") == "stream":
                stream_url = normalize_stream_url_for_user(config.get("stream_url", ""))
                parsed_stream = urlparse(stream_url)
                if parsed_stream.scheme.lower() in {"rtsp", "rtsps"}:
                    if config.get("rtsp_password") and not str(config.get("rtsp_username", "")).strip():
                        self.reveal_workflow_section("source")
                        messagebox.showerror(
                            "RTSP账号不完整",
                            "已填写 RTSP 密码/Token，但账号为空。请填写摄像头或平台账号；如果账号密码已写在 URL 中，可清空这里的密码框。",
                        )
                        return False
                    if config.get("rtsp_use_tls") and parsed_stream.scheme.lower() != "rtsps":
                        if not messagebox.askyesno(
                            "启用 RTSPS/TLS",
                            (
                                "你已勾选 RTSPS/TLS。软件会把运行时播放地址按 rtsps:// 方式交给 FFmpeg。\n\n"
                                "只有摄像头、国标平台或流媒体网关明确支持 RTSPS/TLS 时才能成功。"
                                "如果现场只是普通账号密码认证，请取消该选项。\n\n"
                                "是否继续使用 RTSPS/TLS？"
                            ),
                        ):
                            self.reveal_workflow_section("source")
                            return False
            if config.get("connection_mode") == "private_ssh":
                missing = []
                labels = {
                    "ssh_host": "SSH服务器",
                    "ssh_user": "用户名",
                    "ssh_remote_host": "模型服务地址",
                }
                for key, label in labels.items():
                    if not str(config.get(key, "")).strip():
                        missing.append(label)
                if missing:
                    self.reveal_workflow_section("server")
                    messagebox.showerror(
                        "SSH 信息不完整",
                        "请填写：" + "、".join(missing),
                    )
                    return False
                key_path = str(config.get("ssh_key_path", "")).strip()
                if key_path.lower().endswith(".pub"):
                    self.reveal_workflow_section("server")
                    messagebox.showerror(
                        "私钥文件选择错误",
                        "你选择的是 .pub 公钥文件。SSH 登录通常需要选择没有 .pub 后缀的私钥文件，例如 id_ed25519 或 id_rsa。",
                    )
                    return False
                if key_path and not Path(key_path).exists():
                    self.reveal_workflow_section("server")
                    messagebox.showerror(
                        "私钥文件不存在",
                        "你填写的私钥文件路径不存在。请重新选择私钥文件，或者留空改用密码登录。",
                    )
                    return False
            if check_model and not looks_like_vision_model(config.get("model", "")):
                if not messagebox.askyesno(
                    "模型可能不支持图像分析",
                    (
                        "当前选择的模型名称不像图像分析模型。\n\n"
                        f"当前模型：{config.get('model', '')}\n\n"
                        "建议先点击“读取模型”，选择带 VL 或 vision 标识的模型。\n"
                        "如果你确认这是私有化部署的视觉模型，可以继续。是否继续？"
                    ),
                ):
                    self.reveal_workflow_section("server")
                    return False
            return True

        def save_config_or_notice(self, config, context="保存配置"):
            return save_config_with_notice(config, self.show_notice, context)

        def save_settings(self):
            config = self.collect_config()
            if not self.validate_config(config):
                return False
            if not self.save_config_or_notice(config, "保存设置"):
                return False
            self.config = config
            self.append_log("设置已保存")
            self.status_var.set("设置已保存")
            self.connection_var.set("设置已保存，可以继续测试连接或开始分析")
            self.mark_server_config_saved("已保存：下一轮任务和下次打开软件都会使用当前服务器配置")
            self.mark_analysis_config_saved("已保存：下一轮任务和下次打开软件都会使用当前开始分析配置")
            self.mark_advanced_config_saved("已保存：下一轮任务和下次打开软件都会使用当前任务参数")
            return True

        def make_engine(self):
            config = self.collect_config()
            if not self.validate_config(config):
                return None
            if not self.save_config_or_notice(config, "启动任务前保存配置"):
                return None
            self.config = config
            self.mark_server_config_saved("已自动保存：本次任务和下一轮任务都会使用当前服务器配置")
            self.mark_analysis_config_saved("已自动保存：本次任务和下一轮任务都会使用当前开始分析配置")
            self.mark_advanced_config_saved("已自动保存：本次任务和下一轮任务都会使用当前任务参数")
            self.append_log(f"本次任务服务器配置：{self.server_route_log_text(config)}")
            self.append_log(f"本次任务开始分析配置：{self.analysis_config_log_text(config)}")
            self.append_log(f"本次任务参数：{self.advanced_config_log_text(config)}")
            return AnalysisEngine(config, self.events)

        def start_all(self):
            # 启动前锁住互斥操作，耗时的预检与建链放到后台线程，避免冻结窗口。
            if self.starting:
                self.action_blocked("任务正在启动", "任务正在启动，请等待启动完成后再操作。")
                return
            if self.testing:
                self.action_blocked("接口测试进行中", "接口测试正在进行，请等待测试完成后再开始分析。")
                return
            if self.engine and self.engine.running:
                self.action_blocked("任务已在运行", "当前任务已经在运行。如需重新开始，请先点击“停止”。")
                return

            self.engine = self.make_engine()
            if not self.engine:
                return

            input_source, should_start = self.selected_input_source()
            if not should_start:
                return

            self.starting = True
            self.status_var.set("启动中")
            self.clear_results()

            def worker(engine, source):
                try:
                    engine.start_all(source)
                finally:
                    self.safe_after(0, self.on_start_worker_finished, engine)

            threading.Thread(
                target=worker,
                args=(self.engine, input_source),
                daemon=True,
            ).start()

        def start_monitoring(self):
            # 仅监听模式不启动 FFmpeg，适合外部程序已经在持续写入图片的场景。
            if self.starting:
                self.action_blocked("任务正在启动", "任务正在启动，请等待启动完成后再操作。")
                return
            if self.testing:
                self.action_blocked("接口测试进行中", "接口测试正在进行，请等待测试完成后再启动监听。")
                return
            if self.engine and self.engine.running:
                self.action_blocked("监听已在运行", "监听或分析任务已经在运行。如需重新启动，请先点击“停止”。")
                return

            self.engine = self.make_engine()
            if not self.engine:
                return

            self.starting = True
            self.status_var.set("启动监听中")

            def worker(engine):
                try:
                    engine.start_monitoring()
                finally:
                    self.safe_after(0, self.on_start_worker_finished, engine)

            threading.Thread(target=worker, args=(self.engine,), daemon=True).start()

        def on_start_worker_finished(self, engine):
            self.starting = False
            if self.engine is not engine:
                return
            if engine.running:
                if self.status_var.get() in {"启动中", "启动监听中"}:
                    self.status_var.set("运行中")
            elif self.status_var.get() in {"启动中", "启动监听中", "运行中"}:
                self.status_var.set("启动失败")

        def stop_engine(self):
            # 停止和文件清理可能耗时，仍由后台线程执行，完成后再更新按钮和状态灯。
            if not self.engine:
                self.status_var.set("就绪")
                self.action_blocked("没有正在运行的任务", "当前没有正在运行的分析或监听任务，不需要停止。")
                return
            if self.stopping:
                self.action_blocked("正在停止", "软件正在停止当前任务，请稍等。")
                return
            self.stopping = True
            self.status_var.set("正在停止")
            threading.Thread(target=self._stop_engine_worker, daemon=True).start()

        def _stop_engine_worker(self):
            try:
                self.engine.stop(cleanup_unprocessed=True)
            finally:
                self.safe_after(0, self.on_stop_worker_finished)

        def on_stop_worker_finished(self):
            self.stopping = False
            self.starting = False
            self.status_var.set("已停止")

        def test_api(self, origin="当前界面"):
            # 接口测试与正式任务互斥，防止测试过程中配置或 SSH 隧道被另一流程改动。
            if self.testing:
                self.action_blocked("接口测试进行中", "接口测试正在进行，请等待当前测试完成。")
                return
            if self.starting or (self.engine and self.engine.running):
                self.action_blocked("任务运行中", "任务运行中不能测试接口。请先点击“停止”，确认任务停止后再测试。")
                return
            config = self.collect_config()
            if not self.validate_config(config, check_model=False, check_prompt=False):
                return
            if not self.save_config_or_notice(config, "测试连接前保存配置"):
                return
            self.config = config
            self.mark_server_config_saved("已保存：正在测试当前服务器路线")
            self.mark_analysis_config_saved("已保存：测试前已同步当前开始分析配置")
            self.mark_advanced_config_saved("已保存：测试前已同步当前任务参数")

            self.testing = True
            self.connection_var.set("正在测试...")
            self.status_var.set("正在测试")
            route_text = self.server_route_log_text(config)
            self.append_log(f"{origin}开始测试接口：{route_text}")

            def worker():
                temp_engine = None
                try:
                    if config.get("connection_mode") == "private_ssh":
                        self.queue_ui_event(
                            "log",
                            {"text": "正在临时打开 SSH 隧道用于测试..."},
                        )
                        temp_engine = AnalysisEngine(
                            config,
                            self.events,
                            record_session=False,
                        )
                        if not temp_engine.start_ssh_tunnel():
                            self.safe_after(0, self.on_api_test_failed, "SSH 隧道启动失败", origin, route_text)
                            return
                        ok, ready_message = wait_for_api_ready(config["api_url"], timeout=15)
                        if not ok:
                            self.safe_after(0, self.on_api_test_failed, ready_message, origin, route_text)
                            return

                    ok, message = api_host_is_reachable(config["api_url"])
                    if not ok:
                        self.safe_after(0, self.on_api_test_failed, message, origin, route_text)
                        return

                    models, model_message = fetch_available_models(
                        config["api_url"],
                        config.get("api_key", ""),
                    )
                    self.safe_after(
                        0,
                        self.on_api_models_loaded,
                        config["api_url"],
                        message,
                        models,
                        model_message,
                        origin,
                        route_text,
                    )
                finally:
                    if temp_engine is not None:
                        temp_engine.stop()
                    self.safe_after(0, self.on_api_test_finished)

            threading.Thread(target=worker, daemon=True).start()

        def on_api_test_finished(self):
            self.testing = False
            if self.status_var.get() == "正在测试":
                self.status_var.set("就绪")

        def run_diagnostics(self, origin="当前界面"):
            # 诊断收集过程放在线程中，最终汇总弹窗通过 safe_after 返回主线程。
            config = self.collect_config()
            source_for_diag = runtime_input_source(self.selected_input_source_for_diagnostic(), config)
            diagnostic_items = []
            self.append_log(f"{origin}开始本机诊断")

            def log_line(message, level="INFO"):
                diagnostic_items.append((level, message))
                timestamp = datetime.now().strftime("%H:%M:%S")
                self.queue_ui_event(
                    "log",
                    {
                        "text": f"{timestamp} [{level}] 诊断：{message}",
                    },
                )

            def worker():
                try:
                    mode_names = {
                        "public": "公网大模型",
                        "private_ssh": "SSH 跳板机私有化",
                        "private_direct": "私有化直连",
                    }
                    log_line(f"当前连接路线：{mode_names.get(config.get('connection_mode'), '未知')}")
                    log_line(f"当前服务器配置：{self.server_route_log_text(config)}")
                    log_line(f"当前开始分析配置：{self.analysis_config_log_text(config)}")
                    log_line(f"当前任务参数：{self.advanced_config_log_text(config)}")
                    for tool_name, label in (("ffmpeg", "FFmpeg"), ("ssh", "OpenSSH")):
                        tool_path = find_tool(tool_name)
                        if tool_path:
                            log_line(f"{label} 可用：{tool_path}")
                        elif tool_name == "ssh" and config.get("connection_mode") != "private_ssh":
                            log_line(f"{label} 未找到；当前路线不需要 SSH", "WARN")
                        else:
                            log_line(f"{label} 未找到，请检查发布包 tools 文件夹或系统 PATH", "ERROR")
                    ffmpeg_ok, ffmpeg_message = ffmpeg_smoke_test()
                    log_line(ffmpeg_message, "INFO" if ffmpeg_ok else "ERROR")

                    for label, key in (("图片目录", "image_dir"), ("结果目录", "results_dir")):
                        path = Path(config.get(key) or DEFAULT_CONFIG[key])
                        try:
                            path.mkdir(parents=True, exist_ok=True)
                            test_file = path / ".write_test.tmp"
                            test_file.write_text("ok", encoding="utf-8")
                            test_file.unlink(missing_ok=True)
                            log_line(f"{label}可读写：{path}")
                        except OSError as exc:
                            log_line(f"{label}不可写：{path}，{exc}", "ERROR")

                    ok, api_or_message = validate_api_url(
                        config.get("api_url", ""),
                        config.get("connection_mode", "public"),
                    )
                    if not ok:
                        log_line(api_or_message, "ERROR")
                        return
                    log_line(f"接口地址格式正常：{api_or_message}")

                    if config.get("connection_mode") == "private_ssh":
                        parts = build_ssh_tunnel_parts(config, "ssh")
                        if parts:
                            log_line("SSH 隧道信息完整，可生成启动命令")
                        else:
                            log_line("SSH 隧道信息不完整，请补齐 SSH服务器、用户名、模型服务地址和端口", "ERROR")
                        log_line("SSH 路线需要点击“测试SSH并读取模型”才能真正验证远端服务")
                    else:
                        ok, message = api_host_is_reachable(api_or_message, timeout=2)
                        if ok:
                            log_line(f"接口主机可连接：{message}")
                        else:
                            log_line(f"接口主机暂不可连接：{message}", "WARN")

                    if source_for_diag and source_for_diag["type"] == "stream":
                        ok, message = validate_stream_url(source_for_diag["value"])
                        if ok:
                            log_line(
                                f"实时视频流地址格式正常，识别为：{describe_stream_url(source_for_diag['value'])}；"
                                f"运行地址：{masked_stream_url(source_for_diag['value'])}；"
                                "能否播放还需要 FFmpeg 实际连接验证"
                            )
                            if urlparse(source_for_diag["value"]).scheme.lower() in {"rtsp", "rtsps"}:
                                log_line(rtsp_security_summary(config, source_for_diag["value"]))
                                port_ok, port_message = rtsp_control_port_is_reachable(
                                    source_for_diag["value"],
                                    timeout=4,
                                )
                                if port_ok:
                                    log_line(f"RTSP 控制端口可连接：{port_message}")
                                else:
                                    log_line(
                                        f"RTSP 控制端口不可连接：{port_message}；"
                                        "请先解决网络、VPN、端口映射或摄像头访问权限问题",
                                        "ERROR",
                                    )
                        else:
                            log_line(message, "ERROR")
                    elif source_for_diag and source_for_diag["type"] == "file":
                        if Path(source_for_diag["value"]).exists():
                            log_line(f"本地视频文件存在：{source_for_diag['value']}")
                        else:
                            log_line(f"本地视频文件不存在：{source_for_diag['value']}", "ERROR")
                    else:
                        log_line("未选择视频输入来源；可以只监听图片目录，或在开始页选择视频/视频流", "WARN")

                    log_line("本机诊断完成")
                finally:
                    self.safe_after(0, self.show_diagnostics_result, origin, list(diagnostic_items))

            threading.Thread(target=worker, daemon=True).start()

        def update_base_dirs(self):
            return [APP_DIR, RESOURCE_DIR, RELEASE_SITE_DIR, RESOURCE_RELEASE_SITE_DIR]

        def check_updates(self):
            if self.update_checking or self.update_downloading:
                self.action_blocked("更新进行中", "当前已有更新检查或下载任务，请等待完成后再操作。")
                return
            config = self.collect_config()
            if not self.save_config_or_notice(config, "保存更新设置"):
                return
            self.config = config
            update_url = config.get("update_url") or DEFAULT_UPDATE_INFO
            timeout = int_from(config.get("update_timeout"), 8, 3, 60)
            self.update_checking = True
            self.status_var.set("正在检查更新")
            self.append_log(f"开始检查更新：当前版本 {APP_VERSION}，地址 {update_url}")

            def worker():
                try:
                    info = check_for_update(
                        update_url,
                        APP_VERSION,
                        base_dirs=self.update_base_dirs(),
                        timeout=timeout,
                    )
                    self.safe_after(0, self.on_update_check_finished, info)
                except UpdateError as exc:
                    self.safe_after(0, self.on_update_check_failed, str(exc))
                except Exception as exc:
                    self.safe_after(0, self.on_update_check_failed, f"{type(exc).__name__}: {exc}")

            threading.Thread(target=worker, name="update-check", daemon=True).start()

        def on_update_check_failed(self, message):
            self.update_checking = False
            self.status_var.set("检查更新失败")
            self.append_log(f"检查更新失败：{message}")
            messagebox.showerror("检查更新失败", message)

        def on_update_check_finished(self, info):
            self.update_checking = False
            self.status_var.set("检查更新完成")
            latest = info.get("latest_version", "")
            self.append_log(f"检查更新完成：当前 {APP_VERSION}，远程 {latest}")
            if not info.get("has_update"):
                notes = info.get("release_notes") or "本次发布未填写更新说明。"
                messagebox.showinfo(
                    "已是最新版",
                    (
                        f"当前版本：{APP_VERSION}\n"
                        f"远程版本：{latest}\n"
                        f"文件大小：{format_file_size(info.get('file_size'))}\n"
                        f"下载地址：{info.get('download_url') or '未填写'}\n\n"
                        f"更新说明：\n{notes}\n\n"
                        "当前已经是最新版本。"
                    ),
                )
                return
            self.show_update_available(info)

        def show_update_available(self, info):
            window = tk.Toplevel(self.root)
            window.title("发现新版本")
            window.transient(self.root)
            window.grab_set()
            window.resizable(False, False)
            panel = ttk.Frame(window, padding=16)
            panel.grid(row=0, column=0, sticky=tk.NSEW)
            ttk.Label(
                panel,
                text="发现可用更新",
                font=("Microsoft YaHei UI", 12, "bold"),
            ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))
            rows = [
                ("当前版本", APP_VERSION),
                ("最新版本", info.get("latest_version", "")),
                ("发布通道", info.get("channel", "stable")),
                ("更新方式", info.get("package_type", "") or "未填写"),
                ("发布时间", info.get("release_date", "") or "未填写"),
                ("文件大小", format_file_size(info.get("file_size"))),
            ]
            for row, (label, value) in enumerate(rows, start=1):
                ttk.Label(panel, text=label, width=10, anchor=tk.E, style="Form.TLabel").grid(
                    row=row,
                    column=0,
                    sticky=tk.E,
                    pady=3,
                )
                ttk.Label(panel, text=value, width=48, anchor=tk.W).grid(
                    row=row,
                    column=1,
                    sticky=tk.W,
                    padx=(8, 0),
                    pady=3,
                )
            url_row = len(rows) + 1
            ttk.Label(panel, text="下载地址", width=10, anchor=tk.NE, style="Form.TLabel").grid(
                row=url_row,
                column=0,
                sticky=tk.NE,
                pady=(8, 0),
            )
            url_box = tk.Text(panel, width=54, height=3, wrap=tk.CHAR)
            url_box.grid(row=url_row, column=1, sticky=tk.EW, padx=(8, 0), pady=(8, 0))
            url_box.insert("1.0", info.get("download_url") or "未填写")
            url_box.configure(state=tk.DISABLED)
            notes_row = url_row + 1
            ttk.Label(panel, text="更新说明", width=10, anchor=tk.NE, style="Form.TLabel").grid(
                row=notes_row,
                column=0,
                sticky=tk.NE,
                pady=(8, 0),
            )
            notes = tk.Text(panel, width=54, height=8, wrap=tk.WORD)
            notes.grid(row=notes_row, column=1, sticky=tk.EW, padx=(8, 0), pady=(8, 0))
            notes.insert("1.0", info.get("release_notes") or "")
            notes.configure(state=tk.DISABLED)
            buttons = ttk.Frame(panel)
            buttons.grid(row=notes_row + 1, column=0, columnspan=2, sticky=tk.E, pady=(14, 0))

            def later():
                self.append_log("用户选择稍后再升级")
                window.destroy()

            def upgrade_now():
                window.destroy()
                self.start_update_download(info)

            ttk.Button(buttons, text="稍后再说", command=later, style="Compact.TButton").pack(side=tk.RIGHT)
            ttk.Button(
                buttons,
                text="立即升级",
                command=upgrade_now,
                style="CompactAccent.TButton",
            ).pack(side=tk.RIGHT, padx=(0, 8))
            window.update_idletasks()
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - window.winfo_width()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - window.winfo_height()) // 2)
            window.geometry(f"+{x}+{y}")

        def start_update_download(self, info):
            if self.update_downloading:
                self.action_blocked("正在下载", "升级包正在下载，请等待当前下载完成。")
                return
            if not info.get("download_url"):
                self.show_notice("下载地址为空", "远程 update.json 未填写下载地址，无法升级。", "error")
                return
            self.update_downloading = True
            self.update_cancel_event = threading.Event()
            self.show_update_progress_window(info)
            self.append_log(f"开始下载升级包：版本 {info.get('latest_version')}，地址 {info.get('download_url')}")

            def progress(done, total, speed):
                self.safe_after(0, self.update_download_progress, done, total, speed)

            def worker():
                try:
                    path = download_update_file(
                        info,
                        UPDATE_DIR,
                        base_dirs=self.update_base_dirs(),
                        progress_callback=progress,
                        cancel_event=self.update_cancel_event,
                    )
                    self.safe_after(0, self.on_update_download_finished, info, path)
                except UpdateCancelled as exc:
                    self.safe_after(0, self.on_update_download_failed, str(exc), True)
                except UpdateError as exc:
                    self.safe_after(0, self.on_update_download_failed, str(exc), False)
                except Exception as exc:
                    self.safe_after(0, self.on_update_download_failed, f"{type(exc).__name__}: {exc}", False)

            threading.Thread(target=worker, name="update-download", daemon=True).start()

        def show_update_progress_window(self, info):
            window = tk.Toplevel(self.root)
            window.title("下载升级包")
            window.transient(self.root)
            window.resizable(False, False)
            self.update_progress_window = window
            panel = ttk.Frame(window, padding=14)
            panel.grid(row=0, column=0, sticky=tk.NSEW)
            ttk.Label(
                panel,
                text=f"正在下载 Traffic Light {info.get('latest_version')}",
                font=("Microsoft YaHei UI", 10, "bold"),
            ).grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
            self.update_progress_var = tk.DoubleVar(value=0)
            self.update_progress_label_var = tk.StringVar(value="准备下载...")
            ttk.Progressbar(
                panel,
                variable=self.update_progress_var,
                maximum=100,
                length=420,
            ).grid(row=1, column=0, sticky=tk.EW)
            ttk.Label(panel, textvariable=self.update_progress_label_var).grid(
                row=2,
                column=0,
                sticky=tk.W,
                pady=(8, 0),
            )
            ttk.Button(
                panel,
                text="取消下载",
                command=self.cancel_update_download,
                style="Compact.TButton",
            ).grid(row=3, column=0, sticky=tk.E, pady=(12, 0))
            window.protocol("WM_DELETE_WINDOW", self.cancel_update_download)
            window.update_idletasks()
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - window.winfo_width()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - window.winfo_height()) // 2)
            window.geometry(f"+{x}+{y}")

        def update_download_progress(self, done, total, speed):
            if not getattr(self, "update_progress_label_var", None):
                return
            percent = (done / total * 100) if total else 0
            if total:
                self.update_progress_var.set(max(0, min(100, percent)))
                self.update_progress_label_var.set(
                    f"{percent:.1f}%  {format_file_size(done)} / {format_file_size(total)}  {format_file_size(speed)}/s"
                )
            else:
                self.update_progress_label_var.set(
                    f"已下载 {format_file_size(done)}  {format_file_size(speed)}/s"
                )

        def cancel_update_download(self):
            if self.update_cancel_event is not None and self.update_downloading:
                self.update_cancel_event.set()
                self.append_log("用户取消升级包下载")

        def close_update_progress_window(self):
            window = getattr(self, "update_progress_window", None)
            self.update_progress_window = None
            if window is not None:
                try:
                    window.destroy()
                except tk.TclError:
                    pass

        def on_update_download_failed(self, message, cancelled=False):
            self.update_downloading = False
            self.update_cancel_event = None
            self.close_update_progress_window()
            self.status_var.set("下载已取消" if cancelled else "下载失败")
            self.append_log(("升级下载已取消：" if cancelled else "升级下载失败：") + message)
            if cancelled:
                messagebox.showinfo("下载已取消", message)
            else:
                messagebox.showerror("下载失败", message)

        def on_update_download_finished(self, info, path):
            self.update_downloading = False
            self.update_cancel_event = None
            self.close_update_progress_window()
            self.status_var.set("升级包已下载")
            self.append_log(f"升级包下载并校验完成：{path}")
            suffix = Path(path).suffix.lower()
            if suffix == ".exe":
                if messagebox.askyesno(
                    "升级包已下载",
                    (
                        f"升级包已下载并通过校验：\n{path}\n\n"
                        "是否立即运行安装包？运行后请按安装程序提示关闭当前软件。"
                    ),
                ):
                    try:
                        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                        subprocess.Popen([str(path)], cwd=str(Path(path).parent), creationflags=creationflags)
                    except OSError as exc:
                        messagebox.showerror("启动安装包失败", str(exc))
                return
            messagebox.showinfo(
                "升级包已下载",
                f"升级包已下载并通过校验：\n{path}\n\n请关闭当前软件后，按发布包说明手动替换或安装。",
            )
            try:
                open_path(Path(path).parent)
            except Exception:
                pass

        def show_diagnostics_result(self, origin, diagnostic_items):
            errors = [message for level, message in diagnostic_items if level == "ERROR"]
            warnings = [message for level, message in diagnostic_items if level == "WARN"]
            if errors:
                state = "未通过"
                action = "请优先处理下方错误项；处理后再重新诊断或测试当前路线。"
                important = errors[:4]
                show = messagebox.showwarning
            elif warnings:
                state = "基本可用，但有提醒"
                action = "当前没有阻断错误，但建议按提醒检查，现场运行会更稳定。"
                important = warnings[:4]
                show = messagebox.showwarning
            else:
                state = "通过"
                action = "基础环境、目录和当前配置检查正常，可以继续测试接口或开始任务。"
                important = []
                show = messagebox.showinfo

            lines = [
                f"{origin}本机诊断完成：{state}",
                "",
                f"错误 {len(errors)} 项，提醒 {len(warnings)} 项。",
                "",
                f"当前服务器：{self.current_server_route_summary()}",
                f"当前开始分析：{self.current_analysis_summary()}",
                f"当前任务参数：{self.current_advanced_summary()}",
            ]
            if important:
                lines.extend(["", "需要关注："])
                lines.extend(f"- {item}" for item in important)
            lines.extend(["", action, "详细逐项记录已写入“日志诊断”页。"])
            show("本机诊断结果", "\n".join(lines))

        def review_industrial_readiness(self, origin="当前界面"):
            config = self.collect_config()
            source = runtime_input_source(self.selected_input_source_for_diagnostic(), config)
            errors = []
            warnings = []
            passed = []
            suggestions = []

            def ok(message):
                passed.append(message)

            def warn(message):
                warnings.append(message)

            def error(message):
                errors.append(message)

            if find_tool("ffmpeg"):
                ok("FFmpeg 可用，具备本地视频和实时流抽帧基础能力。")
            else:
                error("未找到 FFmpeg，无法进行视频抽帧。")

            if source is None:
                warn("未选择视频输入源；如果只是监听图片目录可以忽略，否则请先选择本地视频或实时流。")
            elif source["type"] == "file":
                if Path(source["value"]).exists():
                    ok("本地视频文件存在。")
                else:
                    error(f"本地视频文件不存在：{source['value']}")
            elif source["type"] == "stream":
                stream_url = source["value"]
                valid, message = validate_stream_url(stream_url)
                if valid:
                    ok(f"实时流地址格式正常：{describe_stream_url(stream_url)}，运行地址 {masked_stream_url(stream_url)}。")
                    scheme = urlparse(stream_url).scheme.lower()
                    if scheme in {"rtsp", "rtsps"}:
                        ok(rtsp_security_summary(config, stream_url))
                        if not config.get("rtsp_username") and "@" not in urlparse(stream_url).netloc:
                            warn("RTSP 地址未配置账号密码。公开流可以这样使用；摄像头或国标平台开启鉴权时，需要填写账号和密码/Token。")
                        if scheme == "rtsps":
                            ok("已使用 RTSPS/TLS 安全传输；前提是摄像头或平台端支持 RTSPS。")
                        elif config.get("rtsp_use_tls"):
                            ok("已勾选 RTSPS/TLS，运行时会按 rtsps:// 交给 FFmpeg。")
                        else:
                            suggestions.append("如果现场摄像头支持 RTSPS/TLS，可勾选“使用RTSPS/TLS”提升链路安全。")
                        port_ok, port_message = rtsp_control_port_is_reachable(stream_url, timeout=3)
                        if port_ok:
                            ok(f"RTSP 控制端口可连接：{port_message}")
                        else:
                            warn(f"RTSP 控制端口当前不可连接：{port_message}。这通常是网络、VPN、防火墙、端口映射或权限问题。")
                    elif scheme in {"rtmp", "rtmps"}:
                        warn("RTMP/RTMPS 在公网和防火墙环境下不如 RTSP、HTTP-FLV、HLS 稳定，工业现场建议优先使用 RTSP 或平台转 HTTP-FLV/HLS。")
                else:
                    error(message)

            mode = config.get("connection_mode", "public")
            api_ok, api_message = validate_api_url(config.get("api_url", ""), mode)
            if api_ok:
                ok(f"模型接口地址格式正常：{api_message}")
            else:
                error(api_message)
            if mode == "public" and not config.get("api_key"):
                error("公网大模型模式缺少 API Key。")
            if mode == "private_ssh":
                missing = [
                    label
                    for key, label in (
                        ("ssh_host", "SSH服务器"),
                        ("ssh_user", "用户名"),
                        ("ssh_remote_host", "模型服务地址"),
                    )
                    if not str(config.get(key, "")).strip()
                ]
                if missing:
                    error("SSH 跳板机配置不完整：" + "、".join(missing))
                else:
                    ok("SSH 跳板机关键字段完整。")

            model = config.get("model", "")
            if looks_like_vision_model(model):
                ok(f"当前模型名称符合图像分析模型特征：{model}")
            else:
                warn(f"当前模型名称不像图像分析模型：{model}。建议读取模型后选择带 VL 或 vision 标识的模型。")

            interval = int_from(config.get("frame_interval"), 10, 1, 3600)
            concurrency = int_from(config.get("concurrency"), 1, 1, 8)
            timeout = int_from(config.get("request_timeout"), 60, 5, 600)
            try:
                capture_plan = build_capture_plan(config, source.get("type") if source else config.get("source_type"))
                ok(f"抽帧策略有效：{capture_plan['summary']}")
                if source and source.get("type") == "stream" and capture_plan["finite"]:
                    warn("实时流的时间点和时间段按任务启动后的真实时间计算，不代表摄像头历史回放时间。")
            except ValueError as exc:
                error(f"抽帧策略设置错误：{exc}")
            if (
                source
                and source.get("type") == "stream"
                and capture_mode_value(config.get("capture_mode")) != "point"
                and interval <= 1
                and concurrency <= 1
            ):
                warn("实时流设置为 1 秒 1 帧且并发为 1。软件会完整不丢帧，但服务器处理慢时会积压；建议确认模型吞吐或提高并发。")
            if timeout < 30:
                warn("接口超时低于 30 秒，复杂图像或私有化模型响应慢时容易误判失败。")
            if config.get("stream_drop_stale_frames"):
                warn("已开启低延迟优先丢旧帧。适合只看最新画面，不适合需要完整留痕的工业分析。")
            else:
                ok("默认完整分析不丢帧，符合留痕和复盘需求。")
            if config.get("stream_auto_reconnect"):
                ok("实时流断线自动重连已开启。")
            else:
                warn("实时流断线自动重连未开启，现场网络抖动时可能需要人工重新启动。")
            ok("界面缩放已启用延迟重排、稳定滚动条和日志刷新节流，减少大小窗口切换卡顿。")
            suggestions.extend(
                [
                    "如果后续要接入厂家私有加密码流，应优先让平台输出标准 RTSP/RTSPS、HTTP-FLV 或 HLS；无法转流时再开发厂家 SDK 插件。",
                    "长期部署建议在现场准备 1 条 VLC 可播放的标准测试流，用于区分软件问题和摄像头/网络问题。",
                    "大任务场景建议将结果目录放在本地磁盘，避免网络盘写入延迟影响界面响应。",
                ]
            )

            self.append_log(f"{origin}工业配置审查完成：错误 {len(errors)}，提醒 {len(warnings)}，通过 {len(passed)}")
            lines = [
                f"{origin}工业配置审查完成",
                "",
                f"错误 {len(errors)} 项，提醒 {len(warnings)} 项，通过 {len(passed)} 项。",
            ]
            if errors:
                lines.extend(["", "必须先处理："])
                lines.extend(f"- {item}" for item in errors[:6])
            if warnings:
                lines.extend(["", "建议关注："])
                lines.extend(f"- {item}" for item in warnings[:6])
            if passed:
                lines.extend(["", "已具备："])
                lines.extend(f"- {item}" for item in passed[:6])
            if suggestions:
                lines.extend(["", "后续增强建议："])
                lines.extend(f"- {item}" for item in suggestions[:5])

            show = messagebox.showerror if errors else messagebox.showwarning if warnings else messagebox.showinfo
            show("工业配置审查", "\n".join(lines))

        def selected_input_source_for_diagnostic(self):
            if self.source_type_var.get() == "stream":
                stream_url = self.stream_url_var.get().strip()
                return {"type": "stream", "value": stream_url} if stream_url else None
            video = self.video_var.get().strip()
            return {"type": "file", "value": video} if video else None

        def on_api_test_failed(self, message, origin="当前界面", route_text=""):
            self.connection_var.set(message)
            self.append_log(f"接口测试失败：{message}")
            detail = (
                f"{origin}测试完成：未通过\n\n"
                f"问题：{message}\n\n"
                f"本次测试路线：{route_text or self.current_server_route_summary()}\n\n"
                "下一步：请按提示检查接口地址、API Key、SSH 跳板机、模型服务端口或网络连通性。"
            )
            messagebox.showwarning("测试当前路线", detail)

        def on_api_models_loaded(self, api_url, message, models, model_message, origin="当前界面", route_text=""):
            if models:
                self.model_values = models
                display_values = [model_display_name(model) for model in models]
                for combo in self.model_combos:
                    combo.configure(values=display_values)
                selected_model = choose_best_model(models, self.model_var.get())
                selected_display = model_display_name(selected_model)
                if self.model_var.get() != selected_display:
                    self.model_var.set(selected_display)
                self.update_model_hint()
                self.api_url_var.set(normalize_chat_url(api_url))
                summary = format_model_summary(models, selected_model)
                self.connection_var.set(f"{message}；{summary}")
                self.append_log(self.connection_var.get())
                saved_config = self.collect_config()
                if not self.save_config_or_notice(saved_config, "保存模型列表测试结果"):
                    return
                self.config = saved_config
                self.mark_server_config_saved("已保存：模型列表读取成功，下一轮任务会使用当前服务器配置")
                self.mark_analysis_config_saved("已保存：模型列表读取成功，开始分析配置已同步")
                self.mark_advanced_config_saved("已保存：模型列表读取成功，任务参数已同步")
                detail = (
                    f"{origin}测试完成：通过\n\n"
                    f"连接结果：{message}\n\n"
                    f"模型结果：{summary}\n\n"
                    f"本次测试路线：{route_text or self.server_route_log_text(saved_config)}\n\n"
                    "当前配置已保存，下一轮任务会使用这套配置。"
                )
                messagebox.showinfo("测试当前路线", detail)
            else:
                text = f"{message}；{model_message}"
                self.connection_var.set(text)
                self.append_log(text)
                detail = (
                    f"{origin}测试完成：连接可达，但模型列表未正常读取\n\n"
                    f"结果：{text}\n\n"
                    f"本次测试路线：{route_text or self.current_server_route_summary()}\n\n"
                    "下一步：请检查模型列表接口、API Key 权限，或手动填写确认支持图像分析的模型名。"
                )
                messagebox.showwarning("测试当前路线", detail)

        def clear_log(self):
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_line_count = 0
            self.log_text.configure(state=tk.DISABLED)

        def clear_results(self):
            self.result_text.configure(state=tk.NORMAL)
            self.result_text.delete("1.0", tk.END)
            self.result_line_count = 0
            self.result_text.configure(state=tk.DISABLED)

        def poll_events(self):
            # 每轮限制处理数量；窗口缩放期间暂停日志/结果写入，把绘制时间让给布局系统。
            processed = 0
            resizing = self.is_root_resizing()
            backlog = self.events.qsize()
            if resizing:
                try:
                    self.root.after(180, self.poll_events)
                except tk.TclError:
                    pass
                return
            if backlog > 2000:
                max_events_per_tick = 240
            elif backlog > 500:
                max_events_per_tick = 120
            else:
                max_events_per_tick = 45
            while processed < max_events_per_tick:
                try:
                    event_type, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                if event_type == "log":
                    self.append_log(payload["text"])
                elif event_type == "result":
                    self.append_result(payload)
                elif event_type == "stats":
                    self.summary_var.set(
                        f"排队 {payload['queued']} | 分析中 {payload['processing']} | "
                        f"成功 {payload['success']} | 失败 {payload['failed']}"
                    )
                    self.update_stats_dashboard(payload)
                elif event_type == "state":
                    self.status_var.set(payload.get("text", "就绪"))
                elif event_type == "notice":
                    self.show_notice(
                        payload.get("title", "提示"),
                        payload.get("message", ""),
                        payload.get("level", "warning"),
                        log=False,
                    )
                elif event_type == "callback":
                    callback = payload.get("callback")
                    args = payload.get("args", ())
                    if callable(callback):
                        callback(*args)
            if backlog > 2000:
                delay = 10
            elif backlog > 500:
                delay = 25
            else:
                delay = 60 if not self.events.empty() else 130
            try:
                self.root.after(delay, self.poll_events)
            except tk.TclError:
                pass

        def append_log(self, message):
            # 界面只保留近期日志并限制自动滚动频率，完整运行信息仍在任务输出中。
            message = mask_sensitive_text(message)
            self.pending_log_lines.append(message)
            if len(self.pending_log_lines) >= 100:
                self.flush_pending_logs()
            elif self.log_flush_after_id is None:
                try:
                    self.log_flush_after_id = self.root.after(
                        250,
                        self.flush_pending_logs,
                    )
                except tk.TclError:
                    self.log_flush_after_id = None
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_line_count += 1
            if self.log_line_count > 2400:
                self.log_text.delete("1.0", "401.0")
                self.log_line_count = 2000
            now = time.time()
            if not self.is_root_resizing() and now - self.last_log_autoscroll >= 0.15:
                self.log_text.see(tk.END)
                self.last_log_autoscroll = now
            self.log_text.configure(state=tk.DISABLED)

        def flush_pending_logs(self):
            self.log_flush_after_id = None
            if not self.pending_log_lines:
                return
            lines = self.pending_log_lines
            self.pending_log_lines = []
            write_persistent_logs(lines)

        def append_result(self, payload):
            # 结果窗口有行数上限，磁盘上的 Markdown 文件才是完整、长期保存的记录。
            self.current_result_file = payload.get("file")
            frame_text = payload.get("frame_time") or "未知"
            asset_text = "已保存" if payload.get("frame_image_path") else "缺失"
            block = (
                f"【{payload['time']}】分析 {int_from(payload.get('index'), 0, 0):04d}  {payload['image']}\n"
                f"报告图片：{asset_text} | 抽帧时间：{frame_text}\n"
                f"{'-' * 72}\n"
                f"{payload['content']}\n\n"
            )
            self.result_text.configure(state=tk.NORMAL)
            self.result_text.insert(tk.END, block)
            self.result_line_count += block.count("\n")
            if self.result_line_count > 1800:
                self.result_text.delete("1.0", "401.0")
                self.result_line_count = max(0, self.result_line_count - 400)
                self.result_text.insert(
                    "1.0",
                    "【界面提示】实时窗口仅保留最近结果，完整内容已持续保存到结果文件。\n\n",
                )
                self.result_line_count += 2
            now = time.time()
            if not self.is_root_resizing() and now - self.last_result_autoscroll >= 0.2:
                self.result_text.see(tk.END)
                self.last_result_autoscroll = now
            self.result_text.configure(state=tk.DISABLED)

        def open_results_dir(self):
            try:
                open_path(self.results_dir_var.get().strip() or DEFAULT_RESULTS_DIR)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc))

        def open_data_dir(self):
            try:
                open_path(DATA_DIR)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc))

        def open_logs_dir(self):
            try:
                open_path(LOGS_DIR)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc))

        def open_update_dir(self):
            try:
                UPDATE_DIR.mkdir(parents=True, exist_ok=True)
                open_path(UPDATE_DIR)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc))

        def open_session_records(self):
            try:
                open_path(self.results_dir_var.get().strip() or DEFAULT_RESULTS_DIR)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc))

        def export_config_dialog(self):
            initial_name = f"{APP_NAME}_V{APP_VERSION}_配置快照.json"
            path = filedialog.asksaveasfilename(
                title="导出配置快照",
                initialdir=str(APP_DIR),
                initialfile=initial_name,
                defaultextension=".json",
                filetypes=[("JSON 配置快照", "*.json")],
            )
            if not path:
                return
            try:
                export_config_snapshot(path, self.collect_config())
                self.append_log(f"已导出不含密钥的配置快照：{path}")
                messagebox.showinfo(
                    "导出完成",
                    "配置快照已导出。\n\nAPI Key 和 RTSP 密码不会写入快照。",
                )
            except (OSError, ValueError, TypeError) as exc:
                messagebox.showerror("导出失败", str(exc))

        def import_config_dialog(self):
            path = filedialog.askopenfilename(
                title="导入配置快照",
                initialdir=str(APP_DIR),
                filetypes=[("JSON 配置快照", "*.json"), ("所有文件", "*.*")],
            )
            if not path:
                return
            try:
                imported = import_config_snapshot(path, self.collect_config())
                if not self.save_config_or_notice(imported, "导入配置快照"):
                    return
                self.config = imported
                self.append_log(f"已导入配置快照：{path}")
                messagebox.showinfo(
                    "导入完成",
                    "配置已保存，现有 API Key 和 RTSP 密码已保留。\n\n"
                    "请重新打开软件，使所有页面完整加载导入后的配置。",
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                messagebox.showerror("导入失败", str(exc))

        def migrate_legacy_config_dialog(self):
            path = filedialog.askopenfilename(
                title="选择 V1.0 或 V1.1 的 stream_config.json",
                initialdir=str(APP_DIR),
                filetypes=[("旧版配置", "stream_config.json *.json"), ("所有文件", "*.*")],
            )
            if not path:
                return
            try:
                migrated = import_legacy_config(path, self.collect_config())
                if not self.save_config_or_notice(migrated, "迁移旧版配置"):
                    return
                self.config = migrated
                self.append_log(f"已迁移旧版配置：{path}")
                messagebox.showinfo(
                    "迁移完成",
                    f"旧版配置已迁移到 {APP_VERSION_TAG} 独立数据目录。\n\n"
                    f"原配置文件未修改。请重新打开 {APP_VERSION_TAG}，使所有页面完整加载迁移后的设置。",
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                messagebox.showerror("迁移失败", str(exc))

        def export_support_bundle_dialog(self):
            SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
            initial_name = f"support_{datetime.now():%Y%m%d_%H%M%S}.zip"
            path = filedialog.asksaveasfilename(
                title="生成技术支持包",
                initialdir=str(SUPPORT_DIR),
                initialfile=initial_name,
                defaultextension=".zip",
                filetypes=[("ZIP 技术支持包", "*.zip")],
            )
            if not path:
                return
            try:
                bundle = create_support_bundle(path, self.collect_config())
                self.append_log(f"已生成脱敏技术支持包：{bundle}")
                messagebox.showinfo(
                    "支持包已生成",
                    "支持包仅包含脱敏诊断、近期日志片段和最近任务档案，不包含 API Key 或 RTSP 密码。",
                )
            except (OSError, ValueError, TypeError, zipfile.BadZipFile) as exc:
                messagebox.showerror("生成失败", str(exc))

        def report_callback_exception(self, exc_type, exc_value, exc_traceback):
            try:
                report = write_crash_report(
                    exc_type,
                    exc_value,
                    exc_traceback,
                    "tkinter_callback",
                )
                if hasattr(self, "log_text"):
                    self.append_log(f"界面操作发生异常，崩溃报告已保存：{report}")
                messagebox.showerror(
                    "操作未完成",
                    f"界面操作发生异常，已保存诊断报告：\n{report}",
                )
            except Exception:
                sys.__excepthook__(exc_type, exc_value, exc_traceback)

        def open_tutorial(self):
            tutorial = APP_DIR / "Traffic Light使用教程.md"
            if not tutorial.exists():
                extracted = materialize_resource("Traffic Light使用教程.md")
                if extracted is not None:
                    tutorial = extracted
            if not tutorial or not tutorial.exists():
                messagebox.showwarning("使用教程", "当前文件夹没有找到“Traffic Light使用教程.md”。")
                return
            try:
                open_file(tutorial)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc))

        def on_close(self):
            # 运行中关闭窗口必须先走引擎停止流程，防止留下 FFmpeg、SSH 或监听线程。
            if self.closing:
                return
            if self.engine and (self.engine.running or self.starting):
                if not messagebox.askyesno("退出", "任务仍在运行，是否停止并退出？"):
                    return
                self.closing = True
                self.cancel_update_download()
                self.status_var.set("正在停止")
                if self.overview_refresh_after_id:
                    try:
                        self.root.after_cancel(self.overview_refresh_after_id)
                    except tk.TclError:
                        pass
                    self.overview_refresh_after_id = None
                threading.Thread(target=self._close_after_stop, daemon=True).start()
                return
            self.closing = True
            self.cancel_update_download()
            if self.overview_refresh_after_id:
                try:
                    self.root.after_cancel(self.overview_refresh_after_id)
                except tk.TclError:
                    pass
                self.overview_refresh_after_id = None
            self.cancel_status_blink()
            self.flush_pending_logs()
            preview = getattr(self, "video_preview_window", None)
            if preview is not None:
                try:
                    preview.close()
                except tk.TclError:
                    pass
            self.root.destroy()

        def _close_after_stop(self):
            try:
                if self.engine:
                    self.engine.stop(cleanup_unprocessed=True)
            finally:
                try:
                    self.safe_after(0, self.finish_close)
                except tk.TclError:
                    pass

        def finish_close(self):
            self.cancel_status_blink()
            self.flush_pending_logs()
            preview = getattr(self, "video_preview_window", None)
            if preview is not None:
                try:
                    preview.close()
                except tk.TclError:
                    pass
            self.root.destroy()

    workflow_test_result = {"exit_code": 0}
    root = tk.Tk()
    root._stream_app = StreamApp(root)
    if preview_tab == "overview":
        root._stream_app.notebook.select(root._stream_app.overview_tab_container)
    elif preview_tab in {"workbench", "source", "stream", "rules"}:
        root._stream_app.notebook.select(root._stream_app.analysis_tab)
        if preview_tab in {"source", "stream"}:
            root._stream_app.workbench_notebook.select(
                root._stream_app.workbench_section_tabs["source"]
            )
            if preview_tab == "stream":
                root._stream_app.source_type_var.set("stream")
                root._stream_app.on_source_type_change()
        elif preview_tab == "rules":
            root._stream_app.workbench_notebook.select(
                root._stream_app.workbench_section_tabs["rules"]
            )
    elif preview_tab in {
        "connection",
        "connection-public",
        "connection-direct",
        "connection-ssh",
    }:
        root._stream_app.notebook.select(root._stream_app.server_tab_container)
        preview_modes = {
            "connection-public": "public",
            "connection-direct": "private_direct",
            "connection-ssh": "private_ssh",
        }
        if preview_tab in preview_modes:
            root._stream_app.connection_mode_var.set(preview_modes[preview_tab])
            root._stream_app.on_connection_mode_change(initial=True)
    elif preview_tab in {
        "settings",
        "settings-storage",
        "settings-stream",
        "settings-maintenance",
    }:
        root._stream_app.notebook.select(root._stream_app.advanced_tab_container)
        settings_tabs = {
            "settings-storage": 0,
            "settings-stream": 1,
            "settings-maintenance": 2,
        }
        if preview_tab in settings_tabs:
            root._stream_app.settings_notebook.select(settings_tabs[preview_tab])
    elif preview_tab == "logs":
        root._stream_app.notebook.select(root._stream_app.log_tab)
    if workflow_test:
        def run_workflow_test():
            app = root._stream_app
            checks = {}
            try:
                initial_size = (root.winfo_width(), root.winfo_height())
                checks["default_tab"] = app.notebook.select() == str(app.analysis_tab)
                checks["default_connection_fields_blank"] = bool(
                    not app.api_url_var.get().strip()
                    and not app.api_key_var.get().strip()
                    and not model_id_from_display(app.model_var.get())
                    and not app.ssh_api_path_var.get().strip()
                )
                checks["stop_disabled"] = "disabled" in app.stop_task_button.state()
                app.starting = True
                app.status_var.set("启动中")
                root.update_idletasks()
                checks["busy_disables_start"] = (
                    "disabled" in app.start_task_button.state()
                )
                checks["busy_enables_stop"] = (
                    "disabled" not in app.stop_task_button.state()
                )
                app.starting = False
                app.status_var.set("就绪")

                route_checks = {}
                if hasattr(app, "workbench_notebook"):
                    app.workbench_notebook.select(app.workbench_section_tabs["server"])
                for mode in ("public", "private_direct", "private_ssh"):
                    app.connection_mode_var.set(mode)
                    app.on_connection_mode_change(initial=True)
                    root.update_idletasks()
                    route_checks[mode] = bool(
                        app.workflow_server_panels[mode].winfo_ismapped()
                    )
                checks["routes"] = route_checks

                source_checks = {}
                if hasattr(app, "workbench_notebook"):
                    app.workbench_notebook.select(app.workbench_section_tabs["source"])
                for source_type in ("file", "stream"):
                    app.source_type_var.set(source_type)
                    app.on_source_type_change()
                    root.update_idletasks()
                    source_checks[source_type] = bool(
                        app.source_panels[source_type].winfo_ismapped()
                    )
                checks["sources"] = source_checks

                app.reveal_workflow_section("source")
                root.update_idletasks()
                checks["reveal_keeps_workbench"] = (
                    app.notebook.select() == str(app.analysis_tab)
                )
                checks["reveal_selects_source"] = (
                    not hasattr(app, "workbench_notebook")
                    or app.workbench_notebook.select()
                    == str(app.workbench_section_tabs["source"])
                )
                checks["workbench_geometry"] = {
                    "root": [root.winfo_width(), root.winfo_height()],
                    "paned": [
                        app.workbench_paned.winfo_width(),
                        app.workbench_paned.winfo_height(),
                    ],
                    "sash": app.workbench_paned.sash_coord(0)[0],
                }
                checks["fixed_workbench_layout"] = bool(
                    not hasattr(app, "analysis_controls_canvas")
                    and app.task_actions_frame.winfo_ismapped()
                    and app.result_text.winfo_ismapped()
                )
                checks["embedded_preview_panel_present"] = bool(
                    hasattr(app, "video_preview_frame")
                    and hasattr(app, "video_preview_content")
                    and app.video_preview_frame.winfo_ismapped()
                    and app.video_preview_frame.winfo_height() >= 240
                    and app.result_text.winfo_ismapped()
                    and app.result_text.winfo_height() >= 120
                )
                checks["embedded_preview_geometry"] = {
                    "preview": [
                        app.video_preview_frame.winfo_width(),
                        app.video_preview_frame.winfo_height(),
                    ] if hasattr(app, "video_preview_frame") else [],
                    "result": [
                        app.result_text.winfo_width(),
                        app.result_text.winfo_height(),
                    ] if hasattr(app, "result_text") else [],
                }
                if hasattr(app, "workbench_notebook"):
                    app.workbench_notebook.select(app.workbench_section_tabs["rules"])
                root.geometry(f"{MIN_WINDOW_WIDTH}x{MIN_WINDOW_HEIGHT}")
                root.update_idletasks()
                rules_canvas = getattr(app, "workbench_rules_scroll_canvas", None)
                rules_bbox = rules_canvas.bbox("all") if rules_canvas is not None else None
                settings_req_width = (
                    app.workbench_rules_settings_frame.winfo_reqwidth()
                    if hasattr(app, "workbench_rules_settings_frame")
                    else 0
                )
                canvas_width = rules_canvas.winfo_width() if rules_canvas is not None else 0
                checks["rules_page_small_window"] = {
                    "canvas": [
                        canvas_width,
                        rules_canvas.winfo_height() if rules_canvas is not None else 0,
                    ],
                    "content_bbox": list(rules_bbox) if rules_bbox else [],
                    "settings_req_width": settings_req_width,
                    "prompt_height": app.prompt_text.winfo_height() if hasattr(app, "prompt_text") else 0,
                }
                checks["rules_page_scrollable"] = bool(
                    rules_canvas is not None
                    and rules_canvas.winfo_ismapped()
                    and rules_bbox
                    and rules_bbox[3] > rules_canvas.winfo_height()
                )
                checks["rules_page_width_safe"] = bool(
                    rules_canvas is not None
                    and canvas_width >= 430
                    and settings_req_width <= canvas_width + 18
                )
                checks["rules_prompt_visible"] = bool(
                    hasattr(app, "prompt_text")
                    and app.prompt_text.winfo_ismapped()
                    and app.prompt_text.winfo_height() >= 80
                )
                if hasattr(app, "workbench_notebook"):
                    app.workbench_notebook.select(app.workbench_section_tabs["source"])
                app.source_type_var.set("stream")
                app.on_source_type_change()
                root.update_idletasks()
                source_canvas = getattr(app, "workbench_source_scroll_canvas", None)
                source_overflow = []

                def collect_source_overflow(widget):
                    try:
                        if not widget.winfo_ismapped():
                            return
                        if source_canvas is not None:
                            left = widget.winfo_rootx() - source_canvas.winfo_rootx()
                            right = left + widget.winfo_width()
                            if left < -1 or right > source_canvas.winfo_width() + 1:
                                source_overflow.append(
                                    {
                                        "class": widget.winfo_class(),
                                        "left": int(left),
                                        "right": int(right),
                                        "width": int(widget.winfo_width()),
                                    }
                                )
                        for child in widget.winfo_children():
                            collect_source_overflow(child)
                    except tk.TclError:
                        return

                collect_source_overflow(getattr(app, "workbench_source_content", app.workbench_section_tabs["source"]))
                checks["source_page_small_window"] = {
                    "canvas": [
                        source_canvas.winfo_width() if source_canvas is not None else 0,
                        source_canvas.winfo_height() if source_canvas is not None else 0,
                    ],
                    "stream_entry_width": app.stream_url_entry.winfo_width() if hasattr(app, "stream_url_entry") else 0,
                    "format_combo_width": app.stream_format_combo.winfo_width() if hasattr(app, "stream_format_combo") else 0,
                    "overflow_count": len(source_overflow),
                    "overflow": source_overflow[:6],
                }
                checks["source_page_small_window_usable"] = bool(
                    source_canvas is not None
                    and source_canvas.winfo_width() >= 420
                    and source_canvas.winfo_height() >= 220
                    and app.stream_url_entry.winfo_width() >= 260
                    and app.stream_format_combo.winfo_width() >= 260
                    and not source_overflow
                )
                root.geometry(f"{initial_size[0]}x{initial_size[1]}")
                root.update_idletasks()
                app.notebook.select(app.analysis_tab)
                root.update_idletasks()
                next_step_height = app.next_step_frame.winfo_height()
                status_header_width = app.status_header_frame.winfo_width()
                app.next_step_var.set("短提示")
                app.status_var.set("就绪")
                root.update_idletasks()
                short_geometry = (
                    app.next_step_frame.winfo_height(),
                    app.status_header_frame.winfo_width(),
                )
                app.next_step_var.set(
                    "正在测试当前模型路线，请等待测试结果弹窗。提示栏应始终保持固定高度，不推动下方工作区。"
                )
                app.status_var.set("正在测试")
                root.update_idletasks()
                long_geometry = (
                    app.next_step_frame.winfo_height(),
                    app.status_header_frame.winfo_width(),
                )
                checks["fixed_hint_and_status_geometry"] = bool(
                    short_geometry == long_geometry
                    and short_geometry == (next_step_height, status_header_width)
                )
                app.status_var.set("就绪")
                app.refresh_next_step()
                checks["fixed_overview_layout"] = not hasattr(
                    app.overview_tab,
                    "refresh_scroll_region",
                )
                app.notebook.select(app.overview_tab_container)
                root.update_idletasks()
                checks["overview_metrics_complete"] = bool(
                    len(app.overview_metric_frames) == 4
                    and all(
                        frame.winfo_height() >= frame.winfo_reqheight()
                        and frame.winfo_height() >= 70
                        for frame in app.overview_metric_frames
                    )
                )
                checks["industrial_status_lamps"] = bool(
                    len(app.status_lamps) == 3
                    and len(app.status_lamp_bezels) == 3
                    and len(app.status_lamp_wells) == 3
                    and len(app.status_lamp_glows) == 3
                    and len(app.status_lamp_highlights) == 3
                )
                active_colors = {}
                for name, status_text in (
                    ("red", "启动失败"),
                    ("yellow", "运行中"),
                    ("green", "就绪"),
                ):
                    app.status_var.set(status_text)
                    root.update_idletasks()
                    active_colors[name] = app.status_light.itemcget(
                        app.status_lamps[name],
                        "fill",
                    )
                app.status_breath_phase = 0
                app.paint_status_light("yellow")
                yellow_low = app.status_light.itemcget(
                    app.status_lamps["yellow"],
                    "fill",
                )
                app.status_breath_phase = 20
                app.paint_status_light("yellow")
                yellow_high = app.status_light.itemcget(
                    app.status_lamps["yellow"],
                    "fill",
                )
                app.status_var.set("就绪")
                checks["status_colors_distinct"] = len(set(active_colors.values())) == 3
                checks["yellow_breath_changes_brightness"] = yellow_low != yellow_high
                passed = (
                    checks["default_tab"]
                    and checks["default_connection_fields_blank"]
                    and checks["stop_disabled"]
                    and checks["busy_disables_start"]
                    and checks["busy_enables_stop"]
                    and all(route_checks.values())
                    and all(source_checks.values())
                    and checks["reveal_keeps_workbench"]
                    and checks["reveal_selects_source"]
                    and checks["fixed_workbench_layout"]
                    and checks["embedded_preview_panel_present"]
                    and checks["fixed_hint_and_status_geometry"]
                    and checks["fixed_overview_layout"]
                    and checks["overview_metrics_complete"]
                    and checks["rules_page_scrollable"]
                    and checks["rules_page_width_safe"]
                    and checks["rules_prompt_visible"]
                    and checks["source_page_small_window_usable"]
                    and checks["industrial_status_lamps"]
                    and checks["status_colors_distinct"]
                    and checks["yellow_breath_changes_brightness"]
                )
                checks["passed"] = passed
                workflow_test_result["exit_code"] = 0 if passed else 1
                print(json.dumps(checks, ensure_ascii=False))
            except Exception as exc:
                workflow_test_result["exit_code"] = 1
                print(
                    json.dumps(
                        {"passed": False, "error": str(exc)},
                        ensure_ascii=False,
                    )
                )
            finally:
                root.destroy()

        root.after(900, run_workflow_test)
    if tab_switch_test:
        def run_tab_switch_test():
            app = root._stream_app
            checks = {}
            try:
                root.update_idletasks()
                expected_size = (root.winfo_width(), root.winfo_height())
                tab_names = [
                    app.notebook.tab(tab_id, "text")
                    for tab_id in app.notebook.tabs()
                ]
                checks["tab_names"] = tab_names
                checks["clear_tab_names"] = tab_names == [
                    "任务工作台",
                    "任务记录",
                    "模型连接",
                    "参数设置",
                    "运行日志",
                ]

                page_sizes = {}
                started = time.perf_counter()
                for _cycle in range(3):
                    for tab_id in app.notebook.tabs():
                        app.notebook.select(tab_id)
                        root.update_idletasks()
                        page = root.nametowidget(tab_id)
                        page_sizes[app.notebook.tab(tab_id, "text")] = [
                            page.winfo_width(),
                            page.winfo_height(),
                        ]
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                checks["switch_elapsed_ms"] = elapsed_ms
                checks["root_size_stable"] = (
                    root.winfo_width(),
                    root.winfo_height(),
                ) == expected_size
                checks["page_sizes"] = page_sizes
                checks["pages_filled"] = all(
                    width >= expected_size[0] - 80 and height >= 300
                    for width, height in page_sizes.values()
                )
                checks["no_top_level_scroll_canvas"] = not any(
                    child.winfo_class() == "Canvas"
                    for container in (
                        app.server_tab_container,
                        app.advanced_tab_container,
                    )
                    for child in container.winfo_children()
                )
                checks["resize_shield_removed"] = not hasattr(app, "resize_shield")
                checks["passed"] = all(
                    (
                        checks["clear_tab_names"],
                        checks["root_size_stable"],
                        checks["pages_filled"],
                        checks["no_top_level_scroll_canvas"],
                        checks["resize_shield_removed"],
                    )
                )
                workflow_test_result["exit_code"] = 0 if checks["passed"] else 1
                print(json.dumps(checks, ensure_ascii=False))
            except Exception as exc:
                workflow_test_result["exit_code"] = 1
                print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False))
            finally:
                root.destroy()

        root.after(900, run_tab_switch_test)
    if video_preview_test:
        def cleanup_preview_test(temp_dir=None):
            app = getattr(root, "_stream_app", None)
            if app is not None:
                preview = getattr(app, "video_preview_window", None)
                if preview is not None:
                    try:
                        preview.close()
                    except tk.TclError:
                        pass
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

        def run_video_preview_test():
            app = root._stream_app
            checks = {}
            temp_dir = Path(tempfile.mkdtemp(prefix="video_analyzer_preview_"))
            try:
                ffmpeg = find_tool("ffmpeg")
                checks["ffmpeg_found"] = bool(ffmpeg)
                if not ffmpeg:
                    raise RuntimeError("未找到 FFmpeg")

                video_path = temp_dir / "preview_source.mp4"
                command = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=duration=6:size=320x180:rate=10",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:v",
                    "mpeg4",
                    "-q:v",
                    "4",
                    str(video_path),
                ]
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    creationflags=creationflags,
                )
                checks["video_generated"] = (
                    completed.returncode == 0
                    and video_path.exists()
                    and video_path.stat().st_size > 0
                )
                if not checks["video_generated"]:
                    raise RuntimeError(compact_ffmpeg_output(completed.stdout) or "临时视频生成失败")

                app.source_type_var.set("file")
                app.video_var.set(str(video_path))
                app.on_source_type_change()
                app.open_video_preview()
                preview = app.video_preview_window
                checks["window_created"] = bool(preview and preview.window.winfo_exists())
                checks["embedded_preview"] = bool(preview and getattr(preview, "embedded", False))
                checks["duration_seconds"] = round(float(getattr(preview, "duration", 0.0)), 3)
                checks["duration_known"] = checks["duration_seconds"] >= 5.5
                checks["timeline_enabled"] = bool(preview and "disabled" not in preview.timeline.state())

                def verify_frame(attempt=0):
                    try:
                        root.update_idletasks()
                        preview = app.video_preview_window
                        checks["frame_received"] = bool(preview and preview.photo is not None)
                        checks["video_shell_geometry"] = [
                            preview.video_shell.winfo_width(),
                            preview.video_shell.winfo_height(),
                        ] if preview and hasattr(preview, "video_shell") else []
                        checks["video_shell_large_enough"] = bool(
                            checks["video_shell_geometry"]
                            and checks["video_shell_geometry"][0] >= 480
                            and checks["video_shell_geometry"][1] >= 170
                        )
                        if not checks["frame_received"] and attempt < 30:
                            root.after(120, lambda: verify_frame(attempt + 1))
                            return
                        if preview is None:
                            raise RuntimeError("预览区域未创建")
                        preview.position_var.set(2.0)
                        preview.load_single_frame(2.0)
                        root.after(500, verify_take_time)
                    except Exception as exc:
                        checks["passed"] = False
                        checks["error"] = str(exc)
                        workflow_test_result["exit_code"] = 1
                        print(json.dumps(checks, ensure_ascii=False))
                        cleanup_preview_test(temp_dir)
                        root.destroy()

                def verify_take_time():
                    try:
                        preview = app.video_preview_window
                        if preview is None:
                            raise RuntimeError("预览区域已关闭")
                        root.update_idletasks()
                        preview_buttons = list(getattr(preview, "preview_action_buttons", []))
                        button_metrics = []
                        for button in preview_buttons:
                            try:
                                button_metrics.append(
                                    {
                                        "text": str(button.cget("text")),
                                        "width": int(button.winfo_width()),
                                        "required": int(button.winfo_reqwidth()),
                                        "tooltip": str(getattr(button, "_tooltip_text", "")),
                                    }
                                )
                            except tk.TclError:
                                continue
                        checks["preview_button_metrics"] = button_metrics
                        checks["preview_buttons_fit"] = bool(
                            len(button_metrics) >= 7
                            and all(item["width"] >= item["required"] for item in button_metrics)
                        )
                        checks["preview_buttons_have_tooltips"] = bool(
                            len(button_metrics) >= 7
                            and all(item["tooltip"] for item in button_metrics)
                        )
                        checks["tooltip_fits_screen_edge"] = False
                        checks["tooltip_bounds"] = {}
                        try:
                            screen_width = int(root.winfo_screenwidth())
                            screen_height = int(root.winfo_screenheight())
                            root_width = int(root.winfo_width())
                            root_height = int(root.winfo_height())
                            root.geometry(
                                f"{root_width}x{root_height}+"
                                f"{max(0, screen_width - root_width - 2)}+"
                                f"{max(0, screen_height - root_height - 2)}"
                            )
                            root.update_idletasks()
                            tooltip_button = preview_buttons[-1]
                            tooltip_show = getattr(tooltip_button, "_tooltip_show", None)
                            tooltip_hide = getattr(tooltip_button, "_tooltip_hide", None)
                            if tooltip_show is not None:
                                tooltip_show()
                                root.update_idletasks()
                                tip = getattr(tooltip_button, "_tooltip_state", {}).get("window")
                                if tip is not None and tip.winfo_exists():
                                    tip.update_idletasks()
                                    left = int(tip.winfo_rootx())
                                    top = int(tip.winfo_rooty())
                                    width = int(tip.winfo_width())
                                    height = int(tip.winfo_height())
                                    checks["tooltip_bounds"] = {
                                        "screen": [screen_width, screen_height],
                                        "tip": [left, top, width, height],
                                    }
                                    checks["tooltip_fits_screen_edge"] = bool(
                                        left >= 0
                                        and top >= 0
                                        and left + width <= screen_width
                                        and top + height <= screen_height
                                    )
                            if tooltip_hide is not None:
                                tooltip_hide()
                        except tk.TclError:
                            checks["tooltip_fits_screen_edge"] = False
                        preview.point_time = 2.0
                        preview.apply_point()
                        checks["point_written"] = (
                            capture_mode_value(app.capture_mode_var.get()) == "point"
                            and app.capture_point_var.get() == "00:00:02"
                        )
                        preview.range_start = 1.0
                        preview.range_end = 4.0
                        preview.apply_range()
                        checks["range_written"] = (
                            capture_mode_value(app.capture_mode_var.get()) == "range"
                            and app.capture_start_var.get() == "00:00:01"
                            and app.capture_end_var.get() == "00:00:04"
                        )
                        checks["close_releases_window"] = True
                        preview.close()
                        root.update_idletasks()
                        checks["close_releases_window"] = app.video_preview_window is None
                        checks["placeholder_restored"] = bool(
                            getattr(app, "video_preview_content", None)
                            and app.video_preview_content.winfo_children()
                        )
                        checks["passed"] = bool(
                            checks["ffmpeg_found"]
                            and checks["video_generated"]
                            and checks["window_created"]
                            and checks["embedded_preview"]
                            and checks["duration_known"]
                            and checks["timeline_enabled"]
                            and checks["frame_received"]
                            and checks["video_shell_large_enough"]
                            and checks["preview_buttons_fit"]
                            and checks["preview_buttons_have_tooltips"]
                            and checks["tooltip_fits_screen_edge"]
                            and checks["point_written"]
                            and checks["range_written"]
                            and checks["close_releases_window"]
                            and checks["placeholder_restored"]
                        )
                        workflow_test_result["exit_code"] = 0 if checks["passed"] else 1
                        print(json.dumps(checks, ensure_ascii=False))
                    except Exception as exc:
                        checks["passed"] = False
                        checks["error"] = str(exc)
                        workflow_test_result["exit_code"] = 1
                        print(json.dumps(checks, ensure_ascii=False))
                    finally:
                        cleanup_preview_test(temp_dir)
                        root.destroy()

                root.after(700, verify_frame)
            except Exception as exc:
                checks["passed"] = False
                checks["error"] = str(exc)
                workflow_test_result["exit_code"] = 1
                print(json.dumps(checks, ensure_ascii=False))
                cleanup_preview_test(temp_dir)
                root.destroy()

        root.after(900, run_video_preview_test)
    if resize_smooth_test:
        resize_checks = {"steps": 0}

        def run_resize_smooth_test():
            app = root._stream_app
            app.notebook.select(app.analysis_tab)
            root.update_idletasks()
            initial_size = (root.winfo_width(), root.winfo_height())
            app.result_text.configure(state=tk.NORMAL)
            app.result_text.delete("1.0", tk.END)
            app.result_text.insert(
                "1.0",
                (
                    "窗口缩放稳定性测试：这是一段用于触发文本框显示压力的长内容，"
                    "窗口放大缩小时，结果区保持固定行排版，页面不能出现明显跳动或裁切。\n"
                )
                * 28,
            )
            app.result_text.configure(state=tk.DISABLED)
            initial_result_wrap = str(app.result_text.cget("wrap"))
            sizes = [
                (1280, 900),
                (1200, 820),
                (1360, 900),
                (1120, 760),
                (1440, 920),
                (1280, 900),
            ]
            observed_sizes = []
            redraw_seen_locked = False
            locked_widget_classes = set()

            def apply_step(index=0):
                nonlocal redraw_seen_locked
                try:
                    if index >= len(sizes):
                        root.after(650, finish_resize_smooth_test)
                        return
                    width, height = sizes[index]
                    root.geometry(f"{width}x{height}")
                    root.update_idletasks()
                    observed_sizes.append((root.winfo_width(), root.winfo_height()))
                    resize_checks["steps"] += 1
                    if getattr(app, "resize_text_redraw_locked", False):
                        redraw_seen_locked = True
                        for widget in getattr(app, "resize_redraw_locked_widgets", []):
                            try:
                                locked_widget_classes.add(widget.winfo_class())
                            except tk.TclError:
                                continue
                    root.after(35, lambda: apply_step(index + 1))
                except Exception as exc:
                    resize_checks["passed"] = False
                    resize_checks["error"] = str(exc)
                    workflow_test_result["exit_code"] = 1
                    print(json.dumps(resize_checks, ensure_ascii=False))
                    root.destroy()

            def finish_resize_smooth_test():
                try:
                    root.update_idletasks()
                    try:
                        resizable_state = tuple(bool(int(value)) for value in root.resizable())
                    except Exception:
                        resizable_state = (False, False)
                    resize_checks["initial_size"] = list(initial_size)
                    resize_checks["observed_sizes"] = [list(size) for size in observed_sizes]
                    resize_checks["window_resizable"] = all(resizable_state)
                    resize_checks["geometry_changed"] = bool(
                        observed_sizes
                        and len(set(observed_sizes)) >= 3
                        and any(size != initial_size for size in observed_sizes)
                    )
                    resize_checks["min_size_respected"] = all(
                        size[0] >= MIN_WINDOW_WIDTH and size[1] >= MIN_WINDOW_HEIGHT
                        for size in observed_sizes
                    )
                    workbench_width = app.workbench_paned.winfo_width() if hasattr(app, "workbench_paned") else 0
                    output_height = app.output_paned.winfo_height() if hasattr(app, "output_paned") else 0
                    workbench_sash = app.workbench_paned.sash_coord(0)[0] if hasattr(app, "workbench_paned") else 0
                    output_sash = app.output_paned.sash_coord(0)[1] if hasattr(app, "output_paned") else 0
                    resize_checks["workbench_sash_valid"] = bool(
                        workbench_width >= 980 and 500 <= workbench_sash <= workbench_width - 560
                    )
                    resize_checks["output_sash_valid"] = bool(
                        output_height >= 420 and 230 <= output_sash <= output_height - 165
                    )
                    resize_checks["redraw_locked_during_resize"] = redraw_seen_locked
                    resize_checks["locked_widget_classes"] = sorted(locked_widget_classes)
                    resize_checks["only_text_redraw_locked"] = bool(
                        locked_widget_classes
                        and locked_widget_classes.issubset({"Text"})
                    )
                    resize_checks["redraw_unlocked"] = not getattr(app, "resize_text_redraw_locked", False)
                    resize_checks["wrap_unchanged"] = str(app.result_text.cget("wrap")) == initial_result_wrap
                    resize_checks["stable_output_wrap"] = initial_result_wrap == str(tk.NONE)
                    resize_checks["resize_finished"] = not app.is_root_resizing()
                    for tab_id in app.notebook.tabs():
                        app.notebook.select(tab_id)
                        root.update_idletasks()
                        app.repair_visible_tab_layout()
                        root.update_idletasks()
                    app.notebook.select(app.analysis_tab)
                    for section in ("server", "source", "rules"):
                        app.workbench_notebook.select(app.workbench_section_tabs[section])
                        root.update_idletasks()
                        app.repair_visible_tab_layout()
                        root.update_idletasks()
                    resize_checks["action_bar_visible"] = bool(
                        hasattr(app, "task_actions_frame")
                        and app.task_actions_frame.winfo_ismapped()
                        and app.task_actions_frame.winfo_height() >= app.task_actions_frame.winfo_reqheight()
                    )
                    resize_checks["preview_visible"] = bool(
                        hasattr(app, "video_preview_frame")
                        and app.video_preview_frame.winfo_ismapped()
                        and app.video_preview_frame.winfo_width() >= 500
                    )
                    resize_checks["result_visible"] = bool(
                        app.result_text.winfo_ismapped()
                        and app.result_text.winfo_height() >= 100
                    )
                    resize_checks["workbench_visible_after_resize_tabs"] = bool(
                        app.analysis_tab.winfo_ismapped()
                        and app.workbench_paned.winfo_ismapped()
                        and app.workbench_notebook.winfo_ismapped()
                        and all(
                            app.workbench_section_tabs[section].winfo_ismapped()
                            for section in ("rules",)
                        )
                    )
                    resize_checks["passed"] = all(
                        (
                            resize_checks["steps"] == len(sizes),
                            resize_checks["window_resizable"],
                            resize_checks["geometry_changed"],
                            resize_checks["min_size_respected"],
                            resize_checks["workbench_sash_valid"],
                            resize_checks["output_sash_valid"],
                            resize_checks["redraw_locked_during_resize"],
                            resize_checks["only_text_redraw_locked"],
                            resize_checks["redraw_unlocked"],
                            resize_checks["wrap_unchanged"],
                            resize_checks["stable_output_wrap"],
                            resize_checks["resize_finished"],
                            resize_checks["action_bar_visible"],
                            resize_checks["preview_visible"],
                            resize_checks["result_visible"],
                            resize_checks["workbench_visible_after_resize_tabs"],
                        )
                    )
                    workflow_test_result["exit_code"] = 0 if resize_checks["passed"] else 1
                    print(json.dumps(resize_checks, ensure_ascii=False))
                except Exception as exc:
                    resize_checks["passed"] = False
                    resize_checks["error"] = str(exc)
                    workflow_test_result["exit_code"] = 1
                    print(json.dumps(resize_checks, ensure_ascii=False))
                finally:
                    root.destroy()

            apply_step()

        root.after(900, run_resize_smooth_test)
    if button_audit_test:
        def run_button_audit_test():
            app = root._stream_app
            checks = {
                "button_count": 0,
                "missing_text": [],
                "missing_command": [],
                "narrow_buttons": [],
                "status_header_metrics": [],
                "status_header_fits": False,
            }
            seen_widgets = set()
            narrow_widget_keys = set()

            def collect(widget):
                try:
                    widget_id = str(widget)
                    first_seen = widget_id not in seen_widgets
                    if first_seen:
                        seen_widgets.add(widget_id)
                    widget_class = widget.winfo_class()
                    if widget_class in {"TButton", "Button"}:
                        try:
                            text = str(widget.cget("text") or "").strip()
                        except tk.TclError:
                            text = ""
                        try:
                            command = str(widget.cget("command") or "").strip()
                        except tk.TclError:
                            command = ""
                        if first_seen:
                            checks["button_count"] += 1
                            if not text:
                                checks["missing_text"].append(str(widget))
                            if not command:
                                checks["missing_command"].append(text or str(widget))
                        if widget.winfo_ismapped():
                            actual_width = int(widget.winfo_width())
                            required_width = int(widget.winfo_reqwidth())
                            if actual_width + 1 < required_width and widget_id not in narrow_widget_keys:
                                narrow_widget_keys.add(widget_id)
                                try:
                                    style_name = str(widget.cget("style") or "")
                                except tk.TclError:
                                    style_name = ""
                                checks["narrow_buttons"].append(
                                    {
                                        "text": text or str(widget),
                                        "width": actual_width,
                                        "required": required_width,
                                        "style": style_name,
                                    }
                                )
                    for child in widget.winfo_children():
                        collect(child)
                except tk.TclError:
                    return

            try:
                original_status = app.status_var.get()
                for tab_id in app.notebook.tabs():
                    app.notebook.select(tab_id)
                    root.update_idletasks()
                    collect(root)
                for status_text in (
                    "就绪",
                    "正在测试",
                    "正在检查更新",
                    "检查更新完成",
                    "检查更新失败",
                ):
                    app.status_var.set(status_text)
                    root.update_idletasks()
                    checks["status_header_metrics"].append(
                        {
                            "text": status_text,
                            "container": int(app.status_header_frame.winfo_width()),
                            "label": int(app.status_label.winfo_width()),
                            "label_required": int(app.status_label.winfo_reqwidth()),
                        }
                    )
                app.status_var.set(original_status)
                root.update_idletasks()
                checks["status_header_fits"] = bool(
                    checks["status_header_metrics"]
                    and all(
                        item["label"] + 1 >= item["label_required"]
                        for item in checks["status_header_metrics"]
                    )
                )
                checks["passed"] = bool(
                    checks["button_count"] >= 30
                    and not checks["missing_text"]
                    and not checks["missing_command"]
                    and not checks["narrow_buttons"]
                    and checks["status_header_fits"]
                )
                workflow_test_result["exit_code"] = 0 if checks["passed"] else 1
                print(json.dumps(checks, ensure_ascii=False))
            except Exception as exc:
                checks["passed"] = False
                checks["error"] = str(exc)
                workflow_test_result["exit_code"] = 1
                print(json.dumps(checks, ensure_ascii=False))
            finally:
                root.destroy()

        root.after(900, run_button_audit_test)
    if layout_report:
        root.geometry("1120x700")

        def emit_layout_report():
            root.update_idletasks()
            widgets = []

            def collect(widget):
                try:
                    widgets.append(
                        (
                            widget.winfo_reqwidth(),
                            widget.winfo_width(),
                            widget.winfo_x(),
                            widget.winfo_class(),
                            str(widget),
                        )
                    )
                    for child in widget.winfo_children():
                        collect(child)
                except tk.TclError:
                    return

            collect(root)
            app = root._stream_app
            original_tab = app.notebook.select()
            tab_bounds = []
            root_left = root.winfo_rootx()
            root_width = root.winfo_width()

            for tab_id in app.notebook.tabs():
                app.notebook.select(tab_id)
                root.update_idletasks()
                visible = []
                tab_widget = root.nametowidget(tab_id)

                def collect_visible(widget):
                    try:
                        if widget.winfo_ismapped():
                            left = widget.winfo_rootx() - root_left
                            width = widget.winfo_width()
                            right = left + width
                            if width > 1:
                                visible.append(
                                    {
                                        "left": left,
                                        "right": right,
                                        "width": width,
                                        "class": widget.winfo_class(),
                                        "widget": str(widget),
                                    }
                                )
                        for child in widget.winfo_children():
                            collect_visible(child)
                    except tk.TclError:
                        return

                collect_visible(tab_widget)
                overflow = [
                    item
                    for item in visible
                    if item["left"] < -1 or item["right"] > root_width + 1
                ]
                overflow.sort(
                    key=lambda item: max(-item["left"], item["right"] - root_width),
                    reverse=True,
                )
                tab_bounds.append(
                    {
                        "tab": app.notebook.tab(tab_id, "text"),
                        "max_right": max(
                            (item["right"] for item in visible),
                            default=0,
                        ),
                        "overflow_count": len(overflow),
                        "overflow": overflow[:8],
                    }
                )

            app.notebook.select(original_tab)
            root.update_idletasks()
            print(
                json.dumps(
                    {
                        "root_requested": [root.winfo_reqwidth(), root.winfo_reqheight()],
                        "root_actual": [root.winfo_width(), root.winfo_height()],
                        "header": [
                            root._stream_app.header_frame.winfo_x(),
                            root._stream_app.header_frame.winfo_width(),
                            root._stream_app.header_frame.winfo_reqwidth(),
                        ],
                        "status_header": [
                            root._stream_app.status_header_frame.winfo_x(),
                            root._stream_app.status_header_frame.winfo_width(),
                            root._stream_app.status_header_frame.winfo_reqwidth(),
                        ],
                        "next_step": [
                            root._stream_app.next_step_frame.winfo_x(),
                            root._stream_app.next_step_frame.winfo_width(),
                            root._stream_app.next_step_frame.winfo_reqwidth(),
                        ],
                        "workbench": {
                            "tab": [
                                app.analysis_tab.winfo_x(),
                                app.analysis_tab.winfo_y(),
                                app.analysis_tab.winfo_width(),
                                app.analysis_tab.winfo_height(),
                            ],
                            "paned": [
                                app.workbench_paned.winfo_x(),
                                app.workbench_paned.winfo_y(),
                                app.workbench_paned.winfo_width(),
                                app.workbench_paned.winfo_height(),
                                app.workbench_paned.winfo_reqheight(),
                                app.workbench_paned.sash_coord(0)[0],
                            ] if hasattr(app, "workbench_paned") else [],
                            "actions": [
                                app.task_actions_frame.winfo_x(),
                                app.task_actions_frame.winfo_y(),
                                app.task_actions_frame.winfo_width(),
                                app.task_actions_frame.winfo_height(),
                                app.task_actions_frame.winfo_reqheight(),
                            ] if hasattr(app, "task_actions_frame") else [],
                            "preview": [
                                app.video_preview_frame.winfo_x(),
                                app.video_preview_frame.winfo_y(),
                                app.video_preview_frame.winfo_width(),
                                app.video_preview_frame.winfo_height(),
                                app.video_preview_frame.winfo_reqheight(),
                                int(app.video_preview_frame.winfo_ismapped()),
                            ] if hasattr(app, "video_preview_frame") else [],
                            "result": [
                                app.result_text.winfo_x(),
                                app.result_text.winfo_y(),
                                app.result_text.winfo_width(),
                                app.result_text.winfo_height(),
                                app.result_text.winfo_reqheight(),
                                int(app.result_text.winfo_ismapped()),
                            ] if hasattr(app, "result_text") else [],
                        },
                        "tab_bounds": tab_bounds,
                        "widest_widgets": sorted(widgets, reverse=True)[:16],
                    },
                    ensure_ascii=False,
                )
            )
            root.destroy()

        root.after(1400, emit_layout_report)
    if smoke_test:
        root.after(1200, root.destroy)
    root.mainloop()
    return workflow_test_result["exit_code"]


def main():
    install_exception_hooks()
    parser = argparse.ArgumentParser(description=APP_DISPLAY_NAME)
    parser.add_argument("--cli", action="store_true", help="使用原命令行监听模式")
    parser.add_argument("--check", action="store_true", help="检查本机运行环境")
    parser.add_argument("--ui-smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-layout-report", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-workflow-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-tab-switch-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-video-preview-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-resize-smooth-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ui-button-audit-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release-acceptance-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--update-system-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release-site-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report-image-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--ui-preview-tab",
        choices=(
            "workbench",
            "source",
            "stream",
            "rules",
            "overview",
            "connection",
            "connection-public",
            "connection-direct",
            "connection-ssh",
            "settings",
            "settings-storage",
            "settings-stream",
            "settings-maintenance",
            "logs",
        ),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--ffmpeg-smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.check:
        raise SystemExit(run_health_check())
    if args.ffmpeg_smoke_test:
        ok, message = ffmpeg_smoke_test()
        print(message)
        raise SystemExit(0 if ok else 1)
    if args.update_system_test:
        raise SystemExit(update_system_self_test())
    if args.release_site_test:
        raise SystemExit(release_site_self_test())
    if args.report_image_test:
        raise SystemExit(report_image_self_test())
    if args.release_acceptance_test:
        raise SystemExit(release_acceptance_test())
    if args.cli:
        run_cli()
    else:
        raise SystemExit(
            run_gui(
                smoke_test=args.ui_smoke_test,
                layout_report=args.ui_layout_report,
                workflow_test=args.ui_workflow_test,
                tab_switch_test=args.ui_tab_switch_test,
                video_preview_test=args.ui_video_preview_test,
                resize_smooth_test=args.ui_resize_smooth_test,
                button_audit_test=args.ui_button_audit_test,
                preview_tab=args.ui_preview_tab,
            )
        )


if __name__ == "__main__":
    main()
