#!/usr/bin/env bash
# Keep the brain vault (BRAIN_ROOT, default ~/brain) level with its origin remote.
#
# The vault has two writers: the MCP on this box, and Obsidian on the iPad via
# Working Copy. Neither knows about the other, so this is the only thing that
# makes them one vault. It commits whatever the MCP has written, rebases onto
# anything the iPad pushed, and pushes the result.
#
# Never destructive: a rebase that conflicts is aborted and reported, never
# resolved by guessing. Losing a note to an automated merge is worse than a
# loud failure sitting in the journal until it is dealt with by hand.
#
# Any exit 1 here reaches the phone: the unit carries OnFailure=g-alert@%n.
# A clean run clears the dedup key, so the next wedge alerts immediately
# instead of being swallowed by the six-hour cooldown.

set -uo pipefail

# git needs HOME to resolve config even though the credential helper for this
# repo is pinned to an explicit --file. systemd oneshots are not given one.
: "${HOME:=$(getent passwd "$(id -u)" | cut -d: -f6)}"
export HOME

VAULT="${BRAIN_ROOT:-$HOME/brain}"
BRANCH=main
REPO="$(dirname "$(readlink -f "$0")")"

cd "$VAULT" || { echo "vault missing at $VAULT" >&2; exit 1; }

if ! git fetch --quiet origin "$BRANCH"; then
  echo "brain-vault: fetch failed, check the credential store" >&2
  exit 1
fi

# The MCP writes continuously; anything sitting in the tree is its work.
if [[ -n "$(git status --porcelain)" ]]; then
  if ! git add -A; then
    echo "brain-vault: add failed, local writes were not committed" >&2
    exit 1
  fi
  if ! git diff --cached --quiet; then
    if ! git commit -q -m "vault: $(date -u +%Y-%m-%dT%H:%MZ)"; then
      echo "brain-vault: commit failed, local writes were not committed" >&2
      exit 1
    fi
  fi
fi

behind="$(git rev-list --count HEAD..origin/$BRANCH)"
if [[ "$behind" != "0" ]]; then
  if ! git rebase --quiet "origin/$BRANCH"; then
    git rebase --abort 2>/dev/null
    echo "brain-vault: $behind commit(s) from the iPad conflict with local writes." >&2
    echo "brain-vault: rebase aborted, nothing lost. Resolve in $VAULT by hand." >&2
    exit 1
  fi
fi

ahead="$(git rev-list --count "origin/$BRANCH"..HEAD)"
if [[ "$ahead" != "0" ]]; then
  if ! git push --quiet origin "$BRANCH"; then
    echo "brain-vault: push failed with $ahead commit(s) waiting" >&2
    exit 1
  fi
  echo "brain-vault: pushed $ahead commit(s)"
else
  echo "brain-vault: level"
fi

(cd "$REPO" && \
  .venv/bin/python -m unit_alert --clear brain-vault-sync.service) >/dev/null 2>&1 || true
