import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


REQUIRED_FIELDS = (
    "app_name",
    "latest_version",
    "version_code",
    "release_date",
    "channel",
    "minimum_supported_version",
    "force_update",
    "package_type",
    "download_url",
    "sha256",
    "file_size",
    "release_notes",
    "homepage_url",
    "manual_download_url",
)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def site_root_from_script():
    return Path(__file__).resolve().parents[1]


def result(name, passed, detail=""):
    return {"name": name, "passed": bool(passed), "detail": str(detail or "")}


def resolve_download_path(site_root, update_json_path, payload):
    download_url = str(payload.get("download_url") or "").strip()
    file_name = str(payload.get("file_name") or "").strip()
    if not download_url and not file_name:
        raise ValueError("update.json 中缺少 download_url 或 file_name。")

    if download_url:
        parsed = urlparse(download_url)
        if parsed.scheme in {"http", "https"}:
            name = Path(unquote(parsed.path)).name
            if not name:
                raise ValueError(f"download_url 中没有文件名：{download_url}")
            return site_root / "downloads" / name
        return (update_json_path.parent / unquote(download_url)).resolve()

    return site_root / "downloads" / file_name


def index_links_to_file(index_html, file_name):
    encoded = quote(file_name)
    candidates = (
        f"downloads/{file_name}",
        f"downloads/{encoded}",
        f"downloads\\{file_name}",
        f"downloads\\{encoded}",
    )
    return any(candidate in index_html for candidate in candidates)


def validate(site_root):
    site_root = Path(site_root).resolve()
    index_path = site_root / "index.html"
    latest_path = site_root / "releases" / "latest" / "update.json"
    checks = []
    errors = []
    payload = {}
    download_path = None

    checks.append(result("检查 release_site/index.html 是否存在", index_path.exists(), index_path))
    checks.append(result("检查 releases/latest/update.json 是否存在", latest_path.exists(), latest_path))

    if latest_path.exists():
        try:
            payload = json.loads(latest_path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("JSON 根节点不是对象。")
            checks.append(result("检查 latest/update.json 格式是否正确", True, "JSON 格式正确"))
        except Exception as exc:
            checks.append(result("检查 latest/update.json 格式是否正确", False, exc))
            errors.append(f"latest/update.json 格式错误：{exc}")
    else:
        errors.append(f"latest/update.json 不存在：{latest_path}")

    if payload:
        missing = [field for field in REQUIRED_FIELDS if field not in payload]
        checks.append(result("检查 update.json 必需字段", not missing, "字段完整" if not missing else "缺少：" + ", ".join(missing)))
        if missing:
            errors.append("update.json 缺少字段：" + ", ".join(missing))

        download_url = str(payload.get("download_url") or "")
        checks.append(result("读取 update.json 中的 download_url", bool(download_url), download_url or "download_url 为空"))
        if not download_url:
            errors.append("download_url 为空。")

        try:
            download_path = resolve_download_path(site_root, latest_path, payload)
            exists = download_path.exists() and download_path.is_file()
            checks.append(result("检查 downloads 中对应文件是否存在", exists, download_path))
            if not exists:
                errors.append(f"下载文件不存在：{download_path}")
        except Exception as exc:
            checks.append(result("根据 download_url 或文件名解析下载文件", False, exc))
            errors.append(str(exc))

    if download_path is not None and download_path.exists():
        try:
            actual_sha = sha256_file(download_path)
            expected_sha = str(payload.get("sha256") or "").lower()
            checks.append(result("重新计算下载包 SHA256", True, actual_sha))
            sha_ok = actual_sha.lower() == expected_sha
            checks.append(result("对比 update.json 中的 sha256", sha_ok, f"expected={expected_sha}, actual={actual_sha}"))
            if not sha_ok:
                errors.append("SHA256 不一致。")
        except Exception as exc:
            checks.append(result("重新计算下载包 SHA256", False, exc))
            errors.append(f"SHA256 计算失败：{exc}")

        try:
            actual_size = download_path.stat().st_size
            expected_size = int(payload.get("file_size") or 0)
            size_ok = actual_size == expected_size
            checks.append(result("对比 update.json 中的 file_size", size_ok, f"expected={expected_size}, actual={actual_size}"))
            if not size_ok:
                errors.append("file_size 不一致。")
        except Exception as exc:
            checks.append(result("对比 update.json 中的 file_size", False, exc))
            errors.append(f"file_size 检查失败：{exc}")

    if index_path.exists() and download_path is not None:
        try:
            html = index_path.read_text(encoding="utf-8")
            link_ok = index_links_to_file(html, download_path.name)
            checks.append(result("检查 index.html 下载链接是否指向同一个文件", link_ok, download_path.name))
            if not link_ok:
                errors.append(f"index.html 下载链接未指向 {download_path.name}。")
        except Exception as exc:
            checks.append(result("检查 index.html 下载链接是否指向同一个文件", False, exc))
            errors.append(f"index.html 读取失败：{exc}")

    passed = all(item["passed"] for item in checks)
    return {
        "passed": passed,
        "site_root": str(site_root),
        "index_html": str(index_path),
        "latest_update_json": str(latest_path),
        "download_file": str(download_path) if download_path else "",
        "checks": checks,
        "errors": errors,
    }


def print_human(report):
    print("Traffic Light release_site validation")
    print(f"Site root: {report['site_root']}")
    print("")
    for item in report["checks"]:
        mark = "PASS" if item["passed"] else "FAIL"
        detail = f" - {item['detail']}" if item["detail"] else ""
        print(f"[{mark}] {item['name']}{detail}")
    print("")
    if report["passed"]:
        print("Result: PASS")
    else:
        print("Result: FAIL")
        print("Reasons:")
        for error in report["errors"]:
            print(f"- {error}")


def main():
    parser = argparse.ArgumentParser(description="Validate Traffic Light release_site before deployment.")
    parser.add_argument("--site-root", default=str(site_root_from_script()), help="release_site root directory.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    args = parser.parse_args()

    report = validate(args.site_root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
