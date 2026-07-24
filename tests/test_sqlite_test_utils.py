"""Regression coverage for test-only exact SQLite path ownership and cleanup."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.sqlite_test_utils import OwnedSQLitePaths


def test_owned_sqlite_fixture_allocates_multiple_exact_paths(owned_sqlite):
    first = owned_sqlite.path("first.db")
    second = owned_sqlite.path("second.sqlite")
    generated = owned_sqlite.new_path("run")

    assert first.parent == second.parent == generated.parent
    assert first.parent == first.parent.resolve()
    assert [first.name, second.name, generated.name] == [
        "first.db", "second.sqlite", "run-1.db"
    ]


@pytest.mark.parametrize("name", ["/tmp/escape.db", "../escape.db", "a/escape.db", ".", ".."])
def test_owned_sqlite_rejects_absolute_and_traversal_names(tmp_path: Path, name: str):
    paths = OwnedSQLitePaths(tmp_path / "owned")

    with pytest.raises(ValueError, match="relative filename"):
        paths.path(name)


def test_cleanup_removes_only_registered_exact_sqlite_artifacts(tmp_path: Path):
    paths = OwnedSQLitePaths(tmp_path / "owned")
    registered = paths.path("registered.db")
    unregistered = paths.directory / "keep.db"
    for artifact in (
        registered,
        Path(f"{registered}-wal"),
        Path(f"{registered}-shm"),
        Path(f"{registered}-journal"),
        unregistered,
    ):
        artifact.write_text("fixture-owned")

    paths.cleanup()

    assert not registered.exists()
    assert not Path(f"{registered}-wal").exists()
    assert not Path(f"{registered}-shm").exists()
    assert not Path(f"{registered}-journal").exists()
    assert unregistered.read_text() == "fixture-owned"


def test_cleanup_rejects_symlink_escape_without_touching_target(tmp_path: Path):
    paths = OwnedSQLitePaths(tmp_path / "owned")
    database = paths.path("linked.db")
    outside = tmp_path / "outside.db"
    outside.write_text("must survive")
    database.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        paths.cleanup()

    assert outside.read_text() == "must survive"
    assert database.is_symlink()


def test_cleanup_rejects_sidecar_symlink_escape_without_touching_target(tmp_path: Path):
    paths = OwnedSQLitePaths(tmp_path / "owned")
    database = paths.path("test.db")
    database.write_text("database")
    outside = tmp_path / "outside-wal"
    outside.write_text("must survive")
    Path(f"{database}-wal").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        paths.cleanup()

    assert database.exists()
    assert outside.read_text() == "must survive"


def _configure_inner_real_fixture(pytester) -> None:
    """Load the repository's actual fixture, not a copied implementation."""
    repo_root = Path(__file__).resolve().parent.parent
    pytester.makeconftest(
        textwrap.dedent(
            f"""
            import sys

            sys.path.insert(0, {str(repo_root)!r})
            from tests.conftest import owned_sqlite
            """
        )
    )


def _assert_inner_failure_cleanup(pytester, result, *, setup_error: bool = False) -> None:
    result.assert_outcomes(errors=1 if setup_error else 0, failed=0 if setup_error else 1)
    proof = json.loads((pytester.path / "cleanup-proof.json").read_text())
    for database in map(Path, proof["databases"]):
        assert not database.exists()
        assert not Path(f"{database}-wal").exists()
        assert not Path(f"{database}-shm").exists()
        assert not Path(f"{database}-journal").exists()
    unrelated = Path(proof["unrelated"])
    assert unrelated.read_text() == "must survive"


def _inner_test_source(
    database_names: tuple[str, ...], failure: str, *, setup_failure: bool = False
) -> str:
    names = repr(database_names)
    create = textwrap.dedent(
        """
        def create_artifacts(paths):
            for database in paths:
                connection = sqlite3.connect(database)
                connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY)")
                connection.close()
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(f"{database}{suffix}").write_text(suffix)
            unrelated = paths[0].parent / "unrelated-sibling.txt"
            unrelated.write_text("must survive")
            Path(__file__).with_name("cleanup-proof.json").write_text(
                json.dumps({"databases": [str(path) for path in paths],
                            "unrelated": str(unrelated)})
            )
        """
    )
    if setup_failure:
        case = textwrap.dedent(
            f"""
            @pytest.fixture
            def broken_setup(owned_sqlite):
                paths = [owned_sqlite.path(name) for name in {names}]
                create_artifacts(paths)
                raise RuntimeError("expected setup failure")

            def test_inner_failure(broken_setup):
                pass
            """
        )
        imports = "import pytest\n"
    else:
        case = textwrap.dedent(
            f"""
            def test_inner_failure(owned_sqlite):
                paths = [owned_sqlite.path(name) for name in {names}]
                create_artifacts(paths)
                {failure}
            """
        )
        imports = ""
    return "import json\nimport sqlite3\nfrom pathlib import Path\n" + imports + "\n" + create + "\n" + case


def test_owned_sqlite_cleanup_after_assertion_failure(pytester) -> None:
    _configure_inner_real_fixture(pytester)
    pytester.makefile(
        ".py",
        test_inner_failure=_inner_test_source(
            ("assertion.db",), "assert False, 'expected inner failure'"
        ),
    )

    _assert_inner_failure_cleanup(pytester, pytester.runpytest("-q"))


def test_owned_sqlite_cleanup_after_ordinary_exception(pytester) -> None:
    _configure_inner_real_fixture(pytester)
    pytester.makefile(
        ".py",
        test_inner_exception=_inner_test_source(
            ("exception.db",), "raise RuntimeError('expected inner exception')"
        ),
    )

    _assert_inner_failure_cleanup(pytester, pytester.runpytest("-q"))


def test_owned_sqlite_cleanup_after_fixture_setup_failure(pytester) -> None:
    _configure_inner_real_fixture(pytester)
    pytester.makefile(
        ".py",
        test_inner_setup_failure=_inner_test_source(
            ("setup.db",), "", setup_failure=True
        ),
    )

    _assert_inner_failure_cleanup(pytester, pytester.runpytest("-q"), setup_error=True)


def test_owned_sqlite_cleanup_for_multiple_databases_after_failure(pytester) -> None:
    _configure_inner_real_fixture(pytester)
    pytester.makefile(
        ".py",
        test_inner_multiple=_inner_test_source(
            ("first.db", "second.db"), "assert False, 'expected inner failure'"
        ),
    )

    _assert_inner_failure_cleanup(pytester, pytester.runpytest("-q"))


def test_owned_sqlite_cleanup_after_subprocess_pytest_failure(pytester) -> None:
    _configure_inner_real_fixture(pytester)
    pytester.makefile(
        ".py",
        test_inner_subprocess=_inner_test_source(
            ("subprocess.db",), "assert False, 'expected subprocess failure'"
        ),
    )

    _assert_inner_failure_cleanup(pytester, pytester.runpytest_subprocess("-q"))
