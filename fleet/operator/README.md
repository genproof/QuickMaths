# Operator tools for lane programs

How the operator steers lineages and lane programs on a fleet host: read the state, split a stuck lane,
start a route hedge, re-architect, move lanes off an exhausted account. The feeder does most of this by
itself (docs/FEEDER-POLICY.md); these tools are for the decisions it leaves to a person.

Every tool except `run.py` runs **on a fleet host** and imports the deployed feeder from the own root
(`LEH_OWN_ROOT`, default `~/leh-fleet`). Each one is a single file, because it is sent to the host
over stdin. Run them from the operator machine with `run.py`:

```sh
fleet/operator/run.py host-1 lanes.py my_problem
fleet/operator/run.py host-1 split_lane.py my_problem Beta "row part_one stuck at 4k lines"
```

`run.py` uses the ssh options of `local/fleet.py`: `StrictHostKeyChecking=yes`, `BatchMode=yes`,
`LEH_SSH_CONFIG` and `LEH_SSH_KNOWN_HOSTS`. It quotes every argument for the remote shell; a bare
`ssh HOST python3 - PID "a reason"` splits the reason into words. When `LEH_REMOTE_OWN_ROOT` or
`LEH_REMOTE_CAMPAIGNS_ROOT` is set, it passes it to the tool. `--upload FILE` first copies a file to
`<own root>/uploads/<name>`. `--print` shows the ssh command without running it. The plain form also
works: `ssh HOST python3 - ARGS < tool.py`.

Tools that change state take the feeder lock (`feeder.lock`), so a feeder tick never sees half an
update. Where a round has to end, they give it a controlled STOP (`<job>/STOP` plus no successor
round) and never kill a process. Each one logs to `feeder.log` and prints JSON. REASON arguments end
up in the program file and the agents' briefs. Write them for the next agent.

## The tools

Read-only status (change nothing):

| Tool | Arguments | Shows |
|---|---|---|
| `lineages.py` | — | live claims (running, handoff, paused): runner, account label, round, sorry, lines, role, NOTES `Next step` |
| `programs.py` | `[PID ...]` | one line per program: status, end reason, each role's runner and status |
| `lanes.py` | `[PID ...]` | per lane: latest job, files, lines, sorry in the lane dir, stream age, the agent's last message |
| `residuals.py` | `PID`, or `NOTES.md\|JOB_DIR ...` | open RESIDUALS rows and estimates as the progress gate parses them; per lane its own rows (`Lanes.<lane>.`) |
| `hedges.py` | `BASE:LANE ...` | a hedge lineage's progress inside its lane dir and its last message |
| `messages.py` | `BASE\|JOB_DIR [N]` | the agent's last N text messages of a lineage's latest job |
| `accounts.py` | `[--json]` | per runner: account label, status, usage-limit pause, latest rate-limit windows; the feeder's account blocks |
| `export_prog.py` | `PID` | writes the program (base files, each lane's dir, open lanes' own notes) as a tar.gz to stdout |

Programs:

| Tool | Arguments | Does |
|---|---|---|
| `seed_program.py` | `PID SEED_JOB` | starts a lane program from a job's files (archives a stopped program file first) |
| `endround.py` | `PID REASON [--program]` | ends a lineage after its running round; `--program` turns it into a program seeded with that round |
| `resume_prog.py` | `PID REASON [--undo-rearchitect]` | re-opens a stopped program; open lanes continue on their own files |
| `adopt_prog.py` | `EXPORT REASON` | adopts an exported program on this host (lanes continue on the exported files and notes) |

Lanes:

| Tool | Arguments | Does |
|---|---|---|
| `split_lane.py` | `PID LANE REASON` | stops one running lane and asks for an ARCHITECT+SPLIT round on its files; the other lanes keep running |
| `hedge_lane.py` | `PID LANE RUNNER EXPORT ROUTE [--rounds N]` | on another host: a PARALLEL lineage working only on that lane, on the named different route |
| `rearchitect.py` | `PID TRIGGER_LANE REASON [--expect-route NAME]` | stops the running lanes, merges every lane's work and notes, sends the program back to the architect with REASON |
| `stop_lane.py` | `PID LANE REASON` | controlled stop of one lane; it stays stopped (not reassigned) |
| `relane.py` | `PID LANE JOB REASON` | re-opens one stopped or HELD lane on the files of JOB |
| `hand_done.py` | `PID LANE LANE_DIR REASON` | marks a lane done with files proved outside its lineage (by hand, or by a hedge on another host) |
| `stop_lineage.py` | `PID BASE\|- REASON` | an inbox directive that stops one claim of this host (`-`: every lineage and the program of PID) |

Accounts:

| Tool | Arguments | Does |
|---|---|---|
| `switch_lanes.py` | `PID:LANE ... [--restart waveNNN ...]` | stops the lanes, restarts the named runner units (they re-read their token), re-opens the lanes on their own files |
| `unblock.py` | `ACCOUNT REASON` | drops one entry from the feeder's `account-blocks.json` |

`run.py` (operator machine): `[--upload FILE] [--print] HOST TOOL [ARGS ...]`.

## When to use which

1. **Status, every tick of attention.** `programs.py`, then `lanes.py PID` and `residuals.py PID`.
   A lane converges when its own open estimate falls round over round. `lineages.py` covers plain
   lineages, `accounts.py` shows whose quota is gone.
2. **A lane stuck on one row, or over the line cap** (a lane should stay within about 4k lines):
   `split_lane.py PID LANE "which rows, why"`. The split architect sees the lane's files and its notes.
   It may cut the lane into smaller lanes but must not change any other lane's statement. The feeder
   checks this; a rejected split gets one fix round, then the lane continues on its own files.
3. **A lane that stalls on its route.** Export the program on its host
   (`run.py hostA export_prog.py PID > export.tgz`). Then start a hedge on another host with a
   *different* route: `run.py --upload export.tgz hostB hedge_lane.py PID LANE waveNNN uploads/export.tgz "route"`.
   In production, identical duplicate hedges won 1 time in 17. Watch it with `hedges.py BASE:LANE` on
   hostB. If the hedge closes the lane, copy its lane directory to hostA and run `hand_done.py` there,
   then stop the other attempt (`stop_lineage.py` for the hedge, or `stop_lane.py`).
4. **A false or badly cut interface.** A lane reports that its statement is false, or that it needs a
   different interface, while other lanes keep working on the old one. Run `rearchitect.py PID LANE
   "the false statement and the corrected interface"` right away instead of waiting for the lane to
   end. Proved lanes whose statements stay unchanged stay done.
5. **An account is exhausted.** Point the runners at another account in `config.json` `tokens`
   (`fleet.json`, then `fleet.py deploy`). Then run `switch_lanes.py PID:LANE ... --restart waveNNN ...`.
   The pause belongs to the old account; if the feeder blocks the new one for it, drop that entry with
   `unblock.py ACCOUNT`. To move a whole program to another host instead, export it, stop it on the
   old host (`stop_lineage.py PID - REASON`), and run `adopt_prog.py` on the new one.
6. **Rounds that died with their supervisor** (host restart, killed service) leave the lane HELD.
   Re-open it with `relane.py PID LANE JOB REASON` on its last job. If such a kill triggered a
   re-architecture, `resume_prog.py PID REASON --undo-rearchitect` restores the lanes.

## Safety

- **Never print tokens.** `accounts.py` prints account labels (the secret reference's file or
  variable name) and the CLI's rate-limit fields, from a whitelist. No tool opens a token file or
  prints what a secret reference points to. Don't add a tool that does.
- **Stop only your own lineages.** Every stop acts on this host's claims and programs and checks that
  the claim exists here (`stop_lineage.py` refuses an unknown BASE). Never stop a process by name on a
  shared host. Other people's jobs may run under the same user; use the controlled STOP these tools use.
- `stop_lineage.py` and `hedge_lane.py` write inbox directives, which the feeder applies only when
  `inbox` is on in `config.json` (the default). They expire after one hour and three hours; the outcome
  is in `inbox-processed.json`.
- `split_lane.py`, `switch_lanes.py` and `rearchitect.py` wait up to 15 minutes for the stopped
  runners to go idle and report whether they did.
- An operator re-architecture counts toward the feeder's limit of two re-architectures per program
  (`MAX_REARCHITECT`).
- Exports are proof files and notes of your own program. `adopt_prog.py` and `hedge_lane.py` refuse
  an archive that contains links or paths outside its directory.

## Tests

`fleet/tests/test_operator.py` runs every tool as it runs on a host (`python3 - ARGS < tool`
with `LEH_OWN_ROOT` pointing at a temporary own root holding a copy of the feeder). It runs them
against a program that the feeder itself builds from the fixtures of `test_feeder.py`, and then lets
the feeder tick on the result: an adopted program's lanes start, a hedge directive is applied, a split
and a re-architecture get their architect rounds. No host, ssh or model is involved.
