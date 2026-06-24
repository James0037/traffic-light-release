import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_short(version):
    parts = str(version).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(version)


def main():
    parser = argparse.ArgumentParser(description="Generate Traffic Light static release metadata.")
    parser.add_argument("--version", required=True, help="Version such as 2.2.0")
    parser.add_argument("--version-code", required=True, type=int, help="Integer version code, such as 220")
    parser.add_argument("--package", required=True, help="Installer or zip package path")
    parser.add_argument("--site-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--channel", default="stable")
    parser.add_argument("--minimum-supported-version", default="2.0.0")
    parser.add_argument("--release-date", default="")
    parser.add_argument("--homepage-url", default="https://your-domain.example/")
    parser.add_argument("--package-type", default="installer")
    parser.add_argument("--force-update", action="store_true")
    parser.add_argument("--note", action="append", default=[], help="Release note line")
    args = parser.parse_args()

    site_root = Path(args.site_root).resolve()
    package = Path(args.package).resolve()
    downloads = site_root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    target = downloads / package.name
    if package != target:
        target.write_bytes(package.read_bytes())

    digest = sha256_file(target)
    size = target.stat().st_size
    encoded_name = quote(target.name)
    short = version_short(args.version)
    notes = args.note or [f"发布 Traffic Light V{args.version}。"]
    payload = {
        "app_name": "Traffic Light",
        "latest_version": args.version,
        "version": args.version,
        "version_code": args.version_code,
        "release_date": args.release_date,
        "channel": args.channel,
        "minimum_supported_version": args.minimum_supported_version,
        "force_update": bool(args.force_update),
        "mandatory": bool(args.force_update),
        "package_type": args.package_type,
        "download_url": f"../../downloads/{encoded_name}",
        "file_name": target.name,
        "sha256": digest,
        "file_size": size,
        "release_notes": notes,
        "notes": notes,
        "homepage_url": args.homepage_url,
        "manual_download_url": "../../downloads/",
    }

    version_dir = site_root / "releases" / f"v{short}"
    latest_dir = site_root / "releases" / "latest"
    version_dir.mkdir(parents=True, exist_ok=True)
    latest_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    (version_dir / "update.json").write_text(text + "\n", encoding="utf-8")
    (latest_dir / "update.json").write_text(text + "\n", encoding="utf-8")
    (site_root / "update.json").write_text(text + "\n", encoding="utf-8")

    checksums = site_root / "checksums"
    checksums.mkdir(parents=True, exist_ok=True)
    checksum_line = f"{digest}  {target.name}"
    (checksums / f"{target.name}.sha256").write_text(checksum_line + "\n", encoding="utf-8")
    all_lines = []
    for file in sorted(downloads.iterdir()):
        if file.is_file():
            all_lines.append(f"{sha256_file(file)}  {file.name}")
    (checksums / "SHA256SUMS.txt").write_text("\n".join(all_lines) + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
