# Method

What the harness assumes about solving hard formalization problems with agents, and where each assumption
lives in the code. The evidence is our campaign on LeanEval v1 ([AUDIT.md](AUDIT.md)); the mechanisms are
described in [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Only the official verification counts

Every acceptance is a receipt of the organizers' own flow on a pristine workspace (toolchain `lake
comparator` in bubblewrap, Lean kernel and NanoDa), recorded where the agent cannot write. The harness
replicates that flow locally twice: inside the controller for every complete round, and in `verify/`
exactly as the organizers' evaluator runs it, with a CI envelope whose wall time predicted the organizers' CI
within 5% on our largest accepted proofs. Model replies, NOTES.md and gate decisions only steer work.

## 2. The agent writes its own state, in a fixed format

Every round reads and updates three tables at the top of `NOTES.md` (`fleet/server/briefs/COMMON.md`):

- **HANDOFF**: next step, active tactic, last failure, what not to retry. A continuation round (or another
  account, or another host) resumes from it.
- **RESIDUALS**: every open statement the holes still depend on, its source and an honest estimate in
  lines. The controller reads it: rows that become `proved` and the summed estimate of the open rows are the
  progress signal.
- **TACTICS**: one row per route idea, with its status. A route review sees the measured outcome of each and
  must switch.

The format is what makes the orchestration possible: the feeder never asks the model whether it is making
progress; it measures the tables and the build records.

## 3. Measure convergence, not effort

The progress gate stops a lineage when its remaining-work estimate stops shrinking without a row proved
(`fleet/docs/FEEDER-POLICY.md`). In our history, lineages that later passed shrank the estimate about ×0.46
per round, open ones about ×0.96; the sorry count did not separate them. A complete round is never a stall.
`leh evolve report` replays this rule on any history at other thresholds and counts how many passing lineages
each threshold would have cut.

Against the agents' pessimism the briefs say plainly that "infeasible" judgements were often wrong (problems
called "far beyond one round" closed one or two rounds later), and the round budget is stated in minutes of
active model time. The gate, not the agent's opinion of its chances, ends a lineage.

## 4. Get out of local minima on purpose

- A stalled lineage gets one **route review** (`roles/ROUTE_REVIEW.md`) seeded with the best finished files
  of the problem: it receives the measured history of the problem's rounds and the tactic library
  (`TACTICS.md`), marks the stuck tactic `failed`, lists at least three alternatives, falsifies the current
  reduction on degenerate cases and switches.
- A **stuck-goal protocol** inside a round: after about 45 minutes on one goal, write it down, derive two
  or three decompositions from the statement alone, test each on a degenerate case and keep one.
- **Hedges must take another route.** Identical duplicate lineages won 1 time in 17; transfers seeded with
  the best files of another lineage won 10 times in 19. The coordinator starts parallel lineages only with a
  declared different tactic.
- Of the problems we solved in the fleet, 17 of 32 passed only after a restart from earlier files.

## 5. Decompose with a contract

A large problem becomes a program (`roles/ARCHITECT.md`). The lead writes, so that the build succeeds:
`Interfaces.lean` (one statement per lane), `Reduction.lean` (the holes from the lane statements, fully
proved), `Sanity.lean` (each lane statement checked on a concrete instance) and, per lane, a contract that
`validate_architecture` enforces:

| Field | What it records |
|---|---|
| `Statement:` | the lane's statement in words, constants explicit |
| `Used by:` | the step of the reduction that consumes it, and why it suffices |
| `Sanity:` | the instance `Sanity.lean` checks |
| `Estimate:` | expected lines and the critical lemma |
| `Source:` | the published proof step it formalizes, or the inductive step of a stated relative induction |
| `Strength:` | why it is strictly weaker than the hole (what it assumes, which case it leaves to another lane) |
| `Reuse:` | earlier formalizations of the step found in the solution bank, lean-pool or Mathlib, or `none` with the searches made |

Cut along a published proof when one exists. The programs that invented their own decomposition issued 17
or more lane statements that were false, vacuous or as strong as the whole problem; each looked true to its
lane and was found only by audits hours later. Splits are allowed only into strictly weaker children; a
split that moves the core is a design problem, not a split.

**Refutations.** A lane that proves an interface statement false, whether its own or another lane's
(`theorem … : ¬ Submission.Lanes.XStatement`), stops at once and is not counted as done. This holds even when
its directory has no `sorry`: a consumer that closes its statement by contradiction with a refuted hypothesis
is vacuous, not proved. When the other lanes' rounds end, the program goes back to the lead with every
refutation in the brief. In one program, two shared tool statements were refuted five times in total, while
two consumers that used the refutations were counted as finished.

**Shared tools first, consumers second.** Three rules govern lanes that share machinery:
- *One statement per tool.* When several lanes need the same machinery (charts, normal forms, a collapse
  lemma), the lead makes it one tool lane, with a statement strong enough for every consumer. Its `Used by:`
  line names each consumer and the form it needs. The lead lists tool lanes first in `Interfaces.lean`, and the
  feeder assigns lanes in that order.
- *Finished lanes are visible.* A new lane round starts from the architecture plus every finished lane, so
  consumers import finished tools instead of re-deriving them. Seeding lanes from the architecture alone once
  left 143 lemma names defined in more than one lane.
- *Interface requests, not private copies.* A consumer that needs a stronger form of another lane's statement
  writes a row in `## INTERFACE REQUESTS` in NOTES.md. It never writes a private strengthened copy. The feeder
  collects the requests and hands them to the next architecture round, so the producer proves the strong form
  once.

**A reduction at every level.** A 43M-line CFSG formalization (2026) first failed because agents *assumed*
prerequisites instead of proving them. On one day a single milestone of 108 closed, and tens of thousands of
lines were deleted. After a replanning that required every decomposition to come with a short Lean proof that
the parts give the whole, all 108 milestones closed in 11 days. The harness applies the same rule to every split
and to decompositions inside a lane: sub-statements and a sorry-free reduction come before any proof. A
statement taken as a hypothesis must be some lane's statement or an open RESIDUALS row, never a silent
assumption.

**Calibrate estimates by kind.** In one program, lanes that built new geometric infrastructure came out at
3–10× their estimates. Lanes that reused proved libraries came out at 0.1–0.6×. The contract therefore carries
`Kind: new` or `Kind: reuse`, and `leh evolve report` measures the ratio from the logs.

## 6. Reuse before you prove

Adapted, licensed public formalizations were our fastest accepted solutions, and the slowest programs were
the ones that rebuilt an existing library. The **solution bank** (`leh bank`) puts licensed formalizations at
exact commits into the workbench, where `./lean search` finds them next to Mathlib and lean-pool; the
architect must say what it found (`Reuse:`). Only Apache-2.0 and MIT sources are added by default; adapted
code keeps its licence header and is recorded under `## REUSE` in NOTES.md; the package's PROVENANCE
lists the files with third-party licence notices and carries that section. Reuse is credited, never hidden: our audit counts which accepted problems rest on adapted public
proofs.

## 7. Budget for the evaluation from the first build

The organizers' CI has fixed bounds (one 6-hour job on 16 CPUs and 32 GB at the time of writing). The
feeder estimates CI time from the measured module seconds of every whole build and holds a package that does
not fit; a CI_FIT round then makes the same statement cheaper to check. `verify/` preflights disk and memory
maps, which otherwise fail only after hours. Plan the archive size (29.5 MB) before vendoring anything.

## 8. The experiment manager closes the loop

Above the deterministic orchestration sits one level that is not automated: the experiment manager
([EXPERIMENT_MANAGER.md](EXPERIMENT_MANAGER.md)). It chooses which problems deserve the fleet, reads the
convergence of every program, audits lane statements when a program stops converging (an outside audit of
one non-converging program turned five days of a quantitative route into a nine-hour qualitative one),
runs `leh evolve` and approves or rejects its proposals. Web and literature recon belong here too: agents in
rounds have the network, but no search tool of their own beyond `./lean search`; the manager brings sources
into the bank or a per-problem brief.

## 9. Measure what is left in Lean, not in the notes

The RESIDUALS tables are the agents' own accounting, so they can drift: rows get renamed, re-split or
re-estimated. `leh map build` builds the merged tree of all lanes against the workbench and walks the declaration
graph in Lean. It reports:
- which declarations carry a `sorry` of their own and are reached by the final theorem ("what's left");
- which code nothing reaches (dead code);
- which lemma names are defined in several lanes (re-derived work);
- which refutations (`¬ …Statement`) the final proof relies on (vacuous steps).

The same idea gave the CFSG project its "plan vs Lean" view, taken from the full-proof CI report instead of from
the agents' reports. Run the map every few hours on a program and compare it with the RESIDUALS estimates. A lane
whose estimate falls while its own-sorry count does not is renaming rows, not converging.

## 10. Evolve on the logs, with a person in the loop

Every round leaves features (what it was asked, how it ended, what its workspace holds). `leh evolve`
extracts them from the hosts without moving logs or proofs, aggregates them, replays policies and proposes
changes ([EVOLVE.md](EVOLVE.md)). In our campaign the biggest single gains came from this loop (warm builds
cut a cold first build of about 34 minutes; the estimate-based gate; route reviews with measured history;
lane contracts). Nothing it proposes deploys itself.

## Related approaches

We kept to methods we could check against our own logs. Three lines of work shaped how we organized them:

- **Optimizing an agent system in stages** (as in [MASS](https://arxiv.org/abs/2502.02533): first the prompts of the individual blocks, then the
  topology): our evolve loop changes role briefs first and the orchestration (lanes, hedges, gates) second,
  and measures each change separately.
- **Information-directed choices** (in the spirit of [information-directed sampling](https://arxiv.org/abs/1403.5556)): when a route stalls,
  prefer the step that tells you the most for its cost. Falsifying a lane statement on a degenerate case is
  cheap and decisive; a hedge on the same route tells you nothing new.
- **Adaptive evolutionary search** (the AlphaEvolve / [AdaEvolve](https://arxiv.org/abs/2602.20133) family): lineages are a population; the
  best finished files seed the next attempts, and budget follows measured convergence rather than a fixed
  schedule.
