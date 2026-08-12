#!/usr/bin/env python

import json
import os
import subprocess
import sys
from pathlib import Path

STATE_FILE = 'pick-state.json'


def git(*args, check=True):
    result = subprocess.run(
        ['git', *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if check and result.returncode != 0:
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        raise RuntimeError(f"git {' '.join(args)} failed ({result.returncode})")

    return result


def git_dir():
    path = Path.cwd().resolve()

    while True:
        dot_git = path / '.git'

        if dot_git.is_dir():
            return dot_git

        if dot_git.is_file():
            line = dot_git.read_text(encoding='utf-8').strip()
            prefix = 'gitdir: '

            if not line.startswith(prefix):
                raise RuntimeError(f'invalid .git file: {dot_git}')

            git_path = Path(line[len(prefix):])

            if not git_path.is_absolute():
                git_path = dot_git.parent / git_path

            return git_path.resolve()

        if path.parent == path:
            raise RuntimeError('not inside a Git working tree')

        path = path.parent


def state_path():
    return git_dir() / STATE_FILE


def save_state(state):
    state_path().write_text(json.dumps(state, indent=2), encoding='utf-8')


def load_state():
    path = state_path()
    if not path.exists():
        raise RuntimeError('no pick operation in progress')
    return json.loads(path.read_text(encoding='utf-8'))


def clear_state():
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass


def is_clean():
    return git(
        'status', '--porcelain=v1', '--untracked-files=no'
    ).stdout == ''


def resolve_arguments(arguments):
    """
    Plain commit-ish arguments select exactly one commit each.
    Arguments containing '..' are expanded as Git revision ranges.
    Ranges are replayed oldest-to-newest.
    """
    commits = []
    seen = set()

    for argument in arguments:
        if '..' in argument:
            result = git(
                'rev-list', '--reverse', '--topo-order', argument
            ).stdout.splitlines()
            resolved = [line for line in result if line]
        else:
            resolved = [
                git(
                    'rev-parse', '--verify', argument + '^{commit}'
                ).stdout.strip()
            ]

        for commit in resolved:
            if commit not in seen:
                seen.add(commit)
                commits.append(commit)

    return commits


def is_merge_commit(commit):
    fields = git(
        'rev-list', '--parents', '-n', '1', commit
    ).stdout.strip().split()
    return len(fields) > 2


def subject(commit):
    return git('show', '-s', '--format=%s', commit).stdout.strip()


def staged_is_empty():
    return git('diff', '--cached', '--quiet', check=False).returncode == 0


def has_unmerged_entries():
    return bool(git('ls-files', '-u').stdout)


def commit_with_metadata(commit):
    author_name, author_email, author_date, committer_name, committer_email, committer_date = git(
        'show', '-s', '--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI', commit
    ).stdout.rstrip('\n').split('\0')

    author_email = author_email.replace('@actiatelecom.fr', '@actia.com')
    committer_email = committer_email.replace('@actiatelecom.fr', '@actia.com')

    env = os.environ.copy()
    env['GIT_COMMITTER_NAME'] = committer_name
    env['GIT_COMMITTER_EMAIL'] = committer_email
    env['GIT_COMMITTER_DATE'] = committer_date

    return subprocess.run(
        [
            'git', 'commit',
            '-C', commit,
            f'--author={author_name} <{author_email}>',
            f'--date={author_date}',
        ],
        env=env,
    )


def apply_queue(state):
    while state['position'] < len(state['commits']):
        commit = state['commits'][state['position']]
        number = state['position'] + 1
        total = len(state['commits'])

        print(
            f'pick: {number}/{total} '
            f'{commit[:12]} {subject(commit)}',
            flush=True,
        )

        if is_merge_commit(commit):
            save_state(state)
            raise RuntimeError(
                f'merge commit {commit} is not supported by this first version; '
                'cherry-pick it explicitly with -m'
            )

        state['phase'] = 'applying'
        save_state(state)

        result = subprocess.run(
            ['git', 'cherry-pick', '--no-commit', commit]
        )

        if result.returncode != 0:
            state['phase'] = 'commit'
            save_state(state)
            print(
                'pick: stopped for conflict; resolve it, '
                'stage the resolution, then run --continue',
                file=sys.stderr,
            )
            return result.returncode

        if staged_is_empty():
            print('pick: no changes; skipping')
            state['position'] += 1
            state['phase'] = 'idle'
            save_state(state)
            continue

        state['phase'] = 'commit'
        save_state(state)

        # -C preserves the original author/message while creating a new commit.
        result = commit_with_metadata(commit)

        if result.returncode != 0:
            # The pre-commit normalizer may have removed a formatting-only diff.
            if staged_is_empty() and not has_unmerged_entries():
                print(
                    'pick: changes disappeared after '
                    'pre-commit normalization; skipping'
                )
                state['position'] += 1
                state['phase'] = 'idle'
                save_state(state)
                continue

            print(
                'pick: commit failed; fix the problem '
                'and run --continue',
                file=sys.stderr,
            )
            return result.returncode

        state['position'] += 1
        state['phase'] = 'idle'
        save_state(state)

    clear_state()
    print('pick: done')
    return 0


def start(arguments):
    if state_path().exists():
        raise RuntimeError('pick operation already in progress')

    if not is_clean():
        raise RuntimeError('working tree/index is not clean')

    commits = resolve_arguments(arguments)

    if not commits:
        print('pick: no commits selected')
        return 0

    state = {
        'original_head': git('rev-parse', 'HEAD').stdout.strip(),
        'commits': commits,
        'position': 0,
        'phase': 'idle',
    }

    save_state(state)
    return apply_queue(state)


def continue_operation():
    state = load_state()

    if state['phase'] == 'commit':
        if has_unmerged_entries():
            raise RuntimeError('unresolved conflicts remain')

        commit = state['commits'][state['position']]

        if staged_is_empty():
            print('pick: no changes; skipping current commit')
        else:
            result = commit_with_metadata(commit)

            if result.returncode != 0:
                if not (staged_is_empty() and not has_unmerged_entries()):
                    return result.returncode
                print(
                    'pick: changes disappeared after '
                    'pre-commit normalization; skipping'
                )

        state['position'] += 1
        state['phase'] = 'idle'
        save_state(state)

    return apply_queue(state)


def abort_operation():
    state = load_state()

    # Clear Git's own cherry-pick conflict state if one exists.
    subprocess.run(
        ['git', 'cherry-pick', '--abort'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    subprocess.run(
        ['git', 'reset', '--hard', state['original_head']],
        check=True,
    )

    clear_state()
    print('pick: aborted')
    return 0



def skip_operation():
    state = load_state()

    if state['position'] >= len(state['commits']):
        raise RuntimeError('no current commit to skip')

    if state['phase'] == 'idle':
        raise RuntimeError('current commit is not stopped; nothing to skip')

    commit = state['commits'][state['position']]

    # Clear Git's own cherry-pick conflict state if one exists.
    subprocess.run(
        ['git', 'cherry-pick', '--abort'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Discard all staged/working-tree changes belonging to the current
    # uncommitted cherry-pick. Previously completed picks are already
    # committed, so HEAD is exactly the state to return to.
    subprocess.run(
        ['git', 'reset', '--hard', 'HEAD'],
        check=True,
    )

    print(
        f'pick: skipping {commit[:12]} {subject(commit)}'
    )

    state['position'] += 1
    state['phase'] = 'idle'
    save_state(state)

    return apply_queue(state)


def main():
    arguments = sys.argv[1:]

    if not arguments:
        raise RuntimeError(
            'usage: pick <commit|range>... | '
            '--continue | --skip | --abort'
        )

    if arguments == ['--continue']:
        return continue_operation()

    if arguments == ['--abort']:
        return abort_operation()

    if arguments == ['--skip']:
        return skip_operation()

    if arguments[0].startswith('--'):
        raise RuntimeError(f'unsupported option: {arguments[0]}')

    return start(arguments)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'pick: ERROR: {error}', file=sys.stderr)
        sys.exit(1)
