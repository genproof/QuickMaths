# Evolve on logs

`leh evolve` turns what the fleet's rounds left on the hosts into evidence for changing the harness: the
gate thresholds, the briefs, the orchestration. It proposes; a person decides.

## 1. Extract (on each runner host, read-only)

```sh
leh evolve extract HOST > host.jsonl                       # over ssh; the script goes over stdin
leh evolve extract HOST --own leh-fleet --campaigns projects/lean-eval-campaigns > host.jsonl
leh evolve extract --local --own ~/leh-fleet > here.jsonl  # on the host itself
```

`src/harness/evolve/extract.py` runs with `python3 -I` on the host (standard library only, Python 3.10+)
and prints one JSON object per round. It reads each queue's `manifest.json` and `state.json`, the feeder's
`claims.json`, `programs/*.json` and `targets.json`, and measures each round's workspace with the deployed
feeder's own parsers. What leaves the host:

| Group | Fields |
|---|---|
| identity | host, runner, attempt, problem, lineage, round, order, kind, role, program, lane, seeded |
| model | stop reason, status, tokens, output tokens, active seconds, nudges, pause seconds and count, READY marker, wall budget |
| verification | status, verified, verification seconds |
| workspace | sorry count, proof lines, RESIDUALS estimate, open / proved / refuted / replaced rows, largest row, the number of TACTICS rows tried and failed, the names of missing HANDOFF fields, lane count |
| targets | `ref_min_modules`, `n_solvers` |

No NOTES.md text, proof file, prompt, event stream or log leaves the host. Keep the JSONL outside the
repository (the scrubber refuses `*.jsonl`).

## 2. Report

```sh
leh evolve report host-*.jsonl --out ~/leh-evolve/2026-10-27/
```

`report.md` and `summary.json` hold:

- volume: rounds, lineages, problems, solved problems, tokens, model hours, pause hours, tokens per solved
  problem, median rounds to a pass, the problems solved only after an earlier lineage failed;
- convergence: the median round-over-round ratio of the RESIDUALS estimate in lineages that passed and in
  the rest;
- **gate replay**: the feeder's stall rule (the estimate kept above a ratio over two rounds without a proved
  row; rounds 1–2 always run; a complete round never stalls) evaluated at ratios 0.5 … 0.95. For each
  ratio: how many lineages it stops, how many of those passed later (false stops) and how many rounds it saves
  in lineages that never passed. The replay is exact for stops; it never claims what a lineage would have
  done had it run longer;
- seeded versus fresh lineages, pass rates per role, verification outcomes, sorry-free rounds without an
  official verdict, and how rounds ended;
- **lane estimate calibration**: for every program lane, the lines written against the architect's
  `Estimate:` (from the lane's contract), per `Kind:` (new infrastructure or reuse); finished lanes over 3× and
  under 1×; open lanes already over their estimate; interface statements that lanes refuted;
- **proposals**: changes the numbers support (a stall ratio that stops no passing lineage and saves more
  rounds; a gate that would cut passing lineages; seeding when seeded lineages pass clearly more often; a
  verify phase that misses complete rounds; pauses large against model time).

## Map (what is left, from Lean)

`leh map build TREE --problem ID` makes TREE (a program's base plus every lane's latest files) a Lake workspace of
the problem against the workbench's built packages. It builds `Submission`, walks the declaration graph and
writes `map.tsv`, then prints a summary:
- what is left: own-sorry declarations reached by the final theorem;
- per lane: declarations, reached, dead, sorry-dependent;
- names defined in several lanes;
- refutations (`¬ …Statement`) and which of them the final proof uses.

`leh map summarize map.tsv --final NAME [--lanes lanes.json]` reprints it. A persistent TREE rebuilds
incrementally: the first build of a 2,000-file tree took about 30 minutes; the map itself takes under a minute.

## 3. Decide and apply

A proposal changes the harness only through a reviewed commit:

1. The experiment manager reads the report together with the lineages it names (the operator tools show
   their RESIDUALS and TACTICS tables) and writes the decision down: accept, reject, or run longer first.
2. An accepted change is a commit: a policy key in `fleet.json`, a constant of the feeder, or a brief.
   Run the tests (`pytest`) and, for a runtime change, regenerate the manifest.
3. Deploy (`leh fleet deploy`), then compare the next report with this one. Keep each report: the replay of
   a changed rule on the old history is how you see what the change bought.

Change one thing at a time where you can (a brief, then a gate), so the next report can attribute the
difference.
