# Feeder policy

Basis: an analysis of 51 hours of fleet work (outcome, failure, strategy and audit reports of
2026-10-14; the reports are not part of this repository). Only the feeder (`server/feeder.py`), the
briefs and `fleet.json` change; the runtime and the controllers are untouched. Acceptance still comes
only from the official verifier's receipt (`official_local_pass`), never from a model reply.

## New states

| Where | Value | Meaning |
|---|---|---|
| progress kind | `complete` | the round has no `sorry`, and the last `./lean build Submission` (or a successful full check) passed on exactly these files (`source_files` of the `.feedback` step). This is progress, never a stop |
| progress kind | `split` | a decomposition: more `sorry`, new frontier items, the build passes. Progress, but it does not reset the churn counter |
| progress kind | `residual` | only when a RESIDUALS row that was open before became `proved`, or the summed `est. lines` of the open rows fell. Renaming or re-splitting a row is not progress |
| claim.role | `VERIFY` | 1 round (`verify_policy: always`, `max_rounds 1`, `wall 900 s`, `max_nudges 0`, `verification_seconds` from defaults): the model changes nothing, the controller runs the official verification itself |
| claim.role | `CI_FIT` | after a VERIFY timeout: a brief with the measured times (the slowest modules from `official.log`), 2 rounds, then VERIFY again |
| claim.role | `REPAIR` | after a VERIFY rejection: a brief with the tail of `official.log`, 2 rounds, then VERIFY again |
| claim.role | `RESUME` | the same route continued on another account after a quota pause (brief `roles/RESUME.md`: do not re-decide the route) |
| claim.status | `handoff` | the lineage was stopped for quota; it waits until the stopped round closes, then moves to an idle runner with quota |
| program.status | `stopped` | the program was stopped because the problem passed through another lineage on this host |
| lane claim.status | `refuted` | the lane's files prove an interface statement false (`theorem … : ¬ …Statement`). The lane is stopped and not done, and the program re-architects once the other lanes' rounds end |
| program.refuted | `{statement: {lane, theorem, job}}` | the refutations the next architecture round must resolve. Cleared when an architecture is accepted |
| program.interface_requests | `{lane: [rows]}` | the rows of each lane's `## INTERFACE REQUESTS` table (interface changes a consumer needs from a producer). Passed to the architecture round and cleared when it is accepted |

New claim fields: `verifies` (number of VERIFY rounds for the problem, not counted as a retry),
`verify`, `verify_exhausted`, `handoffs`, `handoff`, `trajectory` (`[round, sorry, estimate, kind]`),
`closeness`, `last_decision`, `decisions`, `stop_kind`.

## Rules

1. **Runtime switches only at a lineage boundary.** `ensure_runtime` does not restart the controller
   while the runner has a pending assignment or a running claim or program role (alert
   `runtime switch deferred to lineage boundary`). An idle runner switches first and then gets work.
   `deploy_runtime.sh` gives a deterministic id `lrh-<hash>` without a date. The config merge on
   deploy is `local/fleet.py merge_host_config` (runtime pins of existing runners are kept).
2. **Verification phase.** When the last finished round is `complete` but unverified
   (`not_verified_snapshot_only`, or a timeout with a budget smaller than the current one), its
   continuation gets STOP and a VERIFY lineage starts instead of ROUTE_REVIEW or a program. A VERIFY
   timeout leads to CI_FIT, a rejection to REPAIR, anything else to another VERIFY; at most
   `max_verifies` (2) per problem; after that `verify_exhausted` and an operator alert (neither retries
   nor a program). A problem in the verification phase never becomes a program.
3. **Verification budget.** `defaults.verification_seconds = 21600` (the evaluation CI gives 6 h on
   4 vCPU / 16 GB). COMMON.md: READY when there is no `sorry`, `./lean build Submission` passes on the
   current files and the holes' axioms are standard; a full `./lean check` is optional; do not
   optimise build time without measurements from the controller.
4. **Quota.** `runner_quota()` reads the current job's `<job>/checkpoint.json`
   (`phase: usage_limit_pause`, the open last pause with `until`, `rate_limit.rateLimitType` /
   `resetsAt` / `unifiedWindows`). An account's block is the maximum `until` over its runners; it is
   kept in `account-blocks.json` until it expires, and lifted earlier when a runner of that account
   starts a new model call. A runner of a blocked account gets no new work. A `seven_day` pause is
   always handed off; a `five_hour` pause only if the reset is more than `quota_handoff_after_s`
   (3600) away and the host has an idle runner with quota. Handoff: `stop_lineage` (the round closes
   in a controlled way, with a snapshot), claim `handoff`; an idle runner with quota is held for that
   lineage while the round closes (up to 1 h), then gets a RESUME from the last really finished round
   (`finished_round_job`; `claim.rounds` may count rounds that never started), or a VERIFY if that
   round is `complete`. Order by closeness (1.0 for complete/VERIFY, else `1/(1+sorry+0.5·frontier)`,
   +0.1 for kind sorry/residual). A handoff is not a retry. A root STOP still excludes a runner.
5. **Stopping stalled lineages** (single lineages only, not program roles; never on `complete`):
   (a) the remaining-work estimate (summed `est. lines` of the open RESIDUALS rows) falls by less than
   25% (`estimate_stall_ratio` 0.75) two rounds in a row with no closed row and no closed `sorry`,
   while the estimate is at least `estimate_stall_min_lines` (1000); (b) without an estimate,
   `churn_rounds` (3) rounds without a new `sorry` minimum (counted from round 1; a decomposition may
   raise `sorry`, only a new minimum resets the counter). Then as for an ordinary stop: one
   ROUTE_REVIEW, and if there already was one the lineage ends (exhausted → retry or program).
6. **Seed of a retry and of a route review**: the best finished round of the problem on this host
   (fewest `sorry` with a passing build, the latest on a tie; program lane rounds excluded), not just
   the last round.
7. **Parallel lineages.** A target in `targets.json` may have `avoid_tactics` (a list) and
   `parallel_of` (a description of the main lineage); the ROUTE_REVIEW and RESUME briefs then get
   "a sibling lineage is already following: …; choose a different tactic". When a problem passes on
   this host, all its other lineages here get STOP and an active program becomes `stopped`.
8. **Host load admission.** `max_active_runners` (per host), `min_mem_available_gb`,
   `max_memory_pressure_avg60` (PSI `full avg60` from `/proc/pressure/memory`): when exceeded, no new
   work is assigned (alert). `status.json → host`: MemAvailable, load average, PSI, active runners.
9. **The RESIDUALS table is parsed leniently:** cells with backticks and `|` inside, `**open**`,
   `proved/replaced`, `10k`, `~1,500`, another column order by header; rows without a status are
   skipped, `(history)/(archive)` sections are ignored. The old regex lost half of the rows.
10. **One program per problem per host** (`program_active`); while a program is active there are no
    retries of that problem. Cross-host deduplication (two programs of one problem on two hosts) is
    P1, the coordinator (`docs/COORDINATOR-POLICY.md`: host inbox, claims v2, cross-host handoff,
    parallel lineages, publisher, rollout order).
11. **status.json → signals** per claim: `tier` from the optional target hints (easy:
    `ref_min_modules` ≤ 45 and `n_solvers` ≥ 4; medium ≤ 250; hard 251–600; extreme > 600 or
    ≤ 1 solver), the sorry/estimate trajectory, closeness, the last decision; input for an adaptive
    policy (P1).
12. **Architecture contract** (`ARCHITECT_CONTRACT`, recorded per architect assignment, so a program keeps
    the contract it started with). 2: every open lane's `Main.lean` carries a CONTRACT block (`Statement:`,
    `Used by:`, `Sanity:`, `Estimate:`), and `Submission/Lanes/Sanity.lean`, imported by `Submission.lean`,
    holds a sorry-free `Submission.Lanes.<Id>.sanity` per open lane. 3: new lanes also carry `Source:` and
    `Strength:`. 4 (current): new lanes also carry `Reuse:` (what `./lean search` found in the solution bank,
    lean-pool or Mathlib, or `none` with the searches made). Lanes kept from an earlier architecture keep
    their contract. A rejected architecture goes back to the architect once with the reason.
13. **Credential tripwire.** A round that ends with stop reason `credential_access` halts its controller
    (`halted_credential_access`); the feeder never restarts that runner and raises an alert. The operator
    rotates the account and restarts the unit by hand (docs/SECURITY.md).
14. **Records for evolve.** Every assignment the feeder writes carries `lineage_role` (FRESH, ROUTE_REVIEW,
    RESUME, VERIFY, CI_FIT, REPAIR, PARALLEL, a program role); the controller ignores it and `leh evolve
    extract` reads it. Packages carry the `## REUSE` section of the final round's NOTES.md in
    `PROVENANCE.json` (`reuse_notes`).

## Config keys (fleet.json → config.json on deploy)

| Key | Default | Effect |
|---|---|---|
| `defaults.verification_seconds` | 21600 | official verification budget |
| `verify_lineages` | true | VERIFY/CI_FIT/REPAIR for complete rounds |
| `verify_backfill` | false | pick up older exhausted claims and running ROUTE_REVIEWs whose last round or source is complete and unverified |
| `verify_round_seconds` | 900 | wall time of a VERIFY round |
| `max_verifies` | 2 | VERIFY rounds per problem |
| `churn_rounds` | 3 | 0 switches the churn rule off |
| `estimate_stall_ratio` | 0.75 | 0 switches the estimate rule off |
| `estimate_stall_min_lines` | 1000 | below this the estimate is not a stall |
| `quota_handoff` | true | handoff on quota |
| `quota_handoff_after_s` | 3600 | threshold for five_hour pauses |
| `adopt_paused_claims` | false | treat `paused` claims (parked by an operator) as handoff candidates |
| `stop_siblings_on_pass` | true | STOP the problem's other lineages on a pass |
| `max_active_runners` | 0 (no limit) | per host; can be set in the host entry of fleet.json |
| `min_mem_available_gb` | 0 (off) | MemAvailable threshold |
| `max_memory_pressure_avg60` | null (off) | PSI full avg60 threshold |
| `leaderboard_model` | null (no filter) | skip problems the public leaderboard credits to this model id |
| `contributors` | [] | tags written into the packages' README and PROVENANCE |

## Deploy and migration

1. On the operator machine: `python3.12 fleet/local/fleet.py deploy HOST --apply` for
   every host. It syncs the feeder files and briefs, merges config.json (`merge_host_config`: runtime
   pins of existing runners are kept) and reinstalls the units without restarting runners. The
   runtime deploy creates `runtimes/lrh-<hash>` with a new id, but running runners keep their pins;
   running controllers are not restarted.
2. The first tick right after the deploy finishes counting the lineages' first rounds, stops the
   continuation of lineages with a complete round (→ VERIFY), stops stalled lineages (churn or
   estimate) and hands off lineages in `seven_day` pauses. Check `status.json` (alerts, signals,
   account_blocks).
3. Verification budget of already queued assignments:
   `python3 -B <own root>/feeder.py --migrate-verification-seconds 21600` (under feeder.lock and
   manifest.lock; changes only unfinished assignments; the current round keeps its value in the
   controller's memory, its continuations get the new one).
4. Optionally: `verify_backfill: true` (pick up old complete claims once), then
   `adopt_paused_claims: true`, and remove the operator's root STOP from runners whose accounts have
   reset (the feeder never removes a root STOP itself).

Check on a host before switching the flags on: the `checkpoint.json` fields (`phase`,
`pauses[-1].until`, `ended_at`, `rate_limit.rateLimitType`) of a job that is really paused; that
`.feedback/step-*.json` has `source_files` on `build Submission` steps; that the history entry of a
verification timeout has `receipt_run_id` and `verification.verification_wall_seconds`.

Tests: `cd fleet && PYTHONPATH=server python3.12 -m unittest discover -s tests -v`.
