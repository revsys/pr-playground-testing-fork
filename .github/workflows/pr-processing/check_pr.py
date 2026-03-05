#!/usr/bin/env python3
"""
PR quality checks for Django pull requests.

Each check is an independent function that returns None on success or a
failure message string on failure. All checks are always run so that
contributors see every problem in a single pass.
"""

import csv
import io
import os
import re
import time
from pathlib import Path

import httpx

# ── Configuration (from environment) ─────────────────────────────────────────

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]
PR_NUMBER = os.environ["PR_NUMBER"]
PR_BODY = os.environ.get("PR_BODY", "")
PATCH_POLL_INTERVAL = int(os.environ.get("PATCH_POLL_INTERVAL", "15"))  # seconds between polls
PATCH_POLL_TIMEOUT = int(
    os.environ.get("PATCH_POLL_TIMEOUT", "600")
)  # max seconds to wait (10 min)

MESSAGES_DIR = Path(__file__).parent / "messages"

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_WORDS = 5
GITHUB_PER_PAGE = 100
TRAC_TIMEOUT = 15

# ── Helpers ───────────────────────────────────────────────────────────────────


def load_message(filename: str, **kwargs) -> str:
    """Load a message file and substitute any {variable} placeholders."""
    text = (MESSAGES_DIR / filename).read_text()
    return text.format_map(kwargs) if kwargs else text


def github_request(method: str, path: str, data: dict | None = None) -> object:
    """Make an authenticated GitHub API request."""
    url = f"https://api.github.com/repos/{REPO}{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = httpx.request(method, url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()


def get_pr_files() -> list[str]:
    """Return all filenames changed in the PR, handling pagination."""
    files: list[str] = []
    page = 1
    while True:
        results = github_request(
            "GET", f"/pulls/{PR_NUMBER}/files?per_page={GITHUB_PER_PAGE}&page={page}"
        )
        if not results:
            break
        files.extend(f["filename"] for f in results)
        if len(results) < GITHUB_PER_PAGE:
            break
        page += 1
    return files


# ── Body rewriting ────────────────────────────────────────────────────────────


def rewrite_ticket_links(pr_body: str) -> str:
    """
    Replace bare ticket-XXXXXX references with Markdown links.

    Already-linked references (e.g. [ticket-123](...)) are left untouched via
    a negative lookbehind that skips matches preceded by '['.
    """
    return re.sub(
        r"(?<!\[)\bticket-(\d+)\b",
        r"[ticket-\1](https://code.djangoproject.com/ticket/\1)",
        pr_body,
        flags=re.IGNORECASE,
    )


# ── Checks ────────────────────────────────────────────────────────────────────


def check_trac_ticket(pr_body: str, pr_files: list[str]) -> str | None:
    """
    Check 1: A Trac ticket must be referenced.

    Exception: if the PR only touches files under docs/ no ticket is required.
    """
    if pr_files and all(f.startswith("docs/") for f in pr_files):
        return None  # docs-only PR — ticket not required

    # Look for the ticket reference inside the Trac ticket number section.
    section_match = re.search(
        r"#### Trac ticket number[^\n]*\n(.*?)(?=\r?\n####|\Z)", pr_body, re.DOTALL
    )
    section = section_match.group(1) if section_match else pr_body

    if re.search(r"\bticket-\d+\b", section, re.IGNORECASE):
        return None  # valid ticket reference found

    return load_message("no_trac_ticket.txt")


def check_trac_status(pr_body: str) -> str | None:
    """
    Check 2: The referenced Trac ticket must be in the 'Accepted' stage.

    Fetches ticket data via the public Trac CSV API. Network errors are
    treated as non-fatal so that a Trac outage doesn't block all PRs.
    """
    match = re.search(r"\bticket-(\d+)\b", pr_body, re.IGNORECASE)
    if not match:
        return None  # No ticket found; Check 1 already reported that.

    ticket_id = match.group(1)
    url = f"https://code.djangoproject.com/ticket/{ticket_id}?format=csv"

    try:
        response = httpx.get(url, timeout=TRAC_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return load_message(
                "invalid_trac_status.txt",
                ticket_id=ticket_id,
                stage="(ticket not found)",
            )
        print(
            f"Warning: HTTP {exc.response.status_code} fetching ticket {ticket_id} — skipping status check."
        )
        return None
    except Exception as exc:
        print(f"Warning: Could not fetch ticket {ticket_id}: {exc} — skipping status check.")
        return None

    reader = csv.DictReader(io.StringIO(response.text))
    row = next(reader, None)
    if row is None:
        print(f"Warning: Empty CSV for ticket {ticket_id} — skipping status check.")
        return None

    stage = row.get("stage", "").strip()
    if stage == "Accepted":
        return None

    return load_message("invalid_trac_status.txt", ticket_id=ticket_id, stage=stage)


def check_trac_has_patch(pr_body: str) -> str | None:
    """
    Check 3: The referenced Trac ticket must have has_patch=1.

    Polls the Trac CSV API every PATCH_POLL_INTERVAL seconds for up to
    PATCH_POLL_TIMEOUT seconds. Network errors skip the check. If the
    flag is still unset after the timeout, the PR is closed.
    """
    match = re.search(r"\bticket-(\d+)\b", pr_body, re.IGNORECASE)
    if not match:
        return None  # No ticket found; Check 1 already reported that.

    ticket_id = match.group(1)
    url = f"https://code.djangoproject.com/ticket/{ticket_id}?format=csv"
    deadline = time.monotonic() + PATCH_POLL_TIMEOUT

    elapsed = 0
    while True:
        print(f"Checking has_patch flag for ticket-{ticket_id} (elapsed: {elapsed}s) ...")
        try:
            response = httpx.get(url, timeout=TRAC_TIMEOUT)
            response.raise_for_status()
            reader = csv.DictReader(io.StringIO(response.text))
            row = next(reader, None)
            if row is not None and row.get("has_patch", "0").strip() == "1":
                print(f"✓ ticket-{ticket_id} has_patch flag is set.")
                return None
            print(f"  has_patch not yet set — will retry in {PATCH_POLL_INTERVAL}s.")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None  # Ticket not found — already reported by check_trac_status.
            print(
                f"Warning: HTTP {exc.response.status_code} fetching ticket {ticket_id} — skipping has_patch check."
            )
            return None
        except Exception as exc:
            print(f"Warning: Could not fetch ticket {ticket_id}: {exc} — skipping has_patch check.")
            return None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleep_time = min(PATCH_POLL_INTERVAL, remaining)
        time.sleep(sleep_time)
        elapsed += int(sleep_time)

    print(f"✗ ticket-{ticket_id} has_patch flag was not set after {PATCH_POLL_TIMEOUT}s.")

    return load_message("no_patch_flag.txt", ticket_id=ticket_id)


def check_branch_description(pr_body: str) -> str | None:
    """
    Check 3: The branch description must be present, non-placeholder, and
    at least 5 words long.
    """
    placeholder = (
        "Provide a concise overview of the issue or rationale behind the proposed changes."
    )

    match = re.search(
        r"#### Branch description[ \t]*\r?\n(.*?)(?=\r?\n####|\Z)", pr_body, re.DOTALL
    )
    if not match:
        return load_message("missing_description.txt")

    # Strip HTML comments before evaluating content.
    cleaned = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.DOTALL).strip()

    if not cleaned or cleaned == placeholder or len(cleaned.split()) < MIN_WORDS:
        return load_message("missing_description.txt")

    return None


def check_ai_disclosure(pr_body: str) -> str | None:
    """
    Check 4: Exactly one AI disclosure checkbox must be selected.
    If the "AI tools were used" option is checked, at least 5 words of
    additional description must be present in that section.
    """
    match = re.search(
        r"#### AI Assistance Disclosure[^\n]*\n(.*?)(?=\r?\n####|\Z)", pr_body, re.DOTALL
    )
    if not match:
        return load_message("missing_ai_disclosure.txt")

    section = match.group(1)
    no_ai_checked = bool(re.search(r"-\s*\[x\].*?No AI tools were used", section, re.IGNORECASE))
    ai_used_checked = bool(re.search(r"-\s*\[x\].*?If AI tools were used", section, re.IGNORECASE))

    # Must check exactly one option.
    if no_ai_checked == ai_used_checked:
        return load_message("missing_ai_disclosure.txt")

    if ai_used_checked:
        # Collect any text lines that are not the two checkbox lines or comments.
        extra_lines = [
            line.strip()
            for line in section.splitlines()
            if line.strip()
            and not line.strip().startswith("- [")
            and not line.strip().startswith("<!--")
            and not line.strip().endswith("-->")
        ]
        # Ensure PR author includes at least 5 words about their AI use
        if len(" ".join(extra_lines).split()) < MIN_WORDS:
            return load_message("missing_ai_description.txt")

    return None


def check_checklist(pr_body: str) -> str | None:
    """
    Check 5: The first five items in the Checklist section must be checked.
    """
    match = re.search(r"#### Checklist[ \t]*\r?\n(.*?)(?=\r?\n####|\Z)", pr_body, re.DOTALL)
    if not match:
        return load_message("incomplete_checklist.txt")

    checkboxes = re.findall(r"-\s*\[(.)\]", match.group(1))

    if len(checkboxes) < 5 or not all(c.lower() == "x" for c in checkboxes[:5]):
        return load_message("incomplete_checklist.txt")

    return None


# ── Job summary ───────────────────────────────────────────────────────────────

SKIPPED = object()  # sentinel: check was not run due to a prior failure


def write_job_summary(pr_number: str, results: list[tuple[str, str | None | object]]) -> None:
    """Write a Markdown job summary to $GITHUB_STEP_SUMMARY (if available)."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    lines = [
        f"## PR #{pr_number} Quality Check Results\n",
        "| | Check | Result |",
        "| --- | --- | --- |",
    ]
    for name, result in results:
        if result is SKIPPED:
            icon, status = "⏭️", "Skipped"
        elif result is None:
            icon, status = "✅", "Passed"
        else:
            icon, status = "❌", "Failed"
        lines.append(f"| {icon} | {name} | {status} |")

    with open(summary_file, "a") as f:
        f.write("\n".join(lines) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    pr_files = get_pr_files()

    # Docs-only PRs are exempt from all quality checks.
    if pr_files and all(f.startswith("docs/") for f in pr_files):
        print(f"✓ PR #{PR_NUMBER} only touches docs/ — skipping all checks.")
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a") as f:
                f.write(f"## PR #{PR_NUMBER} Quality Check Results\n\n")
                f.write("> ℹ️ Docs-only PR — all quality checks skipped.\n")
        return

    # Rewrite bare ticket references to Markdown links.
    pr_body = PR_BODY
    rewritten = rewrite_ticket_links(pr_body)
    if rewritten != pr_body:
        print(f"Updating PR #{PR_NUMBER} body to linkify ticket references.")
        github_request("PATCH", f"/pulls/{PR_NUMBER}", {"body": rewritten})
        pr_body = rewritten

    ticket_result = check_trac_ticket(pr_body, pr_files)
    if ticket_result is None:
        status_result = check_trac_status(pr_body)
        has_patch_result = check_trac_has_patch(pr_body)
    else:
        print("No Trac ticket — skipping status and has_patch checks.")
        status_result = SKIPPED
        has_patch_result = SKIPPED

    results = [
        ("Trac ticket referenced", ticket_result),
        ("Trac ticket status is Accepted", status_result),
        ("Trac ticket has_patch flag set", has_patch_result),
        ("Branch description provided", check_branch_description(pr_body)),
        ("AI disclosure completed", check_ai_disclosure(pr_body)),
        ("Checklist completed", check_checklist(pr_body)),
    ]
    write_job_summary(PR_NUMBER, results)

    failures = [msg for _, msg in results if msg is not None and msg is not SKIPPED]

    if not failures:
        print(f"✓ PR #{PR_NUMBER} passed all quality checks.")
        return

    print(f"✗ PR #{PR_NUMBER} failed {len(failures)} check(s). Commenting and closing.")

    header = load_message("closing_header.txt")
    footer = load_message("closing_footer.txt")
    separator = "\n\n---\n\n"
    comment_body = separator.join([header, *failures, footer])

    github_request("POST", f"/issues/{PR_NUMBER}/comments", {"body": comment_body})
    github_request("PATCH", f"/pulls/{PR_NUMBER}", {"state": "closed"})


if __name__ == "__main__":
    main()
