import argparse
import base64
import dataclasses
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


DEFAULT_PATH = "sidecar"
DEFAULT_BRANCH = "main"
DEFAULT_INBOX = "sidecar-inbox/{user}/{host}"


class SidecarError(RuntimeError):
    pass


@dataclasses.dataclass
class SidecarConfig:
    remote: str
    version: int = 1
    path: str = DEFAULT_PATH
    branch: str = DEFAULT_BRANCH
    inbox: str = DEFAULT_INBOX


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SidecarError as exc:
        print("sidecar: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nsidecar: stopped", file=sys.stderr)
        return 130


def build_parser():
    parser = argparse.ArgumentParser(prog="sidecar")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="write .sidecar, gitignore the sidecar path, and clone it")
    init_p.add_argument("remote")
    init_p.add_argument("--path", default=DEFAULT_PATH)
    init_p.add_argument("--branch", default=DEFAULT_BRANCH)
    init_p.add_argument("--inbox", default=DEFAULT_INBOX)
    init_p.add_argument("--no-clone", action="store_true")
    init_p.add_argument("--no-bootstrap-main", action="store_true")
    init_p.set_defaults(func=cmd_init)

    clone_p = sub.add_parser("clone", help="clone the configured sidecar repo")
    clone_p.add_argument("--no-bootstrap-main", action="store_true")
    clone_p.set_defaults(func=cmd_clone)

    status_p = sub.add_parser("status", help="show sidecar status")
    status_p.set_defaults(func=cmd_status)

    snapshot_p = sub.add_parser("snapshot", help="commit local sidecar changes to the inbox branch")
    snapshot_p.add_argument("-m", "--message")
    snapshot_p.add_argument("--push", action="store_true")
    snapshot_p.set_defaults(func=cmd_snapshot)

    push_p = sub.add_parser("push", help="snapshot and push local sidecar changes")
    push_p.add_argument("-m", "--message")
    push_p.add_argument("--no-snapshot", action="store_true")
    push_p.set_defaults(func=cmd_push)

    watch_p = sub.add_parser("watch", help="watch sidecar files and push debounced snapshots")
    watch_p.add_argument("--debounce", type=float, default=30.0)
    watch_p.add_argument("--interval", type=float, default=2.0)
    watch_p.add_argument("--max-interval", type=float, default=300.0)
    watch_p.set_defaults(func=cmd_watch)

    merge_p = sub.add_parser("merge", help="merge remote inbox branches into canonical sidecar main")
    merge_p.add_argument("--fork-files", action="store_true", help="fork conflicted file versions instead of stopping")
    merge_p.add_argument("--llm", action="store_true", help="reserved for an interactive LLM resolver")
    merge_p.add_argument("--delete-merged-inbox", action="store_true")
    merge_p.add_argument("--no-push", action="store_true")
    merge_p.set_defaults(func=cmd_merge)

    return parser


def cmd_init(args):
    root = git_toplevel(Path.cwd())
    config = SidecarConfig(
        remote=args.remote,
        path=args.path,
        branch=args.branch,
        inbox=args.inbox,
    )
    write_config(root / ".sidecar", config)
    ensure_gitignore_entry(root / ".gitignore", config.path)
    print("wrote {}".format(root / ".sidecar"))
    print("ignored {}".format(config.path.rstrip("/") + "/"))

    if not args.no_clone:
        clone_or_update(root, config, bootstrap_main=not args.no_bootstrap_main)
    return 0


def cmd_clone(args):
    root, config = load_project()
    clone_or_update(root, config, bootstrap_main=not args.no_bootstrap_main)
    return 0


def cmd_status(args):
    root, config = load_project()
    sidecar_path = root / config.path
    inbox = expand_inbox(config)
    print("main repo:    {}".format(root))
    print("sidecar path: {}".format(sidecar_path))
    print("remote:       {}".format(config.remote))
    print("main branch:  {}".format(config.branch))
    print("inbox branch: {}".format(inbox))

    if not (sidecar_path / ".git").exists():
        print("checkout:     missing")
        return 0

    branch = git(sidecar_path, "branch", "--show-current").stdout.strip()
    dirty = bool(git(sidecar_path, "status", "--porcelain").stdout.strip())
    print("checkout:     present")
    print("branch:       {}".format(branch or "(detached)"))
    print("dirty:        {}".format("yes" if dirty else "no"))

    fetch(sidecar_path, quiet=True, check=False)
    pending = pending_inbox_branches(sidecar_path, config)
    if pending:
        print("pending inbox:")
        for branch_name in pending:
            print("  {}".format(branch_name))
    else:
        print("pending inbox: none")
    return 0


def cmd_snapshot(args):
    root, config = load_project()
    sidecar_path = require_sidecar_checkout(root, config)
    inbox = expand_inbox(config)
    ensure_commit_identity(sidecar_path)
    ensure_inbox_branch(sidecar_path, config, inbox)
    committed = snapshot(sidecar_path, root, inbox, args.message)
    if committed and args.push:
        push_branch(sidecar_path, inbox)
    return 0


def cmd_push(args):
    root, config = load_project()
    sidecar_path = require_sidecar_checkout(root, config)
    inbox = expand_inbox(config)
    ensure_commit_identity(sidecar_path)
    ensure_inbox_branch(sidecar_path, config, inbox)
    if not args.no_snapshot:
        snapshot(sidecar_path, root, inbox, args.message)
    push_branch(sidecar_path, inbox)
    return 0


def cmd_watch(args):
    if args.debounce < 0:
        raise SidecarError("--debounce must be >= 0")
    if args.interval <= 0:
        raise SidecarError("--interval must be > 0")
    if args.max_interval <= 0:
        raise SidecarError("--max-interval must be > 0")

    root, config = load_project()
    sidecar_path = require_sidecar_checkout(root, config)
    inbox = expand_inbox(config)
    ensure_commit_identity(sidecar_path)
    ensure_inbox_branch(sidecar_path, config, inbox)

    print("watching {} -> {}".format(sidecar_path, inbox))
    last_signature = tree_signature(sidecar_path)
    first_dirty_at = None
    last_change_at = None

    while True:
        time.sleep(args.interval)
        signature = tree_signature(sidecar_path)
        now = time.time()
        if signature != last_signature:
            last_signature = signature
            if first_dirty_at is None:
                first_dirty_at = now
            last_change_at = now
            continue

        if first_dirty_at is None:
            continue

        quiet_for = now - last_change_at
        dirty_for = now - first_dirty_at
        if quiet_for >= args.debounce or dirty_for >= args.max_interval:
            print("snapshotting sidecar changes")
            ensure_inbox_branch(sidecar_path, config, inbox)
            snapshot(sidecar_path, root, inbox, None)
            push_branch(sidecar_path, inbox)
            first_dirty_at = None
            last_change_at = None
            last_signature = tree_signature(sidecar_path)


def cmd_merge(args):
    if args.llm:
        raise SidecarError("--llm is reserved for a configured resolver; use --fork-files for now")
    if not args.fork_files:
        print("sidecar: conflicts will stop the merge; pass --fork-files to preserve all versions")

    root, config = load_project()
    sidecar_path = require_sidecar_checkout(root, config)
    ensure_clean(sidecar_path)
    ensure_commit_identity(sidecar_path)
    fetch(sidecar_path, quiet=False)
    ensure_main_branch(sidecar_path, config)

    inbox_branches = pending_inbox_branches(sidecar_path, config)
    if not inbox_branches:
        print("no inbox branches to merge")
        return 0

    merged = []
    for remote_branch in inbox_branches:
        if is_ancestor(sidecar_path, remote_branch, "HEAD"):
            print("already merged {}".format(remote_branch))
            merged.append(remote_branch)
            continue

        print("merging {}".format(remote_branch))
        result = git(
            sidecar_path,
            "merge",
            "--no-ff",
            "-m",
            "Merge {}".format(remote_branch),
            remote_branch,
            check=False,
        )
        if result.returncode == 0:
            merged.append(remote_branch)
            continue

        if not has_unmerged_paths(sidecar_path):
            raise SidecarError(result.stderr.strip() or "merge failed for {}".format(remote_branch))

        if not args.fork_files:
            git(sidecar_path, "merge", "--abort", check=False)
            raise SidecarError("merge conflict in {}; rerun with --fork-files".format(remote_branch))

        fork_conflicts(sidecar_path, remote_branch)
        git(sidecar_path, "commit", "-m", "Merge {} with forked conflict files".format(remote_branch))
        merged.append(remote_branch)

    if not args.no_push:
        push_branch(sidecar_path, config.branch)
        if args.delete_merged_inbox:
            delete_remote_inbox_branches(sidecar_path, merged)

    print("merged {} inbox branch(es)".format(len(merged)))
    return 0


def clone_or_update(root, config, bootstrap_main):
    sidecar_path = root / config.path
    if sidecar_path.exists() and not (sidecar_path / ".git").exists():
        if any(sidecar_path.iterdir()):
            raise SidecarError("{} exists and is not an empty Git repo".format(sidecar_path))
        sidecar_path.rmdir()

    if not sidecar_path.exists():
        git_raw("clone", config.remote, str(sidecar_path))
    elif (sidecar_path / ".git").exists():
        existing = git(sidecar_path, "remote", "get-url", "origin", check=False)
        if existing.returncode != 0:
            git(sidecar_path, "remote", "add", "origin", config.remote)
        elif existing.stdout.strip() != config.remote:
            raise SidecarError("sidecar origin is {}; expected {}".format(existing.stdout.strip(), config.remote))
        fetch(sidecar_path, quiet=True)
    else:
        raise SidecarError("{} is not usable as a sidecar checkout".format(sidecar_path))

    ensure_commit_identity(sidecar_path)
    if bootstrap_main:
        bootstrap_main_branch(sidecar_path, config)

    inbox = expand_inbox(config)
    ensure_inbox_branch(sidecar_path, config, inbox)
    print("sidecar checkout ready at {}".format(sidecar_path))


def bootstrap_main_branch(repo, config):
    if has_any_commit(repo):
        if remote_ref_exists(repo, config.branch):
            return
        current = git(repo, "branch", "--show-current").stdout.strip()
        if current != config.branch:
            git(repo, "switch", "-c", config.branch, check=False)
        push_branch(repo, config.branch)
        return

    git(repo, "switch", "--orphan", config.branch, check=False)
    root_file = repo / "README.md"
    root_file.write_text("# Sidecar\n\nCanonical sidecar state for this repository.\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "Initialize sidecar")
    push_branch(repo, config.branch)


def ensure_main_branch(repo, config):
    if branch_exists(repo, config.branch):
        git(repo, "switch", config.branch)
    elif remote_ref_exists(repo, config.branch):
        git(repo, "switch", "-c", config.branch, "--track", "origin/{}".format(config.branch))
    elif has_any_commit(repo):
        git(repo, "switch", "-c", config.branch)
    else:
        bootstrap_main_branch(repo, config)
        return

    if remote_ref_exists(repo, config.branch):
        git(repo, "merge", "--ff-only", "origin/{}".format(config.branch))


def ensure_inbox_branch(repo, config, inbox):
    current = git(repo, "branch", "--show-current").stdout.strip()
    if current == inbox:
        return

    if branch_exists(repo, inbox):
        git(repo, "switch", inbox)
        return

    if remote_ref_exists(repo, inbox):
        git(repo, "switch", "-c", inbox, "--track", "origin/{}".format(inbox))
        return

    if remote_ref_exists(repo, config.branch):
        git(repo, "switch", "-c", inbox, "origin/{}".format(config.branch))
        return

    if branch_exists(repo, config.branch):
        git(repo, "switch", "-c", inbox, config.branch)
        return

    if has_any_commit(repo):
        git(repo, "switch", "-c", inbox)
        return

    bootstrap_main_branch(repo, config)
    git(repo, "switch", "-c", inbox, config.branch)


def snapshot(repo, main_root, inbox, message):
    git(repo, "add", "-A")
    if git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
        print("no sidecar changes to snapshot")
        return False

    main_head = git(main_root, "rev-parse", "--short", "HEAD", check=False)
    main_head_text = main_head.stdout.strip() if main_head.returncode == 0 else "unborn"
    source = "{}@{}".format(current_user(), current_host())
    msg = message or "sidecar snapshot"
    body = [
        msg,
        "",
        "source: {}".format(source),
        "main-repo: {}".format(main_root),
        "main-head: {}".format(main_head_text),
        "inbox: {}".format(inbox),
    ]
    git(repo, "commit", "-m", "\n".join(body))
    print("committed sidecar snapshot to {}".format(inbox))
    return True


def push_branch(repo, branch):
    git(repo, "push", "-u", "origin", "HEAD:refs/heads/{}".format(branch))
    print("pushed {}".format(branch))


def delete_remote_inbox_branches(repo, remote_branches):
    for remote_branch in remote_branches:
        branch = remote_branch_name(remote_branch)
        if branch:
            git(repo, "push", "origin", "--delete", branch, check=False)


def fork_conflicts(repo, remote_branch):
    conflicts = unmerged_paths(repo)
    if not conflicts:
        raise SidecarError("merge reported conflicts, but no unmerged paths were found")

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    branch = remote_branch_name(remote_branch) or remote_branch
    branch_label = slug(branch)
    manifest_label = file_label(branch)
    manifest = {
        "timestamp": timestamp,
        "resolved_by": "fork-files",
        "source_branch": branch,
        "paths": [],
    }

    for path, stages in sorted(conflicts.items()):
        versions = []
        for stage, label in ((2, "main"), (3, branch_label)):
            blob = show_stage(repo, stage, path)
            if blob is None:
                continue
            oid = stages.get(stage, "")
            out_path = fork_path(path, label, oid)
            full_out = repo / out_path
            full_out.parent.mkdir(parents=True, exist_ok=True)
            full_out.write_bytes(blob)
            versions.append(
                {
                    "stage": stage,
                    "label": label,
                    "oid": oid,
                    "path": out_path,
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "content_base64": base64.b64encode(blob).decode("ascii"),
                }
            )

        git(repo, "rm", "-f", "--ignore-unmatch", "--", path, check=False)
        original = repo / path
        if original.exists() and original.is_file():
            original.unlink()

        manifest["paths"].append({"path": path, "versions": versions})

    manifest_dir = repo / ".sidecar-conflicts"
    manifest_dir.mkdir(exist_ok=True)
    manifest_path = manifest_dir / "{}-{}.json".format(timestamp, manifest_label)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    if has_unmerged_paths(repo):
        raise SidecarError("fork-files did not clear all unmerged paths")


def fork_path(path, label, oid):
    p = Path(path)
    short_oid = oid[:7] if oid else "missing"
    safe_label = file_label(label)
    name = p.name
    if p.suffix:
        stem = name[: -len(p.suffix)]
        fork_name = "{}.conflict.{}.{}{}".format(stem, safe_label, short_oid, p.suffix)
    else:
        fork_name = "{}.conflict.{}.{}".format(name, safe_label, short_oid)
    return str(p.with_name(fork_name))


def file_label(value):
    return slug(value).replace("/", "-")


def unmerged_paths(repo):
    result = git_bytes(repo, "ls-files", "-u", "-z")
    data = result.stdout
    paths = {}
    for record in data.split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        parts = meta.decode("ascii").split()
        oid = parts[1]
        stage = int(parts[2])
        path = raw_path.decode("utf-8", "surrogateescape")
        paths.setdefault(path, {})[stage] = oid
    return paths


def has_unmerged_paths(repo):
    return bool(unmerged_paths(repo))


def show_stage(repo, stage, path):
    result = git_bytes(repo, "show", ":{}:{}".format(stage, path), check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def pending_inbox_branches(repo, config):
    prefix = "origin/" + inbox_prefix(config)
    refs = git(repo, "branch", "-r", "--format=%(refname:short)").stdout.splitlines()
    branches = []
    for ref in refs:
        ref = ref.strip()
        if ref.startswith(prefix) and ref != "origin/HEAD":
            branches.append(ref)
    return sorted(branches)


def inbox_prefix(config):
    before_vars = config.inbox.split("{", 1)[0]
    return before_vars.rstrip("/") + "/"


def remote_branch_name(remote_branch):
    if remote_branch.startswith("origin/"):
        return remote_branch[len("origin/") :]
    return remote_branch


def expand_inbox(config):
    values = {"user": slug(current_user()), "host": slug(current_host())}
    inbox = config.inbox.format(**values).strip("/")
    validate_branch(inbox)
    return inbox


def validate_branch(branch):
    result = git_raw("check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0:
        raise SidecarError("invalid branch name {!r}".format(branch))


def slug(value):
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._/-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-./")
    return value or "unknown"


def tree_signature(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ".git" in rel.parts:
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        digest.update(str(rel).encode("utf-8", "surrogateescape"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(str(stat.st_size).encode("ascii"))
    return digest.hexdigest()


def load_project():
    root = find_config_root(Path.cwd())
    config = read_config(root / ".sidecar")
    return root, config


def find_config_root(start):
    current = start.resolve()
    while True:
        if (current / ".sidecar").exists():
            return current
        if current.parent == current:
            raise SidecarError("could not find .sidecar")
        current = current.parent


def git_toplevel(cwd):
    result = git_raw("-C", str(cwd), "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise SidecarError("not inside a Git repository")
    return Path(result.stdout.strip())


def require_sidecar_checkout(root, config):
    sidecar_path = root / config.path
    if not (sidecar_path / ".git").exists():
        raise SidecarError("missing sidecar checkout at {}; run `sidecar clone`".format(sidecar_path))
    return sidecar_path


def write_config(path, config):
    text = "\n".join(
        [
            "version = {}".format(config.version),
            'remote = "{}"'.format(config.remote),
            'path = "{}"'.format(config.path),
            'branch = "{}"'.format(config.branch),
            'inbox = "{}"'.format(config.inbox),
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")


def read_config(path):
    values = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise SidecarError("{}:{} expected key = value".format(path, line_number))
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith('"') and raw_value.endswith('"'):
            value = raw_value[1:-1]
        elif raw_value.isdigit():
            value = int(raw_value)
        else:
            value = raw_value
        values[key] = value

    if "remote" not in values:
        raise SidecarError("{} is missing remote".format(path))

    return SidecarConfig(
        remote=str(values["remote"]),
        version=int(values.get("version", 1)),
        path=str(values.get("path", DEFAULT_PATH)),
        branch=str(values.get("branch", DEFAULT_BRANCH)),
        inbox=str(values.get("inbox", DEFAULT_INBOX)),
    )


def ensure_gitignore_entry(path, sidecar_path):
    entry = "/" + sidecar_path.strip("/") + "/"
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    if entry not in lines:
        lines.append(entry)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def ensure_clean(repo):
    dirty = git(repo, "status", "--porcelain").stdout.strip()
    if dirty:
        raise SidecarError("sidecar checkout has uncommitted changes")


def ensure_commit_identity(repo):
    if git(repo, "config", "user.name", check=False).returncode != 0:
        git(repo, "config", "user.name", current_user())
    if git(repo, "config", "user.email", check=False).returncode != 0:
        git(repo, "config", "user.email", "{}@{}.local".format(slug(current_user()), slug(current_host())))


def current_user():
    return os.environ.get("USER") or getpass.getuser() or "unknown"


def current_host():
    return socket.gethostname().split(".", 1)[0] or "unknown"


def fetch(repo, quiet, check=True):
    args = ["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"]
    if quiet:
        args.insert(1, "--quiet")
    git(repo, *args, check=check)


def has_any_commit(repo):
    return git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


def branch_exists(repo, branch):
    return git(repo, "show-ref", "--verify", "--quiet", "refs/heads/{}".format(branch), check=False).returncode == 0


def remote_ref_exists(repo, branch):
    return git(repo, "show-ref", "--verify", "--quiet", "refs/remotes/origin/{}".format(branch), check=False).returncode == 0


def is_ancestor(repo, maybe_ancestor, descendant):
    return git(repo, "merge-base", "--is-ancestor", maybe_ancestor, descendant, check=False).returncode == 0


def git(repo, *args, check=True):
    return git_raw("-C", str(repo), *args, check=check)


def git_bytes(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise SidecarError(result.stderr.decode("utf-8", "replace").strip())
    return result


def git_raw(*args, check=True):
    result = subprocess.run(
        ["git"] + list(args),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise SidecarError(message)
    return result
