"""
Tests for the PR quality checks in check_pr.py.

Each check function is tested in isolation with a range of passing and
failing PR body variants so that changes to parsing logic surface
immediately.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

import check_pr

# ── Helpers ───────────────────────────────────────────────────────────────────

ACCEPTABLE_STAGES = ["Needs Patch", "Needs PR Review", "Waiting on Author"]

NON_DOCS_FILES = ["django/template/base.py", "tests/template_tests/test_base.py"]
DOCS_ONLY_FILES = ["docs/topics/templates.txt", "docs/ref/templates/api.txt"]
MIXED_FILES = ["django/template/base.py", "docs/topics/templates.txt"]


def make_pr_body(
    ticket="ticket-36969",
    description="Fix regression in template rendering where nested blocks were incorrectly evaluated.",
    no_ai_checked=True,
    ai_used_checked=False,
    ai_description="",
    checked_items=5,
):
    """
    Build a PR body string with the given field values.

    checked_items controls how many of the 5 required checklist items are
    marked [x] (counted from the top).
    """
    no_ai_box = "[x]" if no_ai_checked else "[ ]"
    ai_used_box = "[x]" if ai_used_checked else "[ ]"
    ai_extra = f"\n{ai_description}" if ai_description else ""

    checklist_items = [
        "This PR follows the [contribution guidelines](https://docs.djangoproject.com/en/stable/internals/contributing/writing-code/submitting-patches/).",
        "This PR **does not** disclose a security vulnerability (see [vulnerability reporting](https://docs.djangoproject.com/en/stable/internals/security/)).",
        "This PR targets the `main` branch.",
        "The commit message is written in past tense, mentions the ticket number, and ends with a period.",
        'I have checked the "Has patch" ticket flag in the Trac system.',
        "I have added or updated relevant tests.",
        "I have added or updated relevant docs, including release notes if applicable.",
        "I have attached screenshots in both light and dark modes for any UI changes.",
    ]
    checklist_lines = "\n".join(
        f"- [x] {item}" if i < checked_items else f"- [ ] {item}"
        for i, item in enumerate(checklist_items)
    )

    # No indentation — GitHub PR bodies are plain markdown with no leading spaces.
    return (
        f"#### Trac ticket number\n"
        f"<!-- Replace XXXXX with the corresponding Trac ticket number. -->\n"
        f"{ticket}\n"
        f"\n"
        f"#### Branch description\n"
        f"{description}\n"
        f"\n"
        f"#### AI Assistance Disclosure (REQUIRED)\n"
        f"<!-- Please select exactly ONE of the following: -->\n"
        f"- {no_ai_box} **No AI tools were used** in preparing this PR.\n"
        f"- {ai_used_box} **If AI tools were used**, I have disclosed which ones, and fully reviewed and verified their output.{ai_extra}\n"
        f"\n"
        f"#### Checklist\n"
        f"{checklist_lines}\n"
    )


VALID_PR_BODY = make_pr_body()


def make_trac_csv(
    ticket_id="36969",
    stage="Accepted",
    has_patch="0",
    needs_docs="0",
    needs_tests="0",
    needs_better_patch="0",
):
    """Build a minimal Trac CSV response for use in mock HTTP calls."""
    header = "id,summary,reporter,owner,description,type,status,component,version,severity,resolution,keywords,cc,stage,has_patch,needs_docs,needs_tests,needs_better_patch,easy,ui_ux"
    row = f"{ticket_id},Some summary,reporter,,description,Bug,new,core,5.0,Normal,,,,{stage},{has_patch},{needs_docs},{needs_tests},{needs_better_patch},0,0"
    return header + "\n" + row + "\n"


def mock_httpx_get(csv_text):
    """Return a mock for httpx.get that returns a response with the given text."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.text = csv_text
    mock_resp.raise_for_status = MagicMock()
    return MagicMock(return_value=mock_resp)


def make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with the given status code."""
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


# ── rewrite_ticket_links ──────────────────────────────────────────────────────


def test_rewrite_ticket_links_plain_reference():
    result = check_pr.rewrite_ticket_links("See ticket-12345 for details.")
    assert result == "See [ticket-12345](https://code.djangoproject.com/ticket/12345) for details."


def test_rewrite_ticket_links_already_linked_unchanged():
    body = "See [ticket-12345](https://code.djangoproject.com/ticket/12345) for details."
    assert check_pr.rewrite_ticket_links(body) == body


def test_rewrite_ticket_links_multiple_references():
    body = "Fixes ticket-100 and ticket-200."
    result = check_pr.rewrite_ticket_links(body)
    assert "[ticket-100](https://code.djangoproject.com/ticket/100)" in result
    assert "[ticket-200](https://code.djangoproject.com/ticket/200)" in result


def test_rewrite_ticket_links_case_insensitive():
    result = check_pr.rewrite_ticket_links("See TICKET-99 for details.")
    assert "https://code.djangoproject.com/ticket/99" in result


def test_rewrite_ticket_links_no_ticket_unchanged():
    body = "No ticket references here."
    assert check_pr.rewrite_ticket_links(body) == body


def test_rewrite_ticket_links_mixed_linked_and_plain():
    body = "Already [ticket-1](https://code.djangoproject.com/ticket/1) and plain ticket-2."
    result = check_pr.rewrite_ticket_links(body)
    assert result.count("ticket-1") == 1  # not double-linked
    assert "[ticket-2](https://code.djangoproject.com/ticket/2)" in result


# ── Check 1: Trac ticket presence ─────────────────────────────────────────────


def test_trac_ticket_valid_non_docs_passes():
    assert check_pr.check_trac_ticket(VALID_PR_BODY, NON_DOCS_FILES) is None


def test_trac_ticket_valid_docs_only_passes():
    assert check_pr.check_trac_ticket(VALID_PR_BODY, DOCS_ONLY_FILES) is None


def test_trac_ticket_docs_only_no_ticket_passes():
    """docs-only PRs do not require a ticket."""
    body = make_pr_body(ticket="")
    assert check_pr.check_trac_ticket(body, DOCS_ONLY_FILES) is None


def test_trac_ticket_placeholder_fails():
    """ticket-XXXXX (literal X's) is not a valid ticket number."""
    body = make_pr_body(ticket="ticket-XXXXX")
    assert check_pr.check_trac_ticket(body, NON_DOCS_FILES) is not None


def test_trac_ticket_missing_fails():
    body = make_pr_body(ticket="")
    assert check_pr.check_trac_ticket(body, NON_DOCS_FILES) is not None


def test_trac_ticket_mixed_files_requires_ticket():
    """If even one file is outside docs/, a ticket is required."""
    body = make_pr_body(ticket="")
    assert check_pr.check_trac_ticket(body, MIXED_FILES) is not None


def test_trac_ticket_mixed_files_with_ticket_passes():
    assert check_pr.check_trac_ticket(VALID_PR_BODY, MIXED_FILES) is None


def test_trac_ticket_empty_file_list_requires_ticket():
    """An empty file list (edge case) should still require a ticket."""
    body = make_pr_body(ticket="")
    assert check_pr.check_trac_ticket(body, []) is not None


@pytest.mark.parametrize("ticket", ["ticket-1", "ticket-123", "ticket-999999"])
def test_trac_ticket_various_lengths_pass(ticket):
    body = make_pr_body(ticket=ticket)
    assert check_pr.check_trac_ticket(body, NON_DOCS_FILES) is None


# ── compute_trac_stage ────────────────────────────────────────────────────────


def _stage_row(stage, has_patch="0", needs_better_patch="0", needs_docs="0", needs_tests="0"):
    return {
        "stage": stage,
        "has_patch": has_patch,
        "needs_better_patch": needs_better_patch,
        "needs_docs": needs_docs,
        "needs_tests": needs_tests,
    }


def test_compute_stage_unreviewed():
    assert check_pr.compute_trac_stage(_stage_row("Unreviewed")) == "Unreviewed"


def test_compute_stage_someday_maybe():
    assert check_pr.compute_trac_stage(_stage_row("Someday/Maybe")) == "Someday/Maybe"


def test_compute_stage_ready_for_checkin():
    assert (
        check_pr.compute_trac_stage(_stage_row("Ready for Checkin", has_patch="1"))
        == "Ready for Checkin"
    )


def test_compute_stage_accepted_no_patch_is_needs_patch():
    assert check_pr.compute_trac_stage(_stage_row("Accepted", has_patch="0")) == "Needs Patch"


def test_compute_stage_accepted_with_patch_no_flags_is_needs_review():
    assert check_pr.compute_trac_stage(_stage_row("Accepted", has_patch="1")) == "Needs PR Review"


def test_compute_stage_needs_better_patch_is_waiting_on_author():
    assert (
        check_pr.compute_trac_stage(_stage_row("Accepted", has_patch="1", needs_better_patch="1"))
        == "Waiting on Author"
    )


def test_compute_stage_needs_docs_is_waiting_on_author():
    assert (
        check_pr.compute_trac_stage(_stage_row("Accepted", has_patch="1", needs_docs="1"))
        == "Waiting on Author"
    )


def test_compute_stage_needs_tests_is_waiting_on_author():
    assert (
        check_pr.compute_trac_stage(_stage_row("Accepted", has_patch="1", needs_tests="1"))
        == "Waiting on Author"
    )


# ── Check 2: Trac ticket status ───────────────────────────────────────────────


def test_trac_status_no_ticket_skips_check():
    """If there is no ticket reference the status check is a no-op."""
    assert check_pr.check_trac_status("No ticket here.", ACCEPTABLE_STAGES) is None


@pytest.mark.parametrize(
    "stage,has_patch,needs_better_patch,expected_stage",
    [
        ("Accepted", "0", "0", "Needs Patch"),
        ("Accepted", "1", "0", "Needs PR Review"),
        ("Accepted", "1", "1", "Waiting on Author"),
    ],
)
def test_trac_status_acceptable_stages_pass(stage, has_patch, needs_better_patch, expected_stage):
    csv_text = make_trac_csv(
        stage=stage, has_patch=has_patch, needs_better_patch=needs_better_patch
    )
    with patch("httpx.get", mock_httpx_get(csv_text)):
        result = check_pr.check_trac_status("ticket-36969", ACCEPTABLE_STAGES)
    assert result is None, f"Expected {expected_stage} to pass but got a failure message"


@pytest.mark.parametrize(
    "stage,has_patch",
    [
        ("Unreviewed", "0"),
        ("Ready for Checkin", "1"),
        ("Someday/Maybe", "0"),
    ],
)
def test_trac_status_unacceptable_stages_fail(stage, has_patch):
    csv_text = make_trac_csv(stage=stage, has_patch=has_patch)
    with patch("httpx.get", mock_httpx_get(csv_text)):
        assert check_pr.check_trac_status("ticket-36969", ACCEPTABLE_STAGES) is not None


def test_trac_status_failure_message_contains_ticket_id():
    csv_text = make_trac_csv(ticket_id="12345", stage="Unreviewed", has_patch="0")
    with patch("httpx.get", mock_httpx_get(csv_text)):
        result = check_pr.check_trac_status("ticket-12345", ACCEPTABLE_STAGES)
    assert "12345" in result


def test_trac_status_failure_message_contains_current_stage():
    csv_text = make_trac_csv(stage="Unreviewed", has_patch="0")
    with patch("httpx.get", mock_httpx_get(csv_text)):
        result = check_pr.check_trac_status("ticket-36969", ACCEPTABLE_STAGES)
    assert "Unreviewed" in result


def test_trac_status_failure_message_lists_acceptable_stages():
    csv_text = make_trac_csv(stage="Unreviewed", has_patch="0")
    with patch("httpx.get", mock_httpx_get(csv_text)):
        result = check_pr.check_trac_status("ticket-36969", ACCEPTABLE_STAGES)
    for q in ACCEPTABLE_STAGES:
        assert q in result


def test_trac_status_http_404_fails():
    """A 404 means the ticket doesn't exist — that is a failure."""
    with patch("httpx.get", side_effect=make_http_status_error(404)):
        assert check_pr.check_trac_status("ticket-99999", ACCEPTABLE_STAGES) is not None


def test_trac_status_network_error_skips_check():
    """A transient network error should not close valid PRs."""
    with patch("httpx.get", side_effect=OSError("Connection refused")):
        assert check_pr.check_trac_status("ticket-36969", ACCEPTABLE_STAGES) is None


def test_trac_status_http_500_skips_check():
    """Trac server errors are treated as transient — skip the check."""
    with patch("httpx.get", side_effect=make_http_status_error(500)):
        assert check_pr.check_trac_status("ticket-36969", ACCEPTABLE_STAGES) is None


def test_trac_status_custom_acceptable_stages():
    """ACCEPTABLE_STAGES is configurable — verify custom values are respected."""
    csv_text = make_trac_csv(stage="Ready for Checkin", has_patch="1")
    with patch("httpx.get", mock_httpx_get(csv_text)):
        assert check_pr.check_trac_status("ticket-36969", ACCEPTABLE_STAGES) is not None
        assert (
            check_pr.check_trac_status("ticket-36969", [*ACCEPTABLE_STAGES, "Ready for Checkin"])
            is None
        )


# ── Check 3: Branch description ───────────────────────────────────────────────


def test_description_valid_passes():
    assert check_pr.check_branch_description(VALID_PR_BODY) is None


def test_description_placeholder_fails():
    body = make_pr_body(
        description="Provide a concise overview of the issue or rationale behind the proposed changes."
    )
    assert check_pr.check_branch_description(body) is not None


def test_description_empty_fails():
    body = make_pr_body(description="")
    assert check_pr.check_branch_description(body) is not None


def test_description_too_short_fails():
    body = make_pr_body(description="Fix bug.")
    assert check_pr.check_branch_description(body) is not None


def test_description_exactly_five_words_passes():
    body = make_pr_body(description="Fix the template rendering bug.")
    assert check_pr.check_branch_description(body) is None


def test_description_html_comment_only_fails():
    """An HTML comment alone must not satisfy the description requirement."""
    body = make_pr_body(description="<!-- Provide a concise overview of the issue -->")
    assert check_pr.check_branch_description(body) is not None


def test_description_html_comment_words_not_counted():
    """Words inside HTML comments should not count toward the 5-word minimum."""
    body = make_pr_body(description="<!-- this has five words --> fix")
    assert check_pr.check_branch_description(body) is not None


def test_description_missing_section_header_fails():
    body = VALID_PR_BODY.replace("#### Branch description\n", "")
    assert check_pr.check_branch_description(body) is not None


def test_description_multiline_passes():
    body = make_pr_body(
        description="This PR fixes a bug in the ORM.\nThe issue affects queries with multiple joins."
    )
    assert check_pr.check_branch_description(body) is None


# ── Check 4: AI disclosure ────────────────────────────────────────────────────


def test_ai_no_ai_checked_passes():
    assert check_pr.check_ai_disclosure(VALID_PR_BODY) is None


def test_ai_used_with_description_passes():
    body = make_pr_body(
        no_ai_checked=False,
        ai_used_checked=True,
        ai_description="Used GitHub Copilot for autocomplete, all output manually reviewed.",
    )
    assert check_pr.check_ai_disclosure(body) is None


def test_ai_neither_option_checked_fails():
    body = make_pr_body(no_ai_checked=False, ai_used_checked=False)
    assert check_pr.check_ai_disclosure(body) is not None


def test_ai_both_options_checked_fails():
    body = make_pr_body(no_ai_checked=True, ai_used_checked=True)
    assert check_pr.check_ai_disclosure(body) is not None


def test_ai_used_no_description_fails():
    body = make_pr_body(no_ai_checked=False, ai_used_checked=True, ai_description="")
    assert check_pr.check_ai_disclosure(body) is not None


def test_ai_used_short_description_fails():
    body = make_pr_body(no_ai_checked=False, ai_used_checked=True, ai_description="Used Copilot.")
    assert check_pr.check_ai_disclosure(body) is not None


def test_ai_used_exactly_five_word_description_passes():
    body = make_pr_body(
        no_ai_checked=False, ai_used_checked=True, ai_description="Used Claude for code review."
    )
    assert check_pr.check_ai_disclosure(body) is None


def test_ai_missing_section_fails():
    body = VALID_PR_BODY.replace("#### AI Assistance Disclosure (REQUIRED)\n", "")
    assert check_pr.check_ai_disclosure(body) is not None


def test_ai_uppercase_x_in_checkbox_passes():
    """[X] (uppercase) should be treated the same as [x]."""
    body = VALID_PR_BODY.replace(
        "- [x] **No AI tools were used**", "- [X] **No AI tools were used**"
    )
    assert check_pr.check_ai_disclosure(body) is None


# ── Check 5: Checklist ────────────────────────────────────────────────────────


def test_checklist_first_five_checked_passes():
    assert check_pr.check_checklist(VALID_PR_BODY) is None


def test_checklist_all_eight_checked_passes():
    body = make_pr_body(checked_items=8)
    assert check_pr.check_checklist(body) is None


def test_checklist_none_checked_fails():
    body = make_pr_body(checked_items=0)
    assert check_pr.check_checklist(body) is not None


def test_checklist_four_of_five_checked_fails():
    body = make_pr_body(checked_items=4)
    assert check_pr.check_checklist(body) is not None


def test_checklist_three_of_five_checked_fails():
    body = make_pr_body(checked_items=3)
    assert check_pr.check_checklist(body) is not None


def test_checklist_missing_section_fails():
    body = VALID_PR_BODY.replace("#### Checklist\n", "")
    assert check_pr.check_checklist(body) is not None


def test_checklist_uppercase_x_passes():
    body = VALID_PR_BODY.replace("- [x]", "- [X]")
    assert check_pr.check_checklist(body) is None


# ── Integration ───────────────────────────────────────────────────────────────


def test_integration_fully_valid_pr_passes_all_checks():
    """A correctly filled-out PR body should pass every check."""
    csv_text = make_trac_csv(stage="Accepted", has_patch="0")
    with patch("httpx.get", mock_httpx_get(csv_text)):
        results = [
            check_pr.check_trac_ticket(VALID_PR_BODY, NON_DOCS_FILES),
            check_pr.check_trac_status(VALID_PR_BODY, ACCEPTABLE_STAGES),
            check_pr.check_branch_description(VALID_PR_BODY),
            check_pr.check_ai_disclosure(VALID_PR_BODY),
            check_pr.check_checklist(VALID_PR_BODY),
        ]
    failures = [r for r in results if r is not None]
    assert failures == [], "Expected no failures, got:\n" + "\n---\n".join(failures)


def test_integration_blank_body_fails_non_status_checks():
    """A completely empty PR body should fail every check except the Trac status check."""
    results = [
        check_pr.check_trac_ticket("", NON_DOCS_FILES),
        check_pr.check_branch_description(""),
        check_pr.check_ai_disclosure(""),
        check_pr.check_checklist(""),
    ]
    for i, result in enumerate(results, 1):
        assert result is not None, f"Check {i} should have failed on empty body"


def test_integration_unedited_template_fails_all_checks():
    """Submitting the raw PR template without filling anything in fails all checks."""
    with open("pull_request_template.md") as f:
        raw_template = f.read()
    results = [
        check_pr.check_trac_ticket(raw_template, NON_DOCS_FILES),
        check_pr.check_branch_description(raw_template),
        check_pr.check_ai_disclosure(raw_template),
        check_pr.check_checklist(raw_template),
    ]
    for i, result in enumerate(results, 1):
        assert result is not None, f"Check {i} should have failed on raw template"


# ── PR #5 regression fixture ──────────────────────────────────────────────────
# This is the exact body from https://github.com/frankwiles/pr-playground/pull/5
# The PR was incorrectly flagged for missing branch description and incomplete
# checklist even though both were properly filled in. Root cause: GitHub delivers
# PR bodies with \r\n line endings, which the [ \t]*\n section-header regexes
# did not handle.

PR5_BODY = (
    "#### Trac ticket number\r\n"
    "<!-- Replace XXXXX with the corresponding Trac ticket number."
    " All PRs must have a Trac ticket or be only docs changes -->\r\n"
    "\r\n"
    "ticket-37000\r\n"
    "\r\n"
    "#### Branch description\r\n"
    "\r\n"
    "This is a testing PR, but the Trac ticket doesn't exist so this should error. \r\n"
    "\r\n"
    "#### AI Assistance Disclosure (REQUIRED)\r\n"
    "<!-- Please select exactly ONE of the following: -->\r\n"
    "- [x] **No AI tools were used** in preparing this PR.\r\n"
    "- [ ] **If AI tools were used**, I have disclosed which ones,"
    " and fully reviewed and verified their output.\r\n"
    "\r\n"
    "#### Checklist\r\n"
    "- [x] This PR follows the [contribution guidelines]"
    "(https://docs.djangoproject.com/en/stable/internals/contributing/writing-code/submitting-patches/).\r\n"
    "- [x] This PR **does not** disclose a security vulnerability"
    " (see [vulnerability reporting](https://docs.djangoproject.com/en/stable/internals/security/)).\r\n"
    "- [x] This PR targets the `main` branch."
    " <!-- Backports will be evaluated and done by mergers, when necessary. -->\r\n"
    "- [x] The commit message is written in past tense, mentions the ticket number, and ends with a period.\r\n"
    '- [x] I have checked the "Has patch" ticket flag in the Trac system.\r\n'
    "- [ ] I have added or updated relevant tests.\r\n"
    "- [ ] I have added or updated relevant docs, including release notes if applicable.\r\n"
    "- [ ] I have attached screenshots in both light and dark modes for any UI changes.\r\n"
)


def test_pr5_branch_description_passes():
    """PR #5 has a valid branch description; must not be flagged as missing."""
    assert check_pr.check_branch_description(PR5_BODY) is None


def test_pr5_checklist_passes():
    """PR #5 has all 5 required checklist items checked; must not be flagged as incomplete."""
    assert check_pr.check_checklist(PR5_BODY) is None


def test_pr5_ai_disclosure_passes():
    """PR #5 correctly selects 'No AI tools were used'."""
    assert check_pr.check_ai_disclosure(PR5_BODY) is None


def test_pr5_trac_status_fails_for_missing_ticket():
    """PR #5 references ticket-37000 which does not exist; trac status check must fail."""
    with patch("httpx.get", side_effect=make_http_status_error(404)):
        assert check_pr.check_trac_status(PR5_BODY, ACCEPTABLE_STAGES) is not None


def test_crlf_branch_description_passes():
    """Section-header regex must handle \\r\\n line endings from GitHub's API."""
    body = make_pr_body().replace("\n", "\r\n")
    assert check_pr.check_branch_description(body) is None


def test_crlf_checklist_passes():
    """Checklist regex must handle \\r\\n line endings from GitHub's API."""
    body = make_pr_body().replace("\n", "\r\n")
    assert check_pr.check_checklist(body) is None


def test_crlf_full_valid_pr_passes_all_checks():
    """A fully valid PR body with \\r\\n line endings must pass every check."""
    body = make_pr_body().replace("\n", "\r\n")
    csv_text = make_trac_csv(stage="Accepted", has_patch="0")
    with patch("httpx.get", mock_httpx_get(csv_text)):
        results = [
            check_pr.check_trac_ticket(body, NON_DOCS_FILES),
            check_pr.check_trac_status(body, ACCEPTABLE_STAGES),
            check_pr.check_branch_description(body),
            check_pr.check_ai_disclosure(body),
            check_pr.check_checklist(body),
        ]
    failures = [r for r in results if r is not None]
    assert failures == [], "Expected no failures with CRLF body, got:\n" + "\n---\n".join(failures)


def test_integration_docs_only_pr_skips_all_checks(monkeypatch, capsys):
    """A docs-only PR should pass without running any checks, even with an empty body."""
    monkeypatch.setattr(check_pr, "PR_NUMBER", "42")
    monkeypatch.setattr(check_pr, "PR_BODY", "")
    monkeypatch.setattr(check_pr, "get_pr_files", lambda: DOCS_ONLY_FILES)
    # Ensure github_request is never called (no comment posted, no PR closed).
    monkeypatch.setattr(
        check_pr,
        "github_request",
        MagicMock(side_effect=AssertionError("should not call github_request")),
    )

    check_pr.main()

    captured = capsys.readouterr()
    assert "docs/" in captured.out
    assert "skipping" in captured.out.lower()
