## Your role in this round: route review (the previous attempt stalled)

The restored files are our previous attempt. Its last rounds did not move the open frontier
(no `sorry` closed, no residual proved, refuted or replaced), so the controller stopped it.
Repeating the same tactic will stall again. What the controller measured on this problem:

{history}

First hour, before writing new proof code:
1. Build the files, then bring the RESIDUALS table at the top of NOTES.md up to date: every open
   statement the holes still depend on, its source, and an honest size estimate in lines.
2. Read the TACTICS table and the history above. Mark the tactic that was active when the
   frontier stopped moving as `failed`, with the evidence (what blocked it), unless the history
   shows it still moved something.
3. For the largest residual, list at least three alternatives that are not marked `failed`: take
   them from the tactic library below or name a new one (a special case of the hole's setting, a
   weaker sufficient statement, a different published proof, another representation of the
   objects, existing Mathlib/lean-pool results). Estimate each.
4. Falsification: check the current reduction hypotheses on degenerate cases and compile one
   sanity instance; a false hypothesis explains a stall.
5. Pick the cheapest alternative, reusing compiled work where it fits. Add it to TACTICS as
   `trying` with its target residual and the signal you expect this round (which `sorry` or
   residual should close), and write the decision with reasons under `## ROUTE REVIEW` in NOTES.md.
Then spend the rest of the round on that tactic, top-down: reduce the target residual to named
pieces, compile the reduction, and prove pieces on the critical path.

Renaming, re-splitting or refining the same open row is not a new route: the controller
counts RESIDUALS rows that become `proved` and the summed `est. lines` of the open rows, and
it stops lineages whose estimate stays flat.

{parallel}
