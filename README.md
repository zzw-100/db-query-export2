# PostgreSQL MCP 服务器

一个生产级的 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 服务器，使用户能够通过自然语言与 PostgreSQL 数据库进行交互。该服务器基于 FastMCP 构建，将自然语言问题转换为安全的 SQL 查询，执行查询并验证结果。一些参考文档：

- Python Postgres MCP 需求研究
: <https://gemini.google.com/share/c87a73f0969b>
- SQLGlot 深度研究方案
: <https://gemini.google.com/share/cc5e45c76c8f>

## 功能特性

- **自然语言转 SQL**：使用 GPT-5.2-mini 将普通英文问题转换为优化的 PostgreSQL 查询
- **多数据库支持**：通过 `DATABASES` 配置多个数据库，每个请求可用 `database` 参数指定目标库，各库独立连接池与执行器
- **安全至上**：只读强制执行、危险函数黑名单、敏感表/列访问控制、EXPLAIN 策略、SQL 注入防护、查询超时控制
- **结果验证**：基于 AI 的结果验证，提供置信度评分
- **Schema 智能化**：自动 Schema 缓存，基于 TTL 的刷新机制
- **生产就绪**：连接池管理、限流、重试退避、熔断器、Prometheus 指标与全链路追踪，均已整合进请求处理链路
- **健康检查**：`health` MCP 工具与 `GET /health` 端点，报告连接池、熔断器与限流状态
- **MCP 兼容**：支持 Claude Desktop 和任何 MCP 兼容客户端

## 快速开始

### 前置条件

- Python 3.14+
- PostgreSQL 12+
- OpenAI API 密钥（用于 GPT-5.2-mini）
- UV 包管理器（推荐）或 pip

### 安装

#### 使用 UV（推荐）

```bash
# 克隆仓库
git clone <repository-url>
cd pg-mcp

# 安装依赖
uv sync

# 复制环境配置模板
cp .env.example .env

# 编辑 .env 并配置参数
vi .env
```

#### 使用 pip

```bash
# 克隆仓库
git clone <repository-url>
cd pg-mcp

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows 系统: .venv\Scripts\activate

# 安装依赖
pip install -e .

# 复制环境配置模板
cp .env.example .env

# 编辑 .env 并配置参数
vi .env
```

### 配置

编辑 `.env` 文件以配置您的设置：

```bash
# 数据库配置
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=your_database
DATABASE_USER=your_user
DATABASE_PASSWORD=your_password

# OpenAI 配置
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_MODEL=gpt-5.2-mini

# 安全设置（可选，显示默认值）
SECURITY_ALLOW_WRITE_OPERATIONS=false
SECURITY_MAX_ROWS=10000
SECURITY_MAX_EXECUTION_TIME=30

# 敏感对象封锁（可选，默认为空）
SECURITY_BLOCKED_TABLES=salaries,public.audit_log
SECURITY_BLOCKED_COLUMNS=ssn,users.password
SECURITY_ALLOW_EXPLAIN=false
```

完整的配置选项请参考 `.env.example`。

### 多数据库配置

默认使用上方 `DATABASE_*` 配置的单个数据库。需要同时服务多个数据库时，将 `DATABASES` 设置为 JSON 列表；设置后它将取代 `DATABASE_*` 成为服务器的数据库列表：

```bash
DATABASES=[{"name":"blog_small","host":"localhost","port":5432,"user":"postgres","password":"postgres","min_pool_size":2,"max_pool_size":10},{"name":"analytics","host":"localhost","port":5432,"user":"postgres","password":"postgres","min_pool_size":2,"max_pool_size":10}]
```

- 每个条目的 `name` 必须唯一，服务器为每个库建立独立的连接池与 SQL 执行器
- 调用 `query` 工具时通过 `database` 参数选择目标库；仅配置一个库时可省略，会自动选择
- 请求指定了未配置的库名时直接返回错误并列出可用库名，不会回落到其他库执行

### 运行服务器

#### 独立模式

```bash
# 使用 UV
uv run python main.py

# 或使用 pip
python main.py
```

#### 与 Claude Desktop 集成

添加以下配置到 Claude Desktop MCP 设置文件：

**macOS/Linux**: `~/Library/Application Support/Claude/claude_desktop_config.json`

**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_NAME": "your_database",
        "DATABASE_USER": "your_user",
        "DATABASE_PASSWORD": "your_password",
        "OPENAI_API_KEY": "sk-your-api-key-here"
      }
    }
  }
}
```

详细配置说明请参阅 [Claude Desktop 配置](#claude-desktop-配置)。

## 使用方法

### 示例查询

通过 Claude Desktop 或其他 MCP 客户端连接后，您可以提出自然语言问题：

#### 简单查询

```
How many tables are in the database?
→ SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'

Show me all users
→ SELECT * FROM users LIMIT 10000

What are the column names in the products table?
→ SELECT column_name, data_type FROM information_schema.columns
  WHERE table_name = 'products'
```

#### 分析查询

```
What are the top 10 products by sales?
→ SELECT product_name, SUM(quantity * price) as total_sales
  FROM orders
  GROUP BY product_name
  ORDER BY total_sales DESC
  LIMIT 10

How many users registered in the last 30 days?
→ SELECT COUNT(*) FROM users
  WHERE created_at > CURRENT_DATE - INTERVAL '30 days'
```

#### 指定目标数据库

配置了多个数据库时，通过 `query` 工具的 `database` 参数指定查询目标：

```
How many daily active users are there?
Database: analytics
→ 在 analytics 库上生成并执行 SQL，不触及已配置的其他库
```

#### 仅 SQL 模式

您也可以只请求 SQL 而不执行：

```
Generate SQL to find duplicate emails
Return Type: sql
→ Returns: SELECT email, COUNT(*) FROM users GROUP BY email HAVING COUNT(*) > 1
```

### 返回类型

服务器支持两种返回类型：

- **`result`**（默认）：执行查询并返回结果
- **`sql`**：生成并验证 SQL，但不执行

### 响应格式

#### 成功查询响应

```json
{
  "success": true,
  "generated_sql": "SELECT COUNT(*) FROM users",
  "data": {
    "columns": ["count"],
    "rows": [[1523]],
    "row_count": 1,
    "execution_time": 0.023
  },
  "confidence": 95,
  "tokens_used": 234
}
```

#### 仅 SQL 响应

```json
{
  "success": true,
  "generated_sql": "SELECT * FROM users WHERE created_at > CURRENT_DATE - INTERVAL '30 days'",
  "confidence": 90,
  "tokens_used": 156
}
```

#### 错误响应

```json
{
  "success": false,
  "error": {
    "code": "SECURITY_VIOLATION",
    "message": "Query contains blocked operation: DELETE",
    "details": {
      "blocked_operation": "DELETE"
    }
  }
}
```

## 架构

### 核心组件

```
┌─────────────────────────────────────────────────────────────┐
│                      MCP Server (FastMCP)                   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Query Orchestrator                       │
│  - Coordinates all components                               │
│  - Manages retry logic                                      │
│  - Handles error recovery                                   │
└─────────────────────────────────────────────────────────────┘
           │                  │                  │
           ▼                  ▼                  ▼
    ┌───────────┐     ┌────────────┐     ┌──────────────┐
    │   SQL     │     │    SQL     │     │     SQL      │
    │ Generator │────▶│ Validator  │────▶│  Executor    │
    │ (LLM)     │     │ (Security) │     │ (Database)   │
    └───────────┘     └────────────┘     └──────────────┘
           │                                      │
           ▼                                      ▼
    ┌───────────┐                          ┌──────────────┐
    │  Schema   │                          │   Result     │
    │  Cache    │                          │  Validator   │
    └───────────┘                          │  (LLM)       │
                                           └──────────────┘
```

### 安全特性

1. **只读强制执行**：默认仅允许 SELECT 查询
2. **阻止危险函数**：黑名单包含危险的 PostgreSQL 函数（pg_sleep、文件 I/O 等）
3. **敏感表/列封锁**：`SECURITY_BLOCKED_TABLES` / `SECURITY_BLOCKED_COLUMNS` 封锁的表和列（支持 `public.salaries` 这类 schema 限定名）会被 SQL 验证器直接拒绝
4. **EXPLAIN 策略**：默认禁止 EXPLAIN 语句，需通过 `SECURITY_ALLOW_EXPLAIN` 显式开启
5. **输入长度限制**：超过 `VALIDATION_MAX_QUESTION_LENGTH` 的问题在进入 LLM 前即被拒绝
6. **SQL 解析**：使用 sqlglot 进行准确的 SQL 结构验证
7. **注入防护**：参数化查询和输入清理
8. **资源限制**：
   - 行数限制（默认：10,000）
   - 查询超时（默认：30 秒）
   - 每库独立连接池管理
9. **事务隔离**：所有查询在只读事务中运行

### 弹性特性

以下机制均在实际请求处理路径中生效，而非仅停留在设计层面：

- **限流**：`RESILIENCE_MAX_CONCURRENT_QUERIES` / `RESILIENCE_MAX_CONCURRENT_LLM_CALLS` 分别限制并发数据库执行与 LLM 调用，超出的请求排队等待
- **重试退避**：LLM 调用与数据库执行遇到瞬态故障（连接断开、死锁、序列化冲突等）时自动重试，采用指数退避
- **熔断器**：LLM 连续失败后熔断，防止级联失败；恢复期半开探测
- **连接池**：每个数据库独立的连接池，高效的连接复用
- **Schema 缓存**：基于 TTL 的缓存减少数据库元数据查询

## 配置参考

### 数据库设置

| 变量                       | 描述            | 默认值      |
|----------------------------|-----------------|-------------|
| `DATABASE_HOST`            | PostgreSQL 主机 | `localhost` |
| `DATABASE_PORT`            | PostgreSQL 端口 | `5432`      |
| `DATABASE_NAME`            | 数据库名称      | 必需        |
| `DATABASE_USER`            | 数据库用户      | 必需        |
| `DATABASE_PASSWORD`        | 数据库密码      | 必需        |
| `DATABASE_MIN_POOL_SIZE`   | 池中最小连接数  | `5`         |
| `DATABASE_MAX_POOL_SIZE`   | 池中最大连接数  | `20`        |
| `DATABASE_COMMAND_TIMEOUT` | 查询超时（秒）    | `30`        |
| `DATABASES`                | 多数据库 JSON 列表，设置后取代上方 `DATABASE_*` | 空（使用 `DATABASE_*`） |

### OpenAI 设置

| 变量                 | 描述                    | 默认值         |
|----------------------|-------------------------|----------------|
| `OPENAI_API_KEY`     | OpenAI API 密钥         | 必需           |
| `OPENAI_MODEL`       | 使用的模型              | `gpt-5.2-mini` |
| `OPENAI_MAX_TOKENS`  | 每次请求的最大 token 数 | `32000`        |
| `OPENAI_TEMPERATURE` | 模型温度                | `0.0`          |
| `OPENAI_TIMEOUT`     | API 超时（秒）            | `30`           |

### 安全设置

| 变量                              | 描述                      | 默认值            |
|-----------------------------------|---------------------------|-------------------|
| `SECURITY_ALLOW_WRITE_OPERATIONS` | 允许 INSERT/UPDATE/DELETE | `false`           |
| `SECURITY_BLOCKED_FUNCTIONS`      | 逗号分隔的函数黑名单      | 参考 .env.example |
| `SECURITY_BLOCKED_TABLES`         | 禁止访问的表，逗号分隔，支持 schema 限定名 | 空 |
| `SECURITY_BLOCKED_COLUMNS`        | 禁止访问的列，逗号分隔，支持 `table.column` | 空 |
| `SECURITY_ALLOW_EXPLAIN`          | 是否允许 EXPLAIN 语句     | `false`           |
| `SECURITY_READONLY_ROLE`          | 执行会话切换到的只读角色  | 空                |
| `SECURITY_SAFE_SEARCH_PATH`       | 执行会话使用的安全 search_path | `public`     |
| `SECURITY_MAX_ROWS`               | 每个查询的最大行数        | `10000`           |
| `SECURITY_MAX_EXECUTION_TIME`     | 查询超时（秒）              | `30`              |

### 缓存设置

| 变量               | 描述                | 默认值 |
|--------------------|---------------------|--------|
| `CACHE_ENABLED`    | 启用 Schema 缓存    | `true` |
| `CACHE_SCHEMA_TTL` | Schema 缓存 TTL（秒） | `3600` |
| `CACHE_MAX_SIZE`   | 最大缓存 Schema 数  | `100`  |

### 弹性设置

| 变量                                   | 描述             | 默认值 |
|----------------------------------------|------------------|--------|
| `RESILIENCE_MAX_RETRIES`               | 最大重试次数     | `3`    |
| `RESILIENCE_RETRY_DELAY`               | 初始重试延迟（秒） | `1.0`  |
| `RESILIENCE_BACKOFF_FACTOR`            | 指数退避倍数     | `2.0`  |
| `RESILIENCE_MAX_CONCURRENT_QUERIES`    | 并发数据库执行上限 | `10` |
| `RESILIENCE_MAX_CONCURRENT_LLM_CALLS`  | 并发 LLM 调用上限  | `5`  |
| `RESILIENCE_CIRCUIT_BREAKER_THRESHOLD` | 熔断前的失败数   | `5`    |
| `RESILIENCE_CIRCUIT_BREAKER_TIMEOUT`   | 熔断器超时（秒）   | `60`   |

### 可观测性设置

| 变量                            | 描述                 | 默认值 |
|---------------------------------|----------------------|--------|
| `OBSERVABILITY_METRICS_ENABLED` | 启用 Prometheus 指标 | `true` |
| `OBSERVABILITY_METRICS_PORT`    | 指标 HTTP 端口       | `9090` |
| `OBSERVABILITY_LOG_LEVEL`       | 日志级别             | `INFO` |
| `OBSERVABILITY_LOG_FORMAT`      | 日志格式（json/text）  | `json` |

## 开发

### 设置开发环境

```bash
# 安装开发依赖
uv sync --all-extras

# 安装 pre-commit 钩子（可选）
pre-commit install
```

### 运行测试

```bash
# 运行所有测试
uv run pytest

# 运行并生成覆盖率报告
uv run pytest --cov=src --cov-report=html

# 运行特定测试类别
uv run pytest tests/unit/          # 仅单元测试
uv run pytest tests/integration/   # 集成测试
uv run pytest tests/e2e/           # 端到端测试
uv run pytest -m integration       # 标记为集成的测试
```

### 代码质量

```bash
# 类型检查
uv run mypy src

# Lint 和格式化
uv run ruff check --fix .
uv run ruff format .

# 运行所有质量检查
uv run pytest --cov=src --cov-fail-under=80
uv run mypy src
uv run ruff check .
```

### 项目结构

```
pg-mcp/
├── src/pg_mcp/
│   ├── cache/              # Schema 缓存
│   ├── config/             # 配置管理
│   ├── db/                 # 数据库连接池
│   ├── models/             # 数据模型
│   ├── observability/      # 日志、指标、追踪
│   ├── prompts/            # LLM Prompt 模板
│   ├── resilience/         # 熔断器、限流器、重试退避
│   ├── services/           # 核心业务逻辑
│   │   ├── orchestrator.py      # 查询协调
│   │   ├── sql_generator.py     # 基于 LLM 的 SQL 生成
│   │   ├── sql_validator.py     # 安全验证
│   │   ├── sql_executor.py      # 查询执行
│   │   └── result_validator.py  # 结果验证
│   └── server.py           # FastMCP 服务器
├── tests/
│   ├── unit/               # 单元测试
│   ├── integration/        # 集成测试
│   └── e2e/                # 端到端测试
├── fixtures/               # 测试数据库 fixture
├── .env.example            # 环境模板
├── pyproject.toml          # 项目配置
└── main.py                 # 入口点
```

## Docker 部署

### 构建镜像

```bash
docker build -t pg-mcp:latest .
```

### 运行容器

```bash
docker run -d \
  --name pg-mcp \
  -e DATABASE_HOST=your-db-host \
  -e DATABASE_NAME=your-db \
  -e DATABASE_USER=your-user \
  -e DATABASE_PASSWORD=your-password \
  -e OPENAI_API_KEY=sk-your-key \
  -p 9090:9090 \
  pg-mcp:latest
```

### Docker Compose

```bash
# 启动所有服务（PostgreSQL + pg-mcp）
docker-compose up -d

# 查看日志
docker-compose logs -f pg-mcp

# 停止服务
docker-compose down
```

详细配置参考 `docker-compose.yml`。

## 监控

### 指标

服务器在端口 9090（可配置）上暴露 Prometheus 指标：

```bash
curl http://localhost:9090/metrics
```

**可用指标：**

- `pg_mcp_query_requests_total{status,database}` - 按状态与目标库统计的请求数
- `pg_mcp_query_duration_seconds` - 请求端到端处理耗时直方图
- `pg_mcp_llm_calls_total{operation}` - LLM 调用次数
- `pg_mcp_llm_latency_seconds{operation}` - LLM 调用延迟直方图
- `pg_mcp_llm_tokens_used_total{operation}` - LLM token 消耗总数
- `pg_mcp_sql_rejected_total{reason}` - 被安全校验拒绝的 SQL 数
- `pg_mcp_db_query_duration_seconds` - 数据库执行耗时直方图
- `pg_mcp_db_connections_active{database}` - 各库活跃连接数
- `pg_mcp_schema_cache_age_seconds{database}` - Schema 缓存年龄

### 健康检查

`health` MCP 工具返回整体状态（`starting` / `ok` / `degraded`）、各数据库连接池、熔断器与限流状态；熔断器打开时状态为 `degraded`。在支持自定义路由的传输方式下，也可以直接访问 `GET /health` 端点获取相同内容。

### 日志

结构化 JSON 日志（或文本格式）输出到标准输出：

```json
{
  "timestamp": "2025-12-20T10:30:00.123Z",
  "level": "INFO",
  "message": "Query executed successfully",
  "database": "mydb",
  "execution_time": 0.023,
  "row_count": 42
}
```

## 故障排查

### 常见问题

#### 连接被拒绝

```
Error: Connection to database failed
```

**解决方案**：验证 PostgreSQL 正在运行且凭证正确：

```bash
psql -h $DATABASE_HOST -U $DATABASE_USER -d $DATABASE_NAME
```

#### OpenAI API 错误

```
Error: OpenAI API request failed
```

**解决方案**：

1. 检查 API 密钥是否有效且有额度
2. 验证网络连接
3. 如果请求超时，检查 `OPENAI_TIMEOUT` 设置

#### 查询超时

```
Error: Query execution timeout exceeded
```

**解决方案**：

1. 增加 `SECURITY_MAX_EXECUTION_TIME`
2. 优化数据库（添加索引、VACUUM）
3. 简化查询或添加过滤条件

#### Schema 缓存问题

```
Error: Schema not found in cache
```

**解决方案**：

1. 重启服务器以重新加载 Schema
2. 验证数据库用户有 Schema 读取权限
3. 检查 `CACHE_ENABLED` 是否设置为 `true`

### 调试模式

启用调试日志：

```bash
export OBSERVABILITY_LOG_LEVEL=DEBUG
uv run python main.py
```

## Claude Desktop 配置

### macOS/Linux 配置

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/yourname/projects/pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "mydb",
        "DATABASE_USER": "postgres",
        "DATABASE_PASSWORD": "your-password",
        "OPENAI_API_KEY": "sk-your-api-key-here",
        "OPENAI_MODEL": "gpt-5.2-mini",
        "SECURITY_MAX_ROWS": "10000",
        "CACHE_ENABLED": "true",
        "OBSERVABILITY_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Windows 配置

编辑 `%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\Users\\YourName\\projects\\pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_NAME": "mydb",
        "DATABASE_USER": "postgres",
        "DATABASE_PASSWORD": "your-password",
        "OPENAI_API_KEY": "sk-your-api-key-here"
      }
    }
  }
}
```

### 使用 Python Virtualenv

如果不使用 UV，请直接配置 Python：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "/absolute/path/to/pg-mcp/.venv/bin/python",
      "args": ["main.py"],
      "cwd": "/absolute/path/to/pg-mcp",
      "env": {
        "DATABASE_HOST": "localhost",
        ...
      }
    }
  }
}
```

### 重启 Claude Desktop

编辑配置后：

1. 完全退出 Claude Desktop
2. 重启 Claude Desktop
3. PostgreSQL MCP 服务器将可用

## 安全考虑

### 生产环境部署

1. **使用只读数据库用户**：创建专用 PostgreSQL 用户，仅具有 SELECT 权限：

```sql
CREATE USER pg_mcp_readonly WITH PASSWORD 'secure-password';
GRANT CONNECT ON DATABASE your_database TO pg_mcp_readonly;
GRANT USAGE ON SCHEMA public TO pg_mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pg_mcp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO pg_mcp_readonly;
```

2. **保护 API 密钥**：使用环境变量或秘密管理系统，切勿提交到版本控制

3. **网络隔离**：在隔离网络中运行服务器，通过 IP 限制数据库访问

4. **监控使用**：启用指标并为异常模式设置告警

5. **限流**：配置合适的限流参数以防止滥用

6. **日志清理**：敏感数据会自动从日志中过滤

## 本次迭代改动（相对上游 master）

针对 code review（`specs/w5/0006-pg-mcp-code-review.md`）指出的缺失功能完成的改造：

1. **多数据库与安全控制落地**：新增 `DATABASES` 多库配置，启动时为每库建立独立连接池与执行器；`query` 工具的 `database` 参数将请求路由到目标库，未配置的库名直接报错；`SECURITY_BLOCKED_TABLES` / `SECURITY_BLOCKED_COLUMNS` / `SECURITY_ALLOW_EXPLAIN` 由配置真正生效（此前在代码中被硬编码为不启用）。
2. **弹性与可观测性接入请求链路**：限流并发上限改为可配置；新增重试指数退避（`src/pg_mcp/resilience/retry.py`）；限流、重试、熔断器与全套 Prometheus 指标、request_id 全链路追踪均在实际请求处理路径中生效；新增 `health` 工具与 `/health` 端点。
3. **响应与模型缺陷修复**：合并重复的 `ErrorDetail` 定义；移除重复的 `to_dict` 方法，`tokens_used` 字段恒存在；启用此前闲置的配置字段（封锁表/列、EXPLAIN 策略、并发上限、`min_confidence_score` 与 `confidence_threshold` 取严执行、问题长度前置校验）。
4. **测试补强**：新增 `tests/unit/test_retry.py`、`test_gap_fixes.py`、`test_observability.py`、`test_result_validator.py` 与 `tests/integration/test_db.py`，覆盖多库路由、安全配置生效、限流闸门与瞬态错误重试。

## 许可证

[您的许可证信息]

## 贡献

欢迎贡献！请参阅 CONTRIBUTING.md 了解指南。

## 支持

如有问题和疑问：

- GitHub Issues：[repository-url]/issues
- 文档：查看 `specs/w5/` 目录获取详细设计文档

## 致谢

- 基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建
- SQL 解析由 [sqlglot](https://github.com/tobymao/sqlglot) 提供
- 数据库驱动：[asyncpg](https://github.com/MagicStack/asyncpg)
