# Security

## What never enters the repository

- credentials, tokens, keys, or paths to them;
- host names, IP addresses, user names, personal e-mail addresses;
- run logs, event streams, evolve datasets, databases;
- per-problem operator notes, and other systems' code or notes.

`tools/scrub.py` enforces this in CI (the whole tree, every blob and commit message in the history, and the
commit e-mails) and in the pre-commit hook (`tools/install-hooks.sh`: staged files plus a private denylist
kept outside the repository). Findings name the rule, the file and the line, never the matched text.
Third-party sources (the benchmark, its tools, bank sources) are fetched at pinned commits at run time and
never vendored.

## Credentials at run time

- An account is a secret reference (`file:` or `env:`), resolved by `runtime/leaneval/secret_refs.py` in the
  supervisor, outside the agent's sandbox. The feeder and the coordinator handle labels only; deploy checks
  that token files exist with mode 600 and never reads them.
- The CLI runs with a fresh environment: a private empty `HOME`, no inherited `ANTHROPIC_*` or cloud
  variables, the token never in the environment or on a command line.
- For the round, the supervisor writes the token into the CLI's own credentials file in a per-round
  configuration directory outside the job tree, and removes it when the round ends.

**Known limitation.** The CLI has to read that credentials file, and the agent's shell runs inside the same
Landlock domain as the CLI, so the agent can read the token during a round. The system prompt forbids it,
and a **tripwire** watches for it: the supervisor inspects every tool call in the CLI's event stream, and a
Bash command or a file path that reaches for the credentials file, the CLI's configuration directory (its
XDG cache excepted), a process environment (`/proc/*/environ`) or the account directory stops the CLI,
removes the credentials file, records `SECURITY-EVENT.json` in the job and ends the round with stop reason
`credential_access`. The controller halts that queue (`halted_credential_access`, also when the round's
finalization fails) and starts no continuation; the feeder never restarts it, blocks the account for new
work and puts a STOP on every other runner of the host that uses the same account. Rotate the account
(`claude setup-token`), remove the STOP files and restart the units by hand.

The tripwire is detection, not prevention. It sees the command text and paths an agent asks for, so a
script written first and run later, or a Lean program reading the file, passes unseen; and when it fires,
the call has already run. Treat the token as exposed whenever it fires. The complete fix, a credential proxy
outside the sandbox so the CLI never holds the token, needs a validation run with the real CLI and is not
part of this release.

Use accounts dedicated to the fleet, and rotate a token whenever it may have been exposed.

## The agent's sandbox

landrun (Landlock, the kernel's best ABI) around the CLI: the job's workspace and `/tmp` are writable; the
workbench, the toolchains and the runtime are read-only and executable; `/proc`, `/sys` and `/dev` read-only;
the network is open (agents fetch licensed sources and documentation). The tools are Bash, Read, Edit,
Write, Glob and Grep; no MCP servers, no slash commands, no subagents. `./lean` refuses edits outside a
lane's directory and any change to the trusted files.

## Acceptance

Only an official verification receipt counts: a clean pinned workspace, the toolchain's `lake comparator`
in bubblewrap, the Lean kernel and NanoDa, exit 0, both kernels' acceptance lines, inputs unchanged, and the
receipt in a registry the agent cannot write. Model output, NOTES.md and gate decisions never count. The
verification controls (`leh controls`) show the chain accepts a correct proof and rejects `sorry`, a new
axiom, a weakened statement and a missing proof.

## Reporting

Report issues privately to the repository owners. Never open a public issue for a security finding.
