# 语音转写语料包授权管理服务

FastAPI + SQLite 实现的语料包上传/下载服务，支持按受访者号**撤回授权**并自动重写受影响的语料包。
两个 API 实例共享一个数据卷，通过 SQLite WAL 事务与原子文件替换保证并发和崩溃下的数据安全。

## 一、启动（Docker Compose，可直接运行）

```bash
docker compose up --build
```

启动后：

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 统一入口（Nginx 轮询两个实例） | http://localhost:8080 | 所有 API 请求走这里 |
| Swagger 交互式文档 | http://localhost:8080/docs | 可直接在页面上演示全部操作 |
| OpenAPI Schema | http://localhost:8080/openapi.json | |
| 实例直连（调试用） | http://localhost:8001 形式的容器端口未暴露 | 两实例在 compose 内部为 `api1`/`api2` |

数据（SQLite 库 + JSONL 文件）全部存放在命名卷 `corpus-data` 内：

```
/data
├── corpus.db                 # 元数据、记录索引、撤回名单、重写任务（WAL 模式）
├── packages/<包号>.jsonl      # 可下载的语料包交付文件
└── tmp/                      # 原子写入临时文件、故障注入标记
```

停止并清空数据：`docker compose down -v`。

## 二、HTTP API 一览

### 语料包

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/packages/{package_id}` | 上传一个语料包（请求体为 JSONL，最多 100 行；也支持 `multipart/form-data` 的 `file` 字段） |
| `GET` | `/packages` | 列出语料包及状态 |
| `GET` | `/packages/{package_id}` | 查询单个包状态 |
| `GET` | `/packages/{package_id}/lines` | 查询行数（仅 `ready` 时 `line_count` 为权威值） |
| `GET` | `/packages/{package_id}/download` | 下载 JSONL；非 `ready` 返回 `409` 可重试 |

每行 JSONL 的字段（支持中文别名）：

```json
{"record_id": "r1", "respondent_id": "s1", "text": "转写文本"}
```

| 标准字段 | 可接受别名 |
| --- | --- |
| `record_id` | `recordId`、`记录号` |
| `respondent_id` | `respondentId`、`受访者号` |
| `text` | `transcription`、`转写文本` |

校验规则：1–100 行；每行必须是合法 JSON 对象；包内 `record_id` 唯一；
`package_id` / `respondent_id` 仅允许字母、数字、`.`、`_`、`-`（1–64 字符，阻断路径穿越）。

### 撤回授权

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/withdrawals/{respondent_id}?reason=...` | 持久化撤回，返回受影响包与任务数（`202`，幂等） |
| `GET` | `/withdrawals` | 查看持久化撤回名单 |

撤回受理后的保证：

1. **受理之后发起的下载**：受影响包立即转为 `rewriting`，下载返回 `409`，绝不交付仍含该受访者的旧内容；
2. 后台 worker 重写受影响包（原子替换文件 + 删除库内记录），完成后恢复 `ready` 并更新行数；
3. **其他包不受影响**，全程可正常下载；
4. 此后任何**包含该受访者的上传整包拒收**（`403`），不会产生任何包/记录残留。

### 重写任务（可观测、可重试）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/jobs?package_id=...` | 查看任务状态、尝试次数、错误信息、下次执行时间 |
| `POST` | `/jobs/{job_id}/retry` | 手动重试 `failed`/`pending` 任务（成功任务返回 `409`） |

任务失败（如共享卷瞬时不可用）时包保持 `rewriting`、下载持续返回 `409`，任务按
1→2→5→10→20→30 秒指数退避自动重试，重写只删除撤回名单中的受访者，**其他记录原样保留**。

## 三、在 /docs 演示的完整流程

```bash
# 1) 上传（raw JSONL body，Content-Type: application/x-ndjson）
curl -X POST http://localhost:8080/packages/pk-demo \
  -H 'Content-Type: application/x-ndjson' \
  --data-binary $'{"record_id":"r1","respondent_id":"s1","text":"你好"}\n{"record_id":"r2","respondent_id":"s2","text":"天气不错"}\n'

# 2) 查行数 / 下载
curl http://localhost:8080/packages/pk-demo/lines
curl http://localhost:8080/packages/pk-demo/download

# 3) 撤回 s1
curl -X POST 'http://localhost:8080/withdrawals/s1?reason=本人申请撤回'

# 4) 重写期间下载 -> 409 processing；轮询状态直到 ready
curl http://localhost:8080/packages/pk-demo/download
curl http://localhost:8080/packages/pk-demo

# 5) 再次下载只剩 s2；撤回名单持久存在
curl http://localhost:8080/packages/pk-demo/download
curl http://localhost:8080/withdrawals

# 6) 含 s1 的新上传被整包拒收（403）；包号复用返回 409
curl -X POST http://localhost:8080/packages/pk-new \
  -H 'Content-Type: application/x-ndjson' \
  --data-binary $'{"record_id":"n1","respondent_id":"s1","text":"拒收"}\n'
```

## 四、并发与崩溃安全设计（对应验收点）

| 风险 | 机制 |
| --- | --- |
| 两实例并发写库互相覆盖 | SQLite WAL + `synchronous=FULL`；所有多语句写操作在单条 `BEGIN IMMEDIATE` 事务内串行化，`busy_timeout=30s` |
| 同包号并发上传 | 主键占位 + 写事务互斥：恰好一个 `201`，其余 `409`，无记录交错 |
| 撤回与上传竞态 | 上传在同一写事务内检查撤回名单；撤回与上传交叉时包保持 `rewriting` 直到 worker 收敛，下载永不暴露旧内容 |
| 重写中进程退出 | 任务带锁租约（默认 30s），重启后任何实例都会回收 `running` 任务；文件经“唯一临时文件 + fsync + `os.replace` + 目录 fsync”原子切换，旧文件要么完整保留要么被完整新版本替换 |
| 上传在落盘前退出 | 启动恢复发现 `uploading` 状态的包：以数据库记录为权威重建交付文件；若文件缺失时恰有撤回任务，则重建时直接剔除撤回受访者，不会“复活”旧内容 |
| 显示完成却交付旧内容 | 下载只在 `ready` 时放行；worker 发现存在更新的未完成任务时不允许把包置回 `ready` |
| 失败被误报成功 | 任何异常都让任务留在 `failed` 并指数退避，包维持 `rewriting`，下载持续 `409` 直至重试成功 |
| 重试误伤其他记录 | 重写只按持久化撤回名单过滤，其余记录逐行保留；任务天然幂等，重复执行结果相同 |

## 五、本地自测

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python tests/selftest_integration.py   # 双实例端到端 + 并发，40 项断言
python tests/selftest_crash.py         # 重写中/上传中硬退出的恢复，14 项断言
```

> 自测脚本会自行在临时端口启动两个共享同一数据目录的 uvicorn 实例并在结束时优雅停机；
> 仅标准库 + 本项目依赖，无需 Docker。
