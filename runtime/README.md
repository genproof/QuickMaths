# Runtime (`leaneval`)

The per-round agent runtime (internally `lrh`). One controller job is one agent round: prepare the job,
supervise Claude Code in a landrun sandbox, serve `./lean` feedback, then run the official verifier.

- `leaneval/agent_runner.py` supervises
  Claude Code 2.1.289 (`claude -p --output-format stream-json`) pinned by sha256 at
  `~/.local/share/lean-eval-claude/2.1.289/claude` (`LEANEVAL_CLAUDE_BIN`, `LEANEVAL_CLAUDE_SHA256`),
  model `claude-opus-5-5`, effort `xhigh`, tools Bash/Read/Edit/Write/Glob/Grep, no MCP, per-job
  isolated `CLAUDE_CONFIG_DIR=<job>/claude-home`, inside landrun (`LEANEVAL_LANDRUN`; writes:
  workspace, /tmp, claude-home; network unrestricted).
- Official verification (`leaneval/verifier.py`, `finalization.py`): a pristine workspace of the pinned
  benchmark (`resources/verifier-pins.json`, commit 208b6b22), the benchmark's sandbox probe, then
  `lake test`, whose WorkspaceTest runs the toolchain's `lake comparator` (leanexport, the Lean checker,
  the toolchain's `nanoda_bin`) inside the workbench's bubblewrap 0.12.0, through the cache-binding wrapper
  of `EvalTools/RunEval.lean` first on PATH. `tool_identity` checks the benchmark checkout, bubblewrap's
  version and the toolchain before every build and verification.
- Auth: a subscription token given as a secret reference (`leaneval/secret_refs.py`):
  `LEANEVAL_CLAUDE_TOKEN_REF=file:~/.config/leh/accounts/<name>.token` (mode 600) or
  `env:NAME`; the older `LEANEVAL_CLAUDE_TOKEN_FILE=<path>` still works. The supervisor writes the
  token into the per-round CLI credentials file and removes it afterwards. That directory is readable
  inside the sandbox; a tripwire (`credential_access` in `agent_runner.py`) ends the round when a tool
  call reaches for it, records `SECURITY-EVENT.json` and halts the runner (docs/SECURITY.md).
- Native evidence: init event (model/cwd/permission mode/version/session), every
  streamed assistant `message.model`, and transcript `effort`/`perTurnEffort`.
- Tokens: per-message maxima from the stream, plus any unstreamed result excess;
  counted input + cache write + cache read + output (input includes cached input).
  Polled threshold; may overshoot.
- Subscription usage limit (`rate_limit_event` rejected / limit text): the supervisor
  pauses (not counted as active wall time) until the native reset, then resumes the
  same session. Early turn ends are nudged while budget remains, unless the model replies
  READY_FOR_VERIFICATION.
- `finalization.CONTROLLED_STOP_REASONS` adds api_error, external_stop and
  authentication_failure to token/wall limits; quiescence and the official
  verifier remain mandatory.
- `./lean search` searches Mathlib, lean-pool and the solution bank (`<workbench>/bank/`, sources listed
  in `bank/SOURCES.json`, managed by `tools/bank.py`).

Layout variables (defaults relative to the home of the user running the runtime):
`LEANEVAL_HOME` (`~`), `LEANEVAL_WORKBENCH` (`~/projects/lean-eval-workbench`),
`LEANEVAL_CLAUDE_STATE` (`~/projects/lean-eval-claude-state`), `LEANEVAL_QUOTA_STATE`
(`~/projects/lean-eval-quota-state`), `LEANEVAL_QUOTA_POLICY` (`<quota state>/groups.json`),
`LEH_DEPENDENCY_CONTEXTS` (`~/lean-harness-dependency-contexts`).

## lrh P1 (`lrh-p1-20261014`)

`compact.LRH_VERSION`; every feedback step records it as `lrh_version`. Agent-facing `./lean`
changes from the 2026-10-14 fleet analysis. The official verifier
receipt stays the only acceptance; no step ever sets `accepted`.

- **Light `./lean check`** (the default). Refused while code `sorry` remains; then a warm scoped
  `lake build Submission`, `lake build Solution` (the trusted statement wrapper: a renamed or
  changed hole fails here in seconds) and `#print axioms` of every `config.json` theorem name
  (standard axioms only, `axiom_audit`). Remote action `light_check`, scoped build cache, the
  per-call cap `worker.json timeout_seconds`, scoped queue only. Step: `action check`,
  `query ''`, `check_mode light`, phase `light_check` / `light_check_statement` /
  `light_check_axioms`. A passing light check counts as a whole build for the full-check gate
  and for the feeder's `complete` rule (it builds Submission on exactly these files).
- **`./lean check --full`** (`query full`; also the legacy query-less `./lean build`): the
  official `lake test` (lake comparator in bubblewrap, the wrapper in `~/.cache/lrh/evaluation-bin/<workbench digest>`), `check_mode full`, cap `FULL_CHECK_SECONDS` (5400) or larger
  via `worker.json full_check_seconds` / `LRH_FULL_CHECK_SECONDS`. Gate: no sorry and a passing
  `build Submission` or light check on these files. A timeout or host kill on that gated tree
  says "NOT a rejection ... reply READY_FOR_VERIFICATION" (`not_a_rejection: true`).
- **READY footer** (`ready_note`, computed by the broker): sorry-free and the latest whole build
  (`build Submission` or light check) passed on exactly these files, so it suggests
  READY_FOR_VERIFICATION; the official verification runs outside the session.
- **Locks.** Scoped calls share `.feedback/controller.lock`; the full check holds only
  `.feedback/official.lock`. It compiles in the job's own compiler workspace, a different
  directory from the scoped build workspace with its own `.lake` and seal, so builds no longer
  queue behind it. Preflight/repair hold both (same order). Search holds none. Step numbers are
  allocated when a call finishes, under `records.lock` together with the job-state updates, so
  `revision` is the completion order. `feedback_queue`: `compiler`, `official` or `planning`.
- **Compiler parallelism.** `LEAN_NUM_THREADS` = `worker.json lean_num_threads`, else the
  controller environment `LRH_LEAN_NUM_THREADS`, else `LEAN_NUM_THREADS`, else min(8, cores) on
  the compiler host. It is passed into both landrun sandboxes (`--env LEAN_NUM_THREADS`) and into
  `lake test`. Lake jobs (`worker.json lake_jobs` / `LRH_LAKE_JOBS`) add `--jobs=N` only if the
  host's `lake --help` documents `--jobs` (lake 4.21 does not). Recorded as
  `compiler_parallelism`. `fleet/server/run_controller.sh` passes `config.json
  runtime_env` (LRH_* names only) into the controller environment.
- **Host kills.** Exit 137 / signal 9 without our own timeout, `Lean exited with code 137`, or a
  cgroup `oom_kill` give `environment_error` with `resource_killed` and a "not an error in your
  proof" note, and never open the environment circuit breaker.
- **Identical failures.** The same call (action, query) on the same proof files that failed with
  Lean diagnostics in this round is answered from that step (`cached_from_step`,
  `compiler_invoked: false`, `cache_note`). Timeouts, host kills and the full check are never
  cached.
- **Stale compiles.** Module builds, `build Submission`, inspect and the light check are watched
  (local transport). Once a file of the queried module's import closure differs from the compiled
  snapshot (checked every 5 s after the first 10 s), SIGUSR1 stops the compiler, the partial build
  stays sealed (`interrupted_module_build`) and the step is `superseded` with
  `superseded_files`. The full check is never stopped; it reports on its snapshot.
- **Round wall.** A compile is refused (`round_wall_gate`) when `round-clock.json` leaves less
  time than the fastest of the last three comparable runs in this round plus 30 s.

Compatibility: new step fields are additive, so the feeder and continuation rounds read old and
new records alike. Deploying creates a new `lrh-<hash>` directory; the feeder switches a runner
only at a lineage boundary (FEEDER-POLICY rule 1), so a running lineage keeps its runtime.
Tests: `pytest` at the repository root runs them with the other suites; `PYTHONPATH=. python3.12 -m pytest
tests` here runs them alone (deploy_runtime.sh does that on the host). After any change here, regenerate
`runtime-manifest.json` with `tools/runtime_manifest.py`.
