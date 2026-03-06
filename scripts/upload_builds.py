"""Upload dist/ artifacts to the GitLab PyPI registry under the extractor/libs group."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import quote, urlparse

DEFAULT_DIST_DIR = "dist"
DEFAULT_GROUP_PATH = "extractor/libs"
ENV_FILE = ".env"


class UploadError(RuntimeError):
    """Raised when an artifact cannot be uploaded."""


def load_env(env_path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE pairs from the .env file."""
    if not env_path.exists():
        raise UploadError(f"Credentials file not found: {env_path}")

    env: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def read_version_from_version_file(version_file_path: Path) -> str:
    """Extract __version__ from a Python version file."""
    if not version_file_path.exists():
        raise UploadError(f"Version file not found: {version_file_path}")

    contents = version_file_path.read_text(encoding="utf-8")
    match = re.search(
        r"""^__version__\s*=\s*(?:[A-Za-z_]\w*\s*=\s*)*['"]([^'"]+)['"]""",
        contents,
        flags=re.MULTILINE,
    )
    if match is None:
        raise UploadError(
            f"Unable to read __version__ from version file: {version_file_path}"
        )
    return match.group(1)


def read_project_metadata(pyproject_path: Path) -> tuple[str, str]:
    """Extract the project name and version from pyproject.toml."""
    if not pyproject_path.exists():
        raise UploadError(f"pyproject.toml not found: {pyproject_path}")

    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project")
    if not isinstance(project, dict):
        raise UploadError("Unable to read [project] section from pyproject.toml.")

    project_name = project.get("name")
    if not isinstance(project_name, str) or not project_name:
        raise UploadError("Unable to read project name from pyproject.toml.")

    project_version: str | None = None
    dynamic = project.get("dynamic")
    has_dynamic_version = isinstance(dynamic, list) and "version" in dynamic
    if has_dynamic_version:
        tool = data.get("tool")
        setuptools_scm = tool.get("setuptools_scm") if isinstance(tool, dict) else None
        version_file = (
            setuptools_scm.get("version_file")
            if isinstance(setuptools_scm, dict)
            else None
        )
        if not isinstance(version_file, str) or not version_file:
            raise UploadError(
                "project.dynamic contains 'version', but tool.setuptools_scm.version_file is missing."
            )
        project_version = read_version_from_version_file(
            pyproject_path.parent / version_file
        )
    else:
        raw_version = project.get("version")
        if isinstance(raw_version, str) and raw_version:
            project_version = raw_version

    if not project_version:
        raise UploadError("Unable to read project version from pyproject.toml.")

    return project_name, project_version


def collect_artifacts(dist_dir: Path) -> list[Path]:
    """Return build artifacts from dist/ sorted alphabetically."""
    if not dist_dir.exists():
        raise UploadError(f"Distribution directory not found: {dist_dir}")
    artifacts = sorted(path for path in dist_dir.iterdir() if path.is_file())
    if not artifacts:
        raise UploadError(f"No build artifacts found in {dist_dir}.")
    return artifacts


def get_remote_url(remote_name: str, repo_root: Path) -> str:
    """Return the push URL for the requested remote."""
    result = subprocess.run(
        ["git", "remote", "-v"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == remote_name and parts[2] == "(push)":
            return parts[1]
    raise UploadError(f"Could not find push URL for remote '{remote_name}'.")


def parse_remote(remote_url: str) -> tuple[str, str]:
    """Return (base_url, project_path) from a git remote URL."""
    if remote_url.startswith(("http://", "https://")):
        parsed = urlparse(remote_url)
        if not parsed.scheme or not parsed.netloc:
            raise UploadError(f"Unrecognised remote URL: {remote_url}")
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        project_path = parsed.path
    elif remote_url.startswith("git@"):
        try:
            user_host, repo_path = remote_url.split(":", 1)
        except ValueError as exc:
            raise UploadError(f"Unrecognised SSH remote: {remote_url}") from exc
        host = user_host.split("@", 1)[1]
        base_url = f"https://{host}"
        project_path = f"/{repo_path}"
    else:
        raise UploadError(f"Unsupported remote format: {remote_url}")

    if project_path.endswith(".git"):
        project_path = project_path[:-4]
    project_path = project_path.strip("/")
    if not project_path:
        raise UploadError(f"Could not derive project path from remote: {remote_url}")
    return base_url, project_path


def ensure_twine_available() -> None:
    """Check that the twine package is importable."""
    if importlib.util.find_spec("twine") is None:
        raise UploadError(
            "Twine is required to upload to the PyPI registry. "
            "Install it via 'pip install twine' inside the project virtualenv."
        )


def build_repository_url(base_url: str, group_path: str) -> str:
    """Construct the GitLab PyPI repository URL for the given group."""
    clean_group = group_path.strip("/")
    if not clean_group:
        raise UploadError("Group path may not be empty.")
    encoded_group = quote(clean_group, safe="")
    return f"{base_url}/api/v4/groups/{encoded_group}/-/packages/pypi"


def build_project_repository_url(base_url: str, project_path: str) -> str:
    """Construct the project-level GitLab PyPI repository URL."""
    encoded_project = quote(project_path, safe="")
    return f"{base_url}/api/v4/projects/{encoded_project}/packages/pypi"


def run_twine_upload(
    artifacts: list[Path],
    repository_urls: list[str],
    username: str,
    password: str,
    repo_root: Path,
) -> None:
    """Invoke `python -m twine upload` with the provided credentials."""
    ensure_twine_available()
    env = os.environ.copy()
    env["TWINE_USERNAME"] = username
    env["TWINE_PASSWORD"] = password

    last_error: subprocess.CalledProcessError | None = None
    for repo_url in repository_urls:
        cmd = [
            sys.executable,
            "-m",
            "twine",
            "upload",
            "--verbose",
            "--non-interactive",
            "--disable-progress-bar",
            "--repository-url",
            repo_url,
        ]
        cmd.extend(str(artifact) for artifact in artifacts)

        print(
            f"Attempting twine upload of {len(artifacts)} artifact(s) "
            f"to {repo_url}…"
        )
        try:
            subprocess.run(cmd, cwd=str(repo_root), env=env, check=True)
        except subprocess.CalledProcessError as exc:
            last_error = exc
            print("Upload failed, trying next repository (if any).")
            continue
        else:
            print("Upload completed.")
            return

    if last_error is not None:
        raise last_error
    raise UploadError("No repository URLs were provided for upload.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Upload the dist/ artifacts to the GitLab PyPI registry under the "
            "extractor/libs group."
        )
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote to inspect for determining the GitLab host (default: origin).",
    )
    parser.add_argument(
        "--dist-dir",
        default=DEFAULT_DIST_DIR,
        help="Folder with build artifacts relative to the repo root (default: dist).",
    )
    parser.add_argument(
        "--group-path",
        default=DEFAULT_GROUP_PATH,
        help="Group path that hosts the PyPI registry (default: extractor/libs).",
    )
    parser.add_argument(
        "--repository-url",
        help=(
            "Override the PyPI repository URL. If omitted, it is derived from the "
            "group path and remote."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    env_path = repo_root / ENV_FILE
    env = load_env(env_path)
    username = env.get("GIT_USERNAME")
    password = env.get("GIT_TOKEN")
    if not username or not password:
        raise UploadError("GIT_USERNAME and GIT_TOKEN must be defined in .env.")

    pyproject_path = repo_root / "pyproject.toml"
    package_name, package_version = read_project_metadata(pyproject_path)

    dist_dir = (repo_root / args.dist_dir).resolve()
    artifacts = collect_artifacts(dist_dir)

    remote_url = get_remote_url(args.remote, repo_root)
    base_url, project_path = parse_remote(remote_url)

    if args.repository_url:
        repository_urls = [args.repository_url]
    else:
        repository_urls = [
            build_repository_url(base_url, args.group_path),
            build_project_repository_url(base_url, project_path),
        ]

    print(f"Preparing to upload {package_name} {package_version} as {username}.")
    run_twine_upload(
        artifacts=artifacts,
        repository_urls=repository_urls,
        username=username,
        password=password,
        repo_root=repo_root,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UploadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        stderr_text = exc.stderr or ""
        stdout_text = exc.stdout or ""
        details = stderr_text or stdout_text
        joined_cmd = " ".join(str(part) for part in exc.cmd) if exc.cmd else "<unknown>"
        print(
            f"Command '{joined_cmd}' failed with exit code "
            f"{exc.returncode or 1}. {details}",
            file=sys.stderr,
        )
        sys.exit(exc.returncode or 1)
