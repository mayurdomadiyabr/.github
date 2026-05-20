# 🤖 PR Bot — Org-wide AI Code Review + Auto-Merge

Automated PR review for every repo under this owner, powered by OpenRouter and the
[awesome-skills/code-review-skill](https://github.com/awesome-skills/code-review-skill) methodology.

## What it does

For every Pull Request opened across **any repo** in this owner:

1. 🔍 **AI Review** — A senior-engineer-level review using a 4-phase process:
   - Phase 1: Context (PR description, linked issues, file scope)
   - Phase 2: High-level (architecture, performance, tests)
   - Phase 3: Line-by-line (logic, security, maintainability, reuse)
   - Phase 4: Summary + decision
2. 🏷️ **Severity-labelled comments**:
   `🔴 blocker` · `🟠 important` · `🟡 nit` · `🔵 suggestion` · `📚 learning` · `🌟 praise`
3. 🌍 **Language-aware** — auto-detects React, Vue, Rust, Go, Java, Python, Odoo, etc.
4. 🔗 **Issue-linked** — reads linked issues and verifies the PR addresses them.
5. ✅ **Auto-merge** — when AI approves + all CI passes, PR merges automatically (squash + delete branch).

## Architecture

```
this-repo (.github)
├── .github/workflows/
│   ├── ai-review.yml        ← reusable workflow (called by every repo)
│   └── auto-install.yml     ← weekly cron + manual dispatch
├── scripts/
│   ├── ai_review.py         ← OpenRouter-backed reviewer
│   └── installer.py         ← bulk-installs pr-bot.yml across repos
└── workflow-templates/
    ├── pr-bot.yml
    └── pr-bot.properties.json
```

## Setup (one time, per owner)

1. **Create `.github` repo** (this repo) — auto-created by `deploy.sh`.
2. **Add secrets** at the org/user level:
   - `OPENROUTER_API_KEY` — for the AI calls
   - `PR_BOT_INSTALL_PAT` — classic PAT (scopes: `repo`, `workflow`, `admin:org`)
3. **Run dry-run** to preview:
   ```bash
   gh workflow run "Auto-Install PR Bot" --repo OWNER/.github -f dry_run=true
   ```
4. **Run real install** once happy:
   ```bash
   gh workflow run "Auto-Install PR Bot" --repo OWNER/.github
   ```

After this, the **weekly cron** (Mondays 06:00 UTC) auto-installs the bot into any new repo.

## Per-repo controls

- **Opt out**: add topic `no-pr-bot` to a repo, or add label `no-pr-bot` to a single PR.
- **Custom model**: edit `pr-bot.yml` in the repo to pass `model:` input (any OpenRouter id).
- **Skip drafts**: drafts are automatically skipped.
- **Bots**: PRs from bots are skipped (except `dependabot`).

## Cost

Default model is `google/gemini-2.0-flash-001` via OpenRouter — extremely cheap, fast,
good for code review. Typical review costs ~$0.001–$0.005.

To switch model globally, edit `inputs.model.default` in `.github/workflows/ai-review.yml`.

## Rolling back

To stop the bot for a single repo:

```bash
gh api -X DELETE "/repos/<OWNER>/<REPO>/contents/.github/workflows/pr-bot.yml" \
  -f message="chore: remove PR Bot" \
  -f sha=$(gh api /repos/<OWNER>/<REPO>/contents/.github/workflows/pr-bot.yml -q .sha)
```

To stop the org-wide auto-installer:
- Go to `OWNER/.github` → Actions → `Auto-Install PR Bot` → Disable workflow.

## Security model

- **Token scoping**: The reusable workflow uses `secrets.GITHUB_TOKEN` (scoped to the caller repo only).
- **PAT**: The installer PAT is used **only** by the `.github` repo workflow.
- **No PR code execution**: AI review only reads the diff — never runs PR code.
- **Auto-merge safety**: GitHub still enforces branch protection (status checks, required reviews).
  Auto-merge only fires after **all** required checks + approvals are met.

## License

MIT.
