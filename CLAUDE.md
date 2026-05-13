# PR Review Hook

A Claude Code `PostToolUse` hook that automatically reviews pull requests after a `git push` or `gh pr create` command runs.

## What it does

After every Bash command that includes `git push`, `gh pr create`, or `gh pr merge`, the hook:

1. Fetches PR metadata, diff, CI check results, and linked GitHub issues via the `gh` CLI
2. Calls the Claude API (`claude-sonnet-4-6`) with a structured review prompt
3. Returns the review as `additionalContext` so Claude Code surfaces it in the next assistant message

The review covers:
- **Ticket coverage** — does the PR actually solve the linked issue?
- **Tests** — are tests present and passing in CI?
- **CI checks** — lists any failing or pending jobs by name
- **Linter** — flags linting failures visible in CI or the diff
- **Verdict** — ✅ APPROVED / ⚠️ NEEDS WORK / ⏳ PENDING

## Setup

### Prerequisites
- Python 3.10+
- `gh` CLI installed and authenticated (`gh auth login`)
- `ANTHROPIC_API_KEY` environment variable set

### Install Python dependency
```bash
pip install anthropic>=0.40.0
# or into a venv:
python -m venv .venv && .venv/Scripts/pip install anthropic
```

### Activate the hook

**Option A — project-level** (only in repos that contain `.claude/settings.json`):

Copy `.claude/settings.json` from this repo into the target repo's `.claude/` directory, or add the hook entry to an existing settings file.

**Option B — global** (fires in every repo you work in):

Add the following to `C:\Users\<you>\.claude\settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python D:\\coding_projects\\hook_git_pr_reviewer\\pr_reviewer.py",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

Also add these entries to the `permissions.allow` list so the hook's `gh` calls don't prompt:
```json
"Bash(gh pr view*)",
"Bash(gh pr diff*)",
"Bash(gh pr checks*)",
"Bash(gh issue view*)"
```

## File overview

| File | Purpose |
|------|---------|
| `pr_reviewer.py` | Hook entry point — reads stdin, gathers context, calls Claude API, writes output |
| `requirements.txt` | Python dependency (`anthropic`) |
| `.claude/settings.json` | Project-level hook registration for local testing |

## Behavior on errors

The hook always exits with code `0` so it can never block Claude. Errors are written to stderr (visible in the Claude Code hook log) and the hook outputs `{"continue": true}`.

## Testing manually

```bash
echo '{"tool_input":{"command":"git push origin main"},"cwd":"C:\\path\\to\\your\\repo"}' \
  | python pr_reviewer.py
```

