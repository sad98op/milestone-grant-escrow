# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
MilestoneGrantEscrow
=====================

A reusable Intelligent Contract primitive for milestone-gated grant funding.

Problem it solves
------------------
Grant programs (DAO treasuries, retroactive funding rounds, bounty programs)
want to release funds only when a deliverable actually satisfies an
acceptance rubric -- not on a fixed timer, and not on a single trusted
admin's say-so. Traditional escrow either requires a trusted arbiter or
falls back to purely deterministic checks (e.g. "did a specific tx happen"),
which can't judge whether a PR, report, or deployed artifact actually meets
a written spec.

How GenLayer consensus is used
-------------------------------
- The funder writes the acceptance rubric in plain language *at grant
  creation time*, before any deliverable exists. This is the key anti-abuse
  property: the rubric can't be rewritten after the fact to justify a
  decision either party wants.
- On `review_milestone`, every validator independently fetches the
  grantee-submitted URL (`gl.get_webpage`) and asks an LLM whether the
  fetched evidence satisfies that rubric, using
  `gl.eq_principle_prompt_non_comparative`. This is the correct principle
  here (not `strict_eq`) because "does this deliverable satisfy this
  rubric" is a subjective judgment call, and not `prompt_comparative`
  because we don't need the leader and validators to produce byte-identical
  free text -- we need them to agree on a bounded decision (approve/reject)
  plus a short rationale.
- The nondet function returns canonical JSON (`{"approved": bool,
  "rationale": str}`) rather than free text, which keeps the equivalence
  check meaningful: validators are agreeing on a structured decision, not
  merely "similar-sounding" prose.
- Funds only move via `emit_transfer`, and only after a milestone has
  reached the `approved` state through consensus review -- never on a
  single validator's or single party's say-so.

State machine (per milestone)
------------------------------
pending -> submitted -> [consensus review] -> approved -> released
                              |
                              v
                          rejected -> (grantee may resubmit) -> submitted
                              |
                    (after max_disputes_per_milestone rejections)
                              v
                          disputed -> (mutual_resolve_dispute, 2-of-2) -> approved | rejected

Anti-abuse properties
----------------------
- Rubric is immutable after deployment (no `set_description`).
- Only the grantee can submit evidence; only funder or grantee can trigger
  a release (and only after consensus-approval already happened).
- A milestone can't flip-flop forever: after `max_disputes_per_milestone`
  automated rejections it moves to a terminal `disputed` state that
  requires *both* parties to explicitly agree (2-of-2 vote) on an outcome,
  preventing either side from unilaterally stalling or rug-pulling funds.
- Amounts are fixed at deployment per milestone, so a compromised or
  malicious leader validator cannot redirect an arbitrary amount --
  the JSON schema constrains the leader to a boolean decision, and the
  payout amount is read from immutable contract state, not from the LLM
  output.
"""

from genlayer import *
from dataclasses import dataclass
import json
import typing


class MilestoneStatus:
    PENDING = "pending"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISPUTED = "disputed"
    RELEASED = "released"


@allow_storage
@dataclass
class Milestone:
    description: str  # funder-authored acceptance rubric, fixed at creation
    amount: u256  # payout amount in wei, fixed at creation
    status: str
    submission_url: str  # grantee's evidence link, set on submission
    rationale: str  # validator-produced rationale from the last review
    review_count: u32  # number of consensus reviews this milestone has had


class MilestoneGrantEscrow(gl.Contract):
    funder: Address
    grantee: Address
    milestones: DynArray[Milestone]
    max_disputes_per_milestone: u8
    # 2-of-2 dispute resolution votes, keyed by milestone index.
    # Empty string = no vote cast yet.
    funder_dispute_vote: TreeMap[u32, str]
    grantee_dispute_vote: TreeMap[u32, str]

    def __init__(
        self,
        grantee: str,
        milestone_descriptions: list[str],
        milestone_amounts: list[int],
        max_disputes_per_milestone: int = 2,
    ):
        if len(milestone_descriptions) != len(milestone_amounts):
            raise gl.vm.UserError(
                "milestone_descriptions and milestone_amounts must be the same length"
            )
        if len(milestone_descriptions) == 0:
            raise gl.vm.UserError("a grant must have at least one milestone")
        if max_disputes_per_milestone < 1:
            raise gl.vm.UserError("max_disputes_per_milestone must be >= 1")

        self.funder = gl.message.sender_address
        self.grantee = Address(grantee)
        self.max_disputes_per_milestone = u8(max_disputes_per_milestone)
        self.funder_dispute_vote = TreeMap()
        self.grantee_dispute_vote = TreeMap()

        self.milestones = DynArray[Milestone]()
        for description, amount in zip(milestone_descriptions, milestone_amounts):
            if amount <= 0:
                raise gl.vm.UserError("every milestone amount must be positive")
            if not description or not description.strip():
                raise gl.vm.UserError("every milestone needs a non-empty rubric")
            milestone = gl.storage.inmem_allocate(
                Milestone,
                description,
                u256(amount),
                MilestoneStatus.PENDING,
                "",
                "",
                u32(0),
            )
            self.milestones.append(milestone)

    # ------------------------------------------------------------------
    # Funding
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def fund(self) -> None:
        """Anyone can top up the escrow. Native value is auto-credited to
        the contract's ghost-contract balance via gl.message.value."""
        pass

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @gl.public.view
    def milestone_count(self) -> int:
        return len(self.milestones)

    @gl.public.view
    def get_milestone(self, index: int) -> dict:
        m = self._milestone_at(index)
        return {
            "description": m.description,
            "amount": str(m.amount),
            "status": m.status,
            "submission_url": m.submission_url,
            "rationale": m.rationale,
            "review_count": int(m.review_count),
        }

    @gl.public.view
    def total_escrowed(self) -> int:
        return self.balance

    @gl.public.view
    def dispute_votes(self, index: int) -> dict:
        idx = u32(index)
        return {
            "funder_vote": self.funder_dispute_vote.get(idx, ""),
            "grantee_vote": self.grantee_dispute_vote.get(idx, ""),
        }

    # ------------------------------------------------------------------
    # Grantee actions
    # ------------------------------------------------------------------

    @gl.public.write
    def submit_milestone(self, index: int, submission_url: str) -> None:
        if gl.message.sender_address != self.grantee:
            raise gl.vm.UserError("only the grantee can submit a deliverable")
        if not submission_url or not submission_url.strip():
            raise gl.vm.UserError("submission_url must not be empty")

        m = self._milestone_at(index)
        if m.status not in (MilestoneStatus.PENDING, MilestoneStatus.REJECTED):
            raise gl.vm.UserError(
                f"milestone {index} cannot be submitted from status '{m.status}'"
            )
        m.submission_url = submission_url
        m.status = MilestoneStatus.SUBMITTED

    # ------------------------------------------------------------------
    # Consensus review -- the core Intelligent Contract logic
    # ------------------------------------------------------------------

    @gl.public.write
    def review_milestone(self, index: int) -> None:
        """Anyone (funder, grantee, or a neutral keeper) can trigger review
        once a milestone has been submitted. Validators independently fetch
        the submission URL and judge it against the immutable rubric using
        the non-comparative equivalence principle."""
        m = self._milestone_at(index)
        if m.status != MilestoneStatus.SUBMITTED:
            raise gl.vm.UserError(
                f"milestone {index} is not awaiting review (status='{m.status}')"
            )

        description = m.description
        submission_url = m.submission_url

        def nondet_review() -> str:
            evidence = gl.get_webpage(submission_url, mode="text")
            prompt = f"""You are reviewing a grant milestone deliverable for a
decentralized funding program. Decide whether the submitted evidence
satisfies the funder's acceptance rubric below. Be strict: only approve if
the evidence concretely and verifiably demonstrates the rubric is met.

ACCEPTANCE RUBRIC (authored by the funder before the deliverable existed):
{description}

SUBMITTED EVIDENCE (fetched live from {submission_url}):
{evidence[:6000]}

Respond with strict JSON only, no markdown fences, matching exactly this
schema:
{{"approved": true or false, "rationale": "2-3 sentences citing specific
evidence from the fetched content, or specific missing requirements"}}

If the page is empty, unreachable, or unrelated to the rubric, approved
must be false."""
            raw = gl.exec_prompt(prompt)
            cleaned = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(cleaned)
            normalized = {
                "approved": bool(parsed["approved"]),
                "rationale": str(parsed["rationale"])[:500],
            }
            return json.dumps(normalized, sort_keys=True)

        result = gl.eq_principle_prompt_non_comparative(
            nondet_review,
            task="Determine whether the milestone deliverable satisfies the funder's rubric.",
            criteria=(
                "Validators must independently fetch the same submission_url and "
                "judge strictly against the funder-authored rubric text included "
                "in the prompt. Approve only if the fetched evidence concretely "
                "and verifiably satisfies every requirement in the rubric. If the "
                "page is unreachable, empty, or unrelated, reject. Validators "
                "agree on the structured {approved, rationale} decision, not on "
                "identical prose."
            ),
        )

        parsed = json.loads(result)
        m.review_count = u32(int(m.review_count) + 1)
        m.rationale = parsed["rationale"]

        if parsed["approved"]:
            m.status = MilestoneStatus.APPROVED
        elif int(m.review_count) >= int(self.max_disputes_per_milestone):
            m.status = MilestoneStatus.DISPUTED
        else:
            m.status = MilestoneStatus.REJECTED

    # ------------------------------------------------------------------
    # Payout
    # ------------------------------------------------------------------

    @gl.public.write
    def release_milestone(self, index: int) -> None:
        sender = gl.message.sender_address
        if sender != self.funder and sender != self.grantee:
            raise gl.vm.UserError("only funder or grantee can trigger release")

        m = self._milestone_at(index)
        if m.status != MilestoneStatus.APPROVED:
            raise gl.vm.UserError(
                f"milestone {index} is not approved (status='{m.status}')"
            )
        if self.balance < m.amount:
            raise gl.vm.UserError("escrow balance is insufficient for this payout")

        m.status = MilestoneStatus.RELEASED
        recipient = gl.ContractAt(self.grantee)
        recipient.emit_transfer(value=int(m.amount))

    # ------------------------------------------------------------------
    # Deadlock-breaking: 2-of-2 dispute resolution
    # ------------------------------------------------------------------

    @gl.public.write
    def mutual_resolve_dispute(self, index: int, approve: bool) -> None:
        """After a milestone has been automatically rejected
        max_disputes_per_milestone times, consensus review stops and the
        milestone becomes 'disputed'. Escaping that state requires both
        the funder and the grantee to independently agree on the same
        outcome -- this prevents a single party from stalling funds
        forever or forcing an outcome the other party never consented to."""
        sender = gl.message.sender_address
        if sender not in (self.funder, self.grantee):
            raise gl.vm.UserError("only funder or grantee can vote on a dispute")

        m = self._milestone_at(index)
        if m.status != MilestoneStatus.DISPUTED:
            raise gl.vm.UserError(f"milestone {index} is not in disputed status")

        vote = "approve" if approve else "reject"
        idx = u32(index)
        if sender == self.funder:
            self.funder_dispute_vote[idx] = vote
        else:
            self.grantee_dispute_vote[idx] = vote

        funder_vote = self.funder_dispute_vote.get(idx, "")
        grantee_vote = self.grantee_dispute_vote.get(idx, "")

        if funder_vote and funder_vote == grantee_vote:
            m.status = (
                MilestoneStatus.APPROVED
                if funder_vote == "approve"
                else MilestoneStatus.REJECTED
            )
            m.rationale = "Resolved by mutual 2-of-2 funder/grantee agreement."
            # Clear votes so a resubmitted milestone starts with a clean slate.
            del self.funder_dispute_vote[idx]
            del self.grantee_dispute_vote[idx]

    # ------------------------------------------------------------------
    # Funder cleanup
    # ------------------------------------------------------------------

    @gl.public.write
    def withdraw_surplus(self, amount: int) -> None:
        """Lets the funder reclaim balance that isn't earmarked for any
        pending/submitted/approved milestone (e.g. leftover after a
        milestone is permanently rejected, or funds sent in excess)."""
        if gl.message.sender_address != self.funder:
            raise gl.vm.UserError("only the funder can withdraw surplus")
        if amount <= 0:
            raise gl.vm.UserError("amount must be positive")

        committed = u256(0)
        for m in self.milestones:
            if m.status in (
                MilestoneStatus.PENDING,
                MilestoneStatus.SUBMITTED,
                MilestoneStatus.APPROVED,
            ):
                committed += m.amount

        available = int(self.balance) - int(committed)
        if amount > available:
            raise gl.vm.UserError(
                f"only {available} wei is uncommitted and withdrawable"
            )

        recipient = gl.ContractAt(self.funder)
        recipient.emit_transfer(value=amount)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _milestone_at(self, index: int) -> Milestone:
        if index < 0 or index >= len(self.milestones):
            raise gl.vm.UserError(f"milestone index {index} out of range")
        return self.milestones[index]
