## This round: split ONE running lane, {lane}, while the other lanes keep working

The operator asked to split lane `{lane}` because: {reason}

The restored files contain the current split (`Interfaces.lean`, `Reduction.lean`, `Sanity.lean`,
`Submission.lean`), every finished lane, the latest work of lane `{lane}` in `{dir}` and, for each
other open lane, only its stub: those lanes are being proved by other agents right now.

Do exactly this:
- Read `{dir}` (docstrings, its `## LANE {lane}` section and RESIDUALS rows in NOTES.md) and cut
  what remains of `{lane}` into 2-3 new lanes (new CamelCase ids) along its open residuals, each
  with its statement in `Interfaces.lean`, a `Main.lean` stub with the CONTRACT block and a
  sanity theorem in `Sanity.lean`, as for any lane.
- Turn `{dir}` into a library: keep its proved lemmas, delete its `main` stub, and add a sorry-free
  glue theorem `Submission.Lanes.{lane}.of_parts` that derives `Submission.Lanes.{lane}Statement`
  from the new lane statements. Close the holes in `Submission.lean`/`Reduction.lean` with that
  glue applied to the new lanes' `main` theorems. The new lanes may import `{dir}` modules.
- First check that the split closes the lane rather than moving it: each new lane must be strictly
  weaker than `{lane}`'s statement (the CONTRACT `Strength:` line says why), and their estimates
  must add up to less than `{lane}`'s remaining estimate. When the hard core would only move into
  one of the new lanes (in one campaign, chains of splits re-estimated children at 1.2-2.1x their
  parent), do not split: write under `## SPLIT {lane}` in NOTES.md why, which statement is wrong
  or too strong, and what design change is needed, keep `{dir}` unchanged and end the turn.
- Do NOT change the statement, `Main.lean` or directory of any other lane, and keep
  `Submission.Lanes.{lane}Statement` itself unchanged: other agents depend on them.
`./lean build Submission` must succeed with `sorry` only in the lane stubs. End your turn then.
