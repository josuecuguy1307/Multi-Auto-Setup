#!/usr/bin/env bash
# Installs the local pre-commit hook that runs the conformance ratchet. Hooks are not versioned: run this once per clone.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
printf '#!/usr/bin/env bash\ncd "$(git rev-parse --show-toplevel)" && .venv/bin/python conformance/check_ratchet.py\n' > "$ROOT/.git/hooks/pre-commit"
chmod +x "$ROOT/.git/hooks/pre-commit"
echo "installed .git/hooks/pre-commit"
