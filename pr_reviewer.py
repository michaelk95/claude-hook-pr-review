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
import time

TRIGGERS = ["git push", "gh pr create", "gh pr merge"]
DIFF_CHAR_LIMIT = 15_000
PR_POLL_ATTEMPTS = 5
PR_POLL_INTERVAL = 3  # seconds between attempts


def log(msg: str) -> None:
    print(f"[pr-review] {msg}", file=sys.stderr)


def run(args: list[str], cwd: str) -> tuple[str, int]:
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    return result.stdout.strip(), result.returncode


def poll_for_pr(cwd: str) -> dict | None:
    fields = "number,title,body,headRefName,closingIssuesReferences,statusCheckRollup"
    for attempt in range(1, PR_POLL_ATTEMPTS + 1):
        log(f"looking for open PR (attempt {attempt}/{PR_POLL_ATTEMPTS})...")
        out, code = run(["gh", "pr", "view", "--json", fields], cwd)
        if code == 0 and out:
            try:
                pr = json.loads(out)
                log(f"found PR #{pr.get('number')}: {pr.get('title')}")
                return pr
            except json.JSONDecodeError:
                log("gh returned invalid JSON, retrying...")
        else:
            log("no PR found yet")

        if attempt < PR_POLL_ATTEMPTS:
            log(f"waiting {PR_POLL_INTERVAL}s before next attempt...")
            time.sleep(PR_POLL_INTERVAL)

    log(f"no PR found after {PR_POLL_ATTEMPTS} attempts — skipping review")
    return None


def get_diff(cwd: str) -> str:
    log("fetching PR diff...")
    out, code = run(["gh", "pr", "diff"], cwd)
    if code != 0 or not out:
        log("diff unavailable")
        return "(diff unavailable)"
    if len(out) > DIFF_CHAR_LIMIT:
        log(f"diff truncated to {DIFF_CHAR_LIMIT} chars")
        return out[:DIFF_CHAR_LIMIT] + f"\n... (truncated at {DIFF_CHAR_LIMIT} chars)"
    log(f"diff fetched ({len(out)} chars)")
    return out


def get_checks(cwd: str) -> list[dict]:
    log("fetching CI checks...")
    out, code = run(["gh", "pr", "checks", "--json", "name,state,bucket"], cwd)
    if code not in (0, 8) or not out:  # exit 8 = checks still pending
        log("no CI checks available yet")
        return []
    try:
        checks = json.loads(out)
        log(f"fetched {len(checks)} CI check(s)")
        return checks
    except json.JSONDecodeError:
        log("invalid JSON from gh pr checks")
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
        log("no linked issues found in PR")
        return "(no linked issues found — PR description should reference the ticket with 'Closes #N')"
    parts = []
    for ref in closing_refs:
        number = ref.get("number")
        if number is None:
            continue
        log(f"fetching linked issue #{number}...")
        issue = get_issue(number, cwd)
        if issue:
            log(f"fetched issue #{number}: {issue.get('title')}")
            parts.append(f"Issue #{number}: {issue.get('title', '')}\n{issue.get('body', '')}")
        else:
            log(f"could not fetch issue #{number}")
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
        log("ANTHROPIC_API_KEY not set — skipping review")
        return "(review skipped — ANTHROPIC_API_KEY not set)"

    try:
        import anthropic
    except ImportError:
        log("'anthropic' package not installed — skipping review")
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

    log("calling Claude API for review...")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    log("review complete")
    return response.content[0].text


def main() -> None:
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

    log(f"trigger matched: {command!r}")
    cwd = hook_input.get("cwd") or os.getcwd()
    log(f"working directory: {cwd}")

    try:
        pr = poll_for_pr(cwd)
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
        log(f"unexpected error: {exc}")
        print(json.dumps({"continue": True}))
        sys.exit(0)


if __name__ == "__main__":
    main()
