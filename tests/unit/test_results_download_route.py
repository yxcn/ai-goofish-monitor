"""结果文件下载接口：错误走 HTTP 状态码，不再 200 + {"error": ...}。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import results


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # SQLite 落在临时目录
    app = FastAPI()
    app.include_router(results.router)
    return TestClient(app)


def test_download_rejects_path_traversal_with_400(client):
    resp = client.get("/api/results/files/a..b.jsonl")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "非法的文件路径"
    assert "error" not in resp.json()


def test_download_rejects_non_jsonl_with_400(client):
    resp = client.get("/api/results/files/notes.txt")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "只能下载 .jsonl 文件"


def test_download_missing_file_is_404(client):
    resp = client.get("/api/results/files/missing_full_data.jsonl")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "文件不存在"
