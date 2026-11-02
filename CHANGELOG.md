# Changelog

## 2026-10-30: evolve from a long lane program and from the CFSG formalization

Programs (feeder)
- **Finished lanes are visible.** A new or re-opened lane round starts from the architecture plus every finished
  lane, not from the architecture alone, so consumers import finished tools instead of re-deriving them. In one
  program, lanes seeded from the base re-proved the same helpers; 143 lemma names ended up in more than one lane.
- **Refuted interface statements.** A lane whose files prove `¬ …Statement` (its own or another lane's) is
  stopped and is not counted as done, even without a `sorry`, because a consumer closed by contradiction is
  vacuous. The refutations are recorded in `program.refuted`, and the program re-architects with them in the
  brief.
- **Interface requests.** Lanes record the stronger statements they need from another lane in a
  `## INTERFACE REQUESTS` table instead of writing private strengthened copies. The feeder collects the rows into
  `program.interface_requests` and hands them to the next architecture round.
- **Lane order.** Lanes are assigned in the order their statements appear in `Interfaces.lean`, so architects put
  the shared tool lanes first.
- **Briefs.** ARCHITECT, LANE and REARCHITECT were updated:
  - shared tools with one strong statement, listed first;
  - shared definitions and adapters;
  - a sorry-free reduction at every level (also inside lanes);
  - `Kind: new|reuse` with calibrated estimates;
  - modules of at most about 400 lines;
  - the refutation and interface-request protocol.

Map
- **`leh map build TREE --problem ID`** builds a merged proof tree against the workbench and walks the
  declaration graph in Lean. Per declaration it records whether the declaration has its own sorry, whether it
  depends on a sorry, whether the final theorem reaches it, and which statement it refutes.
- **`leh map summarize`** reports per lane: what is left, dead code, names defined in several lanes and the
  refutations the final proof relies on. On a running program it found two refuted tool statements and two
  "finished" lanes that were vacuous.

Evolve
- `leh evolve extract` also emits one record per program lane: the estimate and kind from the lane's contract,
  lines written, done, rounds.
- `leh evolve report` adds a lane estimate calibration section and proposals: finished lanes against their
  estimates, per kind, and refuted statements.

Docs
- METHOD.md:
  - §5 adds refutations, shared tools, interface requests, a reduction at every level, and calibration by kind;
  - a new §9 explains why "what's left" is measured in Lean.
- ARCHITECTURE, EVOLVE and FEEDER-POLICY are updated to match.
- Sources: our logs, and the public AxiomMath/CFSG repository and project site.

## 2026-10-27: release preparation

The project is now called QuickMaths (repository `genproof/QuickMaths`, distribution `quickmaths`). The
command is still `leh` and the Python package `harness`.

Layout and naming
- The runnable system moved from `production/` to the repository root: `runtime/`, `fleet/`, `verify/`. The
  placeholder package and its milestone plan are gone; `src/harness/` is now the `leh` command and `evolve`.
- `runtime/leaneval/codex_runner.py` → `agent_runner.py`; the queue controller is one file,
  `fleet/server/controller.py` (it was a fleet controller plus a derivation script).
- Removed: unused runtime modules (`blocked_recovery`, `continuation_feedback`, `controller_retry`, `campaign`),
  the legacy external verification conveyor in vcur, and an unused PATH entry.
- Target size hints are `ref_min_modules`, `retry_max_ref_modules`, `program_min_ref_modules`. Runner names
  may be any letters followed by digits.

Evaluation
- The runtime verifies on the organizers' current flow: benchmark 208b6b22, the toolchain's `lake comparator`
  in bubblewrap 0.12.0 with the cache-binding wrapper; `./lean check --full` uses the same.
- `leh workbench` builds landrun and bubblewrap (no root) instead of comparator, lean4export and nanoda.
- `tools/make_catalog.py` regenerates the problem catalog from a benchmark checkout.
- Validated on a fresh host: workbench build, `tool_identity`, and all five verification controls.

New
- `leh`: doctor, workbench, controls, verify, bank, evolve, fleet, coordinator, publish, op, scrub.
- Solution bank (`leh bank`): licensed formalizations at exact commits, searched by `./lean search`.
- Architecture contract 4: lanes record `Reuse:`; packages carry the agents' `## REUSE` notes.
- `leh evolve`: round features from the hosts (read-only), report, gate replay, proposals.
- Credential tripwire: a tool call that reaches for the CLI's credentials ends the round and halts the runner.
- Docs: architecture, method, configuration, operations, evolve, the experiment manager's brief.

Upgrading a running fleet
- Old key names (`rival_min_modules`, `retry_max_rival_modules`, `program_min_rival_modules`) in
  `fleet.json`, `config.json` and `targets.json` are read under the new names; rename them at leisure.
- `leh fleet deploy` now also replaces `controller.py` in existing queue roots, keeping each root's pinned
  runtime; a running controller keeps the code it loaded until its next start. Deploy the new runtime and the
  new controllers together: the controller runs `leaneval.agent_runner`, which older runtimes do not have.
- The workbench needs bubblewrap (`leh workbench` builds it) and a benchmark checkout at 208b6b22.

Checks
- One `pytest` runs all suites; `tools/scrub.py --history` scans every blob and commit message;
  `tools/runtime_manifest.py --check` guards the runtime manifest.
