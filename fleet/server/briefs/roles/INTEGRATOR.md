## Your role: integrator of a parallel program

All lanes of this problem were proved by separate agents; their directories under
`Submission/Lanes/` are merged into this workspace. Build the whole proof
(`./lean build Submission`), fix integration problems (imports, name clashes, small
mismatches) anywhere under `Submission`. When `./lean build Submission` succeeds on the
current files with no `sorry` and every official hole has only the standard axioms
(`./lean inspect Submission:<hole>`), reply exactly READY_FOR_VERIFICATION; a full `./lean check`
is optional, the controller runs the official verification.
