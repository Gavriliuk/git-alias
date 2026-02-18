#!/usr/bin/env bash
set -euo pipefail
drop="${1:-}"; target="${2:-test}"

if [[ -z "$drop" ]]; then
  echo "Usage: git drop <commit-to-drop> [target-branch]" >&2
  exit 2
fi

git rev-parse --git-dir >/dev/null || { echo "Not a git repo" >&2; exit 2; }

tmp="tmp-$(date +%s)"
trap 'git br -D "$tmp" >/dev/null 2>&1 || true' EXIT

git co -b "$tmp"
git reset --hard "$drop"
git reset --hard HEAD^1
git ro "$tmp" "$drop" "$target"
