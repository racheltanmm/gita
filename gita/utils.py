import sys
import os
import json
import csv
import asyncio
import platform
import subprocess
from functools import lru_cache, partial
from pathlib import Path
from typing import List, Dict, Coroutine, Union, Iterator, Tuple
from collections import Counter, defaultdict

from . import info
from . import common


MAX_INT = sys.maxsize


def get_relative_path(kid: str, parent: str) -> Union[List[str], None]:
    """
    Return the relative path depth if relative, otherwise MAX_INT.

    Both the `kid` and `parent` should be absolute paths without trailing /
    """
    # Note that os.path.commonpath has no trailing /
    # TODO: python3.9 pathlib has is_relative_to() function
    # TODO: Maybe use os.path.commonprefix? since it's faster?
    if parent == '':
        return None
    if parent == os.path.commonpath((kid, parent)):
        rel = os.path.normpath(os.path.relpath(kid, parent)).split(os.sep)
        if rel == ['.']:
            rel = []
        return rel
    else:
        return None


@lru_cache()
def get_repos(root=None) -> Dict[str, Dict[str, str]]:
    """
    Return a `dict` of repo name to repo absolute path and repo type

    @param root: Use local config if set. If None, use either global or local
                 config depending on cwd.
    """
    path_file = common.get_config_fname('repos.csv', root)
    repos = {}
    if os.path.isfile(path_file) and os.stat(path_file).st_size > 0:
        with open(path_file) as f:
            rows = csv.DictReader(f, ['path', 'name', 'type', 'flags'],
                                  restval='')  # it's actually a reader
            repos = {r['name']:
                    {'path': r['path'], 'type': r['type'],
                        'flags': r['flags'].split()}
                     for r in rows if is_git(r['path'], is_bare=True)}
    if root is None:  # detect if inside a main path
        cwd = os.getcwd()
        for prop in repos.values():
            path = prop['path']
            if prop['type'] == 'm' and get_relative_path(cwd, path) != MAX_INT:
                return get_repos(path)
    return repos


@lru_cache()
def get_context() -> Union[Path, None]:
    """
    Return context file path, or None if not set. Note that if in auto context
    mode, the return value is not auto.context but the resolved context,
    which could be None.

    """
    config_dir = Path(common.get_config_dir())
    matches = list(config_dir.glob('*.context'))
    if len(matches) > 1:
        print("Cannot have multiple .context file")
        sys.exit(1)
    if not matches:
        return None
    ctx = matches[0]
    if ctx.stem == 'auto':
        cwd = str(Path.cwd())
        repos = get_repos()
        # The context is set to be the group with minimal distance to cwd
        candidate = None
        min_dist = MAX_INT
        for gname, prop in get_groups().items():
            rel = get_relative_path(cwd, prop['path'])
            if rel is None:
                continue
            d = len(rel)
            if d < min_dist:
                candidate = gname
                min_dist = d
        if not candidate:
            ctx = None
        else:
            ctx = ctx.with_name(f'{candidate}.context')
    return ctx


@lru_cache()
def get_groups() -> Dict[str, Dict]:
    """
    Return a `dict` of group name to group properties such as repo names and
    group path.
    """
    fname = common.get_config_fname('groups.csv')
    groups = {}
    # Each line is:  group-name:repo1 repo2 repo3:group-path
    if os.path.isfile(fname) and os.stat(fname).st_size > 0:
        with open(fname, 'r') as f:
            rows = csv.DictReader(f, ['name', 'repos', 'path'],
                                  restval='', delimiter=':')
            groups = {
                    r['name']: {
                        'repos': r['repos'].split(),
                        'path': r['path']
                        }
                    for r in rows}
    return groups


def delete_repo_from_groups(repo: str, groups: Dict[str, Dict]) -> bool:
    """
    Delete repo from groups
    """
    deleted = False
    for name in groups:
        try:
            groups[name]['repos'].remove(repo)
        except ValueError as e:
            pass
        else:
            deleted = True
    return deleted


def replace_context(old: Union[Path, None], new: str):
    """

    """
    auto = Path(common.get_config_dir()) / 'auto.context'
    if auto.exists():
        old = auto

    if new == 'none':  # delete
        old and old.unlink()
    elif old:
        # ctx.rename(ctx.with_stem(new_name))  # only works in py3.9
        old.rename(old.with_name(f'{new}.context'))
    else:
        open(auto.with_name(f'{new}.context'), 'w').close()


def get_choices() -> List[Union[str, None]]:
    """
    Return all repo names, group names, and an additional empty list. The empty
    list is added as a workaround of
    argparse's problem with coexisting nargs='*' and choices.
    See https://utcc.utoronto.ca/~cks/space/blog/python/ArgparseNargsChoicesLimitation
    and
    https://bugs.python.org/issue27227
    """
    choices = list(get_repos())
    choices.extend(get_groups())
    choices.append([])
    return choices


def is_git(path: str, is_bare=False) -> bool:
    """
    Return True if the path is a git repo.
    """
    if not os.path.exists(path):
        return False
    # An alternative is to call `git rev-parse --is-inside-work-tree`
    # I don't see why that one is better yet.
    # For a regular git repo, .git is a folder, for a worktree repo, .git is a file.
    # However, git submodule repo also has .git as a file.
    # A more reliable way to differentiable regular and worktree repos is to
    # compare the result of `git rev-parse --git-dir` and
    # `git rev-parse --git-common-dir`
    loc = os.path.join(path, '.git')
    # TODO: we can display the worktree repos in a different font.
    if os.path.exists(loc):
        return True
    if not is_bare:
        return False
    # detect bare repo
    got = subprocess.run('git rev-parse --is-bare-repository'.split(),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            cwd=path
                            )
    if got.returncode == 0 and got.stdout == b'true\n':
        return True
    return False


def rename_repo(repos: Dict[str, Dict[str, str]], repo: str, new_name: str):
    """
    Write new repo name to file
    """
    if new_name in repos:
        print(f"{new_name} is already in use!")
        return
    prop = repos[repo]
    del repos[repo]
    repos[new_name] = prop
    # write to local config if inside a main path
    main_paths = (prop['path'] for prop in repos.values() if prop['type'] == 'm')
    cwd = os.getcwd()
    is_local_config = True
    # TODO: delete
    for p in main_paths:
        if get_relative_path(cwd, p) != MAX_INT:
            write_to_repo_file(repos, 'w', p)
            break
    else:  # global config
        write_to_repo_file(repos, 'w')
        is_local_config = False
    # update groups only when outside any main repos
    if is_local_config:
        return
    groups = get_groups()
    for g, members in groups.items():
        if repo in members:
            members.remove(repo)
            members.append(new_name)
            groups[g] = sorted(members)
    write_to_groups_file(groups, 'w')


def write_to_repo_file(repos: Dict[str, Dict[str, str]], mode: str, root=None):
    """
    @param repos: each repo is {name: {properties}}
    """
    data = [(prop['path'], name, prop['type'], ' '.join(prop['flags']))
                for name, prop in repos.items()]
    fname = common.get_config_fname('repos.csv', root)
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    with open(fname, mode, newline='') as f:
        writer = csv.writer(f, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        writer.writerows(data)


# TODO: combine with the repo writer
def write_to_groups_file(groups: Dict[str, Dict], mode: str):
    """

    """
    fname = common.get_config_fname('groups.csv')
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    if not groups:  # all groups are deleted
        open(fname, 'w').close()
    else:
        # delete the group if there are no repos
        for name in list(groups):
            if not groups[name]['repos']:
                del groups[name]
        with open(fname, mode, newline='') as f:
            data = [
                    (group, ' '.join(prop['repos']), prop['path'])
                    for group, prop in groups.items()
                    ]
            writer = csv.writer(f, delimiter=':', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            writer.writerows(data)


def _make_name(path: str, repos: Dict[str, Dict[str, str]],
                name_counts: Counter) -> str:
    """
    Given a new repo `path`, create a repo name. By default, basename is used.
    If name collision exists, further include parent path name.

    @param path: It should not be in `repos` and is absolute
    """
    name = os.path.basename(os.path.normpath(path))
    if name in repos or name_counts[name] > 1:
        # path has no trailing /
        par_name = os.path.basename(os.path.dirname(path))
        return os.path.join(par_name, name)
    return name


# TODO: delete
def _get_repo_type(path, repo_type, root) -> str:
    """

    """
    if repo_type != '':  # explicitly set
        return repo_type
    if root is not None and os.path.normpath(root) == os.path.normpath(path):
        return 'm'
    return ''


def add_repos(repos: Dict[str, Dict[str, str]], new_paths: List[str],
              repo_type='', root=None, is_bare=False) -> Dict[str, Dict[str, str]]:
    """
    Write new repo paths to file; return the added repos.

    @param repos: name -> path
    """
    existing_paths = {prop['path'] for prop in repos.values()}
    new_paths = {p for p in new_paths if is_git(p, is_bare)}
    new_paths = new_paths - existing_paths
    new_repos = {}
    if new_paths:
        print(f"Found {len(new_paths)} new repo(s).")
        name_counts = Counter(
            os.path.basename(os.path.normpath(p)) for p in new_paths
                )
        new_repos = {_make_name(path, repos, name_counts): {
            'path': path,
            'type': _get_repo_type(path, repo_type, root),
            'flags': '',
            } for path in new_paths}
        # When root is not None, we could optionally set its type to 'm', i.e.,
        # main repo.
        write_to_repo_file(new_repos, 'a+', root)
    else:
        print('No new repos found!')
    return new_repos


def _generate_dir_hash(repo_path: str, paths: List[str]) -> Tuple[
        Tuple[str, ...], str]:
    """
    Return relative parent strings, and the parent head string

    For example, if `repo_path` is /a/b/c/d/here, and one of `paths` is /a/b/
    then return (b, c, d)
    """
    for p in paths:
        rel = get_relative_path(repo_path, p)[:-1]
        if rel is not None:
            break
    else:
        return (), ''
    head, tail = os.path.split(p)
    return (tail, *rel), head


def auto_group(repos: Dict[str, Dict[str, str]], paths: List[str]
        ) -> Dict[str, Dict]:
    """

    @params repos: repos to be grouped
    """
    # FIXME: the upstream code should make sure that paths are all independent
    #        i.e., each repo should be contained in one and only one path
    new_groups = defaultdict(dict)
    for repo_name, prop in repos.items():
        hash, head = _generate_dir_hash(prop['path'], paths)
        if not hash:
            continue
        for i in range(1, len(hash)+1):
            group_name = '-'.join(hash[:i])
            prop = new_groups[group_name]
            prop['path'] = os.path.join(head, *hash[:i])
            if 'repos' not in prop:
                prop['repos'] = [repo_name]
            else:
                prop['repos'].append(repo_name)
    # FIXME: need to make sure the new group names don't clash with old ones
    #        or repo names
    return new_groups


def parse_clone_config(fname: str) -> Iterator[List[str]]:
    """
    Return the url, name, and path of all repos in `fname`.
    """
    with open(fname) as f:
        for line in f:
            yield line.strip().split(',')


async def run_async(repo_name: str, path: str, cmds: List[str]) -> Union[None, str]:
    """
    Run `cmds` asynchronously in `path` directory. Return the `path` if
    execution fails.
    """
    # TODO: deprecated since 3.8, will be removed in 3.10
    process = await asyncio.create_subprocess_exec(
        *cmds,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        cwd=path)
    stdout, stderr = await process.communicate()
    for pipe in (stdout, stderr):
        if pipe:
            print(format_output(pipe.decode(), repo_name))
    # The existence of stderr is not good indicator since git sometimes write
    # to stderr even if the execution is successful, e.g. git fetch
    if process.returncode != 0:
        return path


def format_output(s: str, prefix: str):
    """
    Prepends every line in given string with the given prefix.
    """
    return ''.join([f'{prefix}: {line}' for line in s.splitlines(keepends=True)])


def exec_async_tasks(tasks: List[Coroutine]) -> List[Union[None, str]]:
    """
    Execute tasks asynchronously
    """
    # TODO: asyncio API is nicer in python 3.7
    if platform.system() == 'Windows':
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
    else:
        loop = asyncio.get_event_loop()

    try:
        errors = loop.run_until_complete(asyncio.gather(*tasks))
    finally:
        loop.close()
    return errors


def describe(repos: Dict[str, Dict[str, str]], no_colors: bool = False) -> str:
    """
    Return the status of all repos
    """
    if repos:
        name_width = max(len(n) for n in repos) + 1
    funcs = info.get_info_funcs()

    get_repo_status = info.get_repo_status
    if get_repo_status in funcs and no_colors:
        idx = funcs.index(get_repo_status)
        funcs[idx] = partial(get_repo_status, no_colors=True)

    for name in sorted(repos):
        info_items = ' '.join(f(repos[name]) for f in funcs)
        if repos[name]['type'] == 'm':
            # ANSI color code also takes length in Python
            name = f'{info.Color.underline}{name}{info.Color.end}'
            width = name_width + 8
            yield f'{name:<{width}}{info_items}'
        else:
            yield f'{name:<{name_width}}{info_items}'


def get_cmds_from_files() -> Dict[str, Dict[str, str]]:
    """
    Parse delegated git commands from default config file
    and custom config file.

    Example return
    {
      'branch': {'help': 'show local branches'},
      'clean': {'cmd': 'clean -dfx',
                'help': 'remove all untracked files/folders'},
    }
    """
    # default config file
    fname = os.path.join(os.path.dirname(__file__), "cmds.json")
    with open(fname, 'r') as f:
        cmds = json.load(f)

    # custom config file
    root = common.get_config_dir()
    fname = os.path.join(root, 'cmds.json')
    custom_cmds = {}
    if os.path.isfile(fname) and os.path.getsize(fname):
        with open(fname, 'r') as f:
            custom_cmds = json.load(f)

    # custom commands shadow default ones
    cmds.update(custom_cmds)
    return cmds
