#!/usr/bin/env bash
# Install or update Claude Code and Codex CLI for the current (non-root) user.
set -uo pipefail

export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin"

log() { printf '[ai-cli-update] %s\n' "$*"; }
failed=0

if command -v claude >/dev/null 2>&1; then
    log "Updating Claude Code…"
    timeout 15m claude update || failed=1
else
    log "Claude Code is missing; installing it…"
    timeout 15m bash -c 'curl -fsSL https://claude.ai/install.sh | bash' || failed=1
fi

if command -v codex >/dev/null 2>&1; then
    log "Updating Codex CLI…"
    timeout 15m codex update || failed=1
else
    log "Codex CLI is missing; installing it…"
    timeout 15m bash -c 'curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh' || failed=1
fi

if [ "$failed" -ne 0 ]; then
    log "One or more operations failed; systemd will record the failure."
    exit 1
fi

log "Done: $(claude --version 2>/dev/null || echo 'Claude unavailable'); $(codex --version 2>/dev/null || echo 'Codex unavailable')"
