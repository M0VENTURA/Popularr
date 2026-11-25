"""The collection popularity CSV runs as a job, not inside the request.

Reported: downloading the collection popularity CSV timed out. Building the
whole ``tracks`` table in the request is what outlived the proxy; the job keeps
the file on disk and the Config page/dashboard poll for it.
"""

from __future__ import annotations

import os

import pytest

from services.popularity import popularity_report_export_service as svc


class _RunNow:
    def __init__(self, *args, **kwargs):
        self.target = kwargs.get("target")
        self.args = kwargs.get("args") or args
        self.daemon = kwargs.get("daemon", False)
        self.name = kwargs.get("name", "")

    def start(self):
        self.target(*self.args)


@pytest.fixture()
def job_env(monkeypatch):
    monkeypatch.setattr(svc.threading, "Thread", _RunNow)
    monkeypatch.setattr(
        "services.popularity.popularity_report_service.build_collection_popularity_report",
        lambda: "artist,title\nA,One\nB,Two\n",
    )
    monkeypatch.setattr(
        "services.popularity.popularity_report_service.ensure_playlist_index",
        lambda: None,
    )
    monkeypatch.setattr(
        "services.popularity.popularity_report_service.popularity_report_filename",
        lambda: "popularity.csv",
    )
    yield
    for job in list(svc._JOBS.values()):
        path = job.get("path")
        if path and os.path.exists(path):
            os.remove(path)
    svc._JOBS.clear()


class TestTheReportJob:
    def test_it_writes_the_csv_and_exposes_a_download(self, job_env):
        job = svc.create_report_job()
        assert job["status"] == "done"
        assert job["rows"] == 2
        assert job["download_url"].endswith(job["id"])

        found = svc.report_file(job["id"])
        assert found is not None
        path, filename = found
        assert filename == "popularity.csv"
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        assert body.startswith("\ufeff")
        assert "A,One" in body

    def test_the_latest_completed_job_is_what_the_dashboard_shows(self, job_env):
        first = svc.create_report_job()
        second = svc.create_report_job()
        latest = svc.latest_completed_job()
        assert latest is not None
        assert latest["id"] == second["id"]
        assert latest["id"] != first["id"]

    def test_an_unknown_job_has_no_file(self, job_env):
        assert svc.report_file("nope") is None
        assert svc.get_report_job("nope") is None
