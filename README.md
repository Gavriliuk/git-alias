# git-alias

A small collection of Git shortcuts and helper tools that grew out of my everyday workflow.

## Short aliases

The root `.gitconfig` contains a set of short aliases for commands I use frequently: status, checkout, commit, cherry-pick, diff, log, rebase, push, and a few common variations.

The goal is not to hide Git behind another interface, but simply to make repetitive command-line work faster.

## History editing

Seven history-editing commands are implemented by a single Python helper, `.git-helpers/git_history.py`:

- `git drop <commit>` — remove a commit from history.
- `git edit <commit> "<message>"` — change a commit message.
- `git hide <base> <head>` — absorb an older commit into a newer one.
- `git join <base> <head>` — absorb a newer commit into an older one.
- `git move <base> <head>` — move a newer commit down the history, immediately after `<base>`.
- `git take` — amend staged changes into HEAD while preserving the original commit metadata.
- `git trim <commit>` — reduce a commit message to its subject line.

The helper rewrites linear history while preserving the original author/committer metadata and adjusting local tags to the rewritten history.

If an operation stops because of a conflict:

```text
git <command> --continue
git <command> --abort
```

## Pre-commit hook

`.git-hooks/pre-commit.cpp` is a Windows pre-commit hook used to normalize changed source lines before they are committed.

For supported source files it normalizes line endings, replaces leading indentation tabs with spaces, and removes trailing spaces and tabs. It operates on staged changes and is designed to coexist with partially staged files.

Build it from a Visual Studio developer environment simply by running:

```cmd
cd .git-hooks
pre-commit-build.cmd
```

The build script uses `vswhere.exe` to locate the installed MSVC C++ tools and builds a static x64 C++17 `pre-commit.exe`.

The supplied `.git-hooks/.gitconfig` shows the repository-local `core.hooksPath` setup and also defines the normalizer executable in `pick.filter`.

## pick

`git pick` is a metadata-preserving cherry-pick helper:

```text
git pick <commit|range>...
```

It accepts individual commits and revision ranges, replays ranges from oldest to newest, and preserves the original commit metadata.

Because each replayed change is committed normally, the configured `pre-commit` hook is applied before the new commit is created. This is useful when importing old history: the original change is preserved while current source-formatting rules are applied automatically.

If normalization removes the whole change, `pick` simply skips that commit.

Interrupted operations can be resumed or controlled with:

```text
git pick --continue
git pick --skip
git pick --abort
```
