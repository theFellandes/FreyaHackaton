"""The test suite never writes into the repository's output folders (AUD-59).

A test once wrote a fake "deployed agent ag" build summary into the real ``results/`` folder (the write was silent because
``finalize_run`` swallows ``OSError``). The autouse fixture ``_isolated_output_dirs`` of ``tests/conftest.py`` now points
``builder.paths`` at a temporary folder for every test; these tests prove it without asking for ``tmp_dirs``.
"""

from __future__ import annotations

from builder import paths


def test_unit_tests_never_point_builder_output_dirs_at_the_repo() -> None:
    repo = paths.REPO_ROOT.resolve()
    for directory in paths.output_dirs():
        assert not directory.resolve().is_relative_to(repo), directory


def test_the_output_dirs_exist_so_a_write_succeeds() -> None:
    for directory in paths.output_dirs():
        assert directory.is_dir()
        (directory / "probe.txt").write_text("ok", encoding="utf-8")
