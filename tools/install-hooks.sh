#!/bin/sh
# Install the pre-commit hook that runs the scrubber on staged files (uses your private denylist if present).
set -eu
root=$(git rev-parse --show-toplevel)
hook="$root/.git/hooks/pre-commit"
cat > "$hook" <<'HOOK'
#!/bin/sh
exec python3 "$(git rev-parse --show-toplevel)/tools/scrub.py" --staged
HOOK
chmod +x "$hook"
echo "installed $hook"
