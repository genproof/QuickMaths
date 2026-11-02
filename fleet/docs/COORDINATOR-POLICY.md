# Fleet coordinator, host inbox, publisher (P1, 2026-10-14)

Continues `FEEDER-POLICY.md` (P0, a feeder on every host). P0 decides everything inside one host;
P1 adds fleet-wide decisions and replaces the operator scripts for seeded cross-host transfers,
parallel lineages and parking a runner at a weekly limit (those scripts are not part of this
repository). Acceptance still comes only from the official verifier's receipt (`official_local_pass`);
the coordinator never accepts or verifies anything itself.

Measured (fleet analysis of 2026-10-14; the reports are not part of this repository):

- a seeded transfer to an account with quota won 10 of 19 lineages (25 model-hours per pass), mostly
  thanks to the quota;
- identical parallel duplicates won 1 of 17 (148 model-hours), because the duplicate kept the
  sibling's tactic;
- one problem had two lane programs on two hosts;
- at a weekly limit the P0 handoff looks for a runner only on its own host (often there is none),
  while idle runners on other hosts wait;
- one host was about 3 times slower on single lineages; programs on hard problems are cheap per pass.

## Host inbox (feeder)

Directory `<own root>/inbox/<id>.json`. The feeder reads it under `feeder.lock` on every tick.

- Each `id` is applied once. The outcome goes to `inbox-processed.json` and the file moves to
  `inbox/processed/`. A file sent again with the same `id` is deleted without effect.
- `id` equals the file name and consists of `[A-Za-z0-9_.~-]`. An unknown `type` or broken JSON gives
  `rejected` with a reason. If the directive has `expires_at` (epoch) and it has passed, the outcome
  is `expired`.
- `status.json → inbox`: `pending` (what waits and why) and `recent` (the last 30 outcomes).
  `status.json → holds`, and every runner has a `held` field.

| type | fields | what it does |
|---|---|---|
| `stop_lineage` | `problem_id` or `base`; `what` all/claims/program (default all); `mark` stopped/passed_elsewhere/delegated; `expect_status`; `to_host`; `reason` | Handled at the start of the tick. The lineage's pending rounds stop: the current one gets a controlled `STOP`, unstarted ones are removed from the manifest. The claim gets status `mark` and the fields `previous_status`, `stopped_by`, `stopped_reason`. `passed_elsewhere` also marks the problem's finished claims (exhausted/failed) and blocks a problem without a claim on the host, so neither a retry nor a new target picks it up. `what=program` stops the problem's active program and its running roles; the program's claim gets `program_status: stopped`. With `base` the action applies to one claim, and a mismatching `expect_status` gives `rejected` |
| `hold_runner` / `release_runner` | `runner` (a runner name or a queue root name), `reason` | `holds.json`: the runner gets no new work. Unlike a root `STOP`, the controller keeps running and the feeder restarts it |
| `start_lineage` | `problem_id`, `role`, `seed_dir`, `fields`, `prefer_runner`, `max_rounds`, `reason`, `from_host`/`from_job`/`from_key`, `route_review_done`, `target`, `expires_at` | Checked at the start of the tick, placed in the assignment step (details below) |

`start_lineage` fields:

- `role`: ROUTE_REVIEW, RESUME, PARALLEL or VERIFY;
- `seed_dir`: a directory that, once resolved, lies inside `<own root>/seeds` and contains
  `Submission.lean`. A symlink outside, a relative path or a directory outside seeds gives `rejected`;
- `fields`: string fields of the brief. `avoid_tactics` (a list) and `parallel_of` become the
  `{parallel}` block.

How `start_lineage` is processed:

1. Check at the start of the tick. The directive is rejected if the problem's target is disabled
   (`disabled`), the problem is already credited, there is a `passed`/`passed_elsewhere` claim, or a
   program of the problem is active on the host.
2. In the assignment step an idle runner with quota (no STOP, no hold, account not blocked, host
   admits work) takes the directive after a local handoff and before VERIFY backfill, programs,
   retries and new targets. `prefer_runner` is a hint here.
3. Without such a runner the directive waits in the inbox until `expires_at`.

What a placement creates:

- a `fresh` assignment with `overlay_dir` from the seed and the role's brief;
- the directive's `max_rounds`. For VERIFY the P0 rules apply: verify_policy always, 1 round,
  `verify_round_seconds`;
- for roles other than PARALLEL, a `target` entry in the host's `targets.json` if there was none,
  so that a moved problem lives on the new host as its own.

## Claims v2

`claims.json` is still keyed by problem id. A second lineage of the same problem on a host (a start
from the inbox while the problem's key holds a live claim: running/handoff/paused/program) gets the
key `<pid>~N` and the fields `problem_id` and `key`. Old keys and every P0 path work as before
(`claim_pid(key, claim)`).

A second lineage behaves like this:

- on a stall it ends without a ROUTE_REVIEW: its route was chosen at the start;
- it never becomes a program;
- while it lives, the problem gets no retry on the host;
- a pass of any lineage of the problem on the host stops the others (`stop_siblings`, the claims get
  status `stopped`);
- `signals` in `status.json` are keyed by the claim key and carry `problem_id`.

## Coordinator (`local/coordinator.py`, operator machine)

A tick has three steps.

**gather.** The coordinator streams `local/coordinator_gather.py snapshot IDS` over ssh to every host
in `fleet.json`, read-only. The script imports the deployed feeder and measures with its functions:

- runners: idle, STOP, hold, account;
- account blocks;
- live lineages, each with: the last finished round, sorry, build, complete, the `Active tactic` from
  the HANDOFF in NOTES, tier, closeness, trajectory;
- passes, programs, targets;
- outcomes of our earlier directives;
- the host's state directory (`own_root`), where seeds are placed.

A second call, `best PIDS`, only for parallel candidates, returns the best finished round of the
problem on each host.

**plan.** A pure function `plan(state, now, policy)` builds the list of actions. Tests:
`tests/test_coordinator.py`.

**apply.** Only with `--apply`:

1. The seed is exported with the `EXPORT` snippet of `fleet.py` along source host → operator machine →
   destination host: files are unpacked into `seeds/<name>.partial`, `Submission.lean` is checked, then
   the directory is renamed.
2. The directive is written to the host's `inbox/<id>.json`.

When a step of a group fails, the remaining steps of that group are skipped. A start never goes out
without its seed.

Plan rules:

| | rule |
|---|---|
| a | **A pass anywhere.** Every other host where the problem has a live lineage, an active program, an open target or an exhausted/failed claim gets `stop_lineage` (`passed_elsewhere`). Once per (problem, host) |
| b | **Cross-host handoff.** A `handoff` claim qualifies when all of these hold: the round has closed, the host has not placed the lineage itself within `handoff_wait_s` (900 s), the host itself has no idle runner with quota. The coordinator exports the seed from the last finished round, sends the source `stop_lineage` by `base` (`mark delegated`, `expect_status handoff`), and the destination `start_lineage` RESUME (VERIFY if the round is complete) with the remaining rounds. If the destination rejects the start or lets it expire, the lineage is placed again, up to `max_transfer_attempts` (3) times, then an alert. If the source has resumed the lineage itself in the meantime, the release is rejected and the plan warns that two lineages run side by side |
| c | **One program per problem fleet-wide.** The program with more lanes done stays, on a tie the older one. The others get `stop_lineage` `what=program`. An operator `route` tag in the program file makes it a separate program, not a duplicate |
| d | **A parallel lineage on an idle runner.** Conditions: closeness ≥ `parallel_min_closeness` (0.34), fewer than `max_live_lineages` (2) live lineages, no VERIFY and no complete round, no program, more than `parallel_cooldown_s` since the last parallel start. The seed is the fleet's best round: fewer sorry with a passing build, the newer on a tie. The PARALLEL brief gets `avoid_tactics` (the live lineages' active tactics) and `parallel_of`. At most one per problem per tick, at most `max_starts_per_tick` starts in total |
| e | **Never on a blocked account.** A block seen on any host applies fleet-wide. Hosts whose feeder has no inbox, whose `status.json` is older than `max_status_age_s`, or which report no state directory get no work |

A live lineage is one running on an account without a block (or with a block shorter than
`blocked_live_s` = 1 h), a handoff, or a start in flight.

Runner choice:

1. Host speed, descending (`hosts.<h>.speed`, e.g. 0.33 for a slow host).
2. An account the problem does not use yet.
3. Host and runner name.

Hosts with `heavy_single: false` get no single lineages of problems whose tier is in `heavy_tiers`
(hard, extreme).

Ledger `<state dir>/coordinator-ledger.json` (`LEH_STATE_DIR`, default `~/.local/state/leh-fleet`):

- a directive is recorded as `sending` before it is sent, becomes `sent` once written, and
  `send_failed` on an error. The plan may send such a directive again;
- the host's outcome `applied`/`rejected`/`expired` is read back. A directive that is not on the host
  `result_grace_s` after sending becomes `missing`;
- ids are deterministic, except the ids of parallel starts, which carry a timestamp and whose
  repetition is bounded by the cooldown. A host applies every id at most once.

```sh
python3.12 -I fleet/local/coordinator.py                  # dry run (default): plan, writes nothing
python3.12 -I fleet/local/coordinator.py --json           # the plan as JSON
python3.12 -I fleet/local/coordinator.py --apply --once   # one executed tick (the scheduled job)
python3.12 -I fleet/local/coordinator.py --status         # the ledger, no network
python3.12 -I fleet/local/coordinator.py --hold host-2:wave201 --reason "account-b weekly" --apply
python3.12 -I fleet/local/coordinator.py --release host-2:wave201 --apply
python3.12 -I fleet/local/coordinator.py --stop host-3:green_tao --reason "..." --apply
tail ~/.local/state/leh-fleet/coordinator.log
```

## Publisher (`local/publish.py`)

Ready packages of all hosts are grouped by problem. One artifact on two hosts counts once. The
canonical package is chosen by these criteria in order: verified on the current benchmark (`BENCH` of
`vcur/stage_current.py`), CI fit, fewer predicted CI minutes, smaller archive, older.

Then the decision for the branch `claude/<pid>-private` of `fleet.json publish.repo`:

| branch | action |
|---|---|
| none | an orphan commit of the canonical package |
| carries the same `artifact_digest` (also under vcur's `earlier_verification`) | `already_published` |
| the head is ours: the publisher pushed it (`ledger.jsonl`), it is a vcur commit on top of ours (`vcur-ledger.json`), or it was trusted with `--trust BRANCH@COMMIT` | if the package is strictly better than the branch's `VERIFICATION.json` (same order, without age): `git commit-tree <tree> -p <head>` and a plain push (git refuses a moved head; force is never used), then a readback. Otherwise `superseded` |
| a foreign or unknown history | hold. The alert goes to `publisher.log` and `publisher-alerts.json`; nothing is pushed or marked; after a review `publish.py --trust claude/<pid>-private@<head>` |

The group's other packages get `published.json` with status `superseded`. `-own-` branches are no
longer created. Branch facts are cached per head (`publisher-branch-cache.json`).

## PASS files and provenance (feeder)

Each `PASS-<pid>.json` is copied on first sight to `<queue root>/passes/<pid>-<digest16>.json`. A
later pass of the same problem on the runner overwrites `PASS-<pid>.json`, the copy stays. The copies
are what gets packaged.

`PROVENANCE.json` names the runtime the job ran with: `runtime_id` from `runtime-manifest.json` in the
`remote_agent` directory of `<job>/worker.json`. The job is found by `receipt_run_id` and digest in
the history. If the job is not found, the `controller.py` pin at packaging time is used, and the field
`runtime.source` says so.

## Keys

| where | key | default | effect |
|---|---|---|---|
| host config.json (fleet.json → deploy) | `inbox` | true | false: the feeder does not read the inbox |
| fleet.json `coordinator` (operator machine only) | `enabled` | true | false: `--apply` only prints the plan |
| | `stop_on_pass`, `cross_host_handoff`, `program_dedup`, `parallel` | true | rules a–d |
| | `parallel_min_closeness` | 0.34 | θ |
| | `max_live_lineages` | 2 | k |
| | `parallel_cooldown_s` | 14400 | one parallel start per problem in this time |
| | `parallel_max_rounds` | 6 | budget of a parallel lineage |
| | `blocked_live_s` | 3600 | a lineage on a short block still counts as live |
| | `handoff_wait_s` | 900 | how long the own host may take to place a handoff |
| | `adopt_paused` | false | also move `paused` (operator-parked) claims |
| | `max_transfer_attempts` | 3 | retries of a transfer start |
| | `max_verifies` | 2 | VERIFY rounds when moving a complete lineage |
| | `directive_ttl_s` | 1800 | `expires_at` of a start |
| | `result_grace_s` | 600 | after this a missing directive counts as `missing` |
| | `max_status_age_s` | 900 | a host with an older `status.json` gets no work |
| | `max_starts_per_tick` | 4 | starts (b+d) per tick |
| | `heavy_tiers` | hard, extreme | tiers that do not go to hosts with `heavy_single: false` |
| | `hosts.<h>.speed`, `hosts.<h>.heavy_single` | 1.0, true | relative speed, heavy single lineages allowed |
| | `exclude_problems` | [] | problems outside the automation |

## Rollout

1. **Feeder with inbox on every host.** `python3.12 fleet/local/fleet.py deploy HOST
   --apply` for every host. The change is backward compatible: without `inbox/` and `holds.json` the
   feeder behaves like P0, old claims and PASS files are read as before. On the first tick existing
   `PASS-*.json` files are copied to `passes/`; ready packages are not rebuilt. Check `status.json`:
   `inbox`, `holds`, the runners' `held` field, `signals.*.problem_id`.
2. **Coordinator dry run.** Run `coordinator.py` without flags for several hours (or a day) and read
   every plan. Check:
   - every host shows `inbox yes`, and the status age in minutes;
   - the `Active tactic` values and the best seeds are plausible;
   - there are no unneeded transfers or parallel lineages;
   - the account blocks match reality.

   Compare with what an operator would have done by hand.
3. **Manual ticks.** `coordinator.py --apply --once` a few times by hand. After each: `--status`,
   `inbox-processed.json` and `feeder.log` on the hosts that received a seed (`seeds/<pid>-xfer-…`,
   `seeds/<pid>-par-…`).
4. **Scheduled job.** Only then the template `local/launchd/coordinator.plist` (every 5 min, macOS;
   any scheduler that runs `coordinator.py --apply --once` works):

   ```sh
   sed -e "s|@PYTHON@|$(command -v python3.12)|" -e "s|@REPO@|$PWD|" \
       -e "s|@STATE@|$HOME/.local/state/leh-fleet|" fleet/local/launchd/coordinator.plist \
       > ~/Library/LaunchAgents/local.leh.coordinator.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.leh.coordinator.plist
   launchctl bootout gui/$(id -u)/local.leh.coordinator   # switch off
   ```

   A soft switch without launchctl: `fleet.json → coordinator.enabled: false`.
5. **Publisher.** First `publish.py --dry-run`. Branches created before `ledger.jsonl` existed, or by
   hand, end up in hold. Review them and trust them with `--trust`, then enable the publisher's
   scheduled job.

Check live before `--apply`:

- the run time of `coordinator_gather.py snapshot` and `best` on the host with the most history;
- real jobs have a `worker.json` with `remote_agent`, and `runtime-manifest.json` has `runtime_id`;
- handoff claims have `handoff.at_ts`;
- the hosts have `mv -T` and GNU tar;
- account labels (from the secret references in config.json `tokens`) are the same on every host,
  otherwise blocks do not match between hosts;
- `seeds/` exists on every host and there is enough disk space.

Tests: `cd fleet && PYTHONPATH=server python3.12 -m unittest discover -s tests -v`
(`test_feeder`: inbox, claims v2, PASS, provenance; `test_coordinator`: plan/ledger/executor/gather;
`test_publish`: grouping, decisions, fast-forward against a local bare repository).
