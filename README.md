# QuickMaths

**QuickMaths is an agent harness for proving research-level theorems in Lean 4.** It is **#1 on the
[LeanEval leaderboard](https://lean-lang.org/eval/formalization-evaluation/?sort=total)**: 92 of the 128 LeanEval v1
problems (as of 2026-10-26).

## What QuickMaths is made of

0. **Distilled methodology** from research harnesses: our own plus [MASS](https://arxiv.org/abs/2502.02533), [AdaEvolve](https://arxiv.org/abs/2602.20133) and [IDS](https://arxiv.org/abs/1403.5556).
1. **Proper infra:** servers, many parallel agent sessions, and a harness with four levels:
   - **Director** manages the whole experiment. It watches how everything runs and how problems get solved,
     pulls out insights and evolves the harness.
   - **Orchestrator** decides which problem goes into the pool, how many resources it gets and what context to
     add. It watches for tunnel vision and pushes the Lead out of local minima.
   - **Lead** runs one problem: decomposition, recon, handing the blocks out and merging the results.
   - **Swarm** agents each take one block in their own session, write and build the Lean and report back; critics
     review the work.
2. **Problems are split into blocks with metadata:** which step is being formalized, why the split was
   chosen, related lanes, and relevant or similar solutions from the bank.
3. **A good validation environment with detailed feedback.** We fully replicated the organizers' CI locally.
4. **A solution bank** (RAG over useful problems) from open Lean repositories. The patterns of the strongest
   public proofs are distilled into the agents' instructions.
5. **Evolution on logs.** In our opinion the simplest big win. We log every trace with metadata, so in evolve the
   Director sees exactly where the harness fails and fixes those places. When the system got stuck on one route,
   we sometimes ran evolve by hand.
6. **Our own search for insights** over X, arXiv and git. It brings back more than the built-in web search,
   especially in a niche domain like this one.
7. **CI fitting and pep talks.** For the last 5 problems we had to step in ourselves: they didn't fit into the
   CI's 4 CPUs, 15 GB of RAM and 6 hours. So we built an optimization module, local checks and validation
   forecasts. We also had to cheer the agent up. It was too pessimistic about really hard problems, got
   stuck, and kept saying it would solve them in two weeks (judging by its logs).

## Requirements

- A Linux host (x86-64 or aarch64) with Landlock, unprivileged user namespaces, Python 3.12, git, ripgrep
  and about 30 GB of free disk. No root needed; `leh doctor` checks all of it.
- The pinned [Claude Code](https://github.com/anthropics/claude-code) CLI and a Claude subscription token per
  account, for model rounds only. The offline tests and the verification controls need neither.

## Quick start (one Linux host)

```sh
git clone <this repository> QuickMaths && cd QuickMaths
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]" && . .venv/bin/activate
pytest                    # offline suites: core, runtime, fleet, verify
leh doctor                # what this host still needs
leh workbench             # benchmark at its pin, toolchain, landrun, bubblewrap, Lake packages (~30 GB, no root)
leh controls              # the official verification accepts a proof and rejects sorry/axiom/weakened/missing
```

Then run one round by hand, or the feeder on one host, or a fleet: [docs/OPERATIONS.md](docs/OPERATIONS.md).
Every setting is in [docs/CONFIGURATION.md](docs/CONFIGURATION.md). The command is still called `leh` and the
Python package `harness`.

## Repository

| Path | What it is |
|---|---|
| `runtime/` | the per-round agent runtime, package `leaneval`: job preparation, the Claude Code supervisor in landrun, `./lean` (worker, broker, build cache), in-round gates, official verification and receipts, continuation, quota ([runtime/README.md](runtime/README.md)) |
| `runtime/tools/` | `build_workbench.py`, `build_bwrap.sh`, `bank.py` |
| `fleet/server/` | per host: the queue `controller.py`, `feeder.py` (lineages, gates, programs, quota, verify phase, packaging), the agent briefs (`briefs/COMMON.md`, `TACTICS.md`, `roles/*.md`), systemd templates |
| `fleet/local/` | operator machine: `fleet.py` (deploy, status, partition), `coordinator.py`, `publish.py`, `verify_current.py` (vcur), `repack_numerals.py` |
| `fleet/operator/` | host-side tools for steering lineages and lane programs, run with `leh op` ([README](fleet/operator/README.md)) |
| `fleet/docs/` | the feeder's and the coordinator's policies |
| `verify/` | official verification of one proof exactly as the organizers run it (`leh verify`) |
| `src/harness/` | the `leh` command, `evolve` (feature extraction, report, gate replay, lane calibration) and `map` (what is left, from Lean) |
| `tools/` | `scrub.py` (secrets, personal data, host details; also the history), `runtime_manifest.py`, `make_catalog.py` |
| `docs/` | architecture, method, configuration, operations, evolve, the experiment manager's brief, security, the audit |

## Development

```sh
pytest                                  # all suites (offline: no model, no network, no host)
ruff check .
python tools/scrub.py --all --authors --history
python tools/runtime_manifest.py --check   # after any change under runtime/: run without --check and commit
tools/install-hooks.sh                  # pre-commit scrub of staged files
```

CI runs the same checks. The scrubber refuses credentials, e-mail addresses (except no-reply ones), host
paths, IP addresses, logs and databases, and the terms of a private denylist kept outside the repository.

## Upstream components

Fetched at pinned commits at run time; none is vendored ([NOTICE.md](NOTICE.md)).

- [LeanEval](https://github.com/leanprover/lean-eval): the benchmark, its statements and evaluation tools.
- [lean-eval-submissions](https://github.com/leanprover/lean-eval-submissions): the organizers' evaluator and
  bubblewrap installer.
- [Lean 4](https://github.com/leanprover/lean4) (whose toolchain ships `lake comparator` and NanoDa) and
  [Mathlib](https://github.com/leanprover-community/mathlib4).
- [landrun](https://github.com/Zouuup/landrun): the Landlock sandbox for the agent.
- [bubblewrap](https://github.com/containers/bubblewrap): the sandbox of the official evaluation.
- [Claude Code](https://github.com/anthropics/claude-code): the agent CLI.

## Security and licence

[docs/SECURITY.md](docs/SECURITY.md) lists what the agent can reach during a round, including one known
limitation: the CLI's credentials file is readable inside the sandbox. A tripwire detects tool calls that reach
for it, ends the round and stops every runner of that account, but it is detection, not prevention. QuickMaths
is released under the [MIT License](LICENSE); third-party components keep their own licences
([NOTICE.md](NOTICE.md)). [CHANGELOG.md](CHANGELOG.md) lists what changed.
