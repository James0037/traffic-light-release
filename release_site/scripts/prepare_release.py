import argparse
import datetime as _dt
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import quote


DEFAULT_NOTES = [
    "新增自动检查更新功能",
    "新增公网官网发布端",
    "新增报告抽帧图片导出功能",
    "优化 FFmpeg 或其他抽帧方案",
]


def sha256_file(path):
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise RuntimeError(f"SHA256 计算失败：{exc}") from exc


def normalize_version(value):
    version = str(value or "").strip().lstrip("vV")
    if not version:
        raise ValueError("版本号不能为空。")
    return version


def version_short(version):
    parts = str(version).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(version)


def default_version_code(version):
    parts = []
    for part in str(version).split(".")[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits or 0))
    while len(parts) < 3:
        parts.append(0)
    return parts[0] * 100 + parts[1] * 10 + parts[2]


def normalize_base_url(value):
    base_url = str(value or "").strip()
    if not base_url:
        raise ValueError("网站基础 URL 不能为空。")
    return base_url.rstrip("/") + "/"


def target_file_name(version, package):
    suffix = Path(package).suffix.lower() or ".zip"
    return f"Traffic_Light_v{version}{suffix}"


def package_type_for(path):
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix == "exe":
        return "installer"
    return suffix or "zip"


def write_json(path, payload):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"update.json 写入失败：{path}，{exc}") from exc


def write_update_files(site_root, payload, short_version):
    write_json(site_root / "releases" / f"v{short_version}" / "update.json", payload)
    write_json(site_root / "releases" / "latest" / "update.json", payload)
    write_json(site_root / "update.json", payload)


def update_index(site_root, version, release_date, package_name):
    index = site_root / "index.html"
    if not index.exists():
        return False
    try:
        html = index.read_text(encoding="utf-8")
        encoded_name = quote(package_name)
        html = re.sub(r"当前版本[：:]?\s*V[^<]+", f"当前版本：V{version}", html)
        html = re.sub(r"当前版本 V[^<]+", f"当前版本 V{version}", html)
        html = re.sub(r"V\d+\.\d+(?:\.\d+)?", f"V{version}", html)
        html = re.sub(r"发布时间[：:]?\s*\d{4}-\d{2}-\d{2}", f"发布时间：{release_date}", html)
        html = re.sub(r"发布日期\s+\d{4}-\d{2}-\d{2}", f"发布日期 {release_date}", html)
        html = re.sub(r'href="downloads/[^"]+"', f'href="downloads/{encoded_name}"', html)
        index.write_text(html, encoding="utf-8")
        return True
    except OSError as exc:
        raise RuntimeError(f"index.html 更新失败：{exc}") from exc


def write_checksums(site_root, target):
    checksums = site_root / "checksums"
    downloads = site_root / "downloads"
    try:
        checksums.mkdir(parents=True, exist_ok=True)
        digest = sha256_file(target)
        (checksums / f"{target.name}.sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
        lines = []
        for file in sorted(downloads.iterdir(), key=lambda item: item.name.lower()):
            if file.is_file():
                lines.append(f"{sha256_file(file)}  {file.name}")
        (checksums / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"校验文件写入失败：{exc}") from exc


def update_public_metadata(site_root, base_url, release_date):
    try:
        (site_root / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\n\nSitemap: {base_url}sitemap.xml\n",
            encoding="utf-8",
        )
        sitemap = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base_url}</loc>
    <lastmod>{release_date}</lastmod>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{base_url}releases/latest/update.json</loc>
    <lastmod>{release_date}</lastmod>
    <priority>0.8</priority>
  </url>
</urlset>
"""
        (site_root / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"网站元数据写入失败：{exc}") from exc


def prepare_release(args):
    version = normalize_version(args.version)
    base_url = normalize_base_url(args.base_url)
    site_root = Path(args.site_root).resolve()
    package = Path(args.package).expanduser().resolve()
    if not package.exists() or not package.is_file():
        raise FileNotFoundError(f"安装包不存在：{package}")

    release_date = args.release_date or _dt.date.today().isoformat()
    downloads = site_root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    target = downloads / target_file_name(version, package)
    try:
        shutil.copy2(package, target)
    except OSError as exc:
        raise RuntimeError(f"复制安装包失败：{package} -> {target}，{exc}") from exc

    digest = sha256_file(target)
    size = target.stat().st_size
    encoded_name = quote(target.name)
    short = version_short(version)
    version_code = args.version_code or default_version_code(version)
    notes = args.note or DEFAULT_NOTES

    payload = {
        "app_name": args.app_name,
        "latest_version": version,
        "version_code": version_code,
        "release_date": release_date,
        "channel": args.channel,
        "minimum_supported_version": args.minimum_supported_version,
        "force_update": bool(args.force_update),
        "package_type": package_type_for(target),
        "download_url": f"{base_url}downloads/{encoded_name}",
        "sha256": digest,
        "file_size": size,
        "release_notes": notes,
        "homepage_url": base_url,
        "manual_download_url": f"{base_url}downloads/",
    }

    write_update_files(site_root, payload, short)
    update_index(site_root, version, release_date, target.name)
    update_public_metadata(site_root, base_url, release_date)
    write_checksums(site_root, target)

    return {
        "site_root": str(site_root),
        "version": version,
        "version_code": version_code,
        "package": str(target),
        "sha256": digest,
        "file_size": size,
        "download_url": payload["download_url"],
        "latest_update_json": str(site_root / "releases" / "latest" / "update.json"),
        "version_update_json": str(site_root / "releases" / f"v{short}" / "update.json"),
    }


def main():
    parser = argparse.ArgumentParser(description="Prepare Traffic Light website release files.")
    parser.add_argument("--version", required=True, help="Version such as 2.2.0.")
    parser.add_argument("--package", required=True, help="Installer or zip package path. Chinese paths and spaces are supported.")
    parser.add_argument("--base-url", required=True, help="Website base URL, such as https://traffic-light.cn/")
    parser.add_argument("--site-root", default=str(Path(__file__).resolve().parents[1]), help="release_site root directory.")
    parser.add_argument("--version-code", type=int, default=0, help="Optional integer version code. Default is generated from version.")
    parser.add_argument("--app-name", default="Traffic Light")
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--minimum-supported-version", default="2.0.0")
    parser.add_argument("--release-date", default="", help="YYYY-MM-DD. Default is today.")
    parser.add_argument("--force-update", action="store_true")
    parser.add_argument("--note", action="append", default=[], help="Release note line. Can be used more than once.")
    args = parser.parse_args()

    try:
        report = prepare_release(args)
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
