import os
import pwd
import re
from dataclasses import dataclass


USER_RE = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
NUMERIC_IDENTITY_RE = re.compile(r'([0-9]+):([0-9]+)')
MAX_ID = 2**32 - 2


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int
    home: str


def validate_identity(value, field='user'):
    if not isinstance(value, str):
        raise ValueError(
            f'{field} must be a local user name or quoted "UID:GID" string'
        )
    numeric = NUMERIC_IDENTITY_RE.fullmatch(value)
    if numeric is not None:
        uid, gid = (int(item) for item in numeric.groups())
        if uid <= MAX_ID and gid <= MAX_ID:
            return value
    elif USER_RE.fullmatch(value) is not None:
        return value
    raise ValueError(
        f'{field} must be a local user name or quoted "UID:GID" string'
    )


def account(value):
    validate_identity(value)
    numeric = NUMERIC_IDENTITY_RE.fullmatch(value)
    if numeric is not None:
        uid, gid = (int(item) for item in numeric.groups())
        return Account(value, uid, gid, '/')
    try:
        record = pwd.getpwnam(value)
    except KeyError as err:
        raise ValueError(f'local user does not exist: {value}') from err
    return Account(record.pw_name, record.pw_uid, record.pw_gid, record.pw_dir)


def subprocess_identity(value):
    """Return Popen kwargs which discard root and supplementary groups."""
    record = account(value)
    if os.geteuid() == record.uid and os.getegid() == record.gid:
        return {}
    if os.geteuid() != 0:
        raise PermissionError(
            f'cannot run as {record.uid}:{record.gid} '
            'without root coordinator privileges'
        )
    return {'user': record.uid, 'group': record.gid, 'extra_groups': []}


def fixed_environment(value):
    record = account(value)
    return {
        'HOME': record.home,
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
        'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
    }


def action_user(action):
    return action['run_as']
