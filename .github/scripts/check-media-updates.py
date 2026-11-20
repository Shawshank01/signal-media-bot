"""Compare dependencies in the published image with the build's package sources."""

import json
import os
import re
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path


def installed_versions() -> dict[str, str]:
    return {
        "ffmpeg": subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", "ffmpeg"], text=True
        ).strip(),
        "yt-dlp": version("yt-dlp"),
    }


def available_updates(
    requirement: str, installed_ffmpeg: str, installed_yt_dlp: str
) -> dict[str, tuple[str, bool]]:
    from pip._vendor.packaging.version import Version

    subprocess.run(
        ["apt-get", "-qq", "-o", "APT::Update::Error-Mode=any", "update"],
        check=True,
        stdout=sys.stderr,
    )
    policy = subprocess.check_output(["apt-cache", "policy", "ffmpeg"], text=True)
    candidate = re.search(r"^\s*Candidate:[ \t]+(\S+)", policy, re.MULTILINE)
    if not candidate or candidate[1] == "(none)":
        raise RuntimeError("APT did not report a candidate version for FFmpeg")
    available_ffmpeg = candidate[1]
    comparison = subprocess.run(
        ["dpkg", "--compare-versions", available_ffmpeg, "gt", installed_ffmpeg],
        check=False,
    )
    if comparison.returncode != 1:
        comparison.check_returncode()

    report = subprocess.check_output(
        [
            sys.executable, "-m", "pip", "install", "--quiet",
            "--disable-pip-version-check", "--root-user-action=ignore",
            "--dry-run", "--ignore-installed", "--no-deps", "--report", "-", requirement,
        ],
        text=True,
    )
    update, = json.loads(report)["install"]
    available_yt_dlp = update["metadata"]["version"]
    return {
        "ffmpeg": (available_ffmpeg, comparison.returncode == 0),
        "yt-dlp": (available_yt_dlp, Version(available_yt_dlp) > Version(installed_yt_dlp)),
    }


def probe(image, platform, mode, *arguments):
    command = [
        "docker", "run", "--rm", "--pull=always", "--platform", platform,
        "--interactive", "--entrypoint", "python",
    ]
    if mode == "installed":
        command.extend(["--network", "none"])
    result = subprocess.run(
        [*command, image, "-", mode, *arguments],
        input=Path(__file__).read_text(),
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def check_published_image() -> None:
    published = os.environ["PUBLISHED_IMAGE"]
    base = next(
        line.split()[1] for line in Path("Dockerfile").read_text().splitlines()
        if line.startswith("FROM ")
    )
    requirement = next(
        line.strip() for line in Path("requirements.txt").read_text().splitlines()
        if re.match(r"^yt-dlp(?=$|[\s<>=!~;\[])", line.strip(), re.IGNORECASE)
    )
    rebuild = False
    for platform in ("linux/amd64", "linux/arm64"):
        installed = probe(published, platform, "installed")
        updates = probe(
            base, platform, "available", requirement, installed["ffmpeg"], installed["yt-dlp"]
        )
        for package, (candidate, newer) in updates.items():
            print(
                f"{platform} {package}: published {installed[package]}, available {candidate}",
                flush=True,
            )
            rebuild = rebuild or newer

    print("A new version is available." if rebuild else "No newer version was found.")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        print(f"rebuild={str(rebuild).lower()}", file=output)


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments == ["installed"]:
        print(json.dumps(installed_versions()))
    elif arguments and arguments[0] == "available":
        print(json.dumps(available_updates(*arguments[1:])))
    else:
        check_published_image()
