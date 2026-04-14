#!/usr/bin/env python3
import base64
import glob
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile

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


def get_latest_tag(repo: str) -> str | None:
    result = subprocess.run(
        [
            "gh",
            "release",
            "view",
            "--repo",
            repo,
            "--json",
            "tagName",
            "-q",
            ".tagName",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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


def process_app(app: dict) -> None:
    repo = app["github"]
    allowed_signer = app.get("allowed_signer")
    metadata_path = app.get("metadata_path")

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
        log.info("Package: %s", package)

        for apk in apks:
            shutil.move(apk, os.path.join(REPO_DIR, os.path.basename(apk)))

    if not metadata_path:
        return

    tag = get_latest_tag(repo)
    if not tag:
        log.warning("Skipping metadata for %s: no tag found", repo)
        return
    log.info("Tag: %s", tag)

    dest = os.path.join(METADATA_DIR, package)
    if fetch_metadata(repo, tag, metadata_path, dest):
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
