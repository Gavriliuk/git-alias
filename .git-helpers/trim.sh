#!/usr/bin/env bash
set -euo pipefail
trim="${1:-}"

if [[ -z "$trim" ]]; then
  echo "Usage: git trim <commit-to-trim>" >&2
  exit 2
fi

msg="$(git rev-parse --git-dir)/COMMIT_EDITMSG" || { echo "Not a git repo" >&2; exit 2; }
tgt="$(git symbolic-ref --quiet --short HEAD)" || { echo "Not a branch" >&2; exit 2; }

tmp="tmp-$(date +%s)"
trap 'git br -D "$tmp" >/dev/null 2>&1 || true' EXIT

git co -b "$tmp"
git reset --hard "$trim"
git log -1 --format=%s HEAD>$msg
git ca -F $msg
git ro "$tmp" "$trim" "$tgt"
