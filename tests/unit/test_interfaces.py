"""Tests for the shared contract itself (not for any canonicalizer)."""

import dataclasses

import pytest

from fuzzytv.interfaces import (
    CWD,
    CallAnalysis,
    CallContext,
    fs_dir,
    fs_file,
    sql_table,
    unknown,
)


def test_read_only_understood_call_is_eligible():
    # SELECT name FROM users WHERE id = 1
    a = CallAnalysis(
        canonical="SELECT name FROM users WHERE id = 1",
        reads=frozenset({sql_table("main", "users")}),
    )
    assert a.fuzzy_eligible


def test_write_is_not_eligible():
    # echo hi > a.txt  (cwd /app)
    a = CallAnalysis(
        canonical="echo hi > /app/a.txt",
        writes=frozenset({fs_file("/app/a.txt"), fs_dir("/app")}),
    )
    assert not a.fuzzy_eligible


def test_read_and_write_is_not_eligible():
    # UPDATE employees SET salary = 1 WHERE id = 1
    t = sql_table("main", "employees")
    a = CallAnalysis(canonical="UPDATE ...", reads=frozenset({t}), writes=frozenset({t}))
    assert not a.fuzzy_eligible


def test_cd_writes_cwd():
    a = CallAnalysis(canonical="cd /app/data", writes=frozenset({CWD}))
    assert not a.fuzzy_eligible


def test_unknown_reads_only_is_not_eligible_but_writes_nothing():
    # grep foo a.txt: unsupported but read-only; must not poison later calls
    a = CallAnalysis(canonical=None, reads_unknown=True, reason="unsupported command: grep")
    assert not a.fuzzy_eligible
    assert not a.writes_unknown
    assert not a.writes


def test_unknown_helper_fails_closed():
    a = unknown("unsupported command: python")
    assert a.canonical is None
    assert a.reads_unknown and a.writes_unknown
    assert a.reason == "unsupported command: python"
    assert not a.fuzzy_eligible


def test_missing_canonical_is_not_eligible():
    assert not CallAnalysis(canonical=None).fuzzy_eligible


def test_analysis_and_context_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        unknown("x").reads_unknown = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        CallContext(cwd="/app").cwd = "/"


def test_equal_analyses_compare_equal():
    # Reformatted SQL must produce an identical analysis.
    r = frozenset({sql_table("main", "users")})
    assert CallAnalysis("SELECT 1", reads=r) == CallAnalysis("SELECT 1", reads=r)


def test_state_key_names():
    assert sql_table("main", "users") == "sql:table:main.users"
    assert fs_file("/app/a.txt") == "fs:file:/app/a.txt"
    assert fs_dir("/app") == "fs:dir:/app"


@pytest.mark.parametrize("helper", [fs_file, fs_dir])
def test_relative_paths_rejected(helper):
    with pytest.raises(ValueError):
        helper("data/f.txt")
