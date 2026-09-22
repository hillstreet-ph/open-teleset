import json
from datetime import datetime, timezone

from outreach_policy import OutreachPolicy, hash_identifier


NOW = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)


def _policy(
    tmp_path,
    *,
    suppressed=False,
    approval_action="personal_send",
    consent_action="personal_send",
    approval_expires="2026-09-19T02:00:00Z",
    consent_expires="2026-09-19T02:00:00Z",
):
    subject_hash = hash_identifier("@ExampleUser")
    account_hash = hash_identifier("account-1")
    data = {
        "version": 1,
        "approvals": {
            "approval-1": {
                "action": approval_action,
                "expires_at": approval_expires,
                "account_hashes": [account_hash],
            }
        },
        "consents": [
            {
                "subject_hash": subject_hash,
                "actions": [consent_action],
                "source": "explicit",
                "granted_at": "2026-09-17T02:00:00Z",
                "expires_at": consent_expires,
                "account_hashes": [account_hash],
            }
        ],
        "suppressions": [
            {"subject_hash": subject_hash, "active": suppressed}
        ],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return OutreachPolicy(str(path))


def test_valid_explicit_consent_is_allowed(tmp_path):
    decision = _policy(tmp_path).authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-1",
        approval_id="approval-1",
        now=NOW,
    )
    assert decision.allowed is True
    assert decision.reason == "consent_current"
    assert "exampleuser" not in decision.audit_summary()


def test_suppression_overrides_valid_consent(tmp_path):
    decision = _policy(tmp_path, suppressed=True).authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-1",
        approval_id="approval-1",
        now=NOW,
    )
    assert decision.allowed is False
    assert decision.reason == "suppressed"


def test_missing_policy_and_missing_approval_fail_closed(tmp_path):
    unavailable = OutreachPolicy(str(tmp_path / "missing.json")).authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-1",
        approval_id="approval-1",
        now=NOW,
    )
    missing_approval = _policy(tmp_path).authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-1",
        approval_id=None,
        now=NOW,
    )
    assert unavailable.reason == "policy_unavailable"
    assert missing_approval.reason == "approval_missing"


def test_mismatched_action_and_account_are_denied(tmp_path):
    wrong_action = _policy(tmp_path, approval_action="member_add").authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-1",
        approval_id="approval-1",
        now=NOW,
    )
    wrong_account = _policy(tmp_path).authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-2",
        approval_id="approval-1",
        now=NOW,
    )
    assert wrong_action.reason == "approval_action_mismatch"
    assert wrong_account.reason == "approval_account_mismatch"


def test_scraped_subject_is_never_authorized(tmp_path):
    decision = _policy(tmp_path).authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-1",
        approval_id="approval-1",
        source="scraped",
        now=NOW,
    )
    assert decision.reason == "scraped_source_forbidden"


def test_expired_consent_is_denied(tmp_path):
    policy = _policy(
        tmp_path,
        approval_expires="2026-09-21T02:00:00Z",
        consent_expires="2026-09-19T02:00:00Z",
    )
    decision = policy.authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-1",
        approval_id="approval-1",
        now=datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc),
    )
    assert decision.allowed is False
    assert decision.reason == "consent_missing_or_invalid"


def test_malformed_policy_is_denied(tmp_path):
    path = tmp_path / "malformed.json"
    path.write_text("{not-json", encoding="utf-8")
    decision = OutreachPolicy(str(path)).authorize(
        action="personal_send",
        subject="exampleuser",
        account_id="account-1",
        approval_id="approval-1",
        now=NOW,
    )
    assert decision.allowed is False
    assert decision.reason == "policy_unavailable"
