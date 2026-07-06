#!/usr/bin/env python3
"""Render a Docker Compose env file from the current host deployment env."""

from __future__ import annotations

import argparse
from pathlib import Path


CONTAINER_PATHS = {
    "BASE_DIR": "/app",
    "DATA_DIR": "/app/data",
    "LOGS_DIR": "/app/logs",
    "ACCOUNTS_FILE": "/app/data/accounts.json",
    "USERS_FILE": "/app/data/users.json",
    "CAPTURES_FILE": "/app/data/captures.json",
    "CONTEXT_GENERATED_DIR": "/app/data/context_files",
    "CONTEXT_CACHE_FILE": "/app/data/context_cache.json",
    "UPLOADED_FILES_FILE": "/app/data/uploaded_files.json",
    "CONTEXT_AFFINITY_FILE": "/app/data/session_affinity.json",
    "VIDEO_TASKS_FILE": "/app/data/video_tasks.json",
}

DEFAULTS = {
    "HOST_PORT": "7860",
    "QWEN2API_TAG": "dev-go",
}


def parse_env(path: Path) -> list[tuple[str, str]]:
    """Parse a simple KEY=VALUE env file while preserving entry order."""
    entries: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        entries.append((key.strip(), value.strip()))
    return entries


def render_compose_env(source: Path, image_tag: str) -> str:
    """Render container-safe env content from the host deployment env file."""
    values: dict[str, str] = dict(DEFAULTS)
    values["QWEN2API_TAG"] = image_tag

    ordered_keys: list[str] = list(DEFAULTS)
    for key, value in parse_env(source):
        if key not in ordered_keys:
            ordered_keys.append(key)
        values[key] = CONTAINER_PATHS.get(key, value)

    for key, value in CONTAINER_PATHS.items():
        if key not in ordered_keys:
            ordered_keys.append(key)
        values[key] = value

    return "\n".join(f"{key}={values[key]}" for key in ordered_keys if key in values) + "\n"


def main() -> None:
    """Render the env file and create parent directories when needed."""
    parser = argparse.ArgumentParser(description="Render qwen2api Docker Compose env for us deployment.")
    parser.add_argument("--source", required=True, type=Path, help="Existing host env file, for example .env.host-dev-go")
    parser.add_argument("--output", required=True, type=Path, help="Output .env.compose path")
    parser.add_argument("--image-tag", required=True, help="Docker image tag without repository, for example dev-go-d63e72e")
    args = parser.parse_args()

    content = render_compose_env(args.source, args.image_tag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
