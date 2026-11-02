# Audit of the production proving system (2026-10-03 … 2026-10-26)

This audit of our campaign on LeanEval v1 is the design input for QuickMaths. Every number below
carries a source tag from the list that follows. The sources are our private records and the operator's
analyses of them; none of them is part of this repository.

**Status: operator analysis.** The numbers come from hand-written analysis passes over the logs. `leh evolve
report` ([EVOLVE.md](EVOLVE.md)) now computes the convergence and gate numbers from round features; where a
recomputation differs, the report is the reference.

## Sources

| Tag | Source | Scope |
|---|---|---|
| S1 | runtime audit | 48.7 h window, 2026-10-14 12:00Z … 10-16 12:43Z: 241 rounds, scheduler log, rate-limit windows |
| S2 | lane-program report, 2026-10-16 | 96 lane jobs: RESIDUALS estimates, build records, endings |
| S3 | outcome and strategy reports, 2026-10-14 | 20 passes in 51.6 h; 380 jobs and 963 model-hours by difficulty tier. The report files are lost; their findings survive in the operator's notes |
| S4 | campaign history, 2026-10-06 … 10-16 | every solved and open campaign: time, team, size, unblocker, waste |
| S5 | evaluation-CI timing study, 2026-10-17 | public evaluation runs: fixed setup cost, module-seconds of passes and timeouts |
| S6 | route audit of the quantitative Mandelbrot program, 2026-10-19 | truth tests of every open residual, remaining-work estimate, P(close) |
| S7 | Poincaré CI-fit measurements, 2026-10-18/19 | comparator memory per node, node counts, CI predictions |
| S8 | record counts, 2026-10-20 | job directories on the 5 hosts, the publisher ledger, the leaderboard results file |
| S9 | code reading of the predecessor runtime, 2026-10-20 | sandbox, credentials and feedback paths |
| S10 | operator log of the Mandelbrot and Cerf programs, 2026-10-19/20 | lane sizes, re-architectures, splits |
| S11 | the organizers' evaluation change, 2026-10-22, and our two accepted runs on it | lean-eval #665 (main 208b6b22), lean-eval-submissions #2161/#2162, job and step timings of the runs for #2165 and #2166 |
| S12 | stall forensics of the Cerf programs, 2026-10-21 | 54 lanes, 480 model-hours in 40 h, every lane statement audited |
| S13 | local verifications on the current flow, 2026-10-22/23 | receipts, build logs and cgroup peaks of the two Poincaré packages; the failed first attempt |
| S14 | live validation of this repository, 2026-10-26 and 10-27 | a fresh bubblewrap build without root, an accepted and a rejected control on benchmark 208b6b22 with `verify/`; a fresh workbench and the five runtime controls |

## 1. Scope and data

- **The system has two modes.**
  - The **fleet**: a per-round agent runtime plus a scheduler with single-agent lineages and multi-lane
    programs. The runtime is Claude Code under a Landlock sandbox, with a `./lean` feedback wrapper and an
    official comparator verifier.
  - **Lead-integrated campaigns**: one operator session leading 6–12 Claude Code lane sessions on a separate
    host.
- **Volume:** 1,957 fleet rounds on 5 Linux hosts, 2026-10-03 … 10-20, with 11.3 GB of event streams [S8]. The
  rounds ran on 12 subscription accounts.
- **Outcome since 2026-10-09:** 65 problems accepted on the benchmark [S8].
  - 54 went through the fleet's automated publish path. All 54 are accepted.
  - 11 came from lead-integrated campaigns and hand-pushed fleet results.
  - Among them: Shishikura's theorem on the Mandelbrot boundary, Milnor's exotic 7-sphere, Pardon's p-adic
    Hilbert–Smith case, Linnik's theorem and Friedlander–Iwaniec.
- **Not accepted by 2026-10-20:**
  - 3-D Poincaré was proved, but its proof did not fit the evaluation CI of the time (§6). Both the smooth and
    the topological version were accepted on 10-23 and 10-26, after the organizers moved to a larger runner
    (§13.1).
  - Cerf's Γ₄ = 0 was in progress; it stalled (§13.3).
- **Excluded:** the earlier JSON-action solve loop. No benchmark acceptance is recorded for it (§11).

## 2. How problems actually got solved

| Mode | Start → sorry-free | Start → accepted | Examples [S4] |
|---|---|---|---|
| Lead-integrated team: one lead keeps the interfaces, 6–12 lane agents, operator integration | 7.5–21 h of mathematics | 24–55 h | determinant route 7.5 h of mathematics; local-enclosure route 21 h / 55 h; Fock-space route 15 h / ≤ 48 h; Pardon route 15 h / 44.5 h |
| Scheduler lane program: stateless architect → lanes → integrator | not recorded | 59–63 h | integer multiplication 59 h, exotic 7-sphere 63 h |
| Program after an operator diagnosis brief | solved 12 h after the brief | — | integer multiplication (the brief named 3 numeric traps) |
| Qualitative re-architecture of a non-converging program | — | about 9 h from program start to the official verification pass [S10] | Mandelbrot boundary, after about 5 days of a quantitative lane program |

Lead-integrated teams were faster from start to acceptance: 24–55 h against 59–63 h. The comparison is
confounded, because the problems differ and the lead teams had an operator integrating full time. The two
things the lead teams had that the stateless programs lacked:

1. a persistent owner of the interfaces who reads every lane's notes;
2. immediate re-architecture when a lane reports a false or awkward statement.

## 3. Convergence: what predicts success

- **The best progress signal is the agent's remaining-work estimate:** the summed `est. lines` of the open
  RESIDUALS rows [S3].
  - Lineages that later passed shrank it by ×0.46 per round. Open ones shrank it by ×0.96.
  - A stalled series: one lane's corrected-route estimate went 2.5k → 2.6k → 2.2k → 2.35k → 2.85k over five
    rounds while its lines grew from 4k to 35k [S2]. Another lane's estimate sat at 4–6k for 8 rounds [S2].
- **Sorry count is not a progress signal.**
  - One lane's sorry count over 9 rounds was 2, 4, 4, 4, 3, 4, 4, 4, 3; another's was 1, 2, 6, 3, 3, 1 [S2].
  - The old gate also counted a renamed residual as progress [S3].
- **Re-splitting renames the core.** One problem's open row went MainTerm → MainLarge → MLSpT → SpinPrime
  without being attacked directly [S4].
- **A single stuck residual sinks a lane.** In four problems one row stayed open for 9, 8, 8 and 8 rounds;
  solved residuals had a half-life under one round [S4].
- **Size threshold:** no problem whose remaining estimate exceeded 20k lines passed unless the estimate
  collapsed in the next round [S3].
- **Derived rule:** stop or re-architect a lane when the estimate falls by less than 25% over 2 consecutive
  rounds with no row proved (ratio d = E_r / E_{r−1} ≥ 0.75). A round that renames its open rows without
  proving one counts as d = 1.

## 4. False statements: the top program killer

- **The 5-day failure (Mandelbrot, quantitative lane program).**
  - The first four lane statements were all false or unprovable, after 13.7 lane-hours [S2].
  - Ten lane statements were false in total. The corrections were adopted 16.5 h after the lanes had
    flagged them [S2].
  - The lane statements fixed explicit numeric budgets. The independent audit found one of them false at an
    exit corner, by a factor of 1.36 [S6].
  - In its last two days the route's program wrote about 290k lines, mostly certificates, against an
    estimate of 27–43k [S10].
  - About 20k lines remained, and the audit put P(closed within 72 h) at about 0.5% [S6].
- **The 9-hour success (qualitative re-architecture).** Its lane statements were limit or existence forms,
  so no budget could be false [S10].
  - It started with 4 lanes and was re-architected once, to 6.
  - The lanes closed at 1.4k–8.5k lines each. The whole proof is about 45k lines with no numeric
    certificates.
  - In round 1 a lane proved that its own interface contradicted another lane's statement
    (`Implosion → ¬ Lavaurs`) and proposed a correction. The re-architecture that adopted it took one
    architect round.
- **Falsify-first is cheap** [S4]. Design-time Sanity instances caught 2 false statements in one campaign;
  a dry run of the statement wiring caught 5 interface bugs in another.
- **Lost lane notes broke a re-architecture and three splits** [S10].
  - A lane's notes live in its round's NOTES.md, not in its directory.
  - A re-architecture started without the notes of lanes whose new round had just begun, and had to be
    restarted.
  - Three split architects in one day cut lanes "from the code alone", without the parent lane's record of
    failed routes.

## 5. Hedges, transfers and duplicates

| Variant | Success | Cost |
|---|---|---|
| Identical duplicate lineages (same files, same route) | 1 / 17 [S3] | 148 model-hours per pass |
| Seeded transfers (best round's files → fresh account/runner, route review) | 10 / 19 [S3] | 25 model-hours per pass |
| Explicit route hedges (different tactic declared up front) | decisive in two campaigns: the p = 2 hedge; the qualitative-vs-quantitative split [S4, S10] | — |
| One problem running on 5 hosts at once | — | wasted ≈ 50 of 142 model-hours [S1] |

Transfers helped through fresh quota more than through a fresh context [S3]. A hedge is worth a runner only
when it commits to a different route.

## 6. The post-proof tail

The time from sorry-free to submission often doubled the calendar [S4]:

- **33 h:** a verify timeout, an archive over the hosting cap, a repack.
- **21 h:** verified but not pushed.
- **25 model-hours** after the mathematics was done: a 5,400 s check cap and a false gate stop.

The evaluation runner's limits are a hard second objective:

- **Published limits:** a 360-minute job on 4 vCPUs with 15 GiB of RAM and 3 GiB of swap.
- **Measured:**
  - **Fixed cost:** 38–50 minutes of setup before the first solution module compiles [S5].
  - **Archive cap:** the submission archive must stay at or under 29.5 MB. Larger archives were rejected by
    the hosting API.
  - **CI time:** observed passes reached about 54.8k module-seconds, and timeouts started at about 69.8k
    [S5].
  - **Comparator memory:** about 192 B per distinct expression node [S7]. A complete 3-D Poincaré proof
    with 89M distinct nodes needs about 17 GB, so it could not fit that runner. After two days of
    partitioned CI fitting, the node count went from 123M to 89M against a 75M target, and the predicted CI
    time stayed at 466–673 minutes [S7]. Both limits and that prediction are superseded: see §13.
  - **Heavy imports:** one lemma pulled 138k lines of numeric certificates into an import closure that is
    otherwise about 45k lines. A qualitative re-proof removed them [S10].
  - **Reuse:** importing an already-proved 409k-line library (3-D smooth Schoenflies, isotopy extension,
    sphere germs) let lanes close at a fraction of their estimates: 4.2k against 20k, 1.8k against 22k,
    2.5k against 13k lines [S10].

**Fix:** the CI budget is checked on the import closure from the first whole build, not after
verification.

## 7. Runtime waste (48.7 h window, 241 rounds) [S1, S2]

| Item | Measurement |
|---|---|
| Runner utilisation | model 45% · quota pause 5% · verify 1% · **idle 49%**, of which ≈ 400 runner-hours on accounts with quota left (each host saw only its own work) |
| Cold builds | 89 of 96 lane rounds started with a cold build, even at round 5 of a lineage; 57 of those builds hit the 1,800 s cap; the median wait for the first successful build was 34 min |
| Build share | Lean builds and inspects took 61% of lane round wall time |
| Gate false stops | all 5 "no progress" stops of lanes were reversed by an operator; each cost a cold restart |
| Infrastructure kill | one SIGTERM on a host killed 6 rounds and triggered 2 needless re-architectures |
| Re-architecture idle | lanes sat idle 1.5–2.2 h per re-architecture |

## 8. Quota

- **Weekly limits bind** [S1]. One account-week sustains about 200 runner-hours, roughly 1.2 runners
  nonstop.
- **Program lanes had no quota handoff** [S1]. A lane that hit a limit waited until an operator moved it:
  21 rounds and 104 h of round wall time.
- **Attribution bug** [S10]. When a paused runner was moved to a new account, the scheduler attributed the
  old pause to the *new* account and blocked it until the old reset.
- **Lost work** [S10]. A start directive without a preferred runner took a runner reserved for a program
  lane.

## 9. What accelerated solving [S4]

1. **Diagnosis before architecture:** a literature route, an honest size estimate, falsification of the
   interfaces, and a public module-count sizing of accepted solutions that showed when a compact route had
   to exist.
2. **Small lanes, many runners.** One problem's single stuck row, split onto its own runner, closed in
   1.5 h.
3. **Fast re-architecture with every lane's notes.**
4. **Hedges on a different route.**
5. **Reusing proved libraries** instead of re-deriving them.
6. **Early CI measurement** and an automated post-proof path: verify on the current benchmark, package,
   push.

## 10. Security findings [S9]

| Finding | Status | Fix |
|---|---|---|
| The agent's sandbox can read the provider credential. The per-round agent config directory is in the sandbox's read-write set, and the CLI's credentials file is written into it for the round. The runtime's documentation said the token was passed only through the environment and hidden from subprocesses | found by code reading; not demonstrated | a tripwire on the CLI's event stream ends the round, removes the credentials and halts the runner when a tool call reaches for them (tested with a fake CLI); the complete fix, a credential proxy outside the sandbox, needs a run with the real CLI and is open (SECURITY.md) |
| Gate refusals (edit scope, repeat guard) never reached the agent. The broker answered HTTP 400, and the `./lean` client turned every HTTP error into a generic "controller feedback unavailable" | found by code reading | refusals become feedback records that carry the gate message |
| The predecessor's tree and history carried host names, user names, account file paths, personal research data and other participants' notes | confirmed | this repository starts from a fresh history; CI scrubs every commit (`tools/scrub.py`) |

## 11. Negative results worth keeping

- **The JSON-action solve loop never reached benchmark acceptance.** It used typed obligation ledgers and
  passed only control runs. Every solve came from an agentic CLI with a fast build/check feedback tool.
- **More identical lineages do not help** (§5).
- **CI fitting must start at architecture time, not after the proof.** Two days of fitting a complete
  Poincaré proof moved the comparator node count only from 123M to 89M against a 75M requirement [S7].
- **But first check the limits you fit against.** The same packages were accepted unchanged once the
  organizers moved to a 16-CPU runner, in about a third of the time our gate predicted for the old one (§13.1).

## 12. Findings → design

| # | Finding | Design response | In this release | Metric |
|---|---|---|---|---|
| 1 | Lead-integrated teams were faster (24–55 h vs 59–63 h to acceptance) | a lead that owns the interfaces through architect, re-architect, split and integration | program roles of the feeder; the lead is a role per round, not one persistent session | hours to sorry-free per program |
| 2 | The remaining-work estimate predicts convergence; sorry count does not | progress from RESIDUALS rows and the estimate | feeder stall rule; `leh evolve report` replays it at other thresholds | false stops, rounds saved (replay) |
| 3 | False statements kill programs | sanity theorem per lane, qualitative interfaces preferred, re-architecture on `not_statement` | `Sanity.lean` enforced (contract 2+); `not_statement` returns the program to the architect | false statements found after lanes start |
| 4 | Lost lane notes | lane notes passed to the lead, the splitter and a resumed lane | `append_lane_notes` | — |
| 5 | Duplicates rarely win | hedges must take a distinct route | route review and PARALLEL briefs; coordinator hedges | pass rate per hedge-hour |
| 6 | Post-proof tail | verification as a controller action; CI budget; automated publish path | VERIFY / CI_FIT / REPAIR lineages, CI gate on packages, `publish.py`, vcur | hours from sorry-free to submittable |
| 7 | Cold builds | warm builds shared across rounds and lanes | sealed warm build cache per problem and host (`build_cache.py`) | first successful build latency |
| 8 | Idle quota, unhandled lane pauses | hand-offs for every role; attribution by the invocation's account | attribution fixed (§13.4); lane hand-offs by the operator (`switch_lanes.py`) | idle runner-hours on accounts with quota |
| 9 | Gate false stops | rows proved and the estimate ratio, two-round minimum | feeder | operator reversals |
| 10 | Credential readable in the sandbox | keep the token out of the agent's reach | tripwire (detection); proxy open | — |
| 11 | The CI gate modelled a runner that no longer existed (§13.1) | gate calibrated on our own accepted CI runs; verification follows the benchmark's flow | feeder `CI`; runtime and `verify/` on the current flow | predicted vs actual CI minutes |
| 12 | Host limits failed hours into a verification (§13.2) | preflight of disk and memory maps | `verify/verify_run.py preflight`, `leh doctor` | verifications lost to host limits |
| 13 | Invented decompositions produced false, vacuous or full-strength lanes (§13.3) | contract fields; split only into weaker, smaller children | contract 3 (`Source:`, `Strength:`), contract 4 (`Reuse:`), SPLIT brief | lane statements found false or full-strength after issue |

## 13. Addendum: 2026-10-20 … 10-26

### 13.1 The evaluation runner changed, and our CI model was wrong [S11, S13]

On 2026-10-22 (22:24Z) the organizers changed the evaluation:
- `WorkspaceTest.lean` now runs the toolchain's `lake comparator` (leanexport, the Lean checker, and the
  toolchain's `nanoda_bin` as an external kernel) inside bubblewrap 0.12.0;
- before, it ran comparator d03acab with landrun, lean4export and nanoda;
- jobs run on a 16 CPU / 32 GB runner with the same 360-minute limit.

Our two held Poincaré packages were then verified locally on the new flow and submitted unchanged. Both
were accepted:

| Package | Proof files | Proof lines | Module-seconds (16-CPU host) | Local `lake test` | CI evaluate step |
|---|---|---|---|---|---|
| 3-D Poincaré, smooth | 2,901 | 2.76M | 78,624 | 239 min | 227 min |
| 3-D Poincaré, topological | 2,993 | 3.2M | 86,071 | 223 min | 226 min |

On the CI runner, the evaluate step took 0.95–1.01× the local run on 16 CPUs. Job setup before it took
about 6 minutes. The export files were 4.82 and 6.36 GB. The concurrent anonymous memory peak of the
smooth package was about 11.6 GiB.

The old gate assumed 4 CPUs, a factor of 1.6 per module and 62 + 30 minutes of fixed cost. For the same
packages it predicted 466–673 minutes. That prediction held them for five days, and it was the basis of
the decision to stop the Poincaré CI fit on 10-19 [S7]. Part of the factor was already wrong for the old
runner: a sphere package predicted at 340–385 minutes took 279.7 minutes.

The new gate predicts 6 + 2.8 × (module-seconds / 16 CPUs / 60) minutes, which gives 235 and 257 minutes
for the two packages. The factor 2.8 covers two things: the parallel build is about 76 % efficient, and
the comparator phase costs about as much again. The package is held above 330 minutes.

**Lessons:**
- Re-read the submission workflow's `runs-on` and limits whenever the organizers change it.
- Calibrate every prediction on our own accepted CI runs.
- Re-gate held packages when the runner changes.

### 13.2 Host limits that fail late [S13]

- **Memory maps.** The first verification of the smooth package built 11,858 of 11,861 modules in about
  3.7 hours, then failed at `Submission.lean`: `failed to read file …olean.private`.
  - Elaborating the top file of that 2,901-file module-format closure needed 70,267 memory maps.
  - The host had the default `vm.max_map_count` of 65,530 (Ubuntu 22.04). Ubuntu 24.04 and the CI image
    default to 1,048,576.
- **Disk.** During the topological verification, the comparator wrote a 6.36 GB export file to `/tmp`
  while other tenants filled the shared disk to 368 MB free. Space was freed by hand, and the run passed.
- **Fix:** `verify_run.py` checks both limits before it prepares a run:
  - free space ≥ 5 GiB + 2.5 KB per proof line, on both the run directory and `TMPDIR`;
  - maps ≥ 30,000 + 14 per proof file, within 90 % of the limit.

### 13.3 Invented decompositions [S12]

The two Cerf programs spent 480 model-hours in 40 hours. Only 13 hours produced reusable lanes.
- 54 lanes were issued and 20 were marked done. Three of the done lanes were vacuous, and two depended on a
  statement as strong as the problem.
- About 494k lines were written against a 95k plan.
- The median remaining-estimate ratio per round was 0.98 (main) and 0.91 (second program). 72–79 % of
  rounds had d ≥ 0.75.

Root causes, ranked:
1. **Decompositions invented without a relative induction.** They yielded at least 17 lane statements that
   were false, vacuous or as strong as the whole problem. Audits found them 4.5–8.4 hours after issue. The
   lanes could not see it, because the statements were true-looking. About 113 model-hours went straight
   into such lanes.
2. **Splits moved the core instead of closing it.** Children were re-estimated at 1.2–2.1× their parent.
3. **No persistent lead.** There were 21 fresh-context architect, split and audit runs.
4. **Quota.** 66 usage-limit hits and 25.7 hours of lane stalls.

Cold builds (median 14 s) and runner count were not factors.

The fast campaigns of the same weeks closed in 9–24 hours. Their lanes were sections of a published proof,
one lead owned the interfaces, statements were falsified before lanes started, and at most one hedge ran,
on a different route.

**Fix:**
- **Contract 3:** every new lane names its `Source:` (the published step, or the inductive step of a stated
  relative induction) and its `Strength:` (why it is strictly weaker than the hole). The validator refuses
  new lanes without them.
- **ARCHITECT brief:** prefer the source's qualitative formulation to explicit budgets. Report a route whose
  lane estimates exceed about three times the source proof's length.
- **SPLIT brief:** split only when the children are weaker and their estimates add up to less than the
  parent's.
- **LANE brief:** a lane first checks whether its statement already gives the hole.

### 13.4 Quota attribution

On 2026-10-19 two runners were moved to a fresh account while their jobs were still in a usage-limit pause.
The feeder attributed the pause to the account in the runner's current config, so the fresh account was
marked blocked for 2.5 hours. The feeder now takes the account from the job's last model invocation, which
the runtime records.

### 13.5 What was validated, and what was not [S14]

**Validated on a 32-CPU Ubuntu 22.04 host:**
- `build_bwrap.sh` built bubblewrap 0.12.0 without root. libcap's headers came from the unpacked
  distribution package.
- `verify_tree.sh --ci-envelope` on benchmark 208b6b22 (lake-comparator flow):
  - an accepted Apollonian-packings proof was ACCEPTED in 145 s;
  - the same proof with one hole set to `sorry` was REJECTED (`Illegal axiom detected: 'sorryAx'`).

**Validated 2026-10-27 (release), on another 32-CPU Ubuntu 22.04 host:**
- `build_workbench.py` from an exported copy of the repository built a fresh workbench (benchmark 208b6b22,
  landrun, bubblewrap without root, the Lake packages) and `tool_identity` passed;
- the runtime's own verifier on the current flow ran the five verification controls: the positive control
  was accepted (148 s) and a changed artifact did not inherit its receipt; `sorry`, a new axiom, a weakened
  statement and a missing proof were rejected (87–102 s).

**Not re-run live:**
- multi-host fleet operation;
- the recalibrated gate on a new package;
- a model round since 2026-10-20 (the round path is unchanged; its verification step is the one the
  controls validate).
