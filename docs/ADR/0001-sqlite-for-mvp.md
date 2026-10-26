# ADR-0001: MVP 使用 SQLite

- 问题：首个可运行切片需要哪种存储？
- 候选：SQLite / PostgreSQL。
- 决策：SQLite（`app/db.py`）。
- 理由：用户指定；单机零依赖即可跑通并测试；SQLAlchemy 抽象层可平滑迁移。
- 风险：并发写、多进程 Worker、审计扩容能力有限。
- 迁移成本：改用 PostgreSQL 仅需替换 `DATABASE_URL` 与连接参数，模型层不变。
- 重新评估条件：引入独立任务 Worker 或多实例部署时。
