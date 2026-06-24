import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_url(url, timeout):
    request = Request(url, headers={"User-Agent": "TrafficLightReleaseVerifier/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.status, response.headers, response.read()


def build_update_url(value):
    value = str(value or "").strip()
    if not value:
        raise ValueError("URL is required.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://.")
    if parsed.path.endswith(".json"):
        return value
    return value.rstrip("/") + "/releases/latest/update.json"


def main():
    parser = argparse.ArgumentParser(description="Verify a deployed public Traffic Light update endpoint.")
    parser.add_argument("url", help="Public site base URL or full update.json URL.")
    parser.add_argument("--current-version", default="2.0.0", help="Version used by the test client.")
    parser.add_argument("--download", action="store_true", help="Download the package and verify SHA256.")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    update_url = build_update_url(args.url)
    base_url = update_url.rsplit("/releases/latest/update.json", 1)[0] + "/"
    details = {}
    errors = []

    try:
        status, _, body = read_url(base_url, args.timeout)
        details["homepage_accessible"] = status == 200
    except Exception as exc:
        details["homepage_accessible"] = False
        errors.append(f"homepage failed: {exc}")

    payload = {}
    try:
        status, headers, body = read_url(update_url, args.timeout)
        details["update_json_accessible"] = status == 200
        details["update_json_content_type"] = headers.get("Content-Type", "")
        payload = json.loads(body.decode("utf-8-sig"))
        details["update_json_valid"] = isinstance(payload, dict)
    except Exception as exc:
        details["update_json_accessible"] = False
        details["update_json_valid"] = False
        errors.append(f"update.json failed: {exc}")

    download_url = ""
    if payload:
        required = ["latest_version", "download_url", "sha256", "file_size"]
        missing = [field for field in required if not payload.get(field)]
        details["required_fields_present"] = not missing
        if missing:
            errors.append("missing fields: " + ", ".join(missing))
        download_url = urljoin(update_url, str(payload.get("download_url") or ""))
        details["resolved_download_url"] = download_url

    if download_url and not args.download:
        try:
            request = Request(download_url, method="HEAD", headers={"User-Agent": "TrafficLightReleaseVerifier/1.0"})
            with urlopen(request, timeout=args.timeout) as response:
                details["download_head_accessible"] = response.status in {200, 206}
                details["download_head_content_length"] = response.headers.get("Content-Length", "")
        except Exception as exc:
            details["download_head_accessible"] = False
            errors.append(f"download HEAD failed: {exc}")

    if download_url and args.download:
        try:
            with tempfile.TemporaryDirectory(prefix="traffic_light_public_update_") as temp_dir:
                target = Path(temp_dir) / (Path(urlparse(download_url).path).name or "download.bin")
                request = Request(download_url, headers={"User-Agent": "TrafficLightReleaseVerifier/1.0"})
                with urlopen(request, timeout=args.timeout) as response, target.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 512)
                        if not chunk:
                            break
                        output.write(chunk)
                expected_size = int(payload.get("file_size") or 0)
                actual_size = target.stat().st_size
                actual_sha = sha256_file(target)
                details["download_ok"] = actual_size > 0
                details["file_size_matches"] = actual_size == expected_size
                details["sha256_matches"] = actual_sha.lower() == str(payload.get("sha256") or "").lower()
        except Exception as exc:
            details["download_ok"] = False
            details["file_size_matches"] = False
            details["sha256_matches"] = False
            errors.append(f"download verification failed: {exc}")

    passed = bool(details.get("homepage_accessible")) and bool(details.get("update_json_accessible")) and bool(details.get("update_json_valid")) and bool(details.get("required_fields_present", True))
    if args.download:
        passed = passed and bool(details.get("download_ok")) and bool(details.get("file_size_matches")) and bool(details.get("sha256_matches"))
    else:
        passed = passed and bool(details.get("download_head_accessible", True))

    report = {
        "passed": passed,
        "update_url": update_url,
        "base_url": base_url,
        "details": details,
        "errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
