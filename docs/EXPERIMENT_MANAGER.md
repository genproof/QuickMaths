# Experiment manager

The brief for the level above the orchestration: a person, or an agent session (we ran Claude Code on the
operator machine) that a person supervises. It decides what the fleet works on, watches whether the work
converges, closes the evolve loop and publishes. It never proves; proving runs in the fleet's rounds.

## Standing rules

- **Credentials.** Never read, print, copy or move token files, CLI credential files or the CLI's
  configuration directories, on any host. Accounts are secret references; `fleet.py deploy` checks that the
  files exist and never reads them. A `SECURITY-EVENT.json` or a `halted_credential_access` runner means the
  account must be rotated before that runner restarts.
- **Shared hosts.** Never kill by name (`pkill`, `killall`) on a host other people use; stop only processes
  and units you started, and stop rounds with the controller's STOP files.
- **Acceptance.** Only an official receipt counts. A round's claim, a NOTES.md table or a green build is not
  a solved problem.
- **Repositories.** Never force-push. Commit only scrubbed content (`leh scrub --staged`). Submissions to the
  benchmark are a person's decision.
- **Other teams.** Never use another submission's code. Public metadata (titles, module counts in public CI
  logs) may inform priorities; licensed public formalizations may be adapted with attribution (the bank).

## Every tick (we used 30 minutes)

1. `leh fleet status`: runners, claims, programs, alerts. Read the alerts first.
2. For each running program, the lane table (`leh op HOST lanes.py PROBLEM`) and the RESIDUALS of the lead:
   is the summed estimate falling? A lane whose estimate grew twice is a design problem, not a slow lane.
3. Quota: accounts in a usage-limit pause, hand-offs waiting, runners idle with quota.
4. Packages ready in an outbox: verify on the current benchmark (`leh verify … --ci-envelope`), then publish.
5. A short written status: what passed, what is stuck and why, what you changed.

## When a program stops converging

Do not add lanes. Stop and audit:

1. List every open lane statement. For each: is it true (try degenerate cases, look for a counterexample),
   is it strictly weaker than the hole, does its sanity theorem show its hypotheses can hold together?
2. Compare the route with the published proof. An invented decomposition without a stated relative
   induction is the usual cause; so is an explicit quantitative budget where the source argues qualitatively.
3. If the route is wrong, re-architect (`leh op HOST rearchitect.py …`) with the audit as the reason, or
   start a hedge on a genuinely different route (`hedge_lane.py`), never a duplicate.
4. Search the bank and public licensed formalizations for the stuck step before re-proving it; add what you
   find to the bank.

Keep the agents' estimates honest but do not let them stop on calendar pessimism ("two more weeks"): the
gate measures movement, and the briefs say that "infeasible" judgements were often wrong. If a lane
declares the task infeasible without a refutation, ask for the smallest next statement instead.

## Weekly (or after every few solved problems)

1. `leh evolve extract` on every host, `leh evolve report` on all of them ([EVOLVE.md](EVOLVE.md)).
2. Read the gate replay and the proposals; decide each one in writing.
3. Apply accepted changes one at a time, deploy, and compare the next report.

## Choosing problems

- Prefer problems where a published proof gives a decomposition, and where the pinned libraries or the
  bank already hold the hard ingredients.
- Check the benchmark's issue tracker before starting a campaign: a statement can be known false.
- Estimate the evaluation cost early (closure size, module seconds) against the organizers' CI bounds.
