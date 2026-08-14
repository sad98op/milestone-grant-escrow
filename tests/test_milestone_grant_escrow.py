"""
Tests for MilestoneGrantEscrow using genlayer-test's Direct Mode.

Direct Mode runs the contract's Python in-memory (no Docker/Studio needed)
and lets us deterministically control gl.get_webpage / gl.exec_prompt via
mock_web / mock_llm cheatcodes, plus manipulate the sender via prank/sender.

Run with:  pytest tests/ -v
"""

import json
import pytest

CONTRACT_PATH = "contracts/milestone_grant_escrow.py"

RUBRIC_1 = "Deliver a public GitHub repo containing a working CLI tool named 'gltool' with a README documenting install steps."
RUBRIC_2 = "Publish a signed audit report PDF covering the smart contract, with no unresolved critical findings."


def _approve_json(reason="Evidence clearly satisfies the rubric."):
    return json.dumps({"approved": True, "rationale": reason})


def _reject_json(reason="Evidence does not satisfy the rubric."):
    return json.dumps({"approved": False, "rationale": reason})


def _deploy(direct_deploy, grantee, descriptions=None, amounts=None, max_disputes=2):
    descriptions = descriptions or [RUBRIC_1, RUBRIC_2]
    amounts = amounts or [1000, 2000]
    return direct_deploy(
        CONTRACT_PATH,
        grantee,
        descriptions,
        amounts,
        max_disputes,
    )


# ----------------------------------------------------------------------
# Deployment validation
# ----------------------------------------------------------------------

def test_deploy_happy_path(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice)
    assert contract.milestone_count() == 2
    m0 = contract.get_milestone(0)
    assert m0["status"] == "pending"
    assert m0["amount"] == "1000"
    assert m0["description"] == RUBRIC_1


def test_deploy_rejects_mismatched_lengths(direct_vm, direct_deploy, direct_alice):
    with direct_vm.expect_revert():
        _deploy(direct_deploy, direct_alice, descriptions=[RUBRIC_1], amounts=[1000, 2000])


def test_deploy_rejects_empty_milestones(direct_vm, direct_deploy, direct_alice):
    with direct_vm.expect_revert():
        _deploy(direct_deploy, direct_alice, descriptions=[], amounts=[])


def test_deploy_rejects_non_positive_amount(direct_vm, direct_deploy, direct_alice):
    with direct_vm.expect_revert():
        _deploy(direct_deploy, direct_alice, descriptions=[RUBRIC_1], amounts=[0])


def test_deploy_rejects_blank_rubric(direct_vm, direct_deploy, direct_alice):
    with direct_vm.expect_revert():
        _deploy(direct_deploy, direct_alice, descriptions=["   "], amounts=[1000])


# ----------------------------------------------------------------------
# Funding
# ----------------------------------------------------------------------

def test_fund_increases_balance(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice)
    contract.fund(value=3000)
    assert contract.total_escrowed() == 3000


# ----------------------------------------------------------------------
# Submission access control + state machine
# ----------------------------------------------------------------------

def test_only_grantee_can_submit(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy, direct_alice)
    with direct_vm.prank(direct_bob):  # bob is not the grantee
        with direct_vm.expect_revert():
            contract.submit_milestone(0, "https://github.com/example/gltool")


def test_submit_moves_to_submitted(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice)
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")
    m0 = contract.get_milestone(0)
    assert m0["status"] == "submitted"
    assert m0["submission_url"] == "https://github.com/example/gltool"


def test_cannot_submit_twice_while_pending_review(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice)
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")
        with direct_vm.expect_revert():
            contract.submit_milestone(0, "https://github.com/example/gltool-v2")


def test_cannot_review_before_submission(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice)
    with direct_vm.expect_revert():
        contract.review_milestone(0)


# ----------------------------------------------------------------------
# Consensus review: approval path
# ----------------------------------------------------------------------

def test_review_approves_and_unlocks_release(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy, direct_alice)
    contract.fund(value=5000)

    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")

    direct_vm.mock_web(r"github\.com/example/gltool", {
        "status": 200,
        "body": "gltool CLI. Install: pip install gltool. README present.",
    })
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _approve_json())

    contract.review_milestone(0)
    m0 = contract.get_milestone(0)
    assert m0["status"] == "approved"
    assert m0["review_count"] == 1
    assert "rationale" in m0 and len(m0["rationale"]) > 0

    # Either funder or grantee may trigger the payout once approved.
    with direct_vm.prank(direct_bob):  # bob is neither funder nor grantee
        with direct_vm.expect_revert():
            contract.release_milestone(0)

    with direct_vm.prank(direct_alice):
        contract.release_milestone(0)

    assert contract.get_milestone(0)["status"] == "released"
    assert contract.total_escrowed() == 5000 - 1000


def test_cannot_release_twice(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice)
    contract.fund(value=5000)
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")

    direct_vm.mock_web(r"github\.com/example/gltool", {"status": 200, "body": "ok"})
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _approve_json())
    contract.review_milestone(0)
    contract.release_milestone(0)

    with direct_vm.expect_revert():
        contract.release_milestone(0)


def test_cannot_release_underfunded_escrow(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice)
    # No fund() call -- balance is 0, milestone needs 1000.
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")

    direct_vm.mock_web(r"github\.com/example/gltool", {"status": 200, "body": "ok"})
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _approve_json())
    contract.review_milestone(0)

    with direct_vm.expect_revert():
        contract.release_milestone(0)


# ----------------------------------------------------------------------
# Consensus review: rejection + resubmission path
# ----------------------------------------------------------------------

def test_review_rejects_and_allows_resubmission(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice, max_disputes=3)
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/empty-repo")

    direct_vm.mock_web(r"empty-repo", {"status": 200, "body": ""})
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _reject_json("Repo is empty, no CLI tool present."))
    contract.review_milestone(0)

    m0 = contract.get_milestone(0)
    assert m0["status"] == "rejected"
    assert m0["review_count"] == 1

    # Grantee can resubmit after a rejection.
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool-fixed")
    assert contract.get_milestone(0)["status"] == "submitted"


# ----------------------------------------------------------------------
# Escalation to disputed + 2-of-2 mutual resolution
# ----------------------------------------------------------------------

def test_repeated_rejection_escalates_to_disputed(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice, max_disputes=2)

    direct_vm.mock_web(r"gltool", {"status": 200, "body": ""})
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _reject_json())

    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")
    contract.review_milestone(0)
    assert contract.get_milestone(0)["status"] == "rejected"

    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")
    contract.review_milestone(0)
    assert contract.get_milestone(0)["status"] == "disputed"


def test_mutual_resolve_dispute_requires_agreement(direct_vm, direct_deploy, direct_alice, direct_bob):
    # direct_bob acts as funder (deployer), direct_alice is grantee.
    contract = _deploy(direct_deploy, direct_alice, max_disputes=1)
    contract.fund(value=5000)

    direct_vm.mock_web(r"gltool", {"status": 200, "body": ""})
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _reject_json())
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")
    contract.review_milestone(0)
    assert contract.get_milestone(0)["status"] == "disputed"

    # Only funder or grantee may vote.
    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert():
            contract.mutual_resolve_dispute(0, True)

    funder = direct_vm.sender  # deploy default sender == funder
    with direct_vm.prank(funder):
        contract.mutual_resolve_dispute(0, True)
    # Still disputed: only one side has voted.
    assert contract.get_milestone(0)["status"] == "disputed"

    with direct_vm.prank(direct_alice):
        contract.mutual_resolve_dispute(0, True)
    # Both sides agreed to approve -> resolved.
    assert contract.get_milestone(0)["status"] == "approved"

    with direct_vm.prank(direct_alice):
        contract.release_milestone(0)
    assert contract.get_milestone(0)["status"] == "released"


def test_mutual_resolve_dispute_disagreement_stays_disputed(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice, max_disputes=1)
    direct_vm.mock_web(r"gltool", {"status": 200, "body": ""})
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _reject_json())
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")
    contract.review_milestone(0)

    funder = direct_vm.sender
    with direct_vm.prank(funder):
        contract.mutual_resolve_dispute(0, True)  # funder says approve
    with direct_vm.prank(direct_alice):
        contract.mutual_resolve_dispute(0, False)  # grantee says reject

    assert contract.get_milestone(0)["status"] == "disputed"


# ----------------------------------------------------------------------
# Funder surplus withdrawal
# ----------------------------------------------------------------------

def test_withdraw_surplus_respects_committed_funds(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice, amounts=[1000, 2000])
    contract.fund(value=4000)  # 1000 surplus over the 3000 committed to milestones

    with direct_vm.expect_revert():
        contract.withdraw_surplus(2000)  # more than the 1000 uncommitted

    contract.withdraw_surplus(1000)
    assert contract.total_escrowed() == 3000


def test_only_funder_can_withdraw_surplus(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy, direct_alice)
    contract.fund(value=5000)
    with direct_vm.prank(direct_alice):
        with direct_vm.expect_revert():
            contract.withdraw_surplus(100)


def test_withdraw_surplus_blocked_while_rejected(direct_vm, direct_deploy, direct_alice):
    """Funds for a rejected milestone must stay reserved (it can still be
    resubmitted and later become payable)."""
    contract = _deploy(direct_deploy, direct_alice, amounts=[1000, 2000])
    contract.fund(value=4000)  # 1000 surplus over the 3000 committed

    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/empty")
    direct_vm.mock_web(r"empty", {"status": 200, "body": ""})
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _reject_json())
    contract.review_milestone(0)
    assert contract.get_milestone(0)["status"] == "rejected"

    # Only the true surplus (1000) is withdrawable; the rejected milestone
    # amount remains reserved.
    with direct_vm.expect_revert():
        contract.withdraw_surplus(2000)
    contract.withdraw_surplus(1000)
    assert contract.total_escrowed() == 3000


def test_withdraw_surplus_blocked_while_disputed(direct_vm, direct_deploy, direct_alice):
    """Funds for a disputed milestone must stay reserved (it can still be
    mutually resolved to approved and become payable)."""
    contract = _deploy(direct_deploy, direct_alice, amounts=[1000, 2000], max_disputes=1)
    contract.fund(value=4000)

    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/empty")
    direct_vm.mock_web(r"empty", {"status": 200, "body": ""})
    direct_vm.mock_llm(r"ACCEPTANCE RUBRIC", _reject_json())
    contract.review_milestone(0)
    assert contract.get_milestone(0)["status"] == "disputed"

    with direct_vm.expect_revert():
        contract.withdraw_surplus(2000)
    contract.withdraw_surplus(1000)
    assert contract.total_escrowed() == 3000


def test_review_rejects_non_boolean_approval(direct_vm, direct_deploy, direct_alice):
    """Model approval value must be an actual boolean before payout
    eligibility is changed."""
    contract = _deploy(direct_deploy, direct_alice)
    with direct_vm.prank(direct_alice):
        contract.submit_milestone(0, "https://github.com/example/gltool")

    direct_vm.mock_web(r"gltool", {"status": 200, "body": "ok"})
    # Non-boolean approval must cause the review to revert.
    direct_vm.mock_llm(
        r"ACCEPTANCE RUBRIC",
        json.dumps({"approved": "yes", "rationale": "looks fine"}),
    )

    with direct_vm.expect_revert():
        contract.review_milestone(0)
