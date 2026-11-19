"""GitHub リポジトリ slug（owner/repo）の解決。

リポジトリ名をコードにハードコーディングしないため、以下の順で解決する:

1. 環境変数 ``GITHUB_REPOSITORY``（GitHub Actions が自動で設定する）
2. ``git remote get-url origin`` の URL
3. ``config/podcast_meta.yml`` の ``base_url``
   （``https://<owner>.github.io/<repo>`` 形式のときだけ）

どれでも解決できなければ ``None`` を返す。呼び出し側は
``GITHUB_REPOSITORY=owner/repo`` を設定するよう促すエラーを出すこと。
"""
from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import urlparse

import yaml

DEFAULT_META_PATH = "config/podcast_meta.yml"

_SLUG_RE = re.compile(r"[^/\s]+/[^/\s]+")


def _from_env() -> str | None:
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return slug if _SLUG_RE.fullmatch(slug) else None


def _parse_remote(url: str) -> str | None:
    """git remote の URL から owner/repo を取り出す。"""
    url = url.strip().removesuffix(".git")
    if url.startswith("git@github.com:"):
        slug = url.split(":", 1)[1]
    elif "github.com/" in url:
        slug = url.split("github.com/", 1)[1]
    else:
        return None
    slug = slug.strip("/")
    return slug if _SLUG_RE.fullmatch(slug) else None


def _from_git_remote(remote: str = "origin") -> str | None:
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", remote],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_remote(out)


def _from_base_url(meta_path: str = DEFAULT_META_PATH) -> str | None:
    """``base_url`` が GitHub Pages 形式なら owner/repo に変換する。"""
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(meta, dict):
        return None

    base_url = str(meta.get("base_url") or "").strip()
    if not base_url:
        return None

    parsed = urlparse(base_url)
    m = re.fullmatch(r"([^.]+)\.github\.io", parsed.hostname or "")
    if not m:
        return None

    repo = parsed.path.strip("/").split("/")[0]
    if not repo:
        return None
    return f"{m.group(1)}/{repo}"


def resolve_repo_slug(meta_path: str = DEFAULT_META_PATH) -> str | None:
    """owner/repo を解決する。解決できなければ ``None``。"""
    return _from_env() or _from_git_remote() or _from_base_url(meta_path)
