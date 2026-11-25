"""Take a private, link-free snapshot of the inputs needed by a card-local owner."""

import os
import shutil
import stat
import tempfile
from pathlib import Path

from hermes_dir_acl import make_user_dir


_INPUTS = ('config.yaml', '.env', 'SOUL.md', 'skills', 'plugins', 'hooks', 'locales',
           'skins', 'memories', 'auth.json', 'models_dev_cache.json')


def _private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('unsafe isolated attempt directory')


def _snapshot(parent_fd, name, target):
    mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
    if stat.S_ISDIR(mode):
        source_fd = os.open(name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            target.mkdir(mode=0o700)
            for child in os.listdir(source_fd):
                _snapshot(source_fd, child, target / child)
        finally:
            os.close(source_fd)
    elif stat.S_ISREG(mode):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, 'rb') as source, target.open('xb') as output:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError('profile input changed during snapshot')
            os.fchmod(output.fileno(), 0o700 if mode & 0o100 else 0o600)
            shutil.copyfileobj(source, output)
    else:
        raise ValueError(f'unsupported linked or special profile input: {name}')


def _check_private_input(path):
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        for child in path.iterdir():
            _check_private_input(child)
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PermissionError('unsafe link in existing attempt home')


def prepare_attempt_home(attempt_home: Path, profile_home: Path) -> Path:
    root = attempt_home.parent
    for directory in (root.parent.parent, root.parent, root):
        make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
        _private_directory(directory)
    if os.path.lexists(attempt_home):
        _private_directory(attempt_home)
        for name in _INPUTS:
            if os.path.lexists(attempt_home / name):
                _check_private_input(attempt_home / name)
        return attempt_home
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        staging = Path(tmp) / 'snapshot'
        staging.mkdir(mode=0o700)
        source_fd = os.open(profile_home, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            available = set(os.listdir(source_fd))
            for name in _INPUTS:
                if name in available:
                    _snapshot(source_fd, name, staging / name)
        finally:
            os.close(source_fd)
        staging.rename(attempt_home)
    return attempt_home
