#!/usr/bin/env python

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class GitFailure(RuntimeError):
    def __init__(self, message: str, returncode: int = 1):
        super().__init__(message)
        self.returncode = returncode


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        env.update(extra)
    return env


def git(
    *args: str,
    capture: bool = False,
    check: bool = True,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=_git_env(env),
        check=False,
    )
    if check and result.returncode != 0:
        if capture and result.stderr:
            sys.stderr.buffer.write(result.stderr)
        raise GitFailure(f"git {' '.join(args)} failed", result.returncode)
    return result


def git_text(*args: str) -> str:
    result = git(*args, capture=True)
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def resolve_commit(value: str) -> str:
    return git_text("rev-parse", "--verify", f"{value}^{{commit}}")


def current_branch() -> str:
    result = git("symbolic-ref", "--quiet", "--short", "HEAD", capture=True, check=False)
    if result.returncode != 0:
        raise GitFailure("Not on a branch", 2)
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def ensure_repo() -> None:
    result = git("rev-parse", "--git-dir", capture=True, check=False)
    if result.returncode != 0:
        raise GitFailure("Not a git repo", 2)


def git_path(name: str) -> Path:
    return Path(git_text("rev-parse", "--git-path", name)).resolve()


def state_path() -> Path:
    return git_path("python-history-rewrite-state.json")


def load_state() -> dict[str, Any] | None:
    path = state_path()
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temp.replace(path)


def delete_state() -> None:
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass


def ensure_clean() -> None:
    worktree = git("diff", "--quiet", check=False)
    index = git("diff", "--cached", "--quiet", check=False)
    if worktree.returncode != 0 or index.returncode != 0:
        raise GitFailure("Working tree or index has uncommitted changes", 2)


def has_unmerged_paths() -> bool:
    return bool(git_text("ls-files", "-u"))


def has_unstaged_changes() -> bool:
    return git("diff", "--quiet", check=False).returncode != 0


def has_staged_changes() -> bool:
    return git("diff", "--cached", "--quiet", check=False).returncode != 0


def parents(commit: str) -> list[str]:
    fields = git_text("rev-list", "--parents", "-n", "1", commit).split()
    return fields[1:]


def first_parent_or_none(commit: str) -> str | None:
    values = parents(commit)
    if not values:
        return None
    if len(values) != 1:
        raise GitFailure(f"Merge commit {commit[:12]} is not supported", 2)
    return values[0]


def first_parent(commit: str) -> str:
    parent = first_parent_or_none(commit)
    if parent is None:
        raise GitFailure(f"Commit {commit[:12]} has no parent", 2)
    return parent


def first_parent_path_after(ancestor: str, tip: str) -> list[str]:
    if ancestor == tip:
        return []

    result = git(
        "rev-list",
        "--first-parent",
        "--reverse",
        "--parents",
        f"{ancestor}..{tip}",
        capture=True,
    )
    expected_parent = ancestor
    commits: list[str] = []

    for raw_line in result.stdout.splitlines():
        fields = raw_line.decode("ascii").split()
        commit = fields[0]
        commit_parents = fields[1:]
        if len(commit_parents) != 1:
            raise GitFailure(f"Merge commit {commit[:12]} is not supported", 2)
        if commit_parents[0] != expected_parent:
            raise GitFailure(
                f"Commit {ancestor[:12]} is not on the first-parent history of {tip[:12]}",
                2,
            )
        commits.append(commit)
        expected_parent = commit

    if expected_parent != tip:
        raise GitFailure(
            f"Commit {ancestor[:12]} is not on the first-parent history of {tip[:12]}",
            2,
        )
    return commits


def commit_data(commit: str) -> tuple[dict[str, str], bytes]:
    raw = git("cat-file", "commit", commit, capture=True).stdout
    header, separator, message = raw.partition(b"\n\n")
    if not separator:
        raise GitFailure(f"Cannot parse commit {commit[:12]}", 2)

    author = None
    committer = None
    for line in header.splitlines():
        if line.startswith(b"author "):
            author = line[7:]
        elif line.startswith(b"committer "):
            committer = line[10:]

    if author is None or committer is None:
        raise GitFailure(f"Cannot read author/committer metadata from {commit[:12]}", 2)

    def split_identity(value: bytes) -> tuple[str, str, str]:
        left, timestamp, timezone = value.rsplit(b" ", 2)
        name, email = left.rsplit(b" <", 1)
        if not email.endswith(b">"):
            raise GitFailure(f"Cannot parse identity metadata from {commit[:12]}", 2)
        return (
            name.decode("utf-8", "surrogateescape"),
            email[:-1].decode("utf-8", "surrogateescape"),
            timestamp.decode("ascii") + " " + timezone.decode("ascii"),
        )

    author_name, author_email, author_date = split_identity(author)
    committer_name, committer_email, committer_date = split_identity(committer)
    env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": author_date,
        "GIT_COMMITTER_NAME": committer_name,
        "GIT_COMMITTER_EMAIL": committer_email,
        "GIT_COMMITTER_DATE": committer_date,
    }
    return env, message


def commit_from_source(source: str) -> None:
    env, message = commit_data(source)
    git(
        "commit",
        "--quiet",
        "--no-verify",
        "--no-gpg-sign",
        "--cleanup=verbatim",
        "-F",
        "-",
        input_bytes=message,
        env=env,
    )


def amend_message(source: str, message: str) -> None:
    env, _ = commit_data(source)
    git(
        "commit",
        "--quiet",
        "--amend",
        "--allow-empty",
        "--no-verify",
        "--no-gpg-sign",
        "--cleanup=verbatim",
        "-m",
        message,
        env=env,
    )


def amend_no_edit(source: str) -> None:
    env, _ = commit_data(source)
    git(
        "commit",
        "--quiet",
        "--amend",
        "--allow-empty",
        "--no-edit",
        "--no-verify",
        "--no-gpg-sign",
        env=env,
    )


def make_temp_branch(command: str) -> str:
    return f"python-{command}-{os.getpid()}-{int(time.time() * 1000)}"


def action_replay(source: str) -> dict[str, Any]:
    return {"kind": "replay", "source": source}


def action_replay_range(sources: list[str]) -> dict[str, Any] | None:
    if not sources:
        return None
    return {"kind": "replay-range", "sources": sources}


def action_apply(source: str) -> dict[str, Any]:
    return {"kind": "apply", "source": source}


def action_amend_message(source: str, message: str) -> dict[str, Any]:
    return {"kind": "amend-message", "source": source, "message": message}


def action_amend_no_edit(source: str) -> dict[str, Any]:
    return {"kind": "amend-no-edit", "source": source}


def action_commit_combined(source: str) -> dict[str, Any]:
    return {"kind": "commit-combined", "source": source}


def compact_actions(*actions: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [action for action in actions if action is not None]


def _append_entry(state: dict[str, Any], source: str, temp_commit: str, drop_if_empty: bool) -> None:
    state.setdefault("entries", []).append(
        {"source": source, "temp": temp_commit, "drop_if_empty": drop_if_empty}
    )


def _tag_object_info(ref: str, object_id: str, object_type: str) -> None:
    if object_type == "commit":
        return
    if object_type != "tag":
        raise GitFailure(f"Tag {ref.removeprefix('refs/tags/')} does not point to a commit", 2)

    raw = git("cat-file", "tag", object_id, capture=True).stdout
    header, separator, body = raw.partition(b"\n\n")
    if not separator:
        raise GitFailure(f"Cannot parse annotated tag {ref.removeprefix('refs/tags/')}", 2)

    tag_type = None
    for line in header.splitlines():
        if line.startswith(b"type "):
            tag_type = line[5:]
            break
    if tag_type != b"commit":
        raise GitFailure(
            f"Annotated tag {ref.removeprefix('refs/tags/')} does not point directly to a commit",
            2,
        )

    if b"-----BEGIN PGP SIGNATURE-----" in body or b"-----BEGIN SSH SIGNATURE-----" in body:
        raise GitFailure(
            f"Signed annotated tag {ref.removeprefix('refs/tags/')} cannot be retargeted safely",
            2,
        )


def capture_tag_moves(tag_targets: dict[str, str]) -> list[dict[str, str]]:
    if not tag_targets:
        return []

    result = git(
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(objecttype)%00%(*objectname)%00%(*objecttype)",
        "refs/tags",
        capture=True,
    )
    plans: list[dict[str, str]] = []

    for raw_line in result.stdout.splitlines():
        fields = raw_line.split(b"\0")
        if len(fields) != 5:
            continue
        ref = fields[0].decode("utf-8", "surrogateescape")
        object_id = fields[1].decode("ascii")
        object_type = fields[2].decode("ascii")
        peeled_id = fields[3].decode("ascii")
        peeled_type = fields[4].decode("ascii")

        if object_type == "commit":
            source_commit = object_id
        elif object_type == "tag" and peeled_type == "commit":
            source_commit = peeled_id
        else:
            continue

        if source_commit not in tag_targets:
            continue

        _tag_object_info(ref, object_id, object_type)
        plans.append(
            {
                "ref": ref,
                "old_object": object_id,
                "object_type": object_type,
                "source_commit": source_commit,
                "target_source": tag_targets[source_commit],
            }
        )
    return plans


def _retarget_annotated_tag(object_id: str, new_commit: str) -> str:
    raw = git("cat-file", "tag", object_id, capture=True).stdout
    lines = raw.splitlines(keepends=True)
    if not lines or not lines[0].startswith(b"object "):
        raise GitFailure(f"Cannot parse annotated tag object {object_id[:12]}", 2)
    lines[0] = b"object " + new_commit.encode("ascii") + b"\n"
    rewritten = b"".join(lines)
    return git(
        "hash-object",
        "-t",
        "tag",
        "-w",
        "--stdin",
        capture=True,
        input_bytes=rewritten,
    ).stdout.decode("ascii").strip()


def _mapped_commit(state: dict[str, Any], source: str) -> str:
    return state.get("mapping", {}).get(source, source)


def _prepare_tag_updates(state: dict[str, Any]) -> list[tuple[str, str, str]]:
    updates: list[tuple[str, str, str]] = []
    for plan in state.get("tag_moves", []):
        target_commit = _mapped_commit(state, plan["target_source"])
        old_object = plan["old_object"]
        if plan["object_type"] == "commit":
            new_object = target_commit
        else:
            new_object = _retarget_annotated_tag(old_object, target_commit)

        if new_object != old_object:
            updates.append((plan["ref"], new_object, old_object))
    return updates


def _print_conflict_help(state: dict[str, Any]) -> None:
    command = state["command"]
    print(
        f"{command}: stopped for conflict; resolve it, stage the resolution, "
        f"then run: git {command} --continue",
        file=sys.stderr,
    )
    print(f"{command}: to cancel the operation, run: git {command} --abort", file=sys.stderr)


def _validate_continue_state(state: dict[str, Any], command: str) -> None:
    if state.get("command") != command:
        raise GitFailure(
            f"A '{state.get('command')}' operation is already in progress; "
            f"use git {state.get('command')} --continue or --abort",
            2,
        )
    if state.get("conflict") == "rebase" and _rebase_in_progress():
        return
    branch = current_branch()
    if branch != state.get("temp_branch"):
        raise GitFailure(
            f"Expected temporary branch {state.get('temp_branch')}, but current branch is {branch}",
            2,
        )


def _rebase_in_progress() -> bool:
    return git_path("rebase-merge").exists() or git_path("rebase-apply").exists()


def _rebase_env(action: dict[str, Any]) -> dict[str, str]:
    env, _ = commit_data(action["sources"][0])
    return {
        "GIT_COMMITTER_NAME": env["GIT_COMMITTER_NAME"],
        "GIT_COMMITTER_EMAIL": env["GIT_COMMITTER_EMAIL"],
        "GIT_EDITOR": "true",
    }


def _action_commit_count(action: dict[str, Any]) -> int:
    kind = action["kind"]
    if kind == "replay-range":
        return len(action["sources"])
    if kind in ("replay", "amend-message", "amend-no-edit", "commit-combined"):
        return 1
    return 0


def _progress_total(actions: list[dict[str, Any]]) -> int:
    return sum(_action_commit_count(action) for action in actions)


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _show_progress(state: dict[str, Any], current: int) -> None:
    total = state.get("progress_total", 0)
    if total <= 0:
        return

    current = min(current, total)
    last = state.get("progress_reported", 0)
    if current == last:
        return

    percent = current * 100.0 / total
    started_at = state.get("started_at", time.time())
    elapsed = _format_elapsed(time.time() - started_at)
    text = f"Commit {current} of {total} ({percent:.1f}%)  Elapsed {elapsed}"
    if sys.stderr.isatty():
        sys.stderr.write("\r" + text)
    else:
        if current != total and current - last < 100:
            return
        sys.stderr.write(text + "\n")
    state["progress_reported"] = current
    sys.stderr.flush()


def _end_progress_line() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()


def _finish_action_progress(state: dict[str, Any], action: dict[str, Any]) -> None:
    count = _action_commit_count(action)
    if count <= 0:
        return
    state["progress_done"] = state.get("progress_done", 0) + count
    _show_progress(state, state["progress_done"])


def _run_rebase_with_progress(
    args: list[str],
    state: dict[str, Any],
    action: dict[str, Any],
    env: dict[str, str],
) -> int:
    process = subprocess.Popen(
        ["git", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_git_env(env),
    )
    assert process.stderr is not None

    buffer = b""
    progress_start = state.get("progress_done", 0)
    progress_pattern = re.compile(rb"Rebasing \((\d+)/(\d+)\)")

    def handle_segment(segment: bytes) -> None:
        if not segment:
            return
        match = progress_pattern.search(segment)
        if match:
            local_current = int(match.group(1))
            _show_progress(state, progress_start + local_current)
            return

        text = segment.decode("utf-8", "surrogateescape")
        if text.startswith("Successfully rebased and updated "):
            return
        _end_progress_line()
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    while True:
        chunk = os.read(process.stderr.fileno(), 4096)
        if not chunk:
            break
        buffer += chunk
        while True:
            cr = buffer.find(b"\r")
            lf = buffer.find(b"\n")
            positions = [pos for pos in (cr, lf) if pos >= 0]
            if not positions:
                break
            pos = min(positions)
            handle_segment(buffer[:pos])
            buffer = buffer[pos + 1 :]

    if buffer:
        handle_segment(buffer)

    return process.wait()


def _record_rebase_entries(state: dict[str, Any], action: dict[str, Any]) -> None:
    sources = action["sources"]
    onto = action["onto"]
    rewritten = first_parent_path_after(onto, resolve_commit("HEAD"))
    if len(rewritten) != len(sources):
        raise GitFailure(
            "Native rebase did not preserve the expected one-to-one commit sequence; "
            "the operation was left on the temporary branch for inspection",
            2,
        )
    for source, temp_commit in zip(sources, rewritten):
        _append_entry(state, source, temp_commit, True)


def _start_rebase_action(state: dict[str, Any], action: dict[str, Any]) -> bool:
    sources = action["sources"]
    onto = resolve_commit("HEAD")
    upstream = first_parent(sources[0])
    action["onto"] = onto
    action["upstream"] = upstream
    save_state(state)

    git("reset", "--hard", "--quiet", sources[-1])
    returncode = _run_rebase_with_progress(
        [
            "rebase",
            "--no-verify",
            "--reapply-cherry-picks",
            "--keep-empty",
            "--empty=keep",
            "--onto",
            onto,
            upstream,
            state["temp_branch"],
        ],
        state,
        action,
        _rebase_env(action),
    )
    if returncode != 0:
        if not _rebase_in_progress():
            raise GitFailure("git rebase failed before starting a resumable rebase", returncode)
        state["conflict"] = "rebase"
        save_state(state)
        _print_conflict_help(state)
        return False

    _record_rebase_entries(state, action)
    return True


def _continue_rebase_action(state: dict[str, Any], action: dict[str, Any]) -> bool:
    returncode = _run_rebase_with_progress(
        ["rebase", "--continue"],
        state,
        action,
        _rebase_env(action),
    )
    if returncode != 0:
        if not _rebase_in_progress():
            raise GitFailure("git rebase --continue failed", returncode)
        state["conflict"] = "rebase"
        save_state(state)
        _print_conflict_help(state)
        return False

    _record_rebase_entries(state, action)
    return True


def _finish_manual_conflict(state: dict[str, Any], action: dict[str, Any]) -> None:
    if has_unmerged_paths():
        raise GitFailure("Conflicts are still unresolved", 2)
    if has_unstaged_changes():
        raise GitFailure("Resolved changes are not fully staged; run git add first", 2)

    kind = action["kind"]
    if kind == "replay":
        if has_staged_changes():
            commit_from_source(action["source"])
        _append_entry(state, action["source"], resolve_commit("HEAD"), True)
    elif kind == "apply":
        pass
    else:
        raise GitFailure(f"Internal error: unexpected conflicted action {kind}", 2)


def _finish_conflicted_action(state: dict[str, Any]) -> bool:
    action = state["actions"][state["index"]]
    if state.get("conflict") == "rebase":
        if not _continue_rebase_action(state, action):
            return False
    else:
        _finish_manual_conflict(state, action)

    state["conflict"] = False
    _finish_action_progress(state, action)
    state["index"] += 1
    save_state(state)
    return True


def _run_action(state: dict[str, Any], action: dict[str, Any]) -> bool:
    kind = action["kind"]

    if kind == "replay-range":
        return _start_rebase_action(state, action)

    if kind in ("replay", "apply"):
        result = git("cherry-pick", "--no-commit", action["source"], check=False)
        if result.returncode != 0:
            state["conflict"] = "manual"
            save_state(state)
            _print_conflict_help(state)
            return False

        if kind == "replay":
            if has_staged_changes():
                commit_from_source(action["source"])
            _append_entry(state, action["source"], resolve_commit("HEAD"), True)
        return True

    if kind == "amend-message":
        amend_message(action["source"], action["message"])
        _append_entry(state, action["source"], resolve_commit("HEAD"), False)
        return True

    if kind == "amend-no-edit":
        amend_no_edit(action["source"])
        _append_entry(state, action["source"], resolve_commit("HEAD"), False)
        return True

    if kind == "commit-combined":
        if not has_staged_changes():
            raise GitFailure("Combined commit is empty", 1)
        commit_from_source(action["source"])
        _append_entry(state, action["source"], resolve_commit("HEAD"), False)
        return True

    raise GitFailure(f"Internal error: unknown action {kind}", 2)


def _cat_file_batch(object_ids: list[str]) -> dict[str, bytes]:
    unique = list(dict.fromkeys(object_ids))
    if not unique:
        return {}

    request = b"".join(object_id.encode("ascii") + b"\n" for object_id in unique)
    result = git("cat-file", "--batch", capture=True, input_bytes=request)
    data = result.stdout
    offset = 0
    objects: dict[str, bytes] = {}

    for requested in unique:
        line_end = data.find(b"\n", offset)
        if line_end < 0:
            raise GitFailure("Unexpected end of git cat-file --batch output", 2)
        header = data[offset:line_end].split()
        offset = line_end + 1
        if len(header) != 3 or header[1] != b"commit":
            raise GitFailure(f"Expected commit object {requested[:12]}", 2)
        size = int(header[2])
        body = data[offset : offset + size]
        offset += size
        if data[offset : offset + 1] != b"\n":
            raise GitFailure("Malformed git cat-file --batch output", 2)
        offset += 1
        objects[requested] = body
    return objects


def _parse_commit_object(raw: bytes) -> dict[str, Any]:
    header, separator, message = raw.partition(b"\n\n")
    if not separator:
        raise GitFailure("Cannot parse commit object", 2)

    tree = None
    author = None
    committer = None
    encoding = None
    for line in header.splitlines():
        if line.startswith(b"tree "):
            tree = line[5:].decode("ascii")
        elif line.startswith(b"author "):
            author = line
        elif line.startswith(b"committer "):
            committer = line
        elif line.startswith(b"encoding "):
            encoding = line

    if tree is None or author is None or committer is None:
        raise GitFailure("Commit object is missing required metadata", 2)
    return {
        "tree": tree,
        "author": author,
        "committer": committer,
        "encoding": encoding,
        "message": message,
    }


def _fast_import_rebuild(state: dict[str, Any]) -> tuple[str, str | None]:
    entries = state.get("entries", [])
    base_commit = state["base_commit"]
    if not entries:
        state["mapping"] = {}
        if base_commit is None:
            raise GitFailure("Operation would create an empty history", 2)
        return base_commit, None

    object_ids = [] if base_commit is None else [base_commit]
    for entry in entries:
        object_ids.extend([entry["source"], entry["temp"]])
    raw_objects = _cat_file_batch(object_ids)
    parsed = {object_id: _parse_commit_object(raw) for object_id, raw in raw_objects.items()}

    previous_tree = parsed[base_commit]["tree"] if base_commit is not None else None
    previous_ref = base_commit
    next_mark = 1
    stream = bytearray()
    result_ref = f"refs/git-history/result-{os.getpid()}-{int(time.time() * 1000)}"
    source_refs: dict[str, str] = {}
    created_marks: list[int] = []

    for entry in entries:
        source = entry["source"]
        temp_commit = entry["temp"]
        temp_info = parsed[temp_commit]

        if (
            entry["drop_if_empty"]
            and previous_tree is not None
            and temp_info["tree"] == previous_tree
        ):
            assert previous_ref is not None
            source_refs[source] = previous_ref
            continue

        source_info = parsed[source]
        mark = next_mark
        next_mark += 1
        created_marks.append(mark)

        stream.extend(f"commit {result_ref}\nmark :{mark}\n".encode("ascii"))
        stream.extend(source_info["author"] + b"\n")
        stream.extend(source_info["committer"] + b"\n")
        if source_info["encoding"] is not None:
            stream.extend(source_info["encoding"] + b"\n")
        message = temp_info["message"]
        stream.extend(f"data {len(message)}\n".encode("ascii"))
        stream.extend(message)
        stream.extend(b"\n")
        if previous_ref is not None:
            stream.extend(f"from {previous_ref}\n".encode("ascii"))
        stream.extend(b"deleteall\n")
        stream.extend(f'M 040000 {temp_info["tree"]} ""\n\n'.encode("ascii"))

        previous_ref = f":{mark}"
        previous_tree = temp_info["tree"]
        source_refs[source] = previous_ref

    if not created_marks:
        if base_commit is None:
            raise GitFailure("Internal error: root rewrite produced no commit", 2)
        mapping = {source: base_commit for source in source_refs}
        state["mapping"] = mapping
        return base_commit, None

    stream.extend(b"done\n")
    marks_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            delete=False,
            dir=str(state_path().parent),
            prefix="git-history-marks-",
        ) as marks_file:
            marks_path = marks_file.name

        try:
            git(
                "fast-import",
                "--quiet",
                "--force",
                f"--export-marks={marks_path}",
                input_bytes=bytes(stream),
            )
        except Exception:
            git("update-ref", "-d", result_ref, check=False)
            raise

        marks: dict[str, str] = {}
        with open(marks_path, "r", encoding="ascii") as stream_file:
            for line in stream_file:
                mark, object_id = line.split()
                marks[mark] = object_id

        mapping: dict[str, str] = {}
        for source, ref in source_refs.items():
            mapping[source] = marks[ref] if ref.startswith(":") else ref
        state["mapping"] = mapping
        new_tip = marks[previous_ref] if previous_ref.startswith(":") else previous_ref
        return new_tip, result_ref
    finally:
        if marks_path is not None:
            try:
                os.unlink(marks_path)
            except FileNotFoundError:
                pass


def _finalize(state: dict[str, Any]) -> None:
    target = state["target_branch"]
    temp = state["temp_branch"]
    new_tip, result_ref = _fast_import_rebuild(state)

    updates = _prepare_tag_updates(state)
    commands = [
        "start",
        f"update refs/heads/{target} {new_tip} {state['original_head']}",
    ]
    commands.extend(f"update {ref} {new_object} {old_object}" for ref, new_object, old_object in updates)
    commands.extend(["prepare", "commit", ""])
    git("update-ref", "--stdin", capture=True, input_bytes="\n".join(commands).encode("utf-8", "surrogateescape"))

    git("checkout", "--quiet", target)
    git("branch", "-D", "--quiet", temp)
    if result_ref is not None:
        git("update-ref", "-d", result_ref, check=False)
    delete_state()


def run_pending(state: dict[str, Any], command: str, continuing: bool = False) -> int:
    _validate_continue_state(state, command)

    if continuing:
        if not state.get("conflict"):
            raise GitFailure(f"No conflict is waiting for git {command} --continue", 2)
        if not _finish_conflicted_action(state):
            return 1

    while state["index"] < len(state["actions"]):
        action = state["actions"][state["index"]]
        if not _run_action(state, action):
            return 1
        _finish_action_progress(state, action)
        state["index"] += 1
        save_state(state)

    _end_progress_line()
    _finalize(state)
    return 0


def start_operation(
    command: str,
    start_commit: str,
    base_commit: str | None,
    actions: list[dict[str, Any]],
    tag_targets: dict[str, str],
) -> int:
    existing = load_state()
    if existing:
        raise GitFailure(
            f"A '{existing.get('command')}' operation is already in progress; "
            f"use git {existing.get('command')} --continue or --abort",
            2,
        )

    ensure_clean()
    target = current_branch()
    original_head = resolve_commit("HEAD")
    temp = make_temp_branch(command)
    tag_moves = capture_tag_moves(tag_targets)

    git("checkout", "--quiet", "-b", temp, start_commit)
    state: dict[str, Any] = {
        "version": 3,
        "command": command,
        "target_branch": target,
        "original_head": original_head,
        "temp_branch": temp,
        "base_commit": base_commit,
        "index": 0,
        "conflict": False,
        "actions": actions,
        "entries": [],
        "mapping": {},
        "tag_moves": tag_moves,
        "progress_done": 0,
        "progress_total": _progress_total(actions),
        "progress_reported": 0,
        "started_at": time.time(),
    }
    save_state(state)
    return run_pending(state, command)


def abort_operation(command: str) -> int:
    state = load_state()
    if not state:
        raise GitFailure("No Python history rewrite is in progress", 2)
    if state.get("command") != command:
        raise GitFailure(
            f"A '{state.get('command')}' operation is in progress; "
            f"use git {state.get('command')} --abort",
            2,
        )

    target = state["target_branch"]
    temp = state["temp_branch"]

    if _rebase_in_progress():
        git("rebase", "--abort", check=False)

    if current_branch() != target:
        git("reset", "--hard")
        git("checkout", target)

    git("branch", "-D", temp, check=False)
    delete_state()
    print(f"{command}: aborted")
    return 0


def continue_operation(command: str) -> int:
    state = load_state()
    if not state:
        raise GitFailure("No Python history rewrite is in progress", 2)
    return run_pending(state, command, continuing=True)


def setup_drop(args: list[str]) -> int:
    if len(args) != 1:
        raise GitFailure("Usage: git drop <commit-to-drop>", 2)
    drop = resolve_commit(args[0])
    target = resolve_commit("HEAD")
    after = first_parent_path_after(drop, target)
    start = first_parent(drop)
    tag_targets = {drop: start}
    tag_targets.update({commit: commit for commit in after})
    actions = compact_actions(action_replay_range(after))
    return start_operation("drop", start, start, actions, tag_targets)


def setup_edit(args: list[str]) -> int:
    if len(args) < 2:
        raise GitFailure("Usage: git edit <commit-to-edit> <new-commit-message>", 2)
    source = resolve_commit(args[0])
    message = " ".join(args[1:])
    target = resolve_commit("HEAD")
    after = first_parent_path_after(source, target)
    base = first_parent_or_none(source)
    actions = compact_actions(action_amend_message(source, message), action_replay_range(after))
    tag_targets = {commit: commit for commit in [source, *after]}
    return start_operation("edit", source, base, actions, tag_targets)


def setup_trim(args: list[str]) -> int:
    if len(args) != 1:
        raise GitFailure("Usage: git trim <commit-to-trim>", 2)
    source = resolve_commit(args[0])
    target = resolve_commit("HEAD")
    after = first_parent_path_after(source, target)
    subject = git_text("show", "-s", "--format=%s", source)
    base = first_parent_or_none(source)
    actions = compact_actions(action_amend_message(source, subject), action_replay_range(after))
    tag_targets = {commit: commit for commit in [source, *after]}
    return start_operation("trim", source, base, actions, tag_targets)


def _validate_base_head(base_value: str, head_value: str) -> tuple[str, str, str, list[str]]:
    base = resolve_commit(base_value)
    head = resolve_commit(head_value)
    target = resolve_commit("HEAD")
    after_base = first_parent_path_after(base, target)
    if head not in after_base:
        raise GitFailure("head-commit must be newer than base-commit on the current first-parent history", 2)
    return base, head, target, after_base


def _split_around(commit: str, commits: list[str]) -> tuple[list[str], list[str]]:
    index = commits.index(commit)
    return commits[:index], commits[index + 1 :]


def setup_move(args: list[str]) -> int:
    if len(args) != 2:
        raise GitFailure("Usage: git move <base-commit> <head-commit>", 2)
    base, head, _target, after_base = _validate_base_head(args[0], args[1])
    before_head, after_head = _split_around(head, after_base)
    actions = compact_actions(
        action_replay(head),
        action_replay_range(before_head),
        action_replay_range(after_head),
    )
    tag_targets = {commit: commit for commit in after_base}
    old_parent = first_parent(head)
    if old_parent != base:
        tag_targets[head] = old_parent
    return start_operation("move", base, base, actions, tag_targets)


def setup_join(args: list[str]) -> int:
    if len(args) != 2:
        raise GitFailure("Usage: git join <base-commit> <head-commit>", 2)
    base, head, _target, after_base = _validate_base_head(args[0], args[1])
    before_head, after_head = _split_around(head, after_base)
    root = first_parent_or_none(base)
    actions = compact_actions(
        action_apply(head),
        action_amend_no_edit(base),
        action_replay_range(before_head),
        action_replay_range(after_head),
    )
    tag_targets = {base: base}
    tag_targets.update({commit: commit for commit in after_base})
    tag_targets[head] = first_parent(head)
    return start_operation("join", base, root, actions, tag_targets)


def setup_hide(args: list[str]) -> int:
    if len(args) != 2:
        raise GitFailure("Usage: git hide <base-commit> <head-commit>", 2)
    base, head, target, _after_base = _validate_base_head(args[0], args[1])
    between = first_parent_path_after(base, head)
    if not between or between[-1] != head:
        raise GitFailure("Invalid base/head range", 2)
    intermediate = between[:-1]
    descendants = first_parent_path_after(head, target)
    start = first_parent(base)

    actions = compact_actions(
        action_replay_range(intermediate),
        action_apply(base),
        action_apply(head),
        action_commit_combined(head),
        action_replay_range(descendants),
    )
    tag_targets = {base: start, head: head}
    tag_targets.update({commit: commit for commit in intermediate})
    tag_targets.update({commit: commit for commit in descendants})
    return start_operation("hide", start, start, actions, tag_targets)


def setup_take(args: list[str]) -> int:
    if args:
        raise GitFailure("Usage: git take", 2)
    if has_unmerged_paths():
        raise GitFailure("Conflicts are still unresolved", 2)
    if not has_staged_changes():
        raise GitFailure("No staged changes to take", 1)

    amend_no_edit(resolve_commit("HEAD"))
    return 0


SETUP = {
    "drop": setup_drop,
    "edit": setup_edit,
    "hide": setup_hide,
    "join": setup_join,
    "move": setup_move,
    "take": setup_take,
    "trim": setup_trim,
}


def main(command: str, argv: list[str] | None = None) -> int:
    if command not in SETUP:
        raise ValueError(command)
    if argv is None:
        argv = sys.argv[1:]

    try:
        ensure_repo()
        if argv == ["--continue"]:
            return continue_operation(command)
        if argv == ["--abort"]:
            return abort_operation(command)
        if argv and argv[0].startswith("--"):
            raise GitFailure(f"Usage error for git {command}", 2)
        return SETUP[command](argv)
    except KeyboardInterrupt:
        _end_progress_line()
        print(f"{command}: interrupted by Ctrl+C", file=sys.stderr)
        try:
            state = load_state()
        except Exception:
            state = None
        if state and state.get("command") == command:
            print(f'Run "git {command} --abort" to clean up the interrupted operation.', file=sys.stderr)
        return 130
    except GitFailure as exc:
        print(f"{command}: {exc}", file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    name = Path(sys.argv[0]).stem.lower()
    if name == "git_history":
        if len(sys.argv) < 2 or sys.argv[1] not in SETUP:
            print("Usage: git_history.py <drop|edit|hide|join|move|take|trim> ...", file=sys.stderr)
            sys.exit(2)
        sys.exit(main(sys.argv[1], sys.argv[2:]))

    command_name = name
    if command_name not in SETUP:
        print("Run this module through drop.py/edit.py/hide.py/join.py/move.py/take.py/trim.py", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(command_name))
