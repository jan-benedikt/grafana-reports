from __future__ import annotations

import time

from reporting.util import sweep_old_pdfs


def test_sweep_old_pdfs_removes_only_stale_artifacts(tmp_path):
    old_pdf = tmp_path / "old.pdf"
    old_png = tmp_path / "old.png"
    fresh_pdf = tmp_path / "fresh.pdf"
    other = tmp_path / "keep.txt"
    for f in (old_pdf, old_png, fresh_pdf, other):
        f.write_bytes(b"x")
    stale_mtime = time.time() - 7200
    import os

    os.utime(old_pdf, (stale_mtime, stale_mtime))
    os.utime(old_png, (stale_mtime, stale_mtime))
    os.utime(other, (stale_mtime, stale_mtime))

    removed = sweep_old_pdfs(tmp_path, max_age_s=3600)

    assert removed == 2
    assert not old_pdf.exists()
    assert not old_png.exists()
    assert fresh_pdf.exists()
    assert other.exists()  # non-artifact files are untouched


def test_sweep_disabled_with_nonpositive_age(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    assert sweep_old_pdfs(tmp_path, max_age_s=0) == 0
    assert pdf.exists()
