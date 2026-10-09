"""GameLife 本地网页面板（webapp 包）。

只依赖标准库 http.server + 既有 Jinja2；不引入 Flask/FastAPI 等新依赖。
业务逻辑全部复用 core / engine / render 既有模块，本包只做「接线 + 传输」。

模块：
    api.py     —— 数据/API 层（把引擎调用封装为可 JSON 化的 dict）
    server.py  —— HTTP 服务 + 路由 + run() 入口
"""

__all__ = ["api", "server"]