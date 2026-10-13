"""`zqr release`: build this checkout into images and run them on a deployment host.

Source code never leaves the development machine. The runtime image is tagged `zqr:<version>-<commit>` and the
agent image `zqr-agent:<hash of Dockerfile.agent>`; both are built for the target's platform (cross-built when the
target differs) and streamed into `ssh TARGET docker load` when the target lacks them, and the host
wrapper (`scripts/zqr-host.sh`, installed from the image as ~/.local/bin/zqr) switches the desk container to the
new version and remembers the previous one for `zqr rollback`.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
import time
import tomllib

from quant_research.runtime import in_release_container, root

IMAGE_INPUTS = ("src", "benchmark", "pyproject.toml", "uv.lock", "Dockerfile", "Dockerfile.agent", ".dockerignore",
                "scripts/zqr-host.sh")
HOST_WRAPPER = "~/.local/bin/zqr"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=root(), check=True, capture_output=True, text=True).stdout.strip()


def release_version(allow_dirty: bool = False) -> str:
    base = tomllib.loads((root() / "pyproject.toml").read_text())["project"]["version"]
    v = f"{base}-{_git('rev-parse', '--short=8', 'HEAD')}"
    if _git("status", "--porcelain", "--", *IMAGE_INPUTS):
        if not allow_dirty:
            raise SystemExit("uncommitted changes in files that go into the image; commit them, or pass "
                             "--allow-dirty for a throwaway build")
        v += time.strftime("-dirty%Y%m%d%H%M%S")
    return v


def agent_tag() -> str:
    return "zqr-agent:" + hashlib.sha256((root() / "Dockerfile.agent").read_bytes()).hexdigest()[:12]


class Target:
    """Where the desk runs: `local` (this machine's Docker) or an ssh destination."""

    def __init__(self, name: str):
        self.name = name
        self.local = name == "local"

    def sh(self, script: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
        argv = ["bash", "-c", script] if self.local else ["ssh", self.name, script]
        return subprocess.run(argv, check=check, capture_output=capture, text=True)

    def platform(self) -> str:
        out = self.sh("docker version -f '{{.Server.Os}}/{{.Server.Arch}}'", capture=True).stdout.strip()
        return out.splitlines()[-1]

    def has_image(self, image: str) -> bool:
        return self.sh(f"docker image inspect {shlex.quote(image)} >/dev/null 2>&1", check=False).returncode == 0

    def wrapper(self, image: str) -> str:
        """Command that runs the host wrapper of `image` on the target."""
        if self.local:
            return shlex.quote(str(root() / "scripts" / "zqr-host.sh"))
        self.sh(f"mkdir -p ~/.local/bin && docker run --rm --entrypoint cat {shlex.quote(image)} "
                f"/opt/zqr/scripts/zqr-host.sh > {HOST_WRAPPER}.new && chmod +x {HOST_WRAPPER}.new && "
                f"mv {HOST_WRAPPER}.new {HOST_WRAPPER}")
        return HOST_WRAPPER


def _build(dockerfile: str, image: str, target: Target, platform: str | None, args: dict[str, str],
           labels: dict[str, str]) -> None:
    """Local target: load into this machine's Docker. Remote target: stream the image tarball straight into the
    target's `docker load`, so a cross-built image never replaces a native one under the same local tag."""
    cmd = ["docker", "buildx", "build", "-f", dockerfile, "-t", image]
    if platform:
        cmd += ["--platform", platform]
    for k, v in args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    for k, v in labels.items():
        cmd += ["--label", f"{k}={v}"]
    if target.local:
        print(f"building {image} ...", file=sys.stderr)
        subprocess.run([*cmd, "--load", "."], cwd=root(), check=True)
        return
    print(f"building {image} for {platform} and shipping it to {target.name} ...", file=sys.stderr)
    build = shlex.join([*cmd, "--output", "type=docker,dest=-", "."])
    subprocess.run(f"set -o pipefail; {build} | gzip -1 | ssh {shlex.quote(target.name)} 'gunzip | docker load'",
                   shell=True, executable="/bin/bash", cwd=root(), check=True)


def release(target_name: str, allow_dirty: bool = False, up: bool = True) -> dict:
    if in_release_container():
        raise SystemExit("`zqr release` runs on the development machine, from a checkout")
    target = Target(target_name)
    version = release_version(allow_dirty)
    image, agent = f"zqr:{version}", agent_tag()
    platform = None if target.local else target.platform()
    built = []
    for dockerfile, img, args, labels in (("Dockerfile.agent", agent, {}, {}),
                                          ("Dockerfile", image, {"ZQR_VERSION": version}, {"zqr.agent": agent})):
        if target.local or not target.has_image(img):
            _build(dockerfile, img, target, platform, args, labels)
            built.append(img)
    wrapper = target.wrapper(image)
    if up:
        target.sh(f"{wrapper} up {shlex.quote(version)}")
    return {"version": version, "image": image, "agent_image": agent, "target": target.name,
            "platform": platform or "native", "built": built, "started": up,
            "next": f"{'zqr' if not target.local else wrapper} status"}


def cmd_release(args) -> None:
    print(json.dumps(release(args.target, allow_dirty=args.allow_dirty, up=not args.no_up), indent=2))


def register(sub) -> None:
    p = sub.add_parser("release", help="build images from this checkout and run them on a host (default quant-wsl)")
    p.add_argument("--target", default="quant-wsl", help="ssh destination, or `local` for this machine's Docker")
    p.add_argument("--allow-dirty", action="store_true", help="build uncommitted changes (version gets -dirty...)")
    p.add_argument("--no-up", action="store_true", help="ship only; start later with `zqr up VERSION` on the host")
    p.set_defaults(fn=cmd_release)
