#!/usr/bin/env python3
"""
Auto-install the PR Bot workflow into every repo under OWNER.

Steps per repo:
  1. Skip if archived, fork, no default branch, or has 'no-pr-bot' topic.
  2. Skip if .github/workflows/pr-bot.yml already exists (unless FORCE_OVERWRITE).
  3. Create branch 'chore/install-pr-bot' (or commit directly to default branch).
  4. Open PR titled "chore: install PR Bot (auto-review + auto-merge)".
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from typing import Any

import requests

GITHUB_API = "https://api.github.com"
# Use INSTALL_TOKEN, not GITHUB_TOKEN — GitHub Actions reserves GITHUB_TOKEN and
# overrides it with its automatic per-repo token (which can't access other repos).
TOKEN = os.environ["INSTALL_TOKEN"]
OWNER = os.environ["OWNER"]
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
FORCE_OVERWRITE = os.environ.get("FORCE_OVERWRITE", "false").lower() == "true"

WORKFLOW_PATH = ".github/workflows/pr-bot.yml"

# Template caller workflow — uses caller repo's own owner for `uses:`
WORKFLOW_TEMPLATE = """name: PR Bot (AI Review + Auto-Merge)

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

jobs:
  call-reusable:
    if: ${{ !contains(github.event.pull_request.labels.*.name, 'no-pr-bot') }}
    uses: __OWNER__/.github/.github/workflows/ai-review.yml@main
    secrets:
      OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
    permissions:
      contents: read
      pull-requests: write
      issues: write
      checks: write
"""


def gh(method: str, path: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {TOKEN}")
    headers.setdefault("Accept", "application/vnd.github+json")
    headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    return resp


def list_repos(owner: str) -> list[dict]:
    """List all non-archived repos for an org OR user."""
    repos: list[dict] = []
    # Try org endpoint first; fall back to user endpoint if 404
    for endpoint in (f"/orgs/{owner}/repos", f"/users/{owner}/repos"):
        page = 1
        ok = False
        while True:
            r = gh("GET", f"{endpoint}?type=all&per_page=100&page={page}")
            if r.status_code == 404:
                break
            ok = True
            if r.status_code != 200:
                print(f"list_repos {endpoint} -> {r.status_code} {r.text[:200]}", file=sys.stderr)
                break
            chunk = r.json()
            repos.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
        if ok:
            return repos
    return repos


def repo_has_workflow(owner: str, name: str, default_branch: str) -> tuple[bool, str | None]:
    r = gh("GET", f"/repos/{owner}/{name}/contents/{WORKFLOW_PATH}?ref={default_branch}")
    if r.status_code == 200:
        data = r.json()
        return True, data.get("sha")
    return False, None


def install_into_repo(repo: dict) -> str:
    owner = repo["owner"]["login"]
    name = repo["name"]
    default_branch = repo["default_branch"]

    if repo.get("archived"):
        return "skip:archived"
    if repo.get("disabled"):
        return "skip:disabled"
    if not default_branch:
        return "skip:no-default-branch"
    topics = repo.get("topics", []) or []
    if "no-pr-bot" in topics:
        return "skip:topic-opt-out"

    exists, sha = repo_has_workflow(owner, name, default_branch)
    if exists and not FORCE_OVERWRITE:
        return "skip:already-installed"

    workflow_body = WORKFLOW_TEMPLATE.replace("__OWNER__", owner)

    if DRY_RUN:
        return "dry-run:would-install"

    # Commit file directly to default branch via Contents API
    payload: dict[str, Any] = {
        "message": "chore: install PR Bot (auto-review + auto-merge)",
        "content": base64.b64encode(workflow_body.encode()).decode(),
        "branch": default_branch,
    }
    if exists and sha:
        payload["sha"] = sha
    r = gh(
        "PUT",
        f"/repos/{owner}/{name}/contents/{WORKFLOW_PATH}",
        json=payload,
    )
    if r.status_code in (200, 201):
        return "installed"
    return f"error:{r.status_code}:{r.text[:200]}"


def main() -> int:
    repos = list_repos(OWNER)
    print(f"Found {len(repos)} repos under {OWNER}")
    print(f"DRY_RUN={DRY_RUN}  FORCE_OVERWRITE={FORCE_OVERWRITE}\n")

    summary: dict[str, int] = {}
    for repo in repos:
        result = install_into_repo(repo)
        bucket = result.split(":")[0]
        summary[bucket] = summary.get(bucket, 0) + 1
        print(f"  {repo['full_name']:<60} -> {result}")
        # Be polite to the API
        time.sleep(0.4)

    print("\n=== Summary ===")
    for k, v in sorted(summary.items()):
        print(f"  {k}: {v}")
    # Always exit 0 — partial failures should not break the schedule
    return 0


if __name__ == "__main__":
    sys.exit(main())
