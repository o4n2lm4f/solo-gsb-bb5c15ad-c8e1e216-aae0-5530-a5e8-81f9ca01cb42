# 冷链温度事件分析 API

基于 **FastAPI + SQLite** 的冷链温度监控服务。设备按时间上报温度读数，系统依据每台设备配置的温度上下限、容忍时长与容忍缺口，自动识别**连续超温事件**：

- 🟢 **短暂恢复不切断事件**：超温过程中偶发回到正常区间，只要距上一次超温读数不超过 `merge_gap_seconds`，仍视为同一起事件（恢复时间不计入累计超限时长）；
- ✂️ **间隔过长则分段**：超过 `merge_gap_seconds` 未再次超温，事件在缺口处结束，之后的超温另计为新事件；
- 📊 每起事件返回：起止时间、超温方向（高/低）、峰值温度及时间、累计超限时长；
- ⏱️ 累计超限时长不足 `tolerance_seconds`（容忍时长）的瞬时波动直接忽略。

## 目录结构

```
├── app/
│   ├── main.py        # FastAPI 路由
│   ├── analysis.py    # 纯函数事件识别算法（无 IO 依赖）
│   ├── database.py    # SQLite 连接与建表
│   ├── schemas.py     # 请求/响应模型
│   └── config.py      # 环境变量配置
├── tests/             # 算法单测 + API 集成测试
├── Dockerfile
└── docker-compose.yml
```

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

- 服务端口：**8000**（容器内 8000 → 宿主机 8000）
- SQLite 数据文件持久化在 Docker 卷 `coldchain-data`（容器内 `/data/coldchain.db`）
- 健康检查：`GET http://localhost:8000/health`
- 交互式接口文档（Swagger UI）：<http://localhost:8000/docs>

停止：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 同时删除数据卷
```

### 本地运行（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库路径可用环境变量覆盖：COLDCHAIN_DB_PATH=/your/path.db
```

## 判定规则说明

| 规则参数 | 含义 |
| --- | --- |
| `temp_min` / `temp_max` | 正常温度区间（含边界），高于上限为 `high`，低于下限为 `low` |
| `tolerance_seconds` | **容忍时长**：事件累计超限秒数达到该值才会被输出 |
| `merge_gap_seconds` | **容忍缺口**：距上一次超温读数不超过该秒数时，短暂恢复/上报间隔不切断事件 |

区间归属采用“采样保持”口径：相邻两次读数之间的温度状态归属于前一条读数（读数状态持续到下一次读数上报）。例如 10:00 报 9℃（超上限）、10:01 报 5℃（恢复），则 10:00–10:01 这 60 秒计入超限，10:01 之后为恢复，不计入。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/api/devices/{device_id}` | 注册设备 / 更新备注名 |
| `GET` | `/api/devices` | 设备列表 |
| `PUT` | `/api/devices/{device_id}/rule` | 配置温度规则（设备不存在时自动注册） |
| `GET` | `/api/devices/{device_id}/rule` | 查询规则 |
| `POST` | `/api/devices/{device_id}/readings` | 批量上报温度读数（同一时间戳重复上报自动忽略） |
| `GET` | `/api/devices/{device_id}/readings?start=&end=` | 按时间范围查询原始读数 |
| `GET` | `/api/devices/{device_id}/events?start=&end=` | 按时间范围识别并返回超温事件 |
| `GET` | `/health` | 健康检查 |

所有时间参数均为 ISO 8601（如 `2026-09-18T08:00:00Z`）；不带时区的时间按 UTC 处理。

## 示例请求

### 1. 注册设备并配置规则

要求：冷藏区间 2～8℃；累计超限满 **60 秒**才算事件；**120 秒**内的短暂恢复不切断事件。

```bash
curl -X PUT http://localhost:8000/api/devices/truck-001 \
  -H 'Content-Type: application/json' \
  -d '{"name":"沪A-冷链车1号"}'

curl -X PUT http://localhost:8000/api/devices/truck-001/rule \
  -H 'Content-Type: application/json' \
  -d '{"temp_min":2,"temp_max":8,"tolerance_seconds":60,"merge_gap_seconds":120}'
```

### 2. 上报温度读数

```bash
curl -X POST http://localhost:8000/api/devices/truck-001/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "readings": [
      {"ts":"2026-09-18T08:00:00Z","temperature":9.0},
      {"ts":"2026-09-18T08:01:00Z","temperature":10.0},
      {"ts":"2026-09-18T08:02:00Z","temperature":5.0},
      {"ts":"2026-09-18T08:03:00Z","temperature":9.0}
    ]
  }'
```

响应：

```json
{"device_id":"truck-001","stored":4,"duplicates":0}
```

### 3. 查询事件（短暂恢复被合并）

```bash
curl 'http://localhost:8000/api/devices/truck-001/events'
```

08:02 的一次恢复距上次超温仅 60 秒（≤120 秒），不切断事件，返回**一起**事件，累计超限 120 秒（恢复的 60 秒不计入）：

```json
{
  "device_id": "truck-001",
  "count": 1,
  "events": [
    {
      "index": 1,
      "direction": "high",
      "start_time": "2026-09-18T08:00:00Z",
      "end_time": "2026-09-18T08:03:00Z",
      "peak_temperature": 10.0,
      "peak_time": "2026-09-18T08:01:00Z",
      "duration_seconds": 120.0
    }
  ]
}
```

### 4. 间隔过长分段

再上报一组读数，与上一起超温之间相隔 5 分钟（>120 秒），会成为**新的独立事件**：

```bash
curl -X POST http://localhost:8000/api/devices/truck-001/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "readings": [
      {"ts":"2026-09-18T08:08:00Z","temperature":9.5},
      {"ts":"2026-09-18T08:09:00Z","temperature":9.5}
    ]
  }'

curl 'http://localhost:8000/api/devices/truck-001/events'
# count = 2，第二起事件 start_time=08:08、duration_seconds=60
```

### 5. 按时间范围查询原始读数

```bash
curl 'http://localhost:8000/api/devices/truck-001/readings?start=2026-09-18T08:02:00Z&end=2026-09-18T08:05:00Z'
```

## 运行测试

```bash
pip install -r requirements-dev.txt
python3 -m pytest -q
```

测试覆盖：正常区间、低温事件、峰值选择、容忍时长过滤、短暂恢复合并且不计时长、缺口边界（恰好等于阈值不切断 / 超过阈值切断）、乱序输入、方向切换，以及完整 API 流程与错误码。
