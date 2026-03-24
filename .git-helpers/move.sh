#!/usr/bin/env bash
set -euo pipefail
base="${1:-}"; head="${2:-}"

if [[ -z "$base" || -z "$head" ]]; then
  echo "Usage: git move <base-commit> <head-commit>" >&2
  exit 2
fi

git rev-parse --git-dir >/dev/null || { echo "Not a git repo" >&2; exit 2; }
tgt="$(git symbolic-ref --quiet --short HEAD)" || { echo "Not a branch" >&2; exit 2; }

tmp="tmp-$(date +%s)"
trap 'git br -D "$tmp" >/dev/null 2>&1 || true' EXIT

git co -b "$tmp"
git reset --hard "$base"
git cp "$head"
git ro "$tmp" "$base" "$tgt"
