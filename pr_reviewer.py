#!/usr/bin/env python3
"""
Claude Code PostToolUse hook: automatically reviews a PR after git push / gh pr create.

Reads a JSON payload from stdin (injected by Claude Code), gathers PR context via
the gh CLI, calls the Claude API for a structured review, and writes the result back
as a hookSpecificOutput additionalContext so Claude Code surfaces it to the user.
"""

import json
import os
import subprocess
import sys

TRIGGERS = ["git push", "gh pr create", "gh pr merge"]
DIFF_CHAR_LIMIT = 15_000  # ~3k tokens of diff context


def run(args: list[str], cwd: str) -> tuple[str, int]:
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    return result.stdout.strip(), result.returncode


def get_pr_info(cwd: str) -> dict | None:
    fields = "number,title,body,headRefName,closingIssuesReferences,statusCheckRollup"
    out, code = run(["gh", "pr", "view", "--json", fields], cwd)
    if code != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def get_diff(cwd: str) -> str:
    out, code = run(["gh", "pr", "diff"], cwd)
    if code != 0 or not out:
        return "(diff unavailable)"
    if len(out) > DIFF_CHAR_LIMIT:
        return out[:DIFF_CHAR_LIMIT] + f"\n... (truncated at {DIFF_CHAR_LIMIT} chars)"
    return out


def get_checks(cwd: str) -> list[dict]:
    out, code = run(["gh", "pr", "checks", "--json", "name,state,bucket"], cwd)
    if code not in (0, 8) or not out:  # exit 8 = checks still pending
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def get_issue(number: int, cwd: str) -> dict | None:
    out, code = run(["gh", "issue", "view", str(number), "--json", "title,body"], cwd)
    if code != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def build_issues_text(closing_refs: list[dict], cwd: str) -> str:
    if not closing_refs:
        return "(no linked issues found — PR description should reference the ticket with 'Closes #N')"
    parts = []
    for ref in closing_refs:
        number = ref.get("number")
        if number is None:
            continue
        issue = get_issue(number, cwd)
        if issue:
            parts.append(f"Issue #{number}: {issue.get('title', '')}\n{issue.get('body', '')}")
        else:
            parts.append(f"Issue #{number}: (could not fetch details)")
    return "\n\n".join(parts) if parts else "(no issue details retrieved)"


def build_checks_text(checks: list[dict]) -> str:
    if not checks:
        return "(no CI checks found — they may still be queued)"
    lines = []
    for c in checks:
        bucket = c.get("bucket", "unknown")
        name = c.get("name", "unknown")
        state = c.get("state", "")
        lines.append(f"  [{bucket.upper():8}] {name}  ({state})")
    return "\n".join(lines)


def call_claude(pr: dict, diff: str, issues_text: str, checks_text: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("pr_reviewer: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return "(review skipped — ANTHROPIC_API_KEY not set)"

    try:
        import anthropic
    except ImportError:
        print("pr_reviewer: 'anthropic' package not installed", file=sys.stderr)
        return "(review skipped — install 'anthropic' in your Python environment)"

    pr_summary = (
        f"Title: {pr.get('title', '(none)')}\n"
        f"Branch: {pr.get('headRefName', '(unknown)')}\n\n"
        f"Description:\n{pr.get('body', '(empty)')}"
    )

    prompt = f"""You are a senior code reviewer. Review this pull request and answer each section concisely with bullet points.

1. TICKET COVERAGE — Does the implementation fully address every requirement stated in the linked issue(s)? Call out any requirement that appears unaddressed in the diff.

2. TESTS — Are new or updated tests present for the changed code? Based on the CI check results, do the test jobs pass?

3. CI CHECKS — List every check that is failing or still pending by name. If all pass, say so.

4. LINTER — Are there any linting failures visible in the CI results or the diff?

5. VERDICT — End with exactly one of:
   ✅ APPROVED — ready to merge
   ⚠️ NEEDS WORK — list the blockers
   ⏳ PENDING — CI is still running, re-review when complete

--- PR INFO ---
{pr_summary}

--- DIFF (may be truncated) ---
{diff}

--- LINKED ISSUES ---
{issues_text}

--- CI CHECKS ---
{checks_text}
"""

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def main() -> None:
    with open("C:/Users/bigchungus/hook_debug.log", "a") as f:
        f.write("hook invoked\n")

    raw = sys.stdin.read()
    if not raw:
        sys.exit(0)

    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    command = hook_input.get("tool_input", {}).get("command", "")
    if not any(t in command for t in TRIGGERS):
        sys.exit(0)

    cwd = hook_input.get("cwd") or os.getcwd()

    try:
        pr = get_pr_info(cwd)
        if pr is None:
            sys.exit(0)

        diff = get_diff(cwd)
        checks = get_checks(cwd)
        closing_refs = pr.get("closingIssuesReferences") or []
        issues_text = build_issues_text(closing_refs, cwd)
        checks_text = build_checks_text(checks)

        review = call_claude(pr, diff, issues_text, checks_text)

        pr_number = pr.get("number", "?")
        output = {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"\n\n---\n🔍 **Automated PR Review — PR #{pr_number}**\n\n{review}\n---"
                ),
            },
        }
        print(json.dumps(output))
        sys.exit(0)

    except Exception as exc:
        print(f"pr_reviewer error: {exc}", file=sys.stderr)
        print(json.dumps({"continue": True}))
        sys.exit(0)


if __name__ == "__main__":
    main()
