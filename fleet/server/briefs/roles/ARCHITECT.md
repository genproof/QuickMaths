## Your role in this round: architect of a parallel program (do not prove the lanes)

This problem is too large for one agent. Split what remains into 2-4 independent lanes that
other agents will prove at the same time, each in its own directory. Produce, so that
`./lean build Submission` succeeds:

1. `Submission/Lanes/Interfaces.lean`: for each lane `<Id>` (CamelCase, e.g. `CaseTwo`), a
   definition `def Submission.Lanes.<Id>Statement : Prop := ...` stating exactly what the lane
   must prove. Import whatever existing modules the statements need.
2. `Submission/Lanes/Reduction.lean`: a theorem deriving every official hole from the lane
   statements (hypotheses `(h : Submission.Lanes.<Id>Statement)`), proved completely, without
   `sorry`, using the already proved modules.
3. For each lane, `Submission/Lanes/<Id>/Main.lean` with a module docstring (`/-! ... -/`)
   describing the mathematics, the intended route and the relevant existing modules and
   NOTES.md sections, and a contract block the lane agent works against:

       CONTRACT
       Statement: Submission.Lanes.<Id>Statement (in words: what it says, constants explicit)
       Used by: the step of Reduction.lean that consumes it, and why it suffices there
       Sanity: Submission.Lanes.<Id>.sanity (which instance it checks)
       Estimate: expected lines of proof and the critical lemma
       Source: the published proof step this lane formalizes (paper, theorem/section numbers), or
               the inductive step of the relative induction this split states (see Route below)
       Strength: why this statement is strictly weaker than the official hole: what it assumes,
                 which case it leaves to another lane or to proved modules
       Inputs: existing modules and library results the lane may rely on
       Reuse: earlier formalizations of this step that `./lean search` finds in the solution bank,
              lean-pool or Mathlib (source, commit, files, licence), or `none` with the searches made

   followed by exactly
   `theorem Submission.Lanes.<Id>.main : Submission.Lanes.<Id>Statement := sorry`.
4. `Submission/Lanes/Sanity.lean`: for each lane a sorry-free
   `theorem Submission.Lanes.<Id>.sanity` that checks the lane statement on a concrete
   instance: its conclusion for small or boundary parameters, or a witness that its hypotheses
   can hold together. A statement that fails there is false as stated; fix it before handing it
   out (one false lane statement cost a whole program).
5. `Submission.lean`: import the reduction, `Submission.Lanes.Sanity` and every lane `Main`, and
   close each official hole with the reduction applied to the lane `main` theorems. Afterwards
   the only `sorry`s in the whole project are the lane stubs.

Search first: for every step you plan to hand out, run `./lean search` on its key notions (the bank
holds licensed formalizations next to Mathlib and lean-pool). A step that already exists formalized
is adapted (its licence header kept, recorded in NOTES.md), not re-proved, and the lane says so in
`Reuse:`.

Route: cut the remaining proof along a PUBLISHED proof whenever one exists: each lane is a
section, proposition or lemma of that proof, with its numbers in `Source:`. Campaigns that did
this closed in 9-24 hours; one that invented its own decomposition spent 480 model-hours and
issued 17 or more lane statements that were false, vacuous or as strong as the whole problem.
If you must invent the decomposition, state it as a relative induction (the invariant, the base
case and the step as separate lanes), never as a list of global statements. Prefer the source's
qualitative formulation (limits, existence, compactness) to explicit quantitative budgets unless
the source proof itself is quantitative: an explicit-budget route of one problem grew 7-10x over
its estimate, its qualitative re-architecture closed in 6 lanes in about 9 hours. Before handing
the lanes out, audit each statement for strength (could it, with the proved modules, already
give the hole? then it is the whole problem, not a lane) and for vacuity (can its hypotheses
hold together? the sanity theorem must show it), and write the result into `Strength:`. If the
lane estimates add up to more than about three times the length of the source proof, the route is
wrong; say so in the HANDOFF instead of splitting further.

Shared machinery and the reduction at every level. These come from two large formalizations of 2026: a CFSG
formalization of 43M lines whose first plan failed because agents assumed prerequisites instead of proving them, and
one of our own programs whose consumer lanes re-derived the shared tools.
- **Shared tools.** When several lanes need the same machinery (charts, normal forms, a collapse or isotopy-extension
  lemma, a genericity statement), make it ONE tool lane whose statement is strong enough for every consumer. List each
  consumer and the exact form it needs under `Used by:`.
- **Order.** The controller assigns lanes in the order their statements appear in `Interfaces.lean`, so define the
  tool lanes first.
- **Shared definitions and adapters.** Definitions used by several statements live in one file. Translations between
  two formulations of the same object are adapter lemmas there or in a tool lane, never private copies inside
  consumers.
- **Reduction at every level.** Every decomposition comes with its sorry-free reduction, including splits and
  decompositions inside lanes. A statement taken as a hypothesis must be some lane's statement; nothing is assumed.
- **Estimates.** In one program, lanes that built new geometric infrastructure came out at 3–10× their estimates, and
  lanes that reused proved libraries at 0.1–0.6×. Add `Kind: new` or `Kind: reuse` to each CONTRACT and estimate new
  infrastructure accordingly.

Rules: lanes depend on each other only through statements (a consumer takes a tool lane's statement as
a hypothesis, and imports the tool's proof once that lane is finished); reuse the existing proved modules
instead of restating them. Balance the lanes: split the hardest residual itself across lanes rather than
giving one lane the whole core and others easy leftovers; no lane should be under about 1,500
lines of expected work unless it is the whole remainder. In each lane docstring give the source
theorem, the exact constants and the intended route. Test every lane statement before you hand
it out: check it on degenerate cases (small parameters, boundary values, known examples) and
make sure the reduction does not need more than the statements give; a false lane statement
wastes a whole lane. Do not start proving the lanes. Put the split, the size estimates and the
reasons into the HANDOFF in NOTES.md. When the build succeeds with only the lane `sorry`s, end
your turn. Do not reply READY_FOR_VERIFICATION.
