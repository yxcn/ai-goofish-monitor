"""/api/settings/status 读到的是 reload_settings() 之后的 scraper_settings，而不是 import 时的陈旧对象。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import dependencies as deps
from src.api.routes import settings as settings_routes
from src.infrastructure.config.settings import reload_settings


class _IdleProcessService:
    processes: dict = {}


def test_status_reflects_reloaded_scraper_settings(tmp_path):
    app = FastAPI()
    app.include_router(settings_routes.router)
    app.dependency_overrides[deps.get_process_service] = lambda: _IdleProcessService()
    client = TestClient(app)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.chdir(tmp_path)  # .env / 登录态文件都落在临时目录
            mp.setenv("RUN_HEADLESS", "0")
            mp.setenv("RUNNING_IN_DOCKER", "yes")
            reload_settings()
            body = client.get("/api/settings/status").json()
            assert body["headless_mode"] is False
            assert body["running_in_docker"] is True

            mp.setenv("RUN_HEADLESS", "1")
            mp.setenv("RUNNING_IN_DOCKER", "0")
            reload_settings()
            body = client.get("/api/settings/status").json()
            assert body["headless_mode"] is True
            assert body["running_in_docker"] is False
    finally:
        reload_settings()  # 回到仓库目录 + 原环境，别影响其他测试
