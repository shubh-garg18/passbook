"""Reading a config file without re-parsing it. SPEC §101.4.

Profiling the two queries the Payees page waits on found that a third of each
was `yaml.safe_load` — 10 parses per `/payees` request and **36** per
`/reapply`, because `load_rules`, `load_accounts` and `load_payee_aliases` are
called from inside loops that run once per transaction.

The tests here are about the half that makes it safe rather than fast: a file
that changes is re-read, and what a caller gets back is theirs to mutate.
"""

from __future__ import annotations

import pytest
import yaml

from passbook import yamlfile
from passbook.yamlfile import read_yaml


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("categories:\n  Food:\n    payees: [A]\n", encoding="utf-8")
    return path


def test_it_parses_the_file(config):
    assert read_yaml(config)["categories"]["Food"]["payees"] == ["A"]


def test_a_second_read_does_not_parse_again(config, monkeypatch):
    read_yaml(config)
    monkeypatch.setattr(
        yamlfile.yaml, "safe_load", lambda _text: pytest.fail("re-parsed a file that had not changed")
    )
    assert read_yaml(config)["categories"]["Food"]["payees"] == ["A"]


def test_a_changed_file_is_re_read(config):
    """The whole safety argument. There is no TTL and nothing to invalidate by
    hand — the key is the file's own size and mtime, so a write moves it."""
    read_yaml(config)
    config.write_text("categories:\n  Food:\n    payees: [A, B]\n", encoding="utf-8")
    assert read_yaml(config)["categories"]["Food"]["payees"] == ["A", "B"]


def test_a_same_length_write_in_the_same_instant_is_still_seen(config):
    """The one this was keyed on a stat for, until a test said otherwise.

    Measured on this machine: five `write` + `replace` calls in a row produce
    ONE distinct `(size, st_mtime_ns)` between them — the kernel serves a cached
    coarse clock. `reminders.save` writes `hour: 7` then `hour: 8`, the same
    length, milliseconds apart, and read its own stale copy back.

    Written without any sleeping or `utime`, on purpose: a test that has to slow
    down to pass is testing the clock rather than the cache.
    """
    read_yaml(config)
    config.write_text("categories:\n  Food:\n    payees: [Z]\n", encoding="utf-8")
    assert read_yaml(config)["categories"]["Food"]["payees"] == ["Z"]


def test_an_atomic_replace_is_seen_too(config):
    """How every writer here actually writes: to a temp file, then `replace`.
    The renamed file carries the TEMP file's timestamps, which is what made the
    stat key fail in the first place."""
    read_yaml(config)
    tmp = config.with_suffix(".tmp")
    tmp.write_text("categories:\n  Food:\n    payees: [Q]\n", encoding="utf-8")
    tmp.replace(config)
    assert read_yaml(config)["categories"]["Food"]["payees"] == ["Q"]


def test_each_caller_gets_its_own_copy(config):
    """`load_rules()`'s result is mutated in places. A shared dict handed to
    twenty callers is twenty chances to corrupt the config every later reader
    sees — and the corruption would outlive the request that caused it."""
    first = read_yaml(config)
    first["categories"]["Food"]["payees"].append("MUTATED")
    assert read_yaml(config)["categories"]["Food"]["payees"] == ["A"]


def test_a_missing_file_is_an_empty_mapping(tmp_path):
    assert read_yaml(tmp_path / "nope.yaml") == {}


def test_an_empty_file_is_an_empty_mapping(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    assert read_yaml(path) == {}


def test_a_broken_file_still_raises(tmp_path):
    """A config file that will not parse is not something to paper over — every
    caller here turns it into a named error the operator can act on."""
    path = tmp_path / "broken.yaml"
    path.write_text("categories: [unclosed\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        read_yaml(path)


def test_the_cache_does_not_grow_without_bound(tmp_path):
    for index in range(yamlfile._MAX * 2):
        path = tmp_path / f"{index}.yaml"
        path.write_text(f"n: {index}\n", encoding="utf-8")
        read_yaml(path)
    assert len(yamlfile._CACHE) <= yamlfile._MAX
