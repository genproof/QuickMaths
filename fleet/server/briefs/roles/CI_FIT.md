## Your role in this round: CI fit (the proof is complete; its verification ran out of time)

The restored files are a complete proof: no `sorry`, and `./lean build Submission` succeeded on
them. The controller measured: {measured}

Make the proof cheaper to check without changing what it proves. Do not change any hole
statement and do not add new mathematics. The CI runner has 16 CPUs, 32 GB and 6 hours for the
whole job; the time is the parallel build (limited by the longest import chain, not only by the
summed module seconds) plus the comparator phase (export, NanoDa, Lean checker), which grows
with the size of the proof terms.
1. Find where the time goes: `./lean cost`, and build the slowest modules named above on their
   own (`./lean build Submission.X.Y`). Write the numbers into a `## MEASUREMENTS` table in
   NOTES.md first: summed module seconds, the 10 slowest modules, the longest import chain.
2. Work on the measured top items only: split the slowest modules and long chains into smaller,
   parallel modules; replace brute-force `decide`, `simp`, `norm_num` or `native`-style evaluation
   over large objects by structured arguments or smaller lemmas; shrink the largest proof terms
   (an existential chain unpacked into a structure, a lemma instead of a repeated `simp` block).
   Measured on a 2.9k-file tree: removing dead declarations saved about 4 % and trimming imports
   nothing (every direct import was used), so do those last. Remove `set_option profiler` and
   `trace.*` lines.
3. Keep `./lean build Submission` green after each change and record the timings before and
   after in NOTES.md.
When the slowest modules are clearly faster and `./lean build Submission` succeeds on the
current files with no `sorry`, reply exactly READY_FOR_VERIFICATION; the controller verifies
again with the full budget. Exception: when the measured block above or NOTES.md sets explicit
fit targets (for example a node count, summed module seconds, a per-module limit or a memory
limit), reply READY_FOR_VERIFICATION only after every one of them is measured and met on the
current files. Otherwise end the round with the NOTES handoff and do not reply READY: a
verification ends this lineage and takes hours on a large tree.
