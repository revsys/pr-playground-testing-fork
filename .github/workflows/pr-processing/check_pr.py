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
from pathlib import Path

import httpx

# ── Configuration (from environment) ─────────────────────────────────────────

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["REPO"]
PR_NUMBER = os.environ["PR_NUMBER"]
PR_BODY = os.environ.get("PR_BODY", "")
ACCEPTABLE_STAGES = [
    q.strip()
    for q in os.environ.get(
        "ACCEPTABLE_STAGES", "Needs Patch,Needs PR Review,Waiting on Author"
    ).split(",")
]

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


def compute_trac_stage(row: dict) -> str:
    """
    Derive the human-readable stage name from raw Trac CSV fields.

    Django's triage docs describe three stages within the "Accepted" stage:
      Needs Patch       — stage=Accepted, has_patch=0
      Needs PR Review   — stage=Accepted, has_patch=1, no fix flags set
      Waiting on Author — stage=Accepted, has_patch=1, one or more fix flags set

    Tickets outside "Accepted" return their raw stage value
    (e.g. "Unreviewed", "Ready for Checkin", "Someday/Maybe").
    """
    stage = row.get("stage", "").strip()
    if stage != "Accepted":
        return stage

    has_patch = row.get("has_patch", "0").strip() == "1"
    needs_fix = any(
        row.get(flag, "0").strip() == "1"
        for flag in ("needs_better_patch", "needs_docs", "needs_tests")
    )

    if not has_patch:
        return "Needs Patch"
    if needs_fix:
        return "Waiting on Author"
    return "Needs PR Review"


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


def check_trac_status(pr_body: str, acceptable_stages: list[str]) -> str | None:
    """
    Check 2: The referenced Trac ticket must be in an acceptable stage.

    Fetches ticket data via the public Trac CSV API and derives the stage
    name from the stage + flag fields. Network errors are treated as
    non-fatal so that a Trac outage doesn't block all PRs.
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
                acceptable_stages=", ".join(acceptable_stages),
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

    stage = compute_trac_stage(row)
    if stage in acceptable_stages:
        return None

    return load_message(
        "invalid_trac_status.txt",
        ticket_id=ticket_id,
        stage=stage,
        acceptable_stages=", ".join(acceptable_stages),
    )


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


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    pr_files = get_pr_files()

    # Docs-only PRs are exempt from all quality checks.
    if pr_files and all(f.startswith("docs/") for f in pr_files):
        print(f"✓ PR #{PR_NUMBER} only touches docs/ — skipping all checks.")
        return

    # Rewrite bare ticket references to Markdown links.
    pr_body = PR_BODY
    rewritten = rewrite_ticket_links(pr_body)
    if rewritten != pr_body:
        print(f"Updating PR #{PR_NUMBER} body to linkify ticket references.")
        github_request("PATCH", f"/pulls/{PR_NUMBER}", {"body": rewritten})
        pr_body = rewritten

    checks = [
        lambda: check_trac_ticket(pr_body, pr_files),
        lambda: check_trac_status(pr_body, ACCEPTABLE_STAGES),
        lambda: check_branch_description(pr_body),
        lambda: check_ai_disclosure(pr_body),
        lambda: check_checklist(pr_body),
    ]

    failures = [result for check in checks if (result := check()) is not None]

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
