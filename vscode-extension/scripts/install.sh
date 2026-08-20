#!/usr/bin/env bash
# Install the Claude Usage VS Code extension on macOS / Linux / WSL.
# Usage:  ./scripts/install.sh [path/to/file.vsix]
# Picks the first .vsix in the extension root, or builds one if none exists.

set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

find_code_cli() {
    for name in code code-insiders; do
        if command -v "$name" >/dev/null 2>&1; then
            echo "$name"; return 0
        fi
    done
    for path in \
        "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" \
        "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code-insiders" \
    ; do
        [ -x "$path" ] && { echo "$path"; return 0; }
    done
    echo "Could not find VS Code CLI. Install VS Code or add 'code' to PATH." >&2
    return 1
}

vsix="${1-}"
if [ -z "$vsix" ]; then
    cd "$repo_root"
    [ -d node_modules ] || npm install
    rm -f *.vsix
    npm run package
    vsix="$(ls -t "$repo_root"/*.vsix 2>/dev/null | head -1 || true)"
fi

if [ -z "$vsix" ] || [ ! -f "$vsix" ]; then
    echo "No .vsix file found and packaging failed." >&2
    exit 1
fi

code_cli="$(find_code_cli)"
echo "Installing $vsix via $code_cli ..."
"$code_cli" --install-extension "$vsix" --force
echo "Done. Reload VS Code (Cmd+Shift+P → Reload Window) to see the Claude Usage sidebar."
