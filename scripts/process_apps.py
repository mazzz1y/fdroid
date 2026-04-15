#!/usr/bin/env python3
import base64
import glob
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

import yaml

REPO_DIR = "repo"
METADATA_DIR = "metadata"

log = logging.getLogger("process_apps")


def apk_package_name(apk_path: str) -> str:
    result = subprocess.run(
        ["aapt2", "dump", "packagename", apk_path],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def apk_version_code(apk_path: str) -> int:
    result = subprocess.run(
        ["aapt2", "dump", "badging", apk_path],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("package:"):
            for token in line.split():
                if token.startswith("versionCode="):
                    return int(token.split("=", 1)[1].strip("'"))
    raise ValueError(f"versionCode not found in {apk_path}")


def apk_version_name(apk_path: str) -> str:
    result = subprocess.run(
        ["aapt2", "dump", "badging", apk_path],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("package:"):
            for token in line.split():
                if token.startswith("versionName="):
                    return token.split("=", 1)[1].strip("'")
    raise ValueError(f"versionName not found in {apk_path}")


def apk_signer_sha256(apk_path: str) -> str | None:
    result = subprocess.run(
        ["apksigner", "verify", "--print-certs-pem", apk_path],
        capture_output=True,
        text=True,
        check=True,
    )
    pem_lines: list[str] = []
    in_cert = False
    for line in result.stdout.splitlines():
        if line == "-----BEGIN CERTIFICATE-----":
            in_cert = True
            pem_lines = []
        elif line == "-----END CERTIFICATE-----":
            der = base64.b64decode("".join(pem_lines))
            return hashlib.sha256(der).hexdigest()
        elif in_cert:
            pem_lines.append(line.strip())
    return None


def verify_apk_signer(apk_path: str, expected: str) -> None:
    actual = apk_signer_sha256(apk_path)
    if actual != expected:
        raise SystemExit(
            f"Signer mismatch for {os.path.basename(apk_path)}: "
            f"got {actual}, expected {expected}"
        )


def download_release_apks(repo: str, dest: str) -> list[str]:
    result = subprocess.run(
        [
            "gh",
            "release",
            "download",
            "--repo",
            repo,
            "--pattern",
            "*.apk",
            "--dir",
            dest,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.warning(
            "No releases or download failed for %s: %s", repo, result.stderr.strip()
        )
        return []
    return sorted(glob.glob(os.path.join(dest, "*.apk")))


def get_release_info(repo: str) -> dict | None:
    result = subprocess.run(
        [
            "gh",
            "release",
            "view",
            "--repo",
            repo,
            "--json",
            "tagName,publishedAt",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def fetch_metadata(repo: str, tag: str, metadata_path: str, dest: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp_dir:
        result = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                tag,
                f"https://github.com/{repo}.git",
                tmp_dir,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            log.warning("Failed to clone %s@%s", repo, tag)
            return False

        src = os.path.join(tmp_dir, metadata_path)
        if not os.path.isdir(src):
            log.warning(
                "No metadata directory at %s in %s@%s", metadata_path, repo, tag
            )
            return False

        shutil.copytree(src, dest, dirs_exist_ok=True)
    return True


def expand_changelogs(metadata_dir: str, version_codes: list[int]) -> None:
    if not version_codes:
        return
    for locale_dir in glob.glob(os.path.join(metadata_dir, "*/changelogs")):
        for code in version_codes:
            dest = os.path.join(locale_dir, f"{code}.txt")
            if os.path.exists(dest):
                continue
            base = os.path.join(locale_dir, f"{code // 10}.txt")
            if os.path.isfile(base):
                shutil.copy2(base, dest)


def write_app_metadata(
    package: str,
    template: dict,
    version_name: str,
    version_codes: list[int],
) -> None:
    app = dict(template)
    app["CurrentVersion"] = version_name
    app["CurrentVersionCode"] = max(version_codes)
    app["Builds"] = [
        {"versionName": version_name, "versionCode": code}
        for code in sorted(version_codes)
    ]
    dest = os.path.join(METADATA_DIR, f"{package}.yml")
    with open(dest, "w") as f:
        yaml.safe_dump(app, f, sort_keys=False, default_flow_style=False)
    log.info("Wrote %s", dest)


def process_app(app: dict) -> None:
    repo = app["github"]
    allowed_signer = app.get("allowed_signer")
    metadata_path = app.get("metadata_path")
    metadata_template = app.get("metadata")
    if not metadata_template:
        raise SystemExit(f"Missing 'metadata' block for {repo} in apps.yml")

    log.info("Processing %s", repo)

    with tempfile.TemporaryDirectory() as dl_dir:
        apks = download_release_apks(repo, dl_dir)
        if not apks:
            log.warning("Skipping %s: no APKs", repo)
            return

        if allowed_signer:
            for apk in apks:
                verify_apk_signer(apk, allowed_signer)
                log.info("Verified %s", os.path.basename(apk))

        package = apk_package_name(apks[0])
        version_codes = [apk_version_code(apk) for apk in apks]
        version_name = apk_version_name(apks[0])
        log.info("Package: %s", package)

        moved = []
        for apk in apks:
            dest = os.path.join(REPO_DIR, os.path.basename(apk))
            shutil.move(apk, dest)
            moved.append(dest)

    release = get_release_info(repo)
    if not release:
        log.warning("No release info for %s", repo)
        return

    tag = release["tagName"]
    published = release.get("publishedAt")
    if published:
        ts = datetime.fromisoformat(published).timestamp()
        for apk in moved:
            os.utime(apk, (ts, ts))

    log.info("Tag: %s", tag)

    os.makedirs(METADATA_DIR, exist_ok=True)
    write_app_metadata(package, metadata_template, version_name, version_codes)

    if metadata_path:
        dest = os.path.join(METADATA_DIR, package)
        if fetch_metadata(repo, tag, metadata_path, dest):
            if app.get("abi_suffix"):
                expand_changelogs(dest, version_codes)
            log.info("Metadata copied to %s", dest)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    with open("apps.yml") as f:
        config = yaml.safe_load(f)

    os.makedirs(REPO_DIR, exist_ok=True)

    for app in config["apps"]:
        process_app(app)


if __name__ == "__main__":
    main()
