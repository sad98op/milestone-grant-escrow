# MilestoneGrantEscrow

A reusable GenLayer Intelligent Contract primitive for **milestone-gated grant
funding**, where fund release is decided by validator consensus judging real
deliverables against a funder-authored rubric — not by a trusted admin, and
not by a fixed timer.

## Why this is a primitive, not a demo

Most "AI decides X" contracts hardcode a single prompt for a single
transaction and stop there. This contract is meant to be **deployed
repeatedly by different grant programs** with their own grantee, milestones,
amounts, and rubrics, and it encodes a full escrow lifecycle:

```
pending → submitted → [consensus review] → approved → released
                            │
                            ▼
                        rejected → (resubmit) → submitted
                            │
              (after max_disputes_per_milestone rejections)
                            ▼
                        disputed → (2-of-2 mutual vote) → approved | rejected
```

## How GenLayer consensus is used (and why this equivalence principle)

- **Rubric fixed before evidence exists.** The funder writes the acceptance
  criteria for every milestone at deployment time, before any deliverable
  is produced. This is the load-bearing anti-abuse property: nobody can
  retroactively rewrite the bar a deliverable is judged against.
- **`gl.eq_principle_prompt_non_comparative`, not `strict_eq` or
  `prompt_comparative`.** Whether a deliverable satisfies a rubric is an
  inherently subjective judgment call — exactly the case this principle
  exists for. `strict_eq` would require byte-identical output (impossible
  for LLM judgment calls); `prompt_comparative` would ask validators to
  reproduce the leader's free text and compare it, which is unnecessary
  and more expensive when what we actually need validators to agree on is
  a bounded decision.
- **Structured, bounded leader output.** The nondet function returns
  canonical JSON — `{"approved": bool, "rationale": str}` — rather than
  free text. This keeps what validators are actually reaching consensus on
  narrow and auditable: a boolean decision plus a short justification, not
  "does this paragraph sound similar to that paragraph."
- **Payout amount is never LLM-controlled.** The LLM only ever returns
  `approved: true/false`. The amount transferred always comes from
  immutable contract state set at deployment, so a compromised leader
  can't redirect funds by hallucinating a number.
- **Deadlock breaker that doesn't re-invoke the LLM.** If a milestone is
  rejected `max_disputes_per_milestone` times, review stops and the
  milestone becomes `disputed`. Escaping that state requires a 2-of-2 vote
  from the funder and grantee — deliberately *not* another AI call, since
  repeatedly re-asking the same question after both automated rounds
  failed doesn't add information, and a human/human checkpoint is the
  right circuit-breaker for genuinely contested cases.

## State design

| Field | Type | Purpose |
|---|---|---|
| `funder`, `grantee` | `Address` | Fixed parties |
| `milestones` | `DynArray[Milestone]` | Each milestone: `description` (rubric), `amount`, `status`, `submission_url`, `rationale`, `review_count` |
| `max_disputes_per_milestone` | `u8` | Escalation threshold before requiring mutual resolution |
| `funder_dispute_vote`, `grantee_dispute_vote` | `TreeMap[u32, str]` | 2-of-2 votes for disputed milestones, keyed by milestone index |

`Milestone` is a `@allow_storage @dataclass`, so the state is genuinely
structured (not five parallel maps you have to keep in sync by hand).

## Public interface

- `fund()` *(payable)* — anyone tops up the escrow.
- `submit_milestone(index, submission_url)` — grantee only.
- `review_milestone(index)` — anyone can trigger; runs consensus review.
- `release_milestone(index)` — funder or grantee, only once approved.
- `mutual_resolve_dispute(index, approve)` — funder or grantee, 2-of-2.
- `withdraw_surplus(amount)` — funder only, capped at uncommitted balance.
- Views: `milestone_count()`, `get_milestone(index)`, `total_escrowed()`,
  `dispute_votes(index)`.

## Files

- `contracts/milestone_grant_escrow.py` — the contract.
- `tests/test_milestone_grant_escrow.py` — `genlayer-test` Direct Mode
  suite: deployment validation, access control, the full approve/reject/
  dispute/resolve state machine, double-release and underfunded-release
  guards, and surplus withdrawal accounting. Run with `pytest tests/ -v`.

## Adapting this primitive

To reuse this for a different program: change nothing in the contract.
Deploy with your own `grantee`, `milestone_descriptions` (your rubrics),
`milestone_amounts`, and optionally `max_disputes_per_milestone`. The
consensus logic, escrow accounting, and dispute path are program-agnostic.

## Known limitations / honest trade-offs

- A single grantee per deployment (one contract per grant, not a registry
  of grants). This keeps the state model simple and auditable; a factory
  contract could wrap this for many grants.
- `review_milestone` can be called by anyone once a milestone is
  `submitted` — this is intentional (permissionless "keeper" pattern, no
  party can stall review by refusing to call it), but means gas/LLM costs
  for a review aren't necessarily paid by funder or grantee.
- The 2-of-2 dispute resolution is a deliberate off-ramp from AI judgment,
  not a fully trustless arbitration mechanism — if funder and grantee
  never agree, funds for that milestone stay locked. This is a known,
  documented trade-off rather than a hidden one.
