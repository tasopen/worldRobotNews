#!/usr/bin/env python3
"""
GitHub リポジトリ作成 & push スクリプト

使い方:
  uv run python scripts/publish_to_github.py

事前に GitHub Personal Access Token (PAT) が必要です。
  取得: https://github.com/settings/tokens/new
  必要なスコープ: repo, workflow
"""
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_GITHUB_USER = "tasopen"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def infer_repo_context() -> tuple[str, str]:
    """現在の Git 情報から GitHub owner/repo を推定する。"""
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in repo_slug:
        owner, repo_name = repo_slug.split("/", 1)
        return owner, repo_name

    owner = (
        os.environ.get("GITHUB_OWNER")
        or os.environ.get("GITHUB_USER")
        or ""
    ).strip()
    repo_name = os.environ.get("GITHUB_REPOSITORY_NAME", "").strip()
    if owner and repo_name:
        return owner, repo_name

    remote = run(["git", "remote", "get-url", "origin"], check=False)
    if remote.returncode == 0:
        remote_url = remote.stdout.strip()
        normalized = remote_url.removesuffix(".git")
        if normalized.startswith("git@github.com:"):
            slug = normalized.split(":", 1)[1]
        elif "github.com/" in normalized:
            slug = normalized.split("github.com/", 1)[1].lstrip("/")
        else:
            slug = ""
        if "/" in slug:
            remote_owner, remote_repo = slug.split("/", 1)
            return owner or remote_owner, repo_name or remote_repo

    git_root = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if not repo_name and git_root.returncode == 0:
        repo_name = Path(git_root.stdout.strip()).name

    if not repo_name:
        repo_name = Path.cwd().name

    if not owner:
        owner = DEFAULT_GITHUB_USER

    return owner, repo_name


def create_github_repo(token: str, repo_name: str, github_user: str) -> str:
    """GitHub API でパブリックリポジトリを作成し、clone URL を返す。"""
    payload = json.dumps({
        "name": repo_name,
        "description": "AI news podcast generator — daily episodes via GitHub Pages",
        "private": False,
        "auto_init": False,
    }).encode()
    req = urllib.request.Request(
        "https://api.github.com/user/repos",
        data=payload,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return data["clone_url"]
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        if e.code == 422 and "already exists" in str(body):
            print(f"[info] リポジトリ {repo_name} はすでに存在します。")
            return f"https://github.com/{github_user}/{repo_name}.git"
        print(f"[error] GitHub API: {e.code} {body}")
        sys.exit(1)


def set_github_secret(token: str, secret_name: str, secret_value: str) -> None:
    """GitHub API でリポジトリ Secret を登録する（暗号化なし簡易版）。
    Note: 実際の API は sodium 暗号化が必要なため、ここでは案内のみ。
    """

def main() -> None:
    github_user, repo_name = infer_repo_context()

    print(f"=== {repo_name} GitHub 公開セットアップ ===\n")
    print(f"対象: https://github.com/{github_user}/{repo_name}\n")

    # PAT 入力
    token = os.environ.get("GITHUB_TOKEN") or getpass.getpass(
        "GitHub Personal Access Token (repo + workflow スコープ): "
    )
    if not token:
        print("[error] トークンが入力されませんでした。")
        sys.exit(1)

    # 1. リポジトリ作成
    print(f"\n[1/4] GitHub にリポジトリ '{repo_name}' を作成中...")
    clone_url = create_github_repo(token, repo_name, github_user)
    # トークンを URL に埋め込んで push（ローカルのみ、.git/config に保存されない）
    auth_url = clone_url.replace("https://", f"https://{github_user}:{token}@")
    print(f"      → {clone_url}")

    # 2. リモート設定 & push
    print("\n[2/4] git remote を設定して push 中...")
    result = run(["git", "remote", "get-url", "origin"], check=False)
    if result.returncode == 0:
        run(["git", "remote", "set-url", "origin", auth_url])
    else:
        run(["git", "remote", "add", "origin", auth_url])
    run(["git", "branch", "-M", "main"])
    push = run(["git", "push", "-u", "origin", "main"], check=False)
    if push.returncode != 0:
        print(f"[error] push 失敗:\n{push.stderr}")
        sys.exit(1)
    # push 後にリモートURLをトークンなしに戻す
    run(["git", "remote", "set-url", "origin", clone_url])
    print("      → push 完了")

    # 3. GitHub Pages 有効化
    print("\n[3/4] GitHub Pages を有効化中 (docs/ フォルダ)...")
    pages_payload = json.dumps({
        "source": {"branch": "main", "path": "/docs"}
    }).encode()
    pages_req = urllib.request.Request(
        f"https://api.github.com/repos/{github_user}/{repo_name}/pages",
        data=pages_payload,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(pages_req) as resp:
            pages_data = json.loads(resp.read())
            print(f"      → GitHub Pages URL: {pages_data.get('html_url', '（設定中）')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if e.code == 409:
            print("      → GitHub Pages はすでに有効です。")
        else:
            print(f"      → Pages 設定は手動で行ってください (Settings → Pages): {e.code} {body[:200]}")

    # 4. Secrets 登録案内（sodium 暗号化が必要なため案内のみ）
    print("\n[4/4] GitHub Secrets の登録案内:")
    print("  以下の URL から手動で登録してください：")
    print(f"  https://github.com/{github_user}/{repo_name}/settings/secrets/actions")
    print()

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        # libsodium が使えれば自動登録も可能だが、依存を避けて案内のみ
        print(f"  GEMINI_API_KEY = {gemini_key[:8]}...")
        print("  （.secret/apikeys.txt の値をそのまま使えます）")
    else:
        print("  SECRET 名         | 取得元")
        print("  GEMINI_API_KEY    | https://aistudio.google.com/apikey")

    print(f"""
=== セットアップ完了 ===

リポジトリ : https://github.com/{github_user}/{repo_name}
Pages URL  : https://{github_user}.github.io/{repo_name}/
RSS フィード: https://{github_user}.github.io/{repo_name}/feed.xml

次のステップ:
  1. 上記 URL で Secrets を登録
  2. Actions タブ → Daily AI Podcast → Run workflow でテスト実行
  3. AntennaPod で RSS フィードを購読
""")


if __name__ == "__main__":
    main()
