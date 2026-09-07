"""Reading a config file without re-parsing it. SPEC §101.4.

**Measured, per §6k.** Profiling the two queries the Payees page waits on:

    /api/payees    425ms per request, 240ms of it `yaml.safe_load` — 10 loads
    /api/reapply   690ms per request, 435ms of it `yaml.safe_load` — 36 loads

Thirty-six parses of the same handful of small files, in one request, because
`load_rules`, `load_accounts` and `load_payee_aliases` are called from inside
loops that run once per transaction. Nothing was wrong with any of them
individually; the file is small and parsing it is fast. It is fast thirty-six
times in a row and that is a third of a second.

## Why this is safe to cache when a general one would not be

**The key is a hash of the file's contents.** There is nothing to remember to
invalidate and no TTL to tune: a file that changes changes its key, whoever
changed it and however fast.

It was a stat — path, size and `st_mtime_ns` — for about an hour, on the
reasoning that nanosecond mtimes make a same-length write in the same instant a
theoretical hole. **That reasoning was wrong, and one test said so.** Measured
here, five successive `write` + `replace` calls on both `/tmp` and the repo's
own filesystem: *one* distinct `(size, mtime_ns)` between them. The kernel
serves a cached coarse clock, so rapid writes genuinely share a timestamp, and
`reminders.save` — which writes `hour: 7` then `hour: 8`, the same length,
milliseconds apart — read its own stale copy back and stopped bumping the
calendar SEQUENCE.

Hashing costs what the stat was trying to avoid paying, and it is not close:
on `config/rules.yaml`, 11 KB, `yaml.safe_load` is **12.68ms** and reading the
whole file plus `sha256` is **0.017ms**. The thing that is slow is parsing, not
reading.

## And why each caller gets its own copy

The parsed value is **deep-copied on the way out**. Callers treat what they get
back as theirs: `load_rules()`'s result is mutated in places, and a shared dict
handed to twenty callers is twenty chances for one of them to corrupt the
config every later reader sees. A deepcopy of a config file is tens of
microseconds against a parse of ten milliseconds, so the safety is close to
free — measured, not assumed.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

#: `(resolved path, content digest) -> parsed`.
_CACHE: dict[tuple[str, bytes], object] = {}

#: How many file states to remember. Config files are few and small; the useful
#: entries are the current version of each, plus a little room for the states
#: either side of a write.
_MAX = 32


def forget_everything() -> None:
    """Drop everything. For tests, and for a caller that knows better."""
    _CACHE.clear()


def read_yaml(path: Path, default=None):
    """`yaml.safe_load` of this file, cached on the file's own stat.

    Returns `default` (a fresh `{}` unless given) for a file that does not
    exist, and for one that parses to nothing — the same answer every caller
    was already writing as `or {}`.

    Raises whatever `yaml` raises. A broken config file is not something to
    paper over, and every caller here already handles it.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return {} if default is None else copy.deepcopy(default)

    key = (str(path.resolve()), hashlib.sha256(raw).digest())
    if key in _CACHE:
        return copy.deepcopy(_CACHE[key])

    loaded = yaml.safe_load(raw.decode("utf-8"))
    if loaded is None:
        loaded = {} if default is None else default
    if len(_CACHE) >= _MAX:
        # Insertion order, so a plain FIFO. A cache of this size does not need
        # the bookkeeping an LRU would cost.
        del _CACHE[next(iter(_CACHE))]
    _CACHE[key] = loaded
    return copy.deepcopy(loaded)
