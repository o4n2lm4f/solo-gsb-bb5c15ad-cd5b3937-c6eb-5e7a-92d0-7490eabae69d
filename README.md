# 功能开关判定服务（FastAPI + SQLite）

一个支持**版本化配置**、**前置开关依赖**、**有序属性规则**和**基于 user_id 稳定哈希百分比分流**的功能开关判定服务。

核心特性：

- 📦 **不可变版本**：客户端上传带 `version` 的 JSON 配置，版本一经发布即不可修改；新版本不会影响历史版本的判定。
- 🔗 **前置开关**：开关可声明 `prerequisites`（含期望值 `expect`），按依赖深度优先、短路求值，响应返回完整**依赖链**。
- 📋 **有序规则**：`rules` 按顺序匹配、首条命中即返回，支持 `eq/ne/in/nin/gt/ge/lt/le/prefix/suffix`。
- 🎯 **稳定百分比分流**：`sha1(flag_key:user_id) % 100`，同一用户在同一开关上桶位恒定，放量单调扩大；不同开关独立分布。
- 🔀 **版本影响分析**：对比任意两个已发布版本，给出开关级 `added/removed/modified` 差异，并沿两版本前置依赖边的并集反向传播，列出每个变化开关间接影响的上游开关及一条最短路径。
- 🌍 **环境指针**：命名环境（如 `prod`）指向某个已发布版本，携带单调递增的 `revision`；写入需携带 `expected_revision` 乐观并发控制，判定接口按环境解析，单次请求使用同一指针快照。
- 🚫 **发布时强校验**：循环依赖（含自依赖）、悬挂依赖、非法比例（非整数或越界）、未知操作符、重复规则 ID 一律拒绝。
- 🧪 **纯函数引擎**：判定引擎与校验器零第三方依赖，可直接用系统 Python 跑单测。

---

## 一条命令启动

```bash
docker compose up -d --build
```

服务端口：**http://localhost:8000**

- API 文档（Swagger）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/healthz

数据持久化在 Docker 卷 `flag-data`（容器内 `/data/flags.db`）。

停止：`docker compose down`（加 `-v` 可同时删除数据卷）。

---

## 示例请求

### 1. 发布一份配置

```bash
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' \
  --data @examples/publish_v1.json
```

返回 `201`：

```json
{ "version": "2026-09-17", "flag_count": 4, "created_at": "2026-09-17T12:00:00+00:00" }
```

### 2. 批量判定

```bash
curl -s -X POST http://localhost:8000/api/v1/evaluate \
  -H 'Content-Type: application/json' \
  --data @examples/eval_request.json
```

响应中每个开关都包含**最终值 `value`**、**命中原因 `reason`**、**命中的规则 `rule_id`**、**分流桶位 `rollout`** 以及完整**依赖链 `chain`**（叶子在前，目标开关在最后）：

```json
{
  "version": "2026-09-17",
  "context": { "email": "ada@acme.com", "tier": "silver", "theme": "auto", "user_id": "user-1" },
  "results": [
    {
      "flag": "new_checkout",
      "value": true,
      "hit": true,
      "reason": "rule_match",
      "rule_id": "internal-testers",
      "rollout": null,
      "chain": [
        { "flag": "signed_in", "value": true, "hit": true, "reason": "rule_match",
          "rule_id": "has-user", "prerequisite_of": "new_checkout", "expected": true },
        { "flag": "new_checkout", "value": true, "hit": true, "reason": "rule_match",
          "rule_id": "internal-testers", "prerequisite_of": null, "expected": null }
      ]
    },
    {
      "flag": "dark_mode",
      "value": "system",
      "hit": true,
      "reason": "rollout_out",
      "rollout": { "bucket": 56, "percentage": 50 },
      "chain": [ { "flag": "dark_mode", "value": "system", "hit": true,
                   "reason": "rollout_out",
                   "rollout": { "bucket": 56, "percentage": 50 } } ]
    },
    {
      "flag": "legacy_button",
      "value": false, "hit": false, "reason": "disabled",
      "rule_id": null, "rollout": null,
      "chain": [ { "flag": "legacy_button", "value": false, "hit": false, "reason": "disabled" } ]
    }
  ],
  "missing_flags": []
}
```

> `hit` 是 `value` 的布尔化结果（兼容字符串/数字变体值）；布尔开关请直接使用 `true/false` 作为规则值与默认值。

### 3. 验证稳定性

连续请求 N 次，同一 `version` + 同一 `user_id` 的桶位与最终值完全一致；把 `percentage` 从 25 调到 100 只会新增命中用户，不会把任何人移出。

### 4. 发布新版本，历史版本不受影响

```bash
# 改好 examples/publish_v1.json 的 version（例如 2026-09-18）后再发布
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/publish_v2.json

# 老版本的判定结果保持不变
curl -s -X POST http://localhost:8000/api/v1/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"version":"2026-09-17","flags":["new_checkout"],"user_id":"user-1"}'
```

重复发布同一版本返回 `409 version_exists`；判定/查询不存在的版本返回 `404 version_not_found`。

### 5. 查询版本

```bash
curl -s http://localhost:8000/api/v1/configs                       # 列出全部版本
curl -s http://localhost:8000/api/v1/configs/2026-09-17            # 读取某版本完整配置
```

### 6. 版本影响分析（对比两个版本）

```bash
# 再发布一个修改了前置开关 signed_in 的版本（新增 staff-bypass 规则）
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/publish_v3.json

# 对比 2026-09-18 → 2026-09-19
curl -s -X POST http://localhost:8000/api/v1/config-comparisons \
  -H 'Content-Type: application/json' --data @examples/compare_request.json
```

响应：

```json
{
  "from_version": "2026-09-18",
  "to_version": "2026-09-19",
  "added": [],
  "removed": [],
  "modified": ["signed_in"],
  "impacts": [
    {
      "flag": "signed_in",
      "impacted": [
        { "flag": "new_checkout", "path": ["signed_in", "new_checkout"] }
      ]
    }
  ]
}
```

说明：

- `added` / `removed` / `modified`：按开关名比较两个版本的**规范化定义**（与存储一致的规范化结构比较，键序差异不算修改）；
- `impacts`：在**两个版本前置依赖边的并集**上沿反向依赖传播，逐个列出每个直接变化开关间接影响的上游开关；`path` 是从变化开关到依赖者的一条**最短路径**（同长取字典序最小）；
- 所有名称与路径均按字典序稳定排序，同一受影响开关只出现一次；没有受影响下游的变化开关也会列出（`impacted` 为空）；
- 任一版本不存在返回 `404 version_not_found`。

### 7. 环境指针（按环境发布与判定）

环境指针把命名环境（如 `prod`）关联到某个**已发布版本**，并携带单调递增的 `revision` 做乐观并发控制。

```bash
# 创建指针：仅接受 expected_revision=0，创建后 revision=1
curl -s -X PUT http://localhost:8000/api/v1/environments/prod \
  -H 'Content-Type: application/json' --data @examples/env_put.json
# → {"name":"prod","version":"2026-09-17","revision":1,"updated_at":"..."}

# 读取当前指针
curl -s http://localhost:8000/api/v1/environments/prod

# 移动指针：必须带上当前 revision（基于上一步读到的 1）
curl -s -X PUT http://localhost:8000/api/v1/environments/prod \
  -H 'Content-Type: application/json' \
  -d '{"version":"2026-09-18","expected_revision":1}'
# → revision 变为 2

# 按环境判定：无需传 version，响应回显本次使用的 version 与 revision
curl -s -X POST http://localhost:8000/api/v1/environments/prod/evaluate \
  -H 'Content-Type: application/json' --data @examples/env_eval_request.json
```

并发与一致性语义：

- 创建仅接受 `expected_revision=0`（环境不存在视为 revision 0）；更新必须等于当前 revision，成功后 revision 加一；
- revision 校验、目标版本存在性校验与指针移动在**同一个 `BEGIN IMMEDIATE` 事务**内完成：并发写入时只有一个成功，其余返回 `409 stale_revision`，且指针保持不变；
- 指向不存在的版本返回 `404 version_not_found`，指针同样保持不变；
- 环境不存在时，GET / evaluate 返回 `404 environment_not_found`；
- 单次 `/evaluate` 请求只读取一次指针，请求内所有开关都使用同一 `(version, revision)` 快照，不会在请求中途切换到新版本。

### 非法配置会被拒绝

```bash
# 循环依赖 → 422 invalid_config，errors[].code = circular_dependency
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/invalid_cycle.json

# 比例越界（120）→ 422 invalid_config，errors[].code = invalid_percentage
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/invalid_percentage.json
```

---

## 配置 JSON 结构

```jsonc
{
  "version": "2026-09-17",          // 必填、非空、全局唯一、不可变
  "flags": {
    "new_checkout": {
      "enabled": true,              // 关闭的开关直接取 default，reason=disabled
      "default": false,             // 所有规则/分流都未命中时的兜底值（任意 JSON）
      "prerequisites": [            // 前置开关，按顺序短路
        { "flag": "signed_in", "expect": true }
      ],
      "rules": [                    // 有序规则，首条命中即返回
        {
          "id": "internal-testers", // 同一开关内规则 ID 不可重复（可省略）
          "conditions": [           // 空数组 = 全量兜底规则
            { "attr": "email", "op": "suffix", "value": "@acme.com" }
          ],
          "value": true             // 命中后返回的值（任意 JSON）
        }
      ],
      "rollout": {                  // 规则未命中时进行百分比分流
        "percentage": 25,           // 整数，0–100，发布时强校验
        "key": "user_id",           // 目前仅支持 user_id
        "value": true               // 落入分流时返回的值
      }
    }
  }
}
```

### 判定优先级（每个开关）

1. **前置开关**：任一前置的实际布尔值不等于 `expect` → `prerequisite_failed`，短路返回 `false`；
2. **开关关闭**（`enabled=false`）→ 返回 `default`，`reason=disabled`；
3. **有序规则**：首个全部条件满足的规则命中 → `rule_match`，返回规则 `value`；
4. **百分比分流**：`bucket = sha1("开关名:user_id") 前 8 位十六进制 % 100`，`bucket < percentage` → `rollout_in`，否则 `rollout_out`；无 `user_id` 时一律落在外；
5. 都未命中 → 返回 `default`，`reason=default`。

支持的操作符：`eq`、`ne`、`in`、`nin`（值可为数组或逗号分隔字符串）、`gt`、`ge`、`lt`、`le`、`prefix`、`suffix`。类型不兼容的比较安全地返回不命中。

### 稳定性保证

- 同一版本的配置以规范化 JSON 整体存入 SQLite（`config_versions` 表，主键即版本号），**只增不改**；
- 环境指针存于 `environments` 表（主键为环境名），revision 校验与指针更新在同一 `BEGIN IMMEDIATE` 事务内完成，并发安全；
- 判定是纯函数：`(配置, user_id, attributes) → 结果`，无随机数、无时间因子；
- 哈希同时混入开关名（盐），不同开关互不相关；放量从 0→100 单调，不会反复抖动。

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/configs` | 发布一个新版本（语义校验失败返回 422；版本重复返回 409） |
| `GET` | `/api/v1/configs` | 列出所有版本 |
| `GET` | `/api/v1/configs/{version}` | 获取某版本完整配置（不存在返回 404） |
| `POST` | `/api/v1/config-comparisons` | 版本影响分析，请求体 `{from_version, to_version}`，返回开关差异与反向依赖影响（任一版本不存在返回 404） |
| `POST` | `/api/v1/evaluate` | 批量判定，请求体 `{version, flags[], user_id?, attributes?}` |
| `PUT` | `/api/v1/environments/{name}` | 创建/移动环境指针，请求体 `{version, expected_revision}`；创建仅接受 `expected_revision=0`；版本不存在返回 `404 version_not_found`，revision 过期返回 `409 stale_revision` 且指针不变 |
| `GET` | `/api/v1/environments/{name}` | 读取环境指针当前 `version` 与 `revision`（不存在返回 `404 environment_not_found`） |
| `POST` | `/api/v1/environments/{name}/evaluate` | 按环境指针判定，请求体 `{flags[], user_id?, attributes?}`；响应回显本次快照的 `version` 与 `revision` |
| `GET` | `/healthz` | 健康检查 |

错误统一使用信封格式：`{"error": {"code": "...", "message": "...", "errors": [...]}}`。
批量判定中个别开关不存在不会使整个请求失败：结果里该开关 `reason=flag_not_found`，同时出现在顶层 `missing_flags` 中。

---

## 本地开发与测试（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

FLAG_DB_PATH=data/flags.db uvicorn app.main:app --reload --port 8000
```

核心引擎与校验器只依赖标准库，可直接运行单测（无需安装任何依赖）：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：有序规则与短路前置、依赖链顺序、禁用开关、哈希桶稳定性/单调性/边界（0 与 100）、二/三节点循环与自依赖拒绝、非法比例与未知操作符拒绝、SQLite 版本不可变与历史保留、固定版本重复判定稳定性、版本差异对比与影响传播（边并集、最短路径、字典序平局、并集成环、重复去重）、环境指针创建/更新/过期 revision 拒绝/未知版本拒绝/并发写入仅一方成功。

## 项目结构

```
app/
  main.py        FastAPI 路由与错误处理
  schemas.py     Pydantic v2 请求/响应模型
  db.py          SQLite 存储（WAL）：不可变版本 + 环境指针
  validation.py  发布时语义校验（循环依赖/比例/操作符等）
  evaluator.py   纯函数判定引擎（前置、规则、稳定哈希分流）
  comparison.py  版本对比与反向依赖影响分析（纯函数）
tests/           标准库 unittest 测试
examples/        发布/判定/版本对比/环境指针/非法配置示例
Dockerfile, docker-compose.yml
```
