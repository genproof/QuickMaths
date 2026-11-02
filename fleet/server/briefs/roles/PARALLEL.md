## Your role in this round: parallel lineage (a different route beside a running one)

Another lineage of this problem is running right now on another runner and keeps its route. The
restored files are the best compiled state of the problem across our hosts (fewest `sorry` with
a passing `./lean build Submission`), with its NOTES.md. You were started to try a different
route, not to repeat that lineage's next step: two lineages on the same tactic have won 1 time
in 17, so following the sibling's active tactic wastes this subscription.

{parallel}

What the controller measured on this problem:

{history}

First hour, before writing new proof code:
1. Build the files, then read the HANDOFF, RESIDUALS and TACTICS tables at the top of NOTES.md.
   Mark the sibling's active tactic in TACTICS as `dropped` for this lineage (not `failed`: it
   is still running elsewhere).
2. For the largest open residual, list at least two alternatives that the sibling does not
   follow: take them from the tactic library below or name a new one (a special case of the
   hole's setting, a weaker sufficient statement, a different published proof, another
   representation of the objects, existing Mathlib/lean-pool results). Estimate each.
3. Pick the cheapest one, reusing compiled work where it fits. Add it to TACTICS as `trying`
   with its target residual and the signal you expect this round, and write the decision with
   reasons under `## PARALLEL ROUTE` in NOTES.md.
Then spend the rest of the round on that route, top-down: reduce the target residual to named
pieces, compile the reduction, and prove pieces on the critical path. Keep the HANDOFF and
RESIDUALS current; the controller measures this lineage like any other and verifies a complete
proof officially.
