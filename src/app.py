"""
新架构的主应用入口
整合所有路由和服务
"""
import asyncio
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.api.routes import (
    dashboard,
    health,
    tasks,
    logs,
    settings,
    prompts,
    results,
    login_state,
    websocket,
    accounts,
)
from src.api.auth import (
    clear_session_cookie,
    is_default_password,
    require_auth,
    set_session_cookie,
    verify_password,
)
from src.api.dependencies import (
    set_process_service,
    set_scheduler_service,
    set_task_generation_service,
)
from src.services.task_service import TaskService
from src.services.process_service import ProcessService
from src.services.scheduler_service import SchedulerService
from src.services.task_log_cleanup_service import cleanup_task_logs
from src.services.task_generation_service import TaskGenerationService
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.infrastructure.config.settings import settings as app_settings


# 全局服务实例
process_service = ProcessService()
scheduler_service = SchedulerService(process_service)
task_generation_service = TaskGenerationService()


async def _sync_task_runtime_status(task_id: int, is_running: bool) -> None:
    task_service = TaskService(SqliteTaskRepository())
    task = await task_service.get_task(task_id)
    if not task or task.is_running == is_running:
        return
    await task_service.update_task_status(task_id, is_running)
    await websocket.broadcast_message(
        "task_status_changed",
        {"id": task_id, "is_running": is_running},
    )


process_service.set_lifecycle_hooks(
    on_started=lambda task_id: _sync_task_runtime_status(task_id, True),
    on_stopped=lambda task_id: _sync_task_runtime_status(task_id, False),
)

# 设置全局 ProcessService 实例供依赖注入使用
set_process_service(process_service)
set_scheduler_service(scheduler_service)
set_task_generation_service(task_generation_service)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    print("正在启动应用...")
    if is_default_password():
        print(
            "\n" + "=" * 70 + "\n"
            "⚠️  Web 登录仍是默认账号密码 admin / admin123。\n"
            "    所有 /api 接口（任务、登录态 cookie、AI Key）都只靠这组密码保护，\n"
            "    请在 .env 里设置 WEB_USERNAME / WEB_PASSWORD 后重启，尤其是绑定 0.0.0.0 或 Docker 部署时。\n"
            + "=" * 70 + "\n"
        )
    bootstrap_sqlite_storage()
    cleanup_task_logs(keep_days=app_settings.task_log_retention_days)

    # 重置所有任务状态为停止
    task_repo = SqliteTaskRepository()
    task_service = TaskService(task_repo)
    tasks_list = await task_service.get_all_tasks()

    for task in tasks_list:
        if task.is_running:
            await task_service.update_task_status(task.id, False)

    # 加载定时任务
    await scheduler_service.reload_jobs(tasks_list)
    scheduler_service.start()

    print("应用启动完成")

    yield

    # 关闭时
    print("正在关闭应用...")
    scheduler_service.stop()
    await process_service.stop_all()
    print("应用已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="闲鱼智能监控机器人",
    description="基于AI的闲鱼商品监控系统",
    version="2.0.0",
    lifespan=lifespan
)

# 注册路由：所有 /api 路由统一要求认证（Basic 或登录后签发的会话 cookie）；
# /ws 在 websocket 端点里自己校验（WebSocket 依赖里抛 HTTPException 无法正确关闭连接）
AUTH_DEPENDENCIES = [Depends(require_auth)]
app.include_router(tasks.router, dependencies=AUTH_DEPENDENCIES)
app.include_router(dashboard.router, dependencies=AUTH_DEPENDENCIES)
app.include_router(health.router, dependencies=AUTH_DEPENDENCIES)
app.include_router(logs.router, dependencies=AUTH_DEPENDENCIES)
app.include_router(settings.router, dependencies=AUTH_DEPENDENCIES)
app.include_router(prompts.router, dependencies=AUTH_DEPENDENCIES)
app.include_router(results.router, dependencies=AUTH_DEPENDENCIES)
app.include_router(login_state.router, dependencies=AUTH_DEPENDENCIES)
app.include_router(websocket.router)
app.include_router(accounts.router, dependencies=AUTH_DEPENDENCIES)

# 挂载静态文件
# 旧的静态文件目录（用于截图等）
app.mount("/static", StaticFiles(directory="static"), name="static")

# 挂载 Vue 3 前端构建产物
# 注意：需要在所有 API 路由之后挂载，以避免覆盖 API 路由
import os
if os.path.exists("dist"):
    app.mount("/assets", StaticFiles(directory="dist/assets"), name="assets")


# 健康检查端点
@app.get("/health")
async def health_check():
    """健康检查（无需认证）"""
    return {"status": "healthy", "message": "服务正常运行"}


# 登录 / 登出 / 会话检查
from fastapi import Request, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/status")
async def auth_status(payload: LoginRequest, request: Request, response: Response):
    """校验用户名密码；成功则签发 HttpOnly 会话 cookie。"""
    if verify_password(payload.username, payload.password):
        set_session_cookie(response, payload.username, secure=request.url.scheme == "https")
        return {"authenticated": True, "username": payload.username}
    # 失败时稍作停顿，让暴力尝试变慢
    await asyncio.sleep(0.5)
    raise HTTPException(status_code=401, detail="认证失败")


@app.post("/auth/logout")
async def auth_logout(response: Response):
    """清除会话 cookie。"""
    clear_session_cookie(response)
    return {"authenticated": False}


@app.get("/auth/me")
async def auth_me(username: str = Depends(require_auth)):
    """当前会话是否有效；前端启动时用它校验 localStorage 里的登录标记。"""
    return {"authenticated": True, "username": username}


# 主页路由 - 服务 Vue 3 SPA
from fastapi.responses import JSONResponse

@app.get("/")
async def read_root(request: Request):
    """提供 Vue 3 SPA 的主页面"""
    if os.path.exists("dist/index.html"):
        return FileResponse("dist/index.html")
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "前端构建产物不存在，请先运行 cd web-ui && npm run build"}
        )


# Catch-all 路由 - 处理所有前端路由（必须放在最后）
@app.get("/{full_path:path}")
async def serve_spa(request: Request, full_path: str):
    """
    Catch-all 路由，将所有非 API 请求重定向到 index.html
    这样可以支持 Vue Router 的 HTML5 History 模式
    """
    # 如果请求的是静态资源（如 favicon.ico），返回 404
    if full_path.endswith(('.ico', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js', '.json')):
        return JSONResponse(status_code=404, content={"error": "资源未找到"})

    # 其他所有路径都返回 index.html，让前端路由处理
    if os.path.exists("dist/index.html"):
        return FileResponse("dist/index.html")
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "前端构建产物不存在，请先运行 cd web-ui && npm run build"}
        )


if __name__ == "__main__":
    import uvicorn
    from src.infrastructure.config.settings import settings

    print(f"启动新架构应用，端口: {app_settings.server_port}")
    uvicorn.run(app, host="0.0.0.0", port=app_settings.server_port)
