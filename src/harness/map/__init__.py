"""Plan-vs-Lean map: what a proof tree really still needs, read from Lean rather than from the agents' notes.

`leh map build TREE --problem ID` builds a merged proof tree (Submission.lean + Submission/) against the
workbench's pinned packages and walks the declaration graph of `Submission.*` in Lean: for every declaration,
whether its own term uses `sorryAx`, whether it depends on `sorry` through other declarations, and whether the
final theorem reaches it. `leh map summarize` turns that into, per part (lane): what is left, dead code, names
defined in several lanes (re-derived work), and refutations (`not_*`) the final proof relies on (a vacuous step).

Modelled on the proof map of a 43M-line CFSG formalization (2026), whose "what's left" came from the full-proof
CI report and whose plan-vs-Lean status replaced self-reported progress.
"""
