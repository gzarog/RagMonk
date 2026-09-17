from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.core.models import JobStatus
from ragmonk.storage.repositories import jobs_repo
from ragmonk.storage.sqlite import connect


def test_processing_job_left_by_a_crash_is_requeued_on_next_index(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("print('a')")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    add_result = runner.invoke(app, ["source", "add", str(source_dir)])
    assert add_result.exit_code == 0
    assert runner.invoke(app, ["index"]).exit_code == 0

    project_id = paths.project_id_for_path(source_dir)
    conn = connect(paths.project_db_path(project_id, ragmonk_home))
    try:
        job_id = conn.execute("SELECT id FROM index_jobs LIMIT 1").fetchone()["id"]
        conn.execute(
            "UPDATE index_jobs SET status = ? WHERE id = ?", (JobStatus.PROCESSING.value, job_id)
        )
        stuck = jobs_repo.get(conn, job_id)
        assert stuck is not None and stuck.status is JobStatus.PROCESSING
    finally:
        conn.close()

    # A fresh `index` run must recover the stuck job (PROCESSING -> QUEUED)
    # and then successfully complete it, simulating a process that crashed
    # mid-job and was restarted.
    assert runner.invoke(app, ["index"]).exit_code == 0

    conn = connect(paths.project_db_path(project_id, ragmonk_home))
    try:
        recovered = jobs_repo.get(conn, job_id)
        assert recovered is not None
        assert recovered.status is JobStatus.COMPLETED
    finally:
        conn.close()
