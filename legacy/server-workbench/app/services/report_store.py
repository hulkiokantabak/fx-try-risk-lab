from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Report


def purge_cycle_reports(session: Session, *, cycle_id: int, reports_dir: Path) -> None:
    base_dir = reports_dir.resolve()
    reports = list(
        session.scalars(
            select(Report)
            .where(Report.cycle_id == cycle_id)
            .order_by(Report.id.asc())
        ).all()
    )
    for report in reports:
        _delete_report_file(report.file_path, base_dir)
        session.delete(report)
    session.flush()


def _delete_report_file(file_path: str | None, base_dir: Path) -> None:
    if not file_path:
        return
    candidate = Path(file_path)
    try:
        resolved = candidate.resolve()
    except OSError:
        return
    if not _is_within_dir(resolved, base_dir):
        return
    if resolved.exists() and resolved.is_file():
        resolved.unlink()


def _is_within_dir(path: Path, base_dir: Path) -> bool:
    try:
        path.relative_to(base_dir)
        return True
    except ValueError:
        return False
