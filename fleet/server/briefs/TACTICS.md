## Tactic library (route choices from our accepted and failed campaigns)

Use a tactic's name in the TACTICS table of NOTES.md. A tactic marked `failed` on this problem
is not retried unchanged; combining two tactics or changing their target is a new tactic.

T1 Special case only. When: the residual is a whole paper theorem but the hole fixes the
dimension, the constants or the class of objects. How: state and prove exactly that instance;
inline the constants. Seen: accepted solutions of other systems are statement-targeted; general
ports (differential geometry stacks) did not fit the CI budget.

T2 Weaker sufficient statement. When: the residual is stronger than the reduction needs. How:
re-derive what the reduction actually consumes (non-explicit constants, an epsilon loss, a finite
range, one direction of an equivalence) and replace the residual by that.

T3 Different known proof. When: the current proof needs a large missing theory. How: look for a
more elementary or more combinatorial published proof, an older proof, or a proof via a result
already in Mathlib or lean-pool; compare sizes before switching.

T4 Top-down reduction ("wire" first). When: always at the start, and whenever a residual is a
single large `sorry`. How: compile `hole_of_...` from named statements early, then split the
hardest statement into 2-4 named pieces with exact hypotheses and prove the critical path.
Seen: solved lineages replaced their named residual every round; stuck ones kept one residual
for 4-5 rounds while the library around it grew.

T5 Case split. When: the argument has regimes (small/large parameter, degenerate cases,
parity). How: make each regime its own named statement; prove the easy ones first to confirm
the split. Seen: van der Waerden was finished as four case statements proved in parallel.

T6 Representation change. When: proofs about the current definitions keep needing the same
painful rewriting or case analysis. How: change how the objects are encoded (explicit
coordinates, Finset instead of List, a structure per component, a decidable encoding) and prove
the equivalence once. Seen in formal synthesis work: a per-key state layout unblocked proofs a
global state could not.

T7 Certificate plus checker. When: a finite or numeric fact (enumeration, interval bound,
identity) would need search or brute force. How: precompute a certificate, prove a small checker
lemma sound, and check the certificate with `decide +kernel` on concrete Nat/Int literals.
Cautions: no `decide`/`Nat.ble` on symbolic data (kernel whnf blowup); split long iterations
(about 10k steps per check for nanoda); keep each data module light (the CI has 16 CPUs and
32 GB, and up to 16 modules build at once) and the whole source tarball under about 29 MB.

T8 Library reuse. When: a standard ingredient is missing. How: `./lean search` Mathlib and
lean-pool first; vendor the lean-pool import closure into `Submission/<Topic>/` with its license
header, after checking the closure size.

T9 Falsify the residual. When: a residual has resisted two attempts, or a lane or reduction
hypothesis is new. How: test it on degenerate instances and compile a sanity instance; if it is
false, prove the counterexample, record it as `refuted` and replace the statement. Seen: a lane
proved its own statement false and the program was sent back to the architect.

T10 Strengthen or generalize for induction. When: an induction fails because the hypothesis is
too weak for the step. How: prove a stronger or more general statement (extra parameter, an
invariant carried along) and derive the residual as a corollary.

T11 Explicit bounds instead of limits. When: an analytic step goes through limits or
asymptotics but the hole has concrete numbers. How: prove explicit inequalities with explicit
constants (and T7 certificates for the numerics). Caution: `∫ x, f x + g` parses as
`∫ x, (f x + g)`; parenthesize integrands.

T12 Split into a parallel program. When: what remains is clearly more than two rounds of work
and splits into independent statements. How: the controller turns such problems into lane
programs; as architect, follow the contract and sanity rules of your brief.
