#!/usr/bin/env python3
"""
AI PR Reviewer v2 — powered by OpenRouter.

Implements the awesome-skills/code-review-skill methodology:
  • 4-Phase Review Process (Context → High-Level → Line-by-Line → Summary)
  • Severity Labels: blocker · important · nit · suggestion · learning · praise
  • Language auto-detection → loads relevant review guide
  • Linked-issue validation
  • Inline GitHub review comments + auto-approve/request-changes
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from typing import Any

import requests

GITHUB_API = "https://api.github.com"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

GH_TOKEN = os.environ["GITHUB_TOKEN"]
OR_KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
MAX_DIFF = int(os.environ.get("MAX_DIFF_CHARS", "80000"))
PR_NUM = os.environ["PR_NUMBER"]
REPO = os.environ["REPO"]
PR_SHA = os.environ["PR_SHA"]


# ---------- Language detection -----------------------------------------------

LANG_EXT = {
    "react":      [".jsx", ".tsx"],
    "vue":        [".vue"],
    "angular":    [".component.ts", ".module.ts"],
    "svelte":     [".svelte"],
    "typescript": [".ts"],
    "python":     [".py"],
    "django":     ["settings.py", "models.py", "views.py", "serializers.py"],
    "go":         [".go"],
    "rust":       [".rs"],
    "java":       [".java"],
    "csharp":     [".cs"],
    "kotlin":     [".kt", ".kts"],
    "swift":      [".swift"],
    "cpp":        [".cpp", ".hpp", ".cc"],
    "c":          [".c", ".h"],
    "nestjs":     [".controller.ts", ".service.ts", ".module.ts"],
    "css":        [".css", ".scss", ".sass", ".less"],
    "ruby":       [".rb"],
    "php":        [".php"],
    "sql":        [".sql"],
    "shell":      [".sh", ".bash"],
    "odoo":       ["__manifest__.py", "__openerp__.py", "models/"],
}


def detect_languages(files: list[str]) -> list[str]:
    counter: Counter = Counter()
    for path in files:
        lower = path.lower()
        for lang, exts in LANG_EXT.items():
            if any(lower.endswith(ext) or ext in lower for ext in exts):
                counter[lang] += 1
    # return top 3 by file count
    return [lang for lang, _ in counter.most_common(3)]


# ---------- GitHub API helpers ------------------------------------------------

def gh(method: str, path: str, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {GH_TOKEN}")
    headers.setdefault("Accept", "application/vnd.github+json")
    headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
    url = f"{GITHUB_API}{path}" if path.startswith("/") else path
    resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    if resp.status_code >= 300:
        print(f"GH {method} {path} -> {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
    return resp


def get_pr() -> dict:
    return gh("GET", f"/repos/{REPO}/pulls/{PR_NUM}").json()


def get_pr_files() -> list[dict]:
    files: list[dict] = []
    page = 1
    while True:
        r = gh("GET", f"/repos/{REPO}/pulls/{PR_NUM}/files?per_page=100&page={page}")
        if r.status_code != 200:
            break
        chunk = r.json()
        files.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return files


def get_diff() -> str:
    r = gh(
        "GET",
        f"/repos/{REPO}/pulls/{PR_NUM}",
        headers={"Accept": "application/vnd.github.v3.diff"},
    )
    return r.text


def linked_issue_numbers(pr_body: str, pr_title: str) -> list[int]:
    text = (pr_body or "") + "\n" + (pr_title or "")
    nums: set[int] = set()
    for m in re.finditer(r"(?:close[ds]?|fix(?:e[ds])?|resolve[ds]?)\s*[:#]?\s*#(\d+)", text, re.I):
        nums.add(int(m.group(1)))
    for m in re.finditer(r"(?<![A-Za-z0-9_])#(\d+)", text):
        nums.add(int(m.group(1)))
    return sorted(nums)


def get_issue(num: int) -> dict | None:
    r = gh("GET", f"/repos/{REPO}/issues/{num}")
    if r.status_code != 200:
        return None
    return r.json()


def truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n…[TRUNCATED — {len(text) - n} more chars]"


# ---------- Prompt -----------------------------------------------------------

SYSTEM_PROMPT = """You are a senior staff engineer performing an expert PR review using the
"awesome-skills/code-review-skill" methodology.

Follow the FOUR-PHASE REVIEW PROCESS:

PHASE 1 - CONTEXT GATHERING
  • Read PR description, linked issue(s), file list
  • Note PR size (>400 lines should ask to split)

PHASE 2 - HIGH-LEVEL REVIEW
  • Architecture / design fit (SOLID, coupling, anti-patterns)
  • Performance impact (algorithm complexity, N+1 queries, memory)
  • Test strategy (coverage for new logic, edge cases)

PHASE 3 - LINE-BY-LINE
  • Logic & correctness (off-by-one, null refs, race conditions)
  • Security (input validation, secrets, injection, IDOR)
  • Maintainability (naming, single-responsibility)
  • Reuse audit (could existing helpers replace this?)

PHASE 4 - SUMMARY & DECISION
  • Summarize key concerns
  • Highlight what was done well (praise)
  • Decide: APPROVE / REQUEST_CHANGES / COMMENT

SEVERITY LABELS (use exactly these):
  • blocker    🔴 — must fix before merge (security, correctness bug)
  • important  🟠 — should fix; may block depending on context
  • nit        🟡 — minor style/preference
  • suggestion 🔵 — optional improvement worth considering
  • learning   📚 — educational note, no action required
  • praise     🌟 — explicitly highlight great work

TONE RULES:
  • Use questions over commands ("What happens if items is empty?")
  • Suggest over mandate ("Could be cleaner with async/await — thoughts?")
  • Don't nitpick formatting (linters handle that)
  • Acknowledge good work in praise comments

OUTPUT — STRICT JSON ONLY, this exact schema:
{
  "summary": "<2-3 sentence overall verdict>",
  "verdict": "APPROVE" | "REQUEST_CHANGES" | "COMMENT",
  "issue_alignment": "<does PR satisfy linked issue? or 'no_issue'>",
  "praise_notes": ["<short bullet of what was done well>", ...],
  "comments": [
    {
      "path": "<file path>",
      "line": <int line in NEW file>,
      "severity": "blocker"|"important"|"nit"|"suggestion"|"learning"|"praise",
      "comment": "<phrased as question or suggestion, with reasoning + suggested fix>"
    }
  ]
}

DECISION RULES:
  • APPROVE only if: no blocker AND no important issues AND linked issue satisfied (or no issue).
  • REQUEST_CHANGES if any blocker OR multiple important issues.
  • COMMENT otherwise.
  • Max 8 inline comments. Prioritize by severity.
  • Always include 1-2 praise notes if anything is well done.
"""


def build_user_prompt(
    pr: dict,
    diff: str,
    files: list[dict],
    issues: list[dict],
    languages: list[str],
) -> str:
    file_summary = "\n".join(
        f"  {f['status']:<10} {f['additions']:+}/{f['deletions']:-} {f['filename']}"
        for f in files[:50]
    )
    return f"""PR TITLE: {pr.get('title')}

PR DESCRIPTION:
{pr.get('body') or '(empty)'}

DETECTED LANGUAGES: {', '.join(languages) or 'mixed/unknown'}

PR SIZE: {sum(f['additions'] for f in files)} additions, {sum(f['deletions'] for f in files)} deletions, {len(files)} files

FILES CHANGED:
{file_summary}

LINKED ISSUES:
{json.dumps(issues, indent=2) if issues else 'none'}

DIFF:
{diff}
"""


# ---------- OpenRouter call ---------------------------------------------------

def call_openrouter(user_prompt: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    headers = {
        "Authorization": f"Bearer {OR_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": f"https://github.com/{REPO}",
        "X-Title": "PR-Bot",
    }
    for attempt in range(3):
        try:
            r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=180)
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                # robust JSON extraction
                content = content.strip()
                if content.startswith("```"):
                    content = re.sub(r"^```[a-z]*\n|\n```$", "", content)
                return json.loads(content)
            print(f"OpenRouter {r.status_code}: {r.text[:300]}", file=sys.stderr)
        except Exception as e:
            print(f"OpenRouter error (attempt {attempt + 1}): {e}", file=sys.stderr)
        time.sleep(2 ** attempt)
    return {
        "summary": "AI reviewer unavailable — please review manually.",
        "verdict": "COMMENT",
        "issue_alignment": "unknown",
        "praise_notes": [],
        "comments": [],
    }


# ---------- Posting the review -----------------------------------------------

SEVERITY_EMOJI = {
    "blocker": "🔴",
    "important": "🟠",
    "nit": "🟡",
    "suggestion": "🔵",
    "learning": "📚",
    "praise": "🌟",
}


def post_review(review: dict, languages: list[str]) -> None:
    body_parts = [
        f"## 🤖 AI Code Review · {MODEL}",
        f"_Languages detected: {', '.join(languages) or 'n/a'}_",
        "",
        f"### Summary\n{review.get('summary', '(no summary)')}",
        "",
    ]

    align = review.get("issue_alignment")
    if align and align != "no_issue":
        body_parts += [f"**Issue alignment:** {align}", ""]

    praise = review.get("praise_notes") or []
    if praise:
        body_parts += ["### 🌟 What was done well"] + [f"  • {p}" for p in praise] + [""]

    # Build inline comments
    inline = []
    counts: Counter = Counter()
    for c in review.get("comments", []) or []:
        if not isinstance(c, dict):
            continue
        path = c.get("path")
        line = c.get("line")
        comment = c.get("comment", "")
        sev = (c.get("severity") or "nit").lower()
        if not path or not line or not comment:
            continue
        try:
            line = int(line)
        except (TypeError, ValueError):
            continue
        emoji = SEVERITY_EMOJI.get(sev, "🟡")
        counts[sev] += 1
        inline.append({
            "path": path,
            "line": line,
            "side": "RIGHT",
            "body": f"{emoji} **[{sev}]** {comment}",
        })

    if counts:
        body_parts += ["### Findings"] + [
            f"  • {SEVERITY_EMOJI.get(k, '·')} **{k}**: {v}"
            for k, v in counts.most_common()
        ] + [""]

    verdict_map = {"APPROVE": "APPROVE", "REQUEST_CHANGES": "REQUEST_CHANGES", "COMMENT": "COMMENT"}
    event = verdict_map.get(review.get("verdict", "COMMENT"), "COMMENT")

    body_parts.append(f"<sub>Review skill methodology: awesome-skills/code-review-skill</sub>")

    payload = {
        "body": "\n".join(body_parts),
        "event": event,
        "comments": inline,
        "commit_id": PR_SHA,
    }
    r = gh("POST", f"/repos/{REPO}/pulls/{PR_NUM}/reviews", json=payload)
    if r.status_code >= 300:
        # Fallback to a normal issue comment if review API rejected our comments
        fallback = "\n".join(body_parts) + "\n\n<sub>(inline comments could not be posted)</sub>"
        gh("POST", f"/repos/{REPO}/issues/{PR_NUM}/comments", json={"body": fallback})


# ---------- Main --------------------------------------------------------------

def main() -> int:
    pr = get_pr()
    if (pr.get("user", {}).get("type") == "Bot"
            and "dependabot" not in pr["user"].get("login", "").lower()):
        print("Skipping bot PR")
        return 0
    if pr.get("draft"):
        print("Skipping draft PR")
        return 0

    files_meta = get_pr_files()
    file_paths = [f["filename"] for f in files_meta]
    languages = detect_languages(file_paths)

    diff = truncate(get_diff(), MAX_DIFF)

    issues = []
    for num in linked_issue_numbers(pr.get("body", ""), pr.get("title", "")):
        iss = get_issue(num)
        if iss and "pull_request" not in iss:
            issues.append({
                "number": num,
                "title": iss["title"],
                "state": iss.get("state"),
                "body": (iss.get("body") or "")[:2000],
            })

    user_prompt = build_user_prompt(pr, diff, files_meta, issues, languages)
    review = call_openrouter(user_prompt)
    print(json.dumps(review, indent=2)[:3000])
    post_review(review, languages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
