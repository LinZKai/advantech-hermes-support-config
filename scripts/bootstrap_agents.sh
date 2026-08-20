#!/usr/bin/env bash
# Installs the repo's AGENTS.md baseline into the runtime sandbox.
#
# repo:    $HERMES_SUPPORT_CONFIG_DIR/AGENTS.md   (default /sandbox/hermes-support-config/AGENTS.md)
# runtime: $HERMES_RUNTIME_DIR/AGENTS.md           (default /sandbox/AGENTS.md)
#
# This is a one-way COPY, not a symlink: the runtime file becomes the
# POC-editable surface for a future Curator, and any change it makes is
# expected to be discarded on the next sandbox reset without touching the repo.
#
# Refuses to install (without deleting anything) if a higher-priority project
# context file already exists in the runtime dir (.hermes.md, HERMES.md).
set -euo pipefail

REPO_DIR="${HERMES_SUPPORT_CONFIG_DIR:-/sandbox/hermes-support-config}"
RUNTIME_DIR="${HERMES_RUNTIME_DIR:-/sandbox}"

REPO_AGENTS="$REPO_DIR/AGENTS.md"
RUNTIME_AGENTS="$RUNTIME_DIR/AGENTS.md"

if [ ! -f "$REPO_AGENTS" ]; then
    echo "bootstrap_agents: repo AGENTS.md not found at $REPO_AGENTS" >&2
    exit 1
fi

for higher_priority in "$RUNTIME_DIR/.hermes.md" "$RUNTIME_DIR/HERMES.md"; do
    if [ -e "$higher_priority" ]; then
        echo "bootstrap_agents: refusing to install -- higher-priority project context already exists at $higher_priority" >&2
        exit 1
    fi
done

if [ ! -d "$RUNTIME_DIR" ]; then
    echo "bootstrap_agents: runtime dir not found at $RUNTIME_DIR" >&2
    exit 1
fi

TMP_FILE="$(mktemp "$RUNTIME_DIR/.AGENTS.md.XXXXXX")"
cp "$REPO_AGENTS" "$TMP_FILE"
mv -f "$TMP_FILE" "$RUNTIME_AGENTS"

if [ ! -f "$RUNTIME_AGENTS" ]; then
    echo "bootstrap_agents: install failed -- $RUNTIME_AGENTS not found after move" >&2
    exit 1
fi

REPO_HASH="$(sha256sum "$REPO_AGENTS" | cut -d' ' -f1)"
RUNTIME_HASH="$(sha256sum "$RUNTIME_AGENTS" | cut -d' ' -f1)"

if [ "$REPO_HASH" != "$RUNTIME_HASH" ]; then
    echo "bootstrap_agents: hash mismatch after install ($REPO_HASH != $RUNTIME_HASH)" >&2
    exit 1
fi

echo "bootstrap_agents: installed $RUNTIME_AGENTS (sha256 $RUNTIME_HASH)"
