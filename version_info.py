import json
import sys
from pathlib import Path


DEFAULT_VERSION_INFO = {
    "app_name": "Traffic Light",
    "version": "2.1.1",
    "version_short": "2.1",
    "edition": "便携版",
    "publisher": "Traffic Light",
}


def resource_dir():
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def load_version_info():
    path = resource_dir() / "version.json"
    info = DEFAULT_VERSION_INFO.copy()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            info.update({key: value for key, value in loaded.items() if value not in (None, "")})
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return info


VERSION_INFO = load_version_info()
APP_NAME = str(VERSION_INFO["app_name"])
APP_VERSION = str(VERSION_INFO["version"])
APP_VERSION_SHORT = str(VERSION_INFO.get("version_short") or ".".join(APP_VERSION.split(".")[:2]))
APP_VERSION_TAG = f"V{APP_VERSION_SHORT}"
APP_EDITION = str(VERSION_INFO.get("edition") or "便携版")
APP_PUBLISHER = str(VERSION_INFO.get("publisher") or APP_NAME)
APP_DISPLAY_NAME = f"{APP_NAME} V{APP_VERSION}"
APP_INSTALL_NAME = f"{APP_NAME} {APP_VERSION_TAG}"
APP_DATA_DIR_NAME = f"{APP_NAME}_{APP_VERSION_TAG}_数据"
EXE_NAME = f"{APP_NAME}_{APP_VERSION_TAG}.exe"
PORTABLE_EXE_NAME = f"{APP_NAME}_{APP_VERSION_TAG}_免安装测试版.exe"
SETUP_EXE_NAME = f"{APP_NAME}_{APP_VERSION_TAG}_Setup.exe"
UNINSTALL_EXE_NAME = f"卸载{APP_NAME}_{APP_VERSION_TAG}.exe"
CHANGELOG_NAME = f"CHANGELOG_{APP_VERSION_TAG}.md"
