# 跑道离场尾流间隔校验 API

基于 **Python + FastAPI + SQLite** 的跑道离场尾流（wake turbulence）间隔校验服务。
客户端按跑道提交航班计划与前机—后机最小间隔矩阵，服务端完成两件事：

1. **间隔校验（validate）**：按计划离场时刻、航班号稳定排序，列出同跑道相邻航班的间隔违规。
2. **试排（trial）**：保持原航班顺序，逐项把航班推迟到满足间隔的最早安全时刻；
   延误沿队列向后级联，**原计划时刻绝不改写**。返回调整后时刻、单项延误与触发前机。

每次请求（含输入排序结果、违规/试排结论）都会持久化到 SQLite，可用批次 ID 回查。

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

### `POST /api/v1/trial` — 试排（级联推迟）

请求体与 validate 完全一致。算法：

- 顺序保持为稳定排序结果，首架航班不动；
- 自第二架起，若与**已调整后的直接前机**间隔不足，则把本架推迟到 `前机调整时刻 + 要求间隔`；
- 因此延误自动向后续航班级联；航班不会被提前；
- 每项返回 `adjusted_off`（调整后时刻，UTC）、`delay_seconds`（单项延误，非负）、
  `triggered_by`（触发本次推迟的直接前机航班号；未延误为 `null`）。
- 该接口只返回试排建议，`scheduled_off` 原值原样返回，数据库中也同时保存原计划。

### `GET /api/v1/batches/{batch_id}` — 批次回查

两个 POST 接口的返回都带 `batch_id`，可随时回查当时的输入顺序、生效矩阵与结论（validate/trial 均可）。
不存在的 ID 返回 `404`。

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

- `422`：请求体不合法（缺时区、航班号/跑道重复、尾流等级非法、矩阵条目非法等），响应体含中文原因。
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

`sample.json` 已随仓库提供，可直接使用。

---

## 本地开发（不用 Docker）

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
export WAKE_DB_PATH=./wake.db            # 可选，默认 /data/wake.db
uvicorn app.main:app --reload --port 8000
```

运行测试（含 21 个领域逻辑与 API 端到端用例，使用临时 SQLite 文件）：

```bash
python3 -m pip install pytest httpx
python3 -m pytest -q
```

## 项目结构

```
app/
  main.py         # FastAPI 路由（validate / trial / batches / 元信息）
  schemas.py      # Pydantic 请求与响应模型、输入校验
  service.py      # 请求 -> 领域对象 -> 模式计算
  separation.py   # 核心领域逻辑：稳定排序、相邻违规、级联试排、默认矩阵
  timeutils.py    # 时区归一化（统一 UTC，整数秒运算）
  database.py     # SQLite 连接与建表
  repository.py   # 批次/航班/违规/试排结果的持久化与回查
  config.py       # 环境变量配置
tests/            # pytest 领域单测 + TestClient API 测试
Dockerfile
docker-compose.yml
sample.json
requirements.txt
```
