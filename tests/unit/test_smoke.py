"""Environment smoke test: package layout and key dependencies import."""


def test_package_imports():
    import fuzzytv
    import fuzzytv.fs
    import fuzzytv.sql
    import fuzzytv.state  # noqa: F401


def test_sqlglot_available():
    import sqlglot

    assert sqlglot.transpile("select  1")[0] == "SELECT 1"


def test_tvclient_importable():
    # tvclient is installed editable from upstream/tvclient (see README setup).
    from tvclient import TvcacheToolExecutor, ToolCall  # noqa: F401
