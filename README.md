# 煤质数据仓 · 旧煤样重判入仓服务

按**现行规则版本**重判旧煤样并把每批**最后一次成功结果**入仓 SQLite，供下游按批号查询的 FastAPI 服务。
另提供**预检 + 受控启用**：新规则版本必须先拿一条已成功的历史批次试算，全部样号定级唯一
才签发一次性凭证，凭证只能在其所依据的基准批、原规则、现行版都未变化时启用。

## 业务规则

- 测值为 `0 ~ 10000` 的整数**万分比**；水分、灰分均给出闭区间 `[lo, hi]`。
- 每批最多 **100** 条记录；每个规则版本最多 **20** 条规则。
- 规则按 `seq` 顺序判定，取**首条同时命中两个闭区间**的等级；全部未命中 => **未分级**（JSON 中用 `null` 表示）。
- 旧档按批提交并带**原规则版号**；重判时先从旧档区间内筛出"按原版能得出原等级"的测值对：
  - 现行版下等级**唯一** => 直接定级（未分级也是一种唯一定级）；
  - 出现**多个等级** => 列出每个等级、一对可行测值（见证点）及该等级的可行测值对总数；
  - 原等级**无解** => 报样号并**退回整批**（HTTP 422），不覆盖历史成功结果。
- 重判期间仍可发布并启用新版本；重判开始时对启用版本拍快照，**入仓写事务内复核版本 id**，
  期间换版则整批退回（HTTP 409），下游继续查到上一份成功结果及其所用版号 `judged_version`。
- **预检/受控启用**（本版本新增）：
  - `POST /api/versions/{code}/preflight` 只吃**已成功批号**，基准数据全部取自该批入仓时
    保存的原档区间、原等级与**原规则快照**，按候选版重算；
  - 任一原档无解（`infeasible_original_grade`）或候选等级不唯一（`ambiguous_grade`）
    => 422，返回样号、等级选项与见证值，**不生成凭证**；
  - 全部唯一 => 生成 **15 分钟有效的一次性凭证**（可在 Compose 用 `COAL_TICKET_TTL_SECONDS` 调整）；
  - `POST /api/versions/{code}/activate-checked` 凭凭证启用：唯一写事务内复核
    基准批内容与修订号、原规则（快照与规则表双重比对）、预检时现行版及其规则、候选规则；
    任一变化 => 409，**原现行版保持不变**；成功即消耗凭证，重放为 409 `ticket_consumed`。

## 核心算法：坐标压缩，不枚举测值域

规则是两个轴上闭区间的笛卡尔积矩形。把两版规则的全部边界（`lo`、`hi+1`）加上
`0`、`10001` 排序去重，相邻切点之间形成一维均匀格，水分格 × 灰分格即二维均匀格——
同一格内任一点在任一版规则下的命中结果完全相同。

只遍历与旧档矩形相交的格：

1. 格代表点（夹到交集内）分别用原版、现行版定级；
2. 原版定级 ≠ 原等级的格丢弃；
3. 其余格按现行版等级聚合，格覆盖的整数点数（两轴交集长度之积）累加为该等级的可行对数。

复杂度只随**规则边界数**（每版 ≤20 条规则 => 每轴 ≤41 个切点）与**记录数**（≤100）增长，
与测值域宽度无关，全程不枚举 `0..10000`。单条记录最坏约 `40×40=1600` 个格，
实测 100 条全域记录整批约 26 ms（见下文实测）。

## 目录结构

```
app/
  models.py      # Pydantic 请求/响应模型与入参校验
  rejudge.py     # 坐标压缩核心算法（纯函数，可独立测试）
  fingerprint.py # 规范化 JSON 与指纹（批次行 / 规则快照）
  tickets.py     # 一次性凭证生成、哈希与有效期
  db.py          # SQLite 持久层（WAL、版本守卫、原档快照、旧库迁移、受控启用事务）
  service.py     # 重判 / 预检 / 受控启用编排
  main.py        # FastAPI 路由，启动自动播种 v1/v2 演示版本并启用 v2
tests/           # pytest：算法对拍、服务、预检与迁移、HTTP 端到端
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务: http://localhost:8000
# 交互文档（可直接演示重判、失败预检与成功启用）: http://localhost:8000/docs
```

SQLite 数据文件位于命名卷 `coal-data` 的 `/data/coal.db`（WAL 模式）。
旧库首次启动时**原位迁移**：只补列/建表，原有成功结果仍可查询；迁移后
`GET /api/batches/{batch_no}/original-archive` 可回取预检所用原档。

### 本地直接运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 库路径可用环境变量 COAL_DB_PATH 覆盖，默认 data/coal.db
```

首次启动若库内无版本，自动播种：

| 版号 | 规则 | 说明 |
| --- | --- | --- |
| `v1` | 优质煤 `m≤800,a≤2000`；普通煤 `m≤1200,a≤3000`；等外煤全域兜底 | 旧版 |
| `v2` | 优质煤 `m≤700,a≤1500`；普通煤 `m≤1000,a≤2500`（无兜底，区间外未分级） | 启动即启用 |

演示候选版（由下方 `/docs` 步骤手动发布，不入种子）：`v3` 以水分 500 切两段（多等级）、
`v4` 完整覆盖演示批原档（唯一）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/versions?activate=false` | 发布新版本（1~20 条规则），不影响进行中的重判 |
| GET | `/api/versions` | 版本列表（含是否启用、规则数） |
| GET | `/api/versions/{code}/rules` | 查看某版规则（按判定顺序） |
| POST | `/api/versions/{code}/activate` | 直接启用新版本（不做预检，管理员工单场景） |
| POST | `/api/versions/{code}/preflight` | 用已成功批号预检候选版；唯一才签发一次性凭证 |
| POST | `/api/versions/{code}/activate-checked` | 凭预检凭证受控启用；任一方变化即拒绝并保留原现行版 |
| GET | `/api/active` | 当前启用版号 |
| POST | `/api/rejudge` | 按批重判并入仓最后一次成功结果（同时保存原档快照） |
| GET | `/api/batches/{batch_no}` | 下游按批号查最后成功结果（含 `judged_version`） |
| GET | `/api/batches/{batch_no}/original-archive` | 回取预检所用原档区间、原等级与原规则快照 |
| GET | `/api/batches/{batch_no}/rejections` | 该批退回记录（无解 / 换版） |
| GET | `/api/rejections` | 全部退回审计 |
| GET | `/health`、`/docs`、`/openapi.json` | 健康检查与交互文档 |

### 在 /docs 演示

1. 打开 `http://localhost:8000/docs`，`POST /api/rejudge` 的请求体已内置示例 `BATCH-DEMO-001`，
   直接 Execute 即成功入仓（同时保存原档快照与所用原规则）；
2. 同样可看到：唯一定级样、多等级（含未分级 `null`）样各自的见证点与可行对数；
3. `POST /api/versions` 发布候选版 `v3`（多等级）与 `v4`（唯一）：
   - `POST /api/versions/v3/preflight`，请求体 `{"batch_no": "BATCH-DEMO-001"}`
     => **422** `preflight_failed`，返回样号 `S-001`、候选等级 `甲/乙` 及各自见证值，
     无 `ticket` 字段；
   - `POST /api/versions/v4/preflight`，同一请求体 => **200**，各样唯一并返回 `ticket`；
4. 把 `ticket` 填入 `POST /api/versions/v4/activate-checked` 的请求体
   => **200** `{"code":"v4","active":true,"previous_version":"v2",...}`；同一凭证再提交
   => **409** `ticket_consumed`，现行版仍为 `v4`；
5. 守卫演示：对 `v5` 预检拿到凭证后，用变化过的原档重新 `POST /api/rejudge` 同一批号，
   再 `activate-checked` => **409** `baseline_changed`，`GET /api/active` 仍是 `v4`；
6. `GET /api/batches/BATCH-DEMO-001/original-archive` 可直接看到预检所用的
   原档区间、原等级与 `v1` 规则快照。

### 响应示例（预检失败）

```json
{
  "detail": {
    "reason": "preflight_failed",
    "message": "原档无解或候选等级不唯一，未生成启用凭证",
    "samples": [
      {
        "sample_no": "S-001",
        "original_grade": "优质煤",
        "reason": "ambiguous_grade",
        "message": "候选版下候选等级不唯一，需人工确认后再启用",
        "options": [
          {"grade": "乙", "witness_moisture": 501, "witness_ash": 1000, "feasible_pairs": 40100},
          {"grade": "甲", "witness_moisture": 400, "witness_ash": 1000, "feasible_pairs": 40501}
        ]
      }
    ]
  }
}
```

### 响应示例（多等级样，重判接口）

```json
{
  "sample_no": "S-002",
  "original_grade": "普通煤",
  "unique": false,
  "final_grade": null,
  "options": [
    {"grade": "普通煤", "witness_moisture": 900, "witness_ash": 2300, "feasible_pairs": 20301},
    {"grade": null,      "witness_moisture": 900, "witness_ash": 2501, "feasible_pairs": 80350}
  ]
}
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

- `tests/test_rejudge.py`：闭区间端点、未分级、无解、原版未分级，并在 11×11 小域上做
  **300 组随机规则 × 随机区间的暴力枚举对拍**（等级集合、可行对数、见证点全部核对）；
- `tests/test_service.py`：整批退回、唯一未分级、版本漂移（快照旧于启用版 => 409 语义）、
  最后成功结果覆盖与保留、版号冲突、入参校验；
- `tests/test_preflight.py`：原档快照回取、预检成功/多等级/无解、凭证一次性、
  基准批/原规则/现行版/候选规则四类守卫、过期与版号不匹配、旧库迁移（幂等、可回填原规则、
  缺原档时明确拒绝）；
- `tests/test_api.py` / `tests/test_preflight_api.py`：TestClient 端到端覆盖全部 HTTP 状态码、
  下游查询语义与 `/docs` 演示序列。

## 设计要点与取舍

- **版本漂移防护**：`save_batch_result` 在 `BEGIN IMMEDIATE` 写事务内比对
  `active_version.version_id` 与重判开始时的快照 id，不一致即回滚，保证入仓结果与其版号严格对应。
- **预检基准不可伪造地取自入仓物**：预检不接受测值区间入参，只接受批号；基准批的内容与修订号
  在预检时指纹化，`activate-checked` 在同一写事务内重算比对，因此"预检后重跑该批"（即使测值
  恰好改回原样，修订号也已推进）都会使旧凭证失效。
- **原规则双重核对**：既比对基准批入仓时保存的 `original_rules_json` 快照，也比对原版号当前
  规则表；任一处被改写都返回 `original_rules_changed`。预检与启用都不信任"现行版表"的现状。
- **凭证安全**：明文凭证只在预检响应出现一次，库中仅存 sha256；一次性（`consumed_at`）、
  15 分钟 TTL、绑定候选版号（路径不匹配返回 `ticket_version_mismatch`）。
- **失败不产生副作用**：预检失败不写凭证；启用失败整体回滚、现行版不变；凭证仅在成功启用时消耗。
- **旧库迁移**：`PRAGMA user_version` 驱动，只补列/建表。历史成功结果仍可查，原规则可按
  `original_code` 回填；但当时未保存原档区间，无法凭空还原，`archive_available=false` 时
  预检明确 422，需重新成功重判该批一次补齐。
- **退回可审计**：无解与换版退回都写 `batch_rejections`，但绝不写/改 `batch_results`，
  下游查询语义稳定为"最后一次成功结果"。
- **并发**：SQLite WAL + `busy_timeout=30s`，读不阻塞写、写串行化；每次请求短连接。
- 未分级在 API 中统一以 `grade: null` 表达；多等级选项按等级名排序、未分级固定排末尾，输出确定。
- **迁移后的历史批需重新入仓**：这是有意的显式失败（422），不静默使用现行规则顶替原档。

## 实测记录（2026-11-25，本地 Uvicorn，Docker 入口相同）

本环境无 Docker CLI，因此用与镜像 `CMD` 相同的 `uvicorn app.main:app` 在 8000 端口实测，
请求经真实 HTTP 走通 `/docs` 演示序列：

| 步骤 | 结果 |
| --- | --- |
| `POST /api/rejudge` 演示批 | 200，两个样号入仓，原档快照可回取 |
| `POST /api/versions/v3/preflight`（多等级） | 422 `preflight_failed`，`S-001` 的 `甲/乙` 见证值与可行对数，无 `ticket` |
| `POST /api/versions/v4/preflight`（唯一） | 200，全部唯一定级，返回 43 字符 `ticket` |
| `POST /api/versions/v4/activate-checked` | 200，`previous_version=v2`，`/api/active` 变为 `v4` |
| 同一凭证重放 | 409 `ticket_consumed`，现行版仍 `v4` |
| 预检 `v5` 后改变基准批再启用 | 409 `baseline_changed`，现行版仍 `v4` |
| `GET /api/batches/BATCH-DEMO-001/original-archive` | 200，原区间 `[400,600]×[1000,1400]`、原等级 `优质煤`、`v1` 规则快照 |
| 旧库迁移（用改动前代码生成 v1 库） | `user_version` 0=>2，补 4 列与凭证表；原成功结果仍可查；原规则回填成功；缺原档时预检 422 `baseline_archive_unavailable`；重新入仓后预检 200 |
| `pytest -q` | **43 passed** |

> 交付约束：镜像构建与 `docker compose up` 未能在本环境执行（无 docker 可执行文件），
> Compose 文件与 Dockerfile 未改动入口，仅新增环境变量 `COAL_TICKET_TTL_SECONDS`。
