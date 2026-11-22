"""`aidlc container`: run aidlc and its agents in a container that sees only the current repository.

Works on Linux, macOS and Windows (Docker Desktop). Environment:
  AIDLC_IMAGE    image tag (default aidlc-agent:local; built from the plugin checkout on first use)
  AIDLC_NETWORK  docker network (default bridge); point it at a network whose egress only reaches your
                 model API, git host and package registry.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

from aidlc import gitutil
from aidlc.resources import plugin_root

# Only model keys, a scoped git token, Jira credentials and the adopter's own telemetry collector settings are
# forwarded; never cloud/deploy credentials.
FORWARDED_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GH_TOKEN",
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_PAT",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "AIDLC_TELEMETRY_SALT",
)
DEFAULT_IMAGE = "aidlc-agent:local"


def container_path(host_path: str, windows: bool) -> str:
    """Where a host directory is mounted. POSIX keeps the same path so git worktree paths stay valid;
    Windows `C:\\src\\app` becomes `/c/src/app`."""
    if not windows:
        return host_path
    p = PureWindowsPath(host_path)
    drive = p.drive.rstrip(":").lower()
    return "/" + "/".join([drive, *p.parts[1:]]) if drive else p.as_posix()


def docker_args(
    root: str,
    common: str,
    argv: list[str],
    *,
    image: str,
    network: str,
    env: dict[str, str],
    tty: bool,
    windows: bool,
    user: str | None,
) -> list[str]:
    workdir = container_path(root, windows)
    mounts = ["--mount", f"type=bind,source={root},target={workdir}"]
    root_c, common_c = container_path(root, windows), container_path(common, windows)
    if windows:
        root_c, common_c = root_c.lower(), common_c.lower()
    if common_c != root_c and not common_c.startswith(root_c.rstrip("/") + "/"):
        # A linked worktree keeps its git metadata in the main repository; mount that too.
        mounts += ["--mount", f"type=bind,source={common},target={container_path(common, windows)}"]
    args = ["docker", "run", "--rm"]
    args += [a for var in FORWARDED_ENV if env.get(var) for a in ("-e", var)]
    if tty:
        args.append("-it")
    if user:
        args += ["--user", user]
    args += [
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:exec",
        "--tmpfs",
        "/home/agent:exec",
        "--network",
        network,
        *mounts,
        "-w",
        workdir,
        image,
        *argv,
    ]
    return args


def _build(image: str) -> None:
    root = plugin_root()
    dockerfile = root / "integrations" / "container" / "Dockerfile"
    if not dockerfile.exists():
        raise SystemExit(
            f"aidlc: image {image} not found and no Dockerfile under {root}; set AIDLC_PLUGIN_ROOT to an "
            "aidlc-plugin checkout (e.g. the installed plugin directory) or AIDLC_IMAGE to a prebuilt image"
        )
    subprocess.run(["docker", "build", "-t", image, "-f", str(dockerfile), str(root)], check=True)


def main(argv: list[str]) -> int:
    if not shutil.which("docker"):
        raise SystemExit("aidlc: docker not found; install Docker Desktop (macOS/Windows) or Docker Engine")
    root = gitutil.toplevel(Path.cwd())
    common = gitutil.git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root).strip()
    image = os.environ.get("AIDLC_IMAGE", DEFAULT_IMAGE)
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        _build(image)
    windows = os.name == "nt"
    args = docker_args(
        str(root),
        str(Path(common)),
        argv,
        image=image,
        network=os.environ.get("AIDLC_NETWORK", "bridge"),
        env=dict(os.environ),
        tty=sys.stdin.isatty() and sys.stdout.isatty(),
        windows=windows,
        user=None if windows else f"{os.getuid()}:{os.getgid()}",
    )
    return subprocess.run(args).returncode
