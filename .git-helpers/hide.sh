#!/usr/bin/env bash
set -euo pipefail
base="${1:-}"; head="${2:-}"; target="${3:-test}"

if [[ -z "$base" || -z "$head" ]]; then
  echo "Usage: git hide <base-commit> <head-commit> [target-branch]" >&2
  exit 2
fi

git rev-parse --git-dir >/dev/null || { echo "Not a git repo" >&2; exit 2; }

tmp="tmp-$(date +%s)"
trap 'git br -D "$tmp" >/dev/null 2>&1 || true' EXIT

git co -b "$tmp"
git reset --hard "$base"
git reset --soft HEAD^1
git stash
[ "$(git rev-list --count "$base".."$head"^)" -gt 0 ] && git cps "$base".."$head"^
git stash pop
git add -A
git cc "$head"
git show -s --format=%B "$head" | git ci -F -
git ro "$tmp" "$head" "$target"
