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

## 迟到分级写路径（已落地）

口径锁定在 `docs/迟到分级说明.md`（**甲路径 · 值班令牌鉴权**，乙路径不启用，当天不许改口）。

分级表（lag = 摄入时钟 − 源时钟，SLA 为该源时限）：

| 条件 | 档位 | 源输出态 |
|------|------|----------|
| lag ≤ SLA | 正常 | 可写 |
| SLA < lag ≤ 2×SLA | 告警 | 降级只读 |
| lag > 2×SLA | 违约 | 冻结 |

写路径要点：

- `POST /api/duty/token` 领值班令牌；`POST /api/arrivals` 须带 `X-Duty-Token`。
- 缺令牌：**拒写到达行（403）、记越权、源输出态保持未切档**，不出半截成功。
- 带令牌：到达行与源输出态**同事务**落库；超 2×SLA 必切「冻结」，不会写库成功而源输出态仍旧。
- 同源同源时钟同摄入时钟唯一：两名值班抢交时后到者 409，只记「迟到分级争用」。
- 偏离锁定册（请求带其它口径或改册）：423 并记「分级偏了」。
- 五处读数（源目录、到达列表、迟到详情、补采附页、水位卡片）由 `GET /api/snapshot` 同一快照派生；`band_conflict` 标出迟到档与输出态撕裂。
- 迟到详情 `GET /api/delay/{source}` 标注 `effective_mechanism`（令牌拒写 / 争用排队 / 分级落库）与乙路径档位回退「未启用」。

回归测试：`tests/test_grade_failure.py`、`tests/test_repro_mis_switch.py`（错切夹具 `fixtures/lag_cross_grade_stale_mode.json`）、`tests/test_readouts_consistent.py`。

## 启动（一键）

`powershell
cd projects/06-lagboard
docker compose up -d --build
`

浏览器：http://127.0.0.1:10811/  
健康检查：GET http://127.0.0.1:10811/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10603
python -m pytest -q
`
