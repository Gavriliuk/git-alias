#!/usr/bin/env bash
set -euo pipefail
hash="${1:-}"; text="${2:-}"

if [[ -z "$hash" || -z "$text" ]]; then
  echo "Usage: git edit <commit-to-edit> <new-commit-message>" >&2
  exit 2
fi

msg="$(git rev-parse --git-dir)/COMMIT_EDITMSG" || { echo "Not a git repo" >&2; exit 2; }
tgt="$(git symbolic-ref --quiet --short HEAD)" || { echo "Not a branch" >&2; exit 2; }

tmp="tmp-$(date +%s)"
trap 'git br -D "$tmp" >/dev/null 2>&1 || true' EXIT

git co -b "$tmp"
git reset --hard "$hash"
git ca -m "$text"
git ro "$tmp" "$hash" "$tgt"
