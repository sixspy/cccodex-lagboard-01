# 源到达SLA

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 源到达SLA |
| 目录 | `06-lagboard` |
| 领域建议 | 数据采集、爬虫与 RPA |
| 接口 | http://127.0.0.1:10811 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

源时钟 vs 摄入时钟、分级处罚、降级可读不可写、补齐解冻

## 避开

别做成纯监控大盘；须有写路径与状态机

## 互斥切口候选

- 超SLA冻结 vs 降级只读
- 补齐整源解冻 vs 按业务日窗口解冻
- 只认源时钟 vs 双时钟取严

## 迟到分级落库（本仓锁定口径）

迟到分级互斥写法只允许二选一，本仓**选定甲路径并锁定**，详见 `docs/迟到分级说明.md`，运行期口径存于库内 `grade_rules` 表（`path=甲`、`rule_day` 当日、`lock_sha`）：

| 项 | 口径 |
|----|------|
| 甲路径（已选） | 到达行与源输出态**同一事务**改写；任一失败整笔回退（`503 / code=整笔回退 / rolled_back=true`） |
| 乙路径 | 不实现；请求携带 `path=乙` 返回 `409 / code=分级偏了` 并落偏差记录 |
| 分级函数 | 只由迟到分钟推出：`≤SLA` 正常可写、`≤2×SLA` 告警降级只读、`>2×SLA` 违约冻结 |
| 改口 | 当天不许改口：`POST /api/rules` 一律 `409 / 分级偏了` 并落记录 |
| 抢交 | 同一源到达并发抢交排队，先到落库，后到只记**迟到分级争用**（`409 / code=迟到分级争用`），不双写 |
| 对账 | `GET /api/reconcile` 比对源目录、到达列表、迟到详情、补采附页、水位卡片五处读数 |

复现夹具：`fixtures/late_grade_cross_tier.json`（迟到分钟跨档、源输出态未切）。
失败测：`tests/test_grade_failure.py`（半截成功即红）、`tests/test_contention.py`（缺争用记录即红）。

## 启动（一键）

`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10811/  
健康检查：GET http://127.0.0.1:10811/api/health

主要接口：`/api/sources`、`/api/arrivals`、`/api/late`、`/api/late/{id}`、`/api/catchups`、
`/api/watermark`、`/api/reconcile`、`/api/contention`、`/api/deviations`、`/api/rules`

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
