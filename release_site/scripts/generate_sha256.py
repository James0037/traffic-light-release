import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Generate SHA256 and file size for a release package.")
    parser.add_argument("file", help="Package path. Chinese paths and spaces are supported.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only.")
    args = parser.parse_args()

    input_path = str(args.file)
    path = Path(input_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise SystemExit(f"File not found: {path}")

    size_bytes = path.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    payload = {
        "file": input_path,
        "resolved_file": str(path),
        "file_name": path.name,
        "sha256": sha256_file(path),
        "file_size": size_bytes,
        "file_size_mb": round(size_mb, 2),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"File: {payload['file']}")
        print(f"Size: {payload['file_size']} bytes ({payload['file_size_mb']:.2f} MB)")
        print(f"SHA256: {payload['sha256']}")


if __name__ == "__main__":
    main()
