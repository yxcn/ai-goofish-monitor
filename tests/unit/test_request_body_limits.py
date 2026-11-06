"""登录态 / prompt / 账号写接口的请求体上限：超限 422，不落盘。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import accounts, login_state, prompts


def _client(router) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_login_state_rejects_oversized_body(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client(login_state.router)
    too_big = '"' + "x" * login_state.MAX_LOGIN_STATE_CHARS + '"'  # 合法 JSON，但超过上限

    resp = client.post("/api/login-state", json={"content": too_big})

    assert resp.status_code == 422
    assert not (tmp_path / "xianyu_state.json").exists()

    ok = client.post("/api/login-state", json={"content": "{}"})
    assert ok.status_code == 200
    assert (tmp_path / "xianyu_state.json").read_text(encoding="utf-8") == "{}"


def test_prompt_update_rejects_oversized_body():
    client = _client(prompts.router)

    resp = client.put(
        "/api/prompts/definitely_missing_prompt_for_test.txt",
        json={"content": "x" * (prompts.MAX_PROMPT_CHARS + 1)},
    )
    assert resp.status_code == 422

    # 不超限时才进入 handler：文件不存在 -> 404，说明上面的 422 来自长度校验
    resp = client.put("/api/prompts/definitely_missing_prompt_for_test.txt", json={"content": "x" * 10})
    assert resp.status_code == 404


def test_account_endpoints_reject_oversized_body(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # 其他测试可能通过 load_dotenv(override=True) 把仓库 .env 的 ACCOUNT_STATE_DIR 留在 os.environ 里
    monkeypatch.delenv("ACCOUNT_STATE_DIR", raising=False)
    client = _client(accounts.router)
    too_big = "x" * (accounts.MAX_ACCOUNT_STATE_CHARS + 1)

    assert client.post("/api/accounts", json={"name": "acc", "content": too_big}).status_code == 422
    assert client.put("/api/accounts/acc", json={"content": too_big}).status_code == 422
    assert not (tmp_path / "state").exists()

    assert client.post("/api/accounts", json={"name": "acc", "content": "{}"}).status_code == 200
    assert (tmp_path / "state" / "acc.json").read_text(encoding="utf-8") == "{}"
