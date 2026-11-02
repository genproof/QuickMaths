## Campaign context (fleet runner)

Goal: a complete Lean proof of every official hole of this problem against exactly this pinned
environment (Lean v4.35.0-rc3, the pinned Mathlib and the `lean-pool` package) that passes the
controller's official comparator + Nanoda verification. If the problem section below says that
other systems have already solved it, a complete proof in this environment is known to exist.

Rounds (normally 4 hours) continue automatically while the open frontier moves: a `sorry`
closed, or a residual (below) proved, refuted, or replaced by smaller named statements. A round
that only adds code without moving the frontier is tolerated once; two in a row end the
attempt. If Submission files and NOTES.md already exist when you start, they are our previous
attempt on this problem: build them, read the HANDOFF and RESIDUALS at the top of NOTES.md,
keep the compiled work, and re-decide the route from the RESIDUALS table (do not simply resume
the last step out of inertia).

RESIDUALS table: keep it at the top of NOTES.md, right after the HANDOFF, exactly in this form
(the controller reads it to measure progress: rows that become `proved`, and the summed
`est. lines` of the `open` rows):

    ## RESIDUALS
    | name | status | source | est. lines |
    |---|---|---|---|
    | `Submission.Foo.BarStatement` | open | Paper Thm 3.2 (special case k = 2) | 1500 |

One row per open Prop/lemma the official holes still depend on (a sorry'd declaration or a
hypothesis of a compiled `hole_of_...` reduction). The name cell holds only the name, in
backticks; put the statement in the source column. status is one of open / proved / refuted /
replaced. Every open row needs an honest integer estimate of the lines still to write. Update
the table whenever the frontier changes; renaming or re-splitting a row without closing
anything is not progress.

TACTICS table: right after RESIDUALS, one row per route idea you commit to (the controller
reads it and reports each round's measured outcome to the next route review):

    ## TACTICS
    | tactic | status | target | expected signal | evidence |
    |---|---|---|---|---|
    | special case k = 2 of Thm 3.2 | trying | Submission.Foo.BarStatement | Bar closed this round | - |

status is trying / worked / failed / dropped. Keep exactly one row `trying` for the residual you
work on; mark it `failed` with the evidence (what blocked it) when it stops moving, and do not
retry a `failed` tactic unchanged.

HANDOFF contract: the HANDOFF at the top of NOTES.md always has these four fields, kept current:
- Next step: the exact declaration and file to work on next.
- Active tactic: the TACTICS row being followed.
- Last failure: the stuck goal (statement and key hypotheses) and the error or obstacle.
- Do not retry: approaches that already failed here, one line each.

Method that worked for our accepted solutions:
1. Plan for 20-30 minutes: read the exact hole statements, the informal solution and hints;
   search Mathlib and lean-pool (`./lean search QUERY`) for the main ingredients. Write a
   blueprint in NOTES.md: the chain from library results to the hole, marking what is missing,
   with a size estimate for each missing piece.
2. Choose the shortest route the pinned libraries support. Prove only the special case the hole
   needs; prefer a statement-targeted argument over porting a general theory. If a residual is
   estimated above about two rounds of work, or survived the last round unchanged, spend up to
   45 minutes looking for a cheaper route (a special case, a weaker sufficient statement, a
   different known proof) before continuing, and record the decision in NOTES.md.
3. Reduce top-down. Compile the hole from named intermediate statements early
   (`Submission/<Topic>/*.lean`), then split the hardest open statement again into named pieces
   with exact hypotheses, and prove pieces on the critical path. Do not grow a library towards
   an unreduced central theorem: if the main residual is a whole paper theorem, its first job is
   to be decomposed. Keep modules well under ~1500 lines.
4. Test every reduction hypothesis before building on it: check it on degenerate cases (small
   n/d, boundary parameters, known examples) and compile one sanity instance. A refuted
   residual is progress: record it as `refuted`, with the counterexample, and replace it.
5. Your own "infeasible" judgements were often wrong in past runs (problems called "far beyond
   one round" were solved one or two rounds later). Never stop on such a judgement; reduce and
   keep proving.
6. Use `./lean build <Module>` while working. `./lean check` (the official test, refused while
   a `sorry` remains) is optional: the controller runs the official verification itself.
7. Stuck-goal protocol. When one goal has resisted for about 45 minutes or three different
   attempts (`./lean` also prints a note after an hour without a closed `sorry`): write the goal
   (statement, key hypotheses) and the failed attempts under `## STUCK` in NOTES.md; then, from
   that statement alone and not from your previous attempts, derive two or three different
   decompositions into named helper lemmas with exact statements; test each helper on a
   degenerate case and keep one that holds; compile the helpers as `sorry` stubs and prove them.
   If the whole route is stuck rather than one goal, mark its tactic `failed` in TACTICS and
   switch to another (a special case, a different representation of the objects, another proof).
8. Never weaken or restate a hole, never edit trusted files, never add axioms, `native_decide`,
   `implemented_by`/`extern` tricks or `set_option maxHeartbeats 0` loops.

lean-pool: its modules are NOT prebuilt here and cannot be built in place, so
`import LeanPool.*` fails (names that `./lean search` shows from LeanPool are not importable as
they are). To use them, copy the needed files (their import closure only) into
`Submission/<Topic>/`, rename the module paths, keep the Apache-2.0 header and add the source
(the lean-pool repository and commit pinned in the benchmark's `lake-manifest.json`, and the
files) to NOTES.md. Check the closure size before copying.

The organizers' CI checks the final package on a 16 CPU / 32 GB runner with 6 hours in total
(one job: build, export, then the NanoDa and Lean kernels). Proofs of about 3 million lines in
about 3,000 files have passed it in under 4 hours. Do not spend rounds optimising build or check
time unless the controller reports measured CI numbers (a CI fit round says so explicitly); a
complete proof is verified by the controller with that 6-hour budget. Still avoid brute-force
`decide`/`simp`/`norm_num` over large finite objects (structured arguments are also easier to
repair), vendoring of large libraries (the source archive must stay below 29.5 MB) and
`set_option profiler`/`trace.*` lines in the final files.

Solution bank: `./lean search` also searches licensed formalizations the operator added to the
workbench's bank (`bank/<name>/`, listed with repository, commit and licence in
`bank/SOURCES.json`). Search it before writing a general result yourself. If an Apache-2.0 / MIT
licensed formalization of a needed result exists (in the bank, or publicly: clone into /tmp if the
network allows), you may adapt it: keep its license notice in a `Submission/<Topic>/License.lean`
comment and record repo + commit + files in NOTES.md under `## REUSE`. Never copy code without a
license.

Reply exactly READY_FOR_VERIFICATION as soon as there is no `sorry` left, `./lean build
Submission` succeeds on the current files, and each hole's axioms are standard
(`./lean inspect Submission:<hole>` shows only propext, Classical.choice, Quot.sound). A full
`./lean check` before that is optional; the controller then runs the official verification
(comparator, Lean kernel and Nanoda) with the full CI budget. Otherwise keep working; do not
stop early.

Use the whole round. Do not end your turn after a single step or just to write a handoff.
Keep proving until every hole is closed (then reply READY_FOR_VERIFICATION) or the round is
about to run out; keep the HANDOFF and RESIDUALS in NOTES.md current as you go. The HANDOFF
must name the exact next step (declaration, file, the lemma to prove next).
