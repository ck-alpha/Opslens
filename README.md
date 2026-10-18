# OpsLens

面向运营分析的可验证 Data Agent。这个个人项目把指标口径、SQL 治理、数据溯源、独立校验和可恢复任务组合成一个可运行的分析工作台。

SQL 能执行，不代表它回答了正确的问题。OpsLens 显式记录指标版本和分析范围，检查候选 SQL 的权限与语义，保存结果证据，并把需要复核的结果与已验证事实分开展示。

## 功能与架构

- 版本化指标定义、歧义澄清和带引用的业务知识检索。
- 基于 SQLGlot 的 SQL 策略检查、DuckDB 只读执行、输出列溯源与证据记录。
- 根据指标定义和确认的分析范围独立计算参考结果，生成结构化验证报告。
- PostgreSQL 保存任务、检查点、事件与产物；界面支持历史查询、恢复和重新运行。
- 可选本地 LLM 或 API 生成候选 SQL；候选仍经过相同治理与验证流程。

```mermaid
flowchart TD
    UI[Panel 分析工作台] --> S[ProductService]
    S --> C[指标澄清与业务知识检索]
    C --> I[用户确认分析范围]
    I --> L[可选 LLM 候选 SQL]
    S --> D[演示配方 / 提供的 SQL]
    L --> R[持久运行时]
    D --> R
    R --> G[SQL 策略检查]
    G --> X[只读执行与证据构建]
    X --> V[指标与语义验证]
    V --> O[验证报告 / 需要复核]
    G --> B[策略拒绝]
    R --> PG[(PostgreSQL 检查点与事件)]
    X --> DB[(合成 DuckDB 数据)]
    V --> Q[独立指标计算器]
    Q --> DB
```

本项目实现指标契约、治理规则、证据与验证流程、持久任务状态及应用界面集成。Lumen 提供向量检索和工具适配等基础能力；Panel 提供交互界面和服务框架。Lumen 的 Planner、SQLAgent 等属于第三方原生能力，当前演示采用显式执行管线，不代表自主规划能力。


## 最短运行方式

需要 Docker 和支持 `--wait` 的 Docker Compose。首次构建需要联网下载 Python 依赖、固定版本的 Lumen 和 tokenizer 资源。

```bash
git clone https://github.com/ck-alpha/Opslens.git
cd Opslens
cp .env.example .env
# 将 .env 中 POSTGRES_PASSWORD 的示例值改为本地演示密码。
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec opslens python scripts/readiness_check.py
```

打开 <http://127.0.0.1:5006>。启动时自动生成合成数据并迁移运行数据库。选择 **New Analysis → Demo Analyses → August Gross GMV → Run Demo Analysis**，查看验证事实、证据与事件；再选择 **Known Limitation: channel_id** 或 **Policy Rejection**，观察复核和策略拒绝行为。默认演示不需要模型或 API key。

运行 `docker compose down` 停止服务；命名卷保留演示数据与任务历史。应用端口默认仅绑定本机。

## 原生 Python 运行

以下命令在仓库根目录执行，需要 Python 3.12、Git，以及 Docker 提供本地 PostgreSQL。密码仅为可丢弃的本地示例。原生进程从环境变量读取配置，不自动加载 `.env`。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
docker run -d --name opslens-local-postgres \
  -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=opslens_demo \
  -e POSTGRES_PASSWORD=EXAMPLE_LOCAL_ONLY \
  -e POSTGRES_DB=opslens_runtime postgres:16
until docker exec opslens-local-postgres pg_isready -U opslens_demo -d opslens_runtime; do sleep 1; done
export OPSLENS_RUNTIME_DATABASE_URL='postgresql+psycopg://opslens_demo:EXAMPLE_LOCAL_ONLY@127.0.0.1:55432/opslens_runtime'
python scripts/generate_demo_data.py
python -m alembic upgrade head
python scripts/readiness_check.py
python scripts/serve_ui.py
```

首次构建知识检索索引可能下载 tokenizer 资源。启动后访问同一地址；`Ctrl+C` 停止界面。也可用 [runtime_cli.py](scripts/runtime_cli.py) 的 `start`、`status`、`events`、`resume` 和 `rerun` 操作任务，参数见 `--help`。

可选 Ollama：先在本机安装并启动 Ollama，准备能够按 JSON 格式输出 SQL 的模型。停止界面后，在同一原生 Python 环境设置以下变量再启动；模型标记必须替换为 `ollama list` 中实际存在的值。适配器仅接受本机回环地址，当前 Compose 不接入宿主机 Ollama。

```bash
export OPSLENS_LLM_PROVIDER=ollama
export OPSLENS_OLLAMA_MODEL=REPLACE_WITH_INSTALLED_MODEL_TAG
export OPSLENS_OLLAMA_BASE_URL=http://127.0.0.1:11434
export OPSLENS_LLM_TIMEOUT_SECONDS=180
export OPSLENS_LLM_MAX_REPAIRS=1
python scripts/serve_ui.py
```

## 验证

在上述 Python 环境中执行。集成测试会写入数据并执行隔离迁移，必须使用名称以 `_test` 结尾的独立测试库。下面使用同一 PostgreSQL 实例中的另一个数据库；不要指向保存分析历史的运行库。

```bash
docker exec opslens-local-postgres createdb -U opslens_demo opslens_runtime_test
python -m ruff check .
python -m pyright
python -m pip check
(
  export OPSLENS_RUNTIME_DATABASE_URL='postgresql+psycopg://opslens_demo:EXAMPLE_LOCAL_ONLY@127.0.0.1:55432/opslens_runtime_test'
  python -m alembic upgrade head
  python -m pytest -q
)
python scripts/smoke_test.py
python scripts/packaging_smoke.py
python scripts/ui_smoke_test.py
python scripts/readiness_check.py
```

UI smoke 会在临时本机端口启动真实 Panel 服务。Wheel smoke 在临时目录构建和安装包，检查隔离导入；完整应用仍需仓库中的 `data/`、`alembic/` 和脚本。[CI](.github/workflows/ci.yml) 使用可丢弃的 PostgreSQL 测试库执行这些检查，不调用真实模型。

## 已知边界

- 全部演示数据为合成数据，结果不代表真实业务表现。
- 指标计算器仅覆盖支持的指标、维度和 SQL 形式；复杂表达式、`channel_id` 维度等可能进入复核。长结果表的报告展示可能截断。
- `VERIFIED` 表示通过当前规则及数据范围内的校验，不是任意自然语言问题或数据本身正确性的保证。模型生成失败和验证失败会显式呈现。
- 检查点可复用成功步骤；被中断的读取可能重新执行，不承诺恰好执行一次。

项目代码采用 [MIT License](LICENSE)。第三方框架、数据库驱动和其他依赖保留各自许可，参见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
