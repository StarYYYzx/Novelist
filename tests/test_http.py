"""M3 HTTP 服务测试（docs/07 §6.2）。用 fastapi TestClient + monkeypatch 工作区根。"""

import pytest

from novelist import server as srv
from novelist.storage.checkpoint import Checkpoint
from novelist.storage.workspace import Workspace


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    """指向 tmp_path 工作区 的同步测试客户端（fastapi TestClient）。"""
    from fastapi.testclient import TestClient

    ws = Workspace(root=str(tmp_path))
    ws.create_project("proj-http")
    Checkpoint(ws).save("proj-http", {"id": "proj-http", "title": "HTTP", "pipeline_state": "立项", "event_seq": 0})

    monkeypatch.setattr(srv, "_workspace", lambda: Workspace(root=str(tmp_path)))
    client = TestClient(srv.app)
    return {"tmp_path": tmp_path, "client": client}


# ---------- 核心端点 ----------


def test_list_projects(fx):
    r = fx["client"].get("/projects")
    assert r.status_code == 200
    assert "proj-http" in r.json()["projects"]


def test_project_status(fx):
    r = fx["client"].get("/projects/proj-http/status")
    assert r.status_code == 200
    assert r.json()["id"] == "proj-http"
    assert r.json()["pipeline_state"] == "立项"


def test_run_pipeline(fx):
    r = fx["client"].post("/projects/proj-http/run", params={"to": "细纲"})
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "细纲"


def test_run_to_review_runs_consistency(fx):
    r = fx["client"].post("/projects/proj-http/run", params={"to": "审查"})
    assert r.status_code == 200, r.text
    assert "consistency" in r.json()


def test_write_chapter(fx):
    r = fx["client"].post("/projects/proj-http/chapters", params={"vol": 2, "ch": 3})
    assert r.status_code == 200, r.text
    assert r.json()["events_committed"] == 1
    # 草稿确实落盘
    ws = Workspace(root=str(fx["tmp_path"]))
    assert ws.draft_path("proj-http", 2, 3).exists()


def test_export(fx):
    ws = Workspace(root=str(fx["tmp_path"]))
    ch = ws.chapter_path("proj-http", 1, 1)
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text("HTTP 导出正文。", encoding="utf-8")
    r = fx["client"].get("/projects/proj-http/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "HTTP 导出正文" in r.text


def test_pending_decisions_and_decide(fx):
    from novelist.core.approval import ApprovalQueue

    ws = Workspace(root=str(fx["tmp_path"]))
    q = ApprovalQueue(persist_dir=ws._abs("proj-http/logs"))
    req = q.submit("publish", {"vol": 1, "ch": 1}, _sess(), "publish")

    r = fx["client"].get("/pending-decisions")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["pending"]]
    assert req.id in ids

    r2 = fx["client"].post(f"/decisions/{req.id}")
    assert r2.status_code == 200
    assert r2.json()["decision"] == "allow"

    r3 = fx["client"].get("/pending-decisions")
    assert req.id not in [p["id"] for p in r3.json()["pending"]]


def _sess():
    from novelist.core.session import SessionInfo

    return SessionInfo(project_id="proj-http", agent="test")
