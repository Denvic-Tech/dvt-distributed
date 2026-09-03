"""Validate and upload dist/ artifacts to PyPI with Twine."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_DIST_DIR = "dist"
DEFAULT_REPOSITORY = "pypi"
ENV_FILE = ".env"
PYPI_TOKEN_ENV_VAR = "PYPI_API_TOKEN"
TWINE_USERNAME = "__token__"


class UploadError(RuntimeError):
    """Raised when build artifacts cannot be published."""


def load_env(env_path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE pairs from an optional .env file."""
    if not env_path.exists():
        return {}

    env: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def collect_artifacts(dist_dir: Path) -> list[Path]:
    """Return Python distribution artifacts from dist/ sorted alphabetically."""
    if not dist_dir.exists():
        raise UploadError(f"Distribution directory not found: {dist_dir}")

    artifacts = sorted(
        path
        for path in dist_dir.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith((".tar.gz", ".zip")))
    )
    if not artifacts:
        raise UploadError(f"No wheel or source distribution artifacts found in {dist_dir}.")
    return artifacts


def ensure_twine_available() -> None:
    """Check that Twine is installed in the active Python environment."""
    if importlib.util.find_spec("twine") is None:
        raise UploadError(
            "Twine is required to publish to PyPI. Install it with "
            "'python -m pip install twine' in the project virtual environment."
        )


def resolve_api_token(repo_root: Path) -> str:
    """Resolve the PyPI API token without exposing it on the command line."""
    file_env = load_env(repo_root / ENV_FILE)
    token = (
        os.environ.get(PYPI_TOKEN_ENV_VAR)
        or file_env.get(PYPI_TOKEN_ENV_VAR)
        or os.environ.get("TWINE_PASSWORD")
        or file_env.get("TWINE_PASSWORD")
    )
    if not token:
        raise UploadError(
            f"Set {PYPI_TOKEN_ENV_VAR} (recommended) or TWINE_PASSWORD in the environment or .env file."
        )
    return token


def run_twine_check(artifacts: list[Path], repo_root: Path) -> None:
    """Validate package metadata before attempting publication."""
    command = [sys.executable, "-m", "twine", "check", *(str(path) for path in artifacts)]
    subprocess.run(command, cwd=str(repo_root), check=True)


def run_twine_upload(
    artifacts: list[Path],
    *,
    repository: str,
    api_token: str,
    repo_root: Path,
    verbose: bool = False,
) -> None:
    """Publish artifacts to a configured PyPI repository via Twine."""
    env = os.environ.copy()
    env["TWINE_USERNAME"] = TWINE_USERNAME
    env["TWINE_PASSWORD"] = api_token

    command = [
        sys.executable,
        "-m",
        "twine",
        "upload",
        "--non-interactive",
        "--disable-progress-bar",
        "--repository",
        repository,
        *(["--verbose"] if verbose else []),
        *(str(path) for path in artifacts),
    ]
    subprocess.run(command, cwd=str(repo_root), env=env, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and upload dist/ artifacts to PyPI.")
    parser.add_argument(
        "--dist-dir",
        default=DEFAULT_DIST_DIR,
        help="Directory with build artifacts relative to the repository root (default: dist).",
    )
    parser.add_argument(
        "--repository",
        choices=("pypi", "testpypi"),
        default=DEFAULT_REPOSITORY,
        help="Twine repository to publish to (default: pypi).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose Twine upload output.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    ensure_twine_available()
    artifacts = collect_artifacts((repo_root / args.dist_dir).resolve())
    api_token = resolve_api_token(repo_root)

    print(f"Validating {len(artifacts)} distribution artifact(s) with Twine.")
    run_twine_check(artifacts, repo_root)
    print(f"Publishing {len(artifacts)} distribution artifact(s) to {args.repository}.")
    run_twine_upload(
        artifacts,
        repository=args.repository,
        api_token=api_token,
        repo_root=repo_root,
        verbose=args.verbose,
    )
    print("Upload completed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UploadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(
            f"Command failed with exit code {exc.returncode or 1}: "
            f"{' '.join(str(part) for part in exc.cmd) if exc.cmd else '<unknown>'}",
            file=sys.stderr,
        )
        sys.exit(exc.returncode or 1)