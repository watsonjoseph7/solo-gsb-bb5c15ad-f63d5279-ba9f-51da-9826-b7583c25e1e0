# 跑道离场尾流间隔校验 API

基于 **Python + FastAPI + SQLite** 的跑道离场尾流（wake turbulence）间隔校验服务。
客户端按跑道提交航班计划、前机—后机最小间隔矩阵，以及可选的**跑道临时关闭窗口**，服务端完成两件事：

1. **间隔校验（validate）**：按计划离场时刻、航班号稳定排序，列出同跑道相邻航班的间隔违规，
   以及计划离场时刻落入关闭窗口的航班与命中窗口。
2. **试排（trial）**：保持原航班顺序，逐项把航班推迟到**同时满足尾流间隔、避开关闭窗口**的最早
   安全时刻；尾流推迟后若落入后续关闭窗口会继续迭代，延误沿队列向后级联，**原计划时刻绝不改写**。
   返回调整后时刻、单项延误、触发前机与逐段窗口跳转记录。

每次请求（含输入排序结果、关闭窗口、违规/试排结论）都会持久化到 SQLite，可用批次 ID 回查。

---

## 一条命令启动

```bash
docker compose up -d --build
```

启动后访问 **http://localhost:8000** ：

- Swagger 交互文档：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health
- 默认间隔矩阵：http://localhost:8000/matrix/default

停止：`docker compose down`（数据保留在命名卷 `wake-data` 中；加 `-v` 可一并清除）。

## 必要配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WAKE_DB_PATH` | `/data/wake.db` | SQLite 数据库文件路径，容器内挂载在持久卷 `/data` |
| `WAKE_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `WAKE_PORT` | `8000` | uvicorn 监听端口（容器内固定暴露 8000） |

端口映射在 `docker-compose.yml` 的 `ports` 中配置（默认 `8000:8000`，宿主机端口被占用时改左侧即可）。

---

## 接口

### `POST /api/v1/validate` — 间隔违规校验

请求体：

```json
{
  "runways": [
    {
      "runway": "36L",
      "flights": [
        {"flight_no": "CA101", "scheduled_off": "2026-09-18T08:00:00+08:00", "wake": "J"},
        {"flight_no": "CA102", "scheduled_off": "2026-09-18T08:01:00+08:00", "wake": "M"},
        {"flight_no": "CA103", "scheduled_off": "2026-09-18T08:02:30+08:00", "wake": "M"}
      ],
      "closures": [
        {"start": "2026-09-18T08:02:00+08:00", "end": "2026-09-18T08:03:30+08:00"}
      ]
    }
  ]
}
```

- `flight_no`：航班号，**整个请求内唯一**（跑道内、跑道间均不可重复）。
- `scheduled_off`：计划离场时刻，ISO 8601 且**必须带时区**（`Z` 或 `+08:00` 偏移），服务端统一按 UTC 处理。
- `wake`：尾流等级，四档 —— `L` 轻型 / `M` 中型 / `H` 重型 / `J` 超重型。
- 跑道按其标识独立排序与校验；排序规则为 `(scheduled_off 升序, flight_no 升序)` 的稳定排序。
- 仅检查排序后**相邻**两架航班（单跑道离场的运行惯例）：前机尾流等级 → 后机尾流等级查矩阵，
  实际间隔 < 要求间隔即违规，返回要求秒数、实际秒数与缺口秒数。
- `closures`：该跑道的临时关闭窗口（可选、可多个），见下节。命中关闭窗口的航班随每条跑道返回
  在 `closure_hits` 中（含命中窗口的起止时刻），并汇总为 `total_closure_hits`。

### 临时关闭窗口（每条跑道可选字段 `closures`）

```json
"closures": [
  {"start": "2026-09-18T08:02:00+08:00", "end": "2026-09-18T08:03:30+08:00"},
  {"start": "2026-09-18T08:06:00+08:00", "end": "2026-09-18T08:07:00+08:00"}
]
```

- 每个窗口是**半开区间 `[start, end)`**：`start` 时刻关闭已生效，`end` 时刻跑道已恢复
  （即恰在 `end` 离场不算命中）。
- `start` / `end` 都**必须带时区**，服务端统一换算为 UTC 后比较与存储。
- `end` 必须严格晚于 `start`；同一跑道的多个窗口**不得重叠**（端点相接，即前窗 `end` ==
  后窗 `start`，允许）。违反任一规则返回 `422`。
- 窗口只作用于其所属跑道；不同跑道的窗口互不影响。
- validate 只检查**计划时刻**是否落入窗口；试排时调整后的时刻也会避开窗口（见下）。

### `POST /api/v1/trial` — 试排（尾流 + 关闭窗口，级联推迟）

请求体与 validate 完全一致（含 `closures`）。算法：

- 顺序保持为稳定排序结果；首架航班仅在自身计划时刻落入窗口时才后移；
- 每架航班先把自身计划时刻移出所有关闭窗口（原因记为 `scheduled`），
  再相对**已调整后的直接前机**满足尾流最小间隔；
- 尾流推迟（原因记为 `cascade`）或窗口跳转后，若新时刻又落入**后续**关闭窗口，则跳到该窗口
  `end` 并**继续迭代**，直到时刻既满足间隔又位于所有窗口之外；
- 因此延误自动向后续航班级联；航班不会被提前；
- 每项返回 `adjusted_off`（调整后时刻，UTC）、`delay_seconds`（单项延误，非负）、
  `triggered_by`（触发本次尾流推迟的直接前机航班号；无则为 `null`），以及 `window_pushes`
  （按发生顺序排列的逐段窗口跳转，每段含 `window_start` / `window_end` / `reason`，
  另有 `window_push_count`）。
- 该接口只返回试排建议，`scheduled_off` 原值原样返回，数据库中也同时保存原计划。

### `GET /api/v1/batches/{batch_id}` — 批次回查

两个 POST 接口的返回都带 `batch_id`，可随时回查当时的输入顺序、关闭窗口、生效矩阵与结论
（validate/trial 均可）。关闭窗口随批次原样保存：validate 批次含 `closures` / `closure_hits`，
trial 批次每条试排结果含 `window_pushes`。不存在的 ID 返回 `404`。

### 间隔矩阵（可选字段 `matrix`）

请求可携带 `matrix` 覆盖默认矩阵：

```json
"matrix": [
  {"leader": "H", "follower": "M", "seconds": 120},
  {"leader": "J", "follower": "M", "seconds": 180}
]
```

- 省略 `matrix` 时使用内置的 ICAO 风格默认矩阵（可用 `GET /matrix/default` 查看）：

  | 前机 \ 后机 | L | M | H | J |
  | --- | --- | --- | --- | --- |
  | **L** | 100 | 60 | 60 | 60 |
  | **M** | 100 | 60 | 60 | 60 |
  | **H** | 100 | 120 | 100 | 60 |
  | **J** | 180 | 180 | 180 | 120 |

  （单位：秒，示例默认值，实际运行请以当地管制规定通过 `matrix` 覆盖。）

- **提供** `matrix` 时只有列出的组合生效，未列出的组合视为无最小间隔（0 秒）。
- 同一 `(leader, follower)` 组合重复、`seconds` 为负数均会被拒绝（`422`）。

### 错误约定

- `422`：请求体不合法（缺时区、航班号/跑道重复、尾流等级非法、矩阵条目非法、关闭窗口结束不晚于
  开始或窗口彼此重叠、窗口时刻缺时区等），响应体含中文原因。
- `404`：批次 ID 不存在。

---

## curl 示例

```bash
# 间隔校验
curl -s -X POST http://localhost:8000/api/v1/validate \
  -H 'Content-Type: application/json' \
  -d @sample.json

# 试排
curl -s -X POST http://localhost:8000/api/v1/trial \
  -H 'Content-Type: application/json' \
  -d @sample.json

# 回查批次
curl -s http://localhost:8000/api/v1/batches/<batch_id>
```

`sample.json` 已随仓库提供，内含两条跑道的关闭窗口，可直接使用。
以仓库自带样例为例，`36L` 跑道在 `08:02–08:03:30`、`08:06–08:07` 关闭，试排结果为：

| 航班 | 计划（UTC） | 调整后（UTC） | 延误 | 说明 |
| --- | --- | --- | --- | --- |
| CA101 | 00:00:00 | 00:00:00 | 0 | 首架不动 |
| CA102 | 00:01:00 | 00:03:30 | 150s | J→M 尾流推到 00:03，落入第一窗口 → 级联跳到 00:03:30 |
| CA103 | 00:02:30 | 00:04:30 | 120s | 计划时刻自身命中窗口（`scheduled`），再让 CA102 间隔 |
| CA104 | 00:03:00 | 00:07:00 | 240s | 自身命中第一窗；尾流推迟后又落入第二窗（`cascade`），继续迭代 |

---

## 本地开发（不用 Docker）

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
export WAKE_DB_PATH=./wake.db            # 可选，默认 /data/wake.db
uvicorn app.main:app --reload --port 8000
```

运行测试（含 39 个领域逻辑与 API 端到端用例，使用临时 SQLite 文件）：

```bash
python3 -m pip install pytest httpx
python3 -m pytest -q
```

## 项目结构

```
app/
  main.py         # FastAPI 路由（validate / trial / batches / 元信息）
  schemas.py      # Pydantic 请求与响应模型、输入校验（含关闭窗口半开区间/重叠校验）
  service.py      # 请求 -> 领域对象 -> 模式计算
  separation.py   # 核心领域逻辑：稳定排序、相邻违规、关闭窗口命中、避窗级联试排、默认矩阵
  timeutils.py    # 时区归一化（统一 UTC，整数秒运算）
  database.py     # SQLite 连接与建表（批次/航班/违规/窗口/命中/试排/窗口跳转）
  repository.py   # 批次及上述结论的持久化与回查
  config.py       # 环境变量配置
tests/            # pytest 领域单测 + TestClient API 测试
Dockerfile
docker-compose.yml
sample.json
requirements.txt
```
