# Operations

How to run the harness: one host first, then a fleet, then the steps after a proof. Each step says how it
was checked: **tested** (the offline suite of this repository), **validated** (run on a real host, with the
date) or *not re-run* (it ran in our campaign, and its code has not changed in a way the tests do not cover,
but it has not been run since).

## Prerequisites

**Runner host** (one or more):

- Linux on x86-64 or aarch64 with Landlock (the agent's sandbox, landrun) and unprivileged user namespaces
  (the official evaluation's sandbox, bubblewrap; on Ubuntu 24.04, which restricts them through AppArmor,
  the organizers' installer loads a profile with sudo). `/proc`, systemd for the fleet units, and
  optionally cgroup v2 delegation (without it compiles run unconfined; the runtime falls back by itself).
- Tools: `git`, `rsync`, `ripgrep` (`./lean search`), GNU `tar`; for building bubblewrap a C compiler,
  `pkg-config`, `curl` and `xz`; `sudo` for the fleet user limited to `systemctl` and `install` (the units
  and `start.sh` use `sudo -n`).
- Python 3.12 for the runtime and the tools, and a system `python3` ≥ 3.10 for the feeder.
- elan in `~/.elan` (or `ELAN_HOME`); the builder installs the benchmark toolchain
  (`leanprover/lean4:v4.35.0-rc3`), which ships `nanoda_bin`.
- The pinned Claude Code CLI 2.1.289, a single binary at `~/.local/share/lean-eval-claude/2.1.289/claude`.
  Before every round the supervisor compares its SHA-256 with `CLI_SHA256` in
  `runtime/leaneval/agent_runner.py` and refuses a mismatch or a symlink. Another version needs
  `LEANEVAL_CLAUDE_BIN` and `LEANEVAL_CLAUDE_SHA256`; the stream parsing and the flags were written against
  2.1.289.
- One Claude subscription token per account, from `claude setup-token`, in a file of your own such as
  `~/.config/leh/accounts/account-a.token`, mode 600. API keys are not supported by this runtime.
- `vm.max_map_count` ≥ 262144 for large proofs (a 2,900-file closure needed 70,267 maps; the Ubuntu
  default 65,530 fails late, with `failed to read file …olean.private`). About 30 GB of free disk.

**Operator machine** (fleet, publishing, re-verification): Python 3.12, `ssh` with keys for the fleet user on
every host (host keys already known), `rsync`, `git`, and the GitHub CLI `gh` with access to your private
publish repository.

`leh doctor` checks the runner-host items it can see without changing anything.

## 0. Install and test (tested)

```sh
git clone <this repository> QuickMaths && cd QuickMaths
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                     # all suites: core, runtime, fleet, verify (offline: no model, network or host)
.venv/bin/leh doctor
```

## 1. One host

### 1.1 Build the workbench (validated 2026-10-27)

```sh
leh workbench --dry-run              # the plan; changes nothing
leh workbench                        # LEANEVAL_WORKBENCH or --workbench DIR
```

`runtime/tools/build_workbench.py` checks out the benchmark at its pin, installs the toolchain, builds
landrun (Go, SHA-256 checked) and bubblewrap (`runtime/tools/build_bwrap.sh`: the organizers'
`scripts/install_bwrap.sh` at a pinned commit, meson and ninja from a venv, and libcap's headers unpacked
from the distribution package when missing; no root), fetches and builds the Lake packages, and ends with
`leaneval.verifier.tool_identity`. It is idempotent and refuses (exit 2, nothing changed) a checkout at
another commit, with another origin or with modified files, and a `pins.json` or `lean-toolchain` that
differ. The binary hashes go into `.tools/workbench-build.json`.

The packages step builds only what the official flow needs at the benchmark root (`EvalTools`, `lean-eval`,
Mathlib, the ProofWidgets widget targets), never the root `LeanEval` library or lean-pool's
`Challenge`/`Solution`: their outputs would shadow every problem's own modules and make every proof fail.

**Validated 2026-10-27** on Ubuntu 22.04 (32 CPUs, the builder restricted to 8): from an exported copy of
this repository, the build ran end to end without root and `tool_identity` passed.

### 1.2 Check the verification chain (validated 2026-10-27)

```sh
leh controls                         # positive, sorry, axiom, weakened, missing
```

Each control runs the real official verification on the benchmark's `two_plus_two` problem: `positive` must
pass (and a changed artifact must not inherit its receipt), the other four must be rejected. **Validated
2026-10-27** on the workbench of 1.1: all five passed (positive accepted in 148 s; the others rejected in
87–102 s).

### 1.3 Optional: the solution bank

```sh
leh bank add geometry https://github.com/<owner>/<repo>.git <40-hex commit> --note "differential geometry"
leh bank list
leh bank check
```

The bank holds licensed formalizations at exact commits under `<workbench>/bank/`; `./lean search` searches
them next to Mathlib and lean-pool, and the architect contract's `Reuse:` field records what was found.
Only licences of the profile's source policy are accepted (Apache-2.0 and MIT by default). Tested offline.

### 1.4 One round by hand (validated 2026-10-20)

These are the steps the queue controller performs for every round. The control problem `two_plus_two` is
not in frozen v1; add `--controls` to `leaneval.jobs` for it.

```sh
cd runtime
python3.12 -m venv .venv && .venv/bin/pip install -q pytest      # the runtime runs from its own .venv
export LEANEVAL_WORKBENCH=~/projects/lean-eval-workbench
export LEANEVAL_CLAUDE_TOKEN_REF=file:~/.config/leh/accounts/account-a.token
JOB=~/leh-jobs/try-1                 # outside the runtime tree and the workbench; must not exist yet
PYTHONPATH=. .venv/bin/python -m leaneval.jobs --job "$JOB" --workbench "$LEANEVAL_WORKBENCH" \
    --problem annals_unit_conjecture --variant B0 --attempt-id try-1 \
    --wall-seconds 14400 --token-limit 1000000000
{ printf '\nController brief for this attempt:\n'; cat ../fleet/server/briefs/COMMON.md; } >> "$JOB/prompt.txt"
PYTHONPATH=. .venv/bin/python -B -m leaneval.agent_runner --job "$JOB"
PYTHONPATH=. .venv/bin/python -m leaneval.finalization --job "$JOB" --workbench "$LEANEVAL_WORKBENCH" \
    --runs ~/projects/lean-eval-runs --seconds 21600 --allow-controlled-stop
```

`agent_runner` writes the round's result into the job (`result.json`); `finalization` exits 0 only for
`official_local_pass`. **Validated 2026-10-20**: one real `claude-opus-5-5` round on `two_plus_two` reached
`official_local_pass`, with the earlier evaluation flow; the round path is unchanged since, and its official
verification step is the one 1.2 validates on the current flow.

### 1.5 One host with the feeder (not re-run)

The simplest way is `leh fleet deploy HOST` (section 2) with a `fleet.json` that lists only this host. By
hand, on the host:

1. Copy `fleet/server/{feeder.py,run_controller.sh,start.sh,own_status.py,smoke_account.py,restart_from_files.py}`
   and `fleet/server/briefs/` to `~/leh-fleet/`, and make the shell scripts executable.
2. Install the runtime: `fleet/server/deploy_runtime.sh` from the operator machine, or copy `runtime/` to
   `~/leh-fleet/runtimes/lrh-local/` with a Python 3.12 venv (with pytest) as its `.venv`. The controller
   checks every file listed in `runtime-manifest.json` against its hash (`tools/runtime_manifest.py
   --check`).
3. Create the queue root `~/projects/lean-eval-campaigns/fleet-1-wave101/` with a `HOST` file (the host's
   name), a copy of `start.sh`, `controller.py` from `fleet/server/controller.py` with its
   `RUNTIME = Path("...")` line set to the runtime directory, and a `manifest.json`:
   `{"campaign_id": "fleet-1-wave101", "host": "<host>", "requested_config": {"model": "claude-opus-5-5", "reasoning_effort": "xhigh"}, "excluded_problem_ids": [], "assignments": []}`.
4. Write `~/leh-fleet/config.json`:
   `{"host": "<host>", "runners": ["<absolute queue root>"], "tokens": {"wave101": "file:~/.config/leh/accounts/account-a.token"}, "runtimes": {"wave101": "<absolute runtime dir>"}}`
   plus any policy keys from `fleet.example.json`, and `~/leh-fleet/targets.json` from
   `fleet/server/targets.example.json`.
5. Fill in the unit templates in `fleet/server/systemd/` (`@USER@`, `@GROUP@`, `@OWN@`, `@CAMPAIGNS@`),
   install them into `/etc/systemd/system/`, `systemctl daemon-reload` and
   `systemctl enable --now leh-feeder.timer`. The feeder starts the runner's unit through `start.sh`.
6. Watch it: `python3 ~/leh-fleet/own_status.py`, `~/leh-fleet/status.json`, `~/leh-fleet/feeder.log`
   and the queue's `controller.log`. `touch <queue root>/STOP` stops a runner after its current job.

Smoke-test an account first (one tiny CLI call; prints only the status and the limit windows):
`cd ~/leh-fleet/runtimes/lrh-local && PYTHONPATH=. .venv/bin/python ~/leh-fleet/smoke_account.py file:~/.config/leh/accounts/account-a.token`.

## 2. A fleet (not re-run)

On the operator machine:

1. `cp fleet/fleet.example.json fleet/fleet.json` (gitignored) or set `LEH_FLEET_CONFIG`. Fill in the hosts
   (ssh names), runners, accounts (secret references) and policy. Optionally set `LEH_SSH_CONFIG` and
   `LEH_SSH_KNOWN_HOSTS`.
2. On every host, place the token files the accounts reference (mode 600), the workbench, elan and the
   pinned CLI.
3. For each host: `leh fleet deploy HOST` (prints the plan), then with `--apply`. Deploy syncs the server
   files and briefs, checks the account files, installs the runtime (`deploy_runtime.sh`, which runs the
   runtime tests on the host), creates the queue roots with `fleet/server/controller.py`, merges
   `config.json` and installs the units.
4. Targets: put a `targets.json` on one host and spread it with `leh fleet partition --from HOST` (then
   `--apply`), or write each host's `targets.json` yourself.
5. `leh fleet status` shows every host's queues, claims, programs and alerts.
6. Coordinator: run `leh coordinator` (a dry run) for a while and read the plans, then `--apply --once` by
   hand, then schedule it every 5 minutes (macOS template `fleet/local/launchd/coordinator.plist`, or cron).
   Rollout and checks: [fleet/docs/COORDINATOR-POLICY.md](../fleet/docs/COORDINATOR-POLICY.md).
7. A new runtime: `fleet/server/deploy_runtime.sh HOST` prints `deployed: <dir>`; put that directory into
   the host's `config.json` `runtimes` (or into `fleet.json` and deploy again). The feeder switches a
   runner only between lineages.
8. Steering lineages and lane programs by hand (status, split a stuck lane, a hedge on another route,
   re-architecture, moving lanes off an exhausted account): `leh op HOST TOOL ARGS` with the tools in
   `fleet/operator/` ([fleet/operator/README.md](../fleet/operator/README.md)). They are tested offline
   against feeder-built programs.
9. The experiment manager's routine (status, convergence, evolve, approvals, publishing):
   [EXPERIMENT_MANAGER.md](EXPERIMENT_MANAGER.md).

## 3. After a proof

1. **Official verification on the pinned benchmark** happens inside the controller: a round that replies
   READY_FOR_VERIFICATION (or a VERIFY lineage the feeder starts for a complete round) runs the official
   verification with `verification_seconds` (6 h by default). A pass leaves `PASS-<problem>.json` in the
   queue root and the receipt in the runs directory. To verify a proof by hand from a git commit:

   ```sh
   cd runtime
   PYTHONPATH=. .venv/bin/python -m leaneval bundle <problem> --repo <git repo> --commit <40-hex> \
       --workspace . --output <bundle dir>        # --workspace: the directory of Submission.lean in that commit
   PYTHONPATH=. .venv/bin/python -m leaneval verify --bundle <bundle dir> \
       --workbench "$LEANEVAL_WORKBENCH" --runs ~/projects/lean-eval-runs --seconds 21600
   ```

2. **Package.** The feeder turns each kept pass into `~/leh-fleet/outbox/<problem>/<digest16>/` with a CI-fit
   estimate for the organizers' runner (16 CPUs, 360 min; `CI` in `feeder.py`: 6 + 2.8 × module-seconds /
   16 / 60 minutes, held above 330, calibrated on two accepted 3k-file proofs, [AUDIT.md §13.1](AUDIT.md));
   a package that does not fit stays `hold_ci_review` (then CI_FIT). The submission archive must stay under
   about 29.5 MB; `fleet/local/repack_numerals.py SRC OUT --check` shrinks numeral-heavy certificate files
   without changing any term.
3. **Publish.** Set `publish.repo` (and optionally `results_account`, `contributors`) in `fleet.json`, then
   `leh publish --dry-run` and, after reading it, `leh publish`. It pushes one `claude/<problem>-private`
   branch per problem as an orphan commit, fast-forwards it only with a strictly better package, never
   force-pushes, holds branches with unknown history (`--trust BRANCH@COMMIT` after a review) and reads
   every push back. The decision logic and the push against a local bare repository are tested.
4. **Official verification as the organizers run it** (`verify/`; validated 2026-10-26). Before submitting,
   run the organizers' own flow, with their evaluator, on the current benchmark commit:

   ```sh
   leh verify <problem> <dir with Submission.lean> --dry-run         # plan + preflight
   leh verify <problem> claude/<problem>-private --repo <clone or URL>
   leh verify <problem> <commit> --ci-envelope                       # the CI runner's bounds: 16 CPUs, 31G, 6 h
   ```

   SOURCE is a directory or a git ref; only `Submission.lean` and `Submission/**/*.lean` are taken and a
   symlink is refused, as the official `submission_paths` does. The script clones the workbench's benchmark
   at the current commit with the workbench's packages (and refuses when that commit pins other packages),
   fetches the organizers' `evaluate_submission.py` at its pinned commit and checks its SHA-256, and runs
   the official `lake test` in a fresh workspace with Lean Pool built privately inside it, in a systemd
   user scope with `MemoryMax`, `MemorySwapMax=0` and `AllowedCPUs` (`LEH_VERIFY_SCOPE=none`: taskset
   only). It checks that the sources, the protected files, the binaries and Lean Pool's sources did not
   change, writes `receipt.json` and `RUN-DONE`, and exits 0 for accepted, 1 for rejected, 2 when the run
   could not be prepared.

   **Preflight** refuses (unless `--no-preflight`) too little free disk on the run directory and on
   `TMPDIR` (the comparator's export: about 2 KB per proof line) and a `vm.max_map_count` below what the
   closure needs. **Resources:** give the official run 10–16 CPUs and 64 GB (`--cpus 0-15 --mem 64G`).
   `--ci-envelope` predicts CI: for two accepted 3k-file proofs the CI evaluate step took 0.95–1.01× the
   local run on 16 CPUs (226–227 min). **Validated 2026-10-26** on Ubuntu 22.04: an accepted 10-file proof
   ACCEPTED in 145 s with `--ci-envelope`, the same proof with one hole set to `sorry` REJECTED
   (`Illegal axiom detected: 'sorryAx'`); and on 2026-10-23 a 2,993-file, 3.2M-line proof passed in
   223 min and was then accepted by the organizers' CI.

   **vcur** automates this for published branches: `fleet/local/verify_current.py` installs
   `vcur/run_lane.sh`, `vcur/prep_current.py` and the `verify/` files on the verify host, runs one branch at
   a time and, when it passes, commits the new `VERIFICATION.json` on the branch. Copy
   `vcur/config.example.json` to `vcur/config.json` and set `host` (and `workbench`). Tested offline with a
   fake host.
5. **Submit.** Submitting a branch to the benchmark is outside this code.

## 4. Evolve

```sh
leh evolve extract HOST > host.jsonl          # read-only on the host; only round features come back
leh evolve report *.jsonl --out reports/       # report.md and summary.json, with the gate replay and proposals
```

What the report contains and how a proposal becomes a policy change: [EVOLVE.md](EVOLVE.md).
