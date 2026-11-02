## Re-architecture: lane {lane} ended without a proof

The restored files contain the previous split, every finished lane (their directories under
`Submission/Lanes/` have no `sorry`), and the work of the failed lane in `{dir}`. Read the failed
lane's notes (NOTES.md and the docstrings in `{dir}`) first: often its statement was false as
stated or too large, and it proposed a corrected interface.

Revise the split: fix or replace the failed statement (prove the reduction from the corrected
statements), split it further if it is too large, and keep every finished lane whose statement
you keep unchanged (do not re-prove or edit them). Reuse the failed lane's proved lemmas by
moving them into the new lane directories. The rules above for the architect still apply; lanes
that are already proved count as lanes without a `sorry`.

What the controller recorded since the last architecture; resolve each item:
{findings}

- **Refuted statements.** Replace each one with a corrected statement, then check that the refuting lane's theorem
  no longer applies to it. Restate every lane that took the refuted statement as a hypothesis against the correction:
  such a lane is not done, even if its directory has no `sorry`.
- **Interface requests.** Either grant the request (strengthen the producing lane's statement so that it gives exactly
  what the consumer needs, once) or refuse it with the reason in the HANDOFF.
