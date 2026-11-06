"""FsCanonicalizer edge cases beyond the shared acceptance tests.

The worked examples live in test_canonicalizer_contract.py. These cover inputs
where a naive shell split would report a write as a read, or merge calls that
bash treats differently.
"""

import pytest

from fuzzytv.eval.calls import bash_call
from fuzzytv.eval.canonicalizers import RealCanonicalizer
from fuzzytv.eval.workloads import generate_fs
from fuzzytv.fs import FsCanonicalizer
from fuzzytv.interfaces import CWD, CallAnalysis, CallContext, fs_dir, fs_file
from fuzzytv.state.tracker import StateTracker

APP = CallContext(cwd="/app")
SRC = CallContext(cwd="/app/src")
NO_CWD = CallContext()
FS = FsCanonicalizer()


def analyze(command: str, ctx: CallContext = APP) -> CallAnalysis:
    return FS.analyze(bash_call(command), ctx)


# -------------------------------------------------- hidden writes and chains


@pytest.mark.parametrize(
    "command",
    [
        "cat a.txt;ls",
        "cat a.txt&",
        "cat a.txt|wc",
        "cat<a.txt",
        "(cat a.txt)",
        "cat a.txt\nrm a.txt",
        "cat a.txt\r",
        "ls>out.txt",
        "ls > out.txt",
        "cat a.txt > b.txt",
        "cat a.txt >> b.txt",
        "> out.txt ls",
        "grep foo a.txt > out.txt",
        "wc -l a.txt > out.txt",
        "echo hi 2> err.txt",
        "echo hi 2>err.txt",
        "echo hi >&2",
        "echo hi &> out.txt",
        "echo hi >| out.txt",
        "echo hi > a.txt > b.txt",
        "echo > a.txt hi",
        "echo hi >",
        "X=1 cat a.txt",
        "/bin/cat a.txt",
        "cat a.txt # comment",
        "rm a.txt",
        "mv a.txt b.txt",
        "touch a.txt",
        "mkdir d",
    ],
)
def test_commands_that_may_write_or_chain_are_fully_unknown(command: str) -> None:
    a = analyze(command)
    assert a.canonical is None
    assert a.reads_unknown and a.writes_unknown
    assert a.reason


def test_no_space_redirect_is_seen_as_a_write() -> None:
    a = analyze("echo hi>out.txt")
    assert a.canonical == "echo hi > /app/out.txt"
    assert a.writes == {fs_file("/app/out.txt"), fs_dir("/app")}


def test_quoted_operators_are_literal_text() -> None:
    a = analyze("echo '>' 'a;b' > out.txt")
    assert a.writes == {fs_file("/app/out.txt"), fs_dir("/app")}
    assert analyze("echo '>' x").writes == frozenset()
    assert analyze("cat 'a;b'").reads == {fs_file("/app/a;b")}


def test_quoted_digit_before_redirect_is_text_not_a_descriptor() -> None:
    assert analyze('echo "2">out.txt').writes == {fs_file("/app/out.txt"), fs_dir("/app")}


# ---------------------------------------------------------------- expansions


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("cat *.txt", "glob"),
        ("cat a?.txt", "glob"),
        ("cat [ab].txt", "glob"),
        ("cat $HOME/a.txt", "shell expansion"),
        ('cat "$HOME/a.txt"', "shell expansion"),
        ("cat `echo a.txt`", "shell expansion"),
        ('cat "`echo a.txt`"', "shell expansion"),
        ("cat ~/a.txt", "tilde expansion"),
        ("cat a\\ b", "backslash escape"),
        ('cat "a\\b"', "backslash escape"),
        ("echo hi!", "history expansion"),
        ('echo "hi!"', "history expansion"),
        ("cat {a,b}.txt", "brace expansion"),
        ("cat a{1..3}.txt", "brace expansion"),
        ('echo {"debug":true,"level":5} > c.json', "brace expansion"),
        ("cat 'a", "unterminated quote"),
        ('cat "a', "unterminated quote"),
    ],
)
def test_expansions_are_unknown(command: str, reason: str) -> None:
    a = analyze(command)
    assert a.canonical is None
    assert a.reads_unknown and a.writes_unknown
    assert a.reason == reason


def test_braces_without_expansion_are_literal() -> None:
    a = analyze('echo {"debug":true} > c.json')
    assert a.writes == {fs_file("/app/c.json"), fs_dir("/app")}
    assert a.canonical == "echo '{debug:true}' > /app/c.json"


def test_single_quotes_keep_expansion_characters_literal() -> None:
    a = analyze("""echo '{"debug":true,"level":5}' > c.json""")
    assert a.writes == {fs_file("/app/c.json"), fs_dir("/app")}
    assert analyze("cat '*.txt'").reads == {fs_file("/app/*.txt")}
    assert analyze("cat '$HOME'").reads == {fs_file("/app/$HOME")}


# -------------------------------------------------------- canonical identity


def test_quoting_keeps_word_boundaries_apart() -> None:
    assert analyze("cat 'a /app/b'").canonical != analyze("cat a /app/b").canonical
    assert analyze("echo 'a b' > f").canonical != analyze("echo a b > f").canonical


def test_equivalent_quoting_of_the_same_word_matches() -> None:
    assert analyze("cat 'a.txt'").canonical == analyze("cat a.txt").canonical
    assert analyze('cat "a b"').canonical == analyze("cat a' 'b").canonical


def test_append_and_overwrite_differ() -> None:
    assert analyze("echo x > f").canonical != analyze("echo x >> f").canonical


def test_echo_without_redirect_depends_on_nothing() -> None:
    a = analyze("echo  hi   there")
    assert a.canonical == "echo hi there"
    assert a.fuzzy_eligible and not a.reads


# ------------------------------------------------------------ path resolution


@pytest.mark.parametrize(
    ("path", "ctx", "expected"),
    [
        ("../x", SRC, "/app/x"),
        ("./../x", SRC, "/app/x"),
        ("../../x", SRC, "/x"),
        ("../../../x", SRC, "/x"),
        ("a/./b", APP, "/app/a/b"),
        ("a//b", APP, "/app/a/b"),
        ("//app/x", APP, "/app/x"),
        ("/app/./x", NO_CWD, "/app/x"),
    ],
)
def test_lexically_safe_paths_resolve(path: str, ctx: CallContext, expected: str) -> None:
    assert analyze(f"cat {path}", ctx).reads == {fs_file(expected)}


@pytest.mark.parametrize(
    "path", ["a/../b", "missing/../f.txt", "/app/../etc/x", "../a/../b", "/../x"]
)
def test_dotdot_inside_a_path_is_not_collapsed(path: str) -> None:
    a = analyze(f"cat {path}", SRC)
    assert a.canonical is None and a.reads_unknown and not a.writes_unknown
    assert analyze(f"echo hi > {path}", SRC).writes_unknown
    assert analyze(f"cd {path}", SRC).writes_unknown


@pytest.mark.parametrize("path", ["a.txt/", "a.txt/.", ".", "..", "d/..", "/"])
def test_operand_that_must_be_a_directory_is_not_a_file_read(path: str) -> None:
    assert not analyze(f"cat {path}").fuzzy_eligible
    assert analyze(f"echo hi > {path}").writes_unknown


@pytest.mark.parametrize("path", ["/proc/self/status", "/dev/urandom", "/sys/x", "/dev"])
def test_volatile_paths_are_not_eligible(path: str) -> None:
    assert not analyze(f"cat {path}").fuzzy_eligible
    assert not analyze(f"ls {path}").fuzzy_eligible
    assert analyze(f"echo hi > {path}").writes_unknown


def test_relative_path_into_a_volatile_tree_is_not_eligible() -> None:
    assert not analyze("cat status", CallContext(cwd="/proc/self")).fuzzy_eligible
    assert not analyze("cat ../proc/version", APP).fuzzy_eligible


def test_similarly_named_path_is_not_volatile() -> None:
    assert analyze("cat /device/x").fuzzy_eligible


def test_empty_path_is_not_eligible() -> None:
    assert not analyze("cat ''").fuzzy_eligible
    assert analyze("echo hi > ''").writes_unknown
    assert analyze("cd ''").writes_unknown


@pytest.mark.parametrize("cwd", ["app", "/app/", "/app/../x", "//app", ""])
def test_unusable_cwd_is_treated_as_unknown(cwd: str) -> None:
    ctx = CallContext(cwd=cwd)
    assert analyze("cat a.txt", ctx).reads_unknown
    assert analyze("/bin/true", ctx).reason  # never raises
    assert analyze("cat /app/a.txt", ctx).fuzzy_eligible
    assert analyze("echo hi > a.txt", ctx).writes_unknown


# ----------------------------------------------------------- cat and ls flags


@pytest.mark.parametrize(
    "command", ["cat", "cat -", "cat -n a.txt", "cat a.txt -n", "cat -- a.txt", "ls -a", "ls a -l"]
)
def test_flags_and_stdin_are_read_only_unknown(command: str) -> None:
    a = analyze(command)
    assert a.canonical is None
    assert a.reads_unknown and not a.writes_unknown
    assert not a.reads and not a.writes


@pytest.mark.parametrize("command", ["grep x a.txt", "head a.txt", "tail -n 1 a.txt", "wc -l a"])
def test_read_only_tools_do_not_disable_later_calls(command: str) -> None:
    tracker = StateTracker("t")
    a = analyze(command)
    assert a.reads_unknown and not a.writes_unknown and a.canonical is None
    assert tracker.fuzzy_key(a) is None
    tracker.observe(a)
    assert not tracker.fuzzy_disabled


# ------------------------------------------------------------------------- ls


@pytest.mark.parametrize("operand", ["", ".", "./", "/app", "/app/", "..", "../src", "/"])
def test_ls_of_cwd_or_ancestor_reads_only_the_directory(operand: str) -> None:
    a = analyze(f"ls {operand}", SRC)
    assert a.fuzzy_eligible
    assert len(a.reads) == 1 and next(iter(a.reads)).startswith("fs:dir:")


def test_ls_of_other_operand_also_depends_on_a_file_at_that_path() -> None:
    a = analyze("ls notes")
    assert a.reads == {fs_dir("/app/notes"), fs_file("/app/notes")}


def test_ls_is_invalidated_by_creating_a_file_at_its_operand() -> None:
    read = analyze("ls notes")
    before = StateTracker("t")
    after = StateTracker("t")
    after.observe(analyze("echo hi > notes"))
    assert before.fuzzy_key(read) != after.fuzzy_key(read)


def test_ls_keeps_spelling_unless_operand_is_known_to_be_a_directory() -> None:
    # `ls ./f` prints "./f" and `ls f` prints "f" when f is a file.
    spellings = ["ls f", "ls ./f", "ls /app/f", "ls f/"]
    assert len({analyze(s).canonical for s in spellings}) == len(spellings)
    assert analyze("ls ../f", SRC).canonical == analyze("ls /app/f", APP).canonical + " # as ../f"


def test_ls_multiple_operands_is_read_only_unknown() -> None:
    a = analyze("ls a b")
    assert a.reads_unknown and not a.writes_unknown


# ------------------------------------------------------------------------- cd


@pytest.mark.parametrize("command", ["cd", "cd -", "cd a b", "cd -P a"])
def test_cd_with_undetermined_target_is_unknown(command: str) -> None:
    assert analyze(command).writes_unknown


@pytest.mark.parametrize(
    ("command", "canonical"),
    [("cd src/", "cd /app/src"), ("cd ./src", "cd /app/src"), ("cd .", "cd /app"), ("cd ..", "cd /")],
)
def test_cd_resolves(command: str, canonical: str) -> None:
    a = analyze(command)
    assert a.canonical == canonical
    assert a.writes == {CWD}


def test_cd_absolute_without_cwd_is_known() -> None:
    assert analyze("cd /app", NO_CWD).writes == {CWD}


# ---------------------------------------------------- integration with others


def test_real_canonicalizer_routes_bash_here() -> None:
    real = RealCanonicalizer()
    assert real.analyze(bash_call("cat a.txt"), APP) == analyze("cat a.txt")


def test_never_more_eligible_than_the_oracle_and_writes_agree() -> None:
    """Over the fs workload generator: same writes as the spec, never a riskier read."""
    workload = generate_fs(seed=3, tasks=3, rollouts=8)
    for task in workload.tasks:
        for rollout in task.rollouts:
            for step in rollout:
                real = FS.analyze(step.call, step.ctx)
                if real.fuzzy_eligible:
                    assert not step.oracle["writes"] and step.oracle["canonical"] is not None
                    assert set(step.oracle["reads"]) <= real.reads
                elif real.canonical is not None:
                    assert real.writes == set(step.oracle["writes"])
