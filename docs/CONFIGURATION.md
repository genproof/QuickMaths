# Configuration

Every path has a default relative to the home directory of the user that runs the component, and an
environment variable that moves it. Nothing in the repository names a host, a user or an account.

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `LEANEVAL_WORKBENCH` | `~/projects/lean-eval-workbench` | runtime, controller, feeder, `leh workbench`, `leh bank`, `verify/` |
| `LEH_OWN_ROOT` | `~/leh-fleet` | feeder, run_controller.sh, operator tools, `leh evolve extract` |
| `LEH_CAMPAIGNS_ROOT` | `~/projects/lean-eval-campaigns` | controller, feeder, `leh evolve extract` |
| `LEH_RUNS_ROOT` | `~/projects/lean-eval-runs` | controller, feeder (packaging) |
| `LEH_HOSTS` | empty (accept the `--host` given) | controller: optional comma-separated host allowlist |
| `LEH_TRANSFER_RECEIVER` | unset (built-in copy of the seed's proof files) | controller: module with `apply(source, workspace)` for seed overlays |
| `LEH_DEPENDENCY_CONTEXTS` | `~/lean-harness-dependency-contexts` | runtime: producer-context registry |
| `LEH_RUNTIME_VENV` | `~/.venvs/leh-runtime` | deploy_runtime.sh (on the host; created with pytest when missing) |
| `LEANEVAL_CLAUDE_TOKEN_REF` | `file:~/.config/leh/accounts/default.token` | runtime: the account's secret reference (`file:` or `env:`); `LEANEVAL_CLAUDE_TOKEN_FILE=<path>` is the older form |
| `LEANEVAL_CLAUDE_BIN`, `LEANEVAL_CLAUDE_SHA256` | the pinned Claude Code 2.1.289 path and checksum | runtime |
| `LEANEVAL_LANDRUN` | `<workbench>/.tools/bin/landrun` | runtime |
| `LEANEVAL_HOME` | `~` | runtime (the home used for the sandbox policy and for `~` in references) |
| `LEANEVAL_CLAUDE_STATE`, `LEANEVAL_QUOTA_STATE`, `LEANEVAL_QUOTA_POLICY` | `~/projects/lean-eval-claude-state`, `~/projects/lean-eval-quota-state`, `<quota state>/groups.json` | runtime (run_controller.sh sets the first two under the own root) |
| `ELAN_HOME` | `~/.elan` | runtime and builder: where the benchmark toolchain (with `nanoda_bin`) is found |
| `LRH_LEAN_NUM_THREADS`, `LRH_LAKE_JOBS`, `LRH_FULL_CHECK_SECONDS`, `LRH_NAME_INDEX` | min(8, cores), unset, 5400, `~/.cache/lrh/decl-names.txt` | runtime `./lean` (the first three also from config.json `runtime_env`) |
| `LEH_FLEET_CONFIG` | `fleet/fleet.json` | operator tools |
| `LEH_SSH_CONFIG`, `LEH_SSH_KNOWN_HOSTS` | unset (your ssh defaults) | operator tools, `leh evolve extract`; `StrictHostKeyChecking=yes` and `BatchMode=yes` always |
| `LEH_REMOTE_OWN_ROOT`, `LEH_REMOTE_CAMPAIGNS_ROOT` | `leh-fleet`, `projects/lean-eval-campaigns` (relative to the remote home) | operator tools, gather |
| `LEH_STATE_DIR` | `~/.local/state/leh-fleet` | coordinator, publisher and vcur ledgers and logs |
| `LEH_VCUR_CONFIG` | `fleet/local/vcur/config.json` | verify_current.py (template `config.example.json`) |
| `VCUR_BASE`, `VCUR_PY`, `VCUR_PREP`, `VCUR_RUN_VERIFY` | `~/lean-eval-vcur`, `python3.12`, next to `run_lane.sh`, `run_verify.sh` next to `run_lane.sh` | vcur scripts on the verify host |
| `LEH_VERIFY_ROOT`, `LEH_VERIFY_CACHE`, `LEH_VERIFY_SCOPE`, `LEH_BWRAP` | `~/projects/lean-eval-verify`, `~/.cache/leh-verify`, `auto`, `<workbench>/.tools/bin/bwrap` | `verify/`: run directories of `verify_tree.sh`, the fetched evaluator, the cgroup scope (`systemd`, `none`), the bubblewrap binary |
| `LEH_SCRUB_DENYLIST` | `~/.config/leh/scrub-denylist.txt` | `tools/scrub.py`: your private terms (host names, account labels) |

## Files

- **`fleet/fleet.json`** (a copy of `fleet.example.json`, gitignored), on the operator machine:
  `accounts` (label → secret reference), `hosts` (runners → account label, per-host policy overrides), the
  policy keys that deploy writes into each host's `config.json` ([fleet/docs/FEEDER-POLICY.md](../fleet/docs/FEEDER-POLICY.md)),
  `leaderboard_model` (skip problems the public leaderboard already credits to that model id; null = no
  filter), `contributors` (tags in packages), `publish` (`repo`: the private repository for branches;
  `intake`; `results_account`) and `coordinator` (its policy; unknown keys are refused).
- **`<own root>/config.json`** on a host (written by deploy): `host`, `runners` (absolute queue roots),
  `tokens` (runner → secret reference), `runtimes` (runner → runtime directory) and the policy keys.
- **`<own root>/targets.json`**: the queue, in order (`fleet/server/targets.example.json`). Optional keys per
  target: `max_rounds`, `disabled`, `title`, `seed_dir`, `avoid_tactics`, `parallel_of`, and two size hints
  for ordering and the difficulty tier: `ref_min_modules` (modules of the smallest known accepted solution,
  e.g. from public CI logs) and `n_solvers` (systems credited with the problem).
- **`<own root>/briefs/<problem>.md`**: an optional per-problem brief, appended after `COMMON.md`. Keep
  per-problem notes there, on the host; the repository tracks only the generic briefs.
- **`fleet/local/vcur/config.json`** (a copy of `config.example.json`): the verify host, its limits and
  `workbench` (the host's `LEANEVAL_WORKBENCH`, relative to its home; null = the default).
- **`<workbench>/bank/SOURCES.json`**: the solution bank's record, written by `leh bank`.

Runner names are letters followed by digits (`wave101`, `r7`): the queue root is `fleet-1-<runner>`, and
attempts are named `fleet-1-w<number>-<seq>-<problem>`, which the runtime's build cache parses. Deploy refuses
other names.

## Pins

| File | Pins |
|---|---|
| `runtime/leaneval/resources/verifier-pins.json` | the benchmark commit, the evaluation flow, bubblewrap's version and the organizers' installer script (commit and SHA-256), landrun |
| `runtime/leaneval/resources/profile.toml` | the benchmark commit and toolchain, the model and effort, the source policy (licences allowed for adapted code and bank sources) |
| `runtime/leaneval/resources/benchmark.json` | the problem catalog at the benchmark commit; regenerate it with `tools/make_catalog.py <lean-eval checkout> <commit>` |
| `runtime/leaneval/agent_runner.py` | the Claude Code version and binary SHA-256 |
| `verify/verify_run.py` | the current benchmark commit and the organizers' evaluator (commit, SHA-256) |

A pin change is a runtime change: regenerate the manifest (`tools/runtime_manifest.py`), rebuild or check
the workbench (`leh workbench`, `leh doctor`) and run the verification controls (`leh controls`).

## Credentials

An account is a secret reference, never a value in a repository file: `file:~/.config/leh/accounts/<name>.token`
(a file the operator places on each runner host, mode 600) or `env:NAME`. `runtime/leaneval/secret_refs.py` is
the one resolver; the feeder and the coordinator only derive the account's label from a reference (the file
name without `.token`, or the variable name). Fleet runners need `file:` references, because
`run_controller.sh` starts the controller with a cleared environment; `env:` works for direct runs.
`fleet.py deploy` checks that each referenced file exists with mode 600 and never reads, copies or prints it.
What the agent can and cannot reach during a round is in [SECURITY.md](SECURITY.md).
