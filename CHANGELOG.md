# 更新日志 / Changelog

本项目版本号见根目录 `VERSION` 文件，Docker 镜像 tag 与之对应（`p0luz/ombre-brain:<VERSION>`）。

## 2.4.13

### 修复 / Fixed

- 修复向量 API 被反复重复调用的问题：`trace(content=...)` / `plan()` / `letter_write()` 在
  `bucket_mgr.update()` / `create()` 已经内部同步生成并存好向量之后，又各自显式调用了一次
  `embedding_engine.generate_and_store()`，导致每次写操作都对同一段内容打两次向量 API。
  现在移除了这些多余的显式调用。
- `EmbeddingEngine` 新增进程内小容量 LRU 查询缓存：`breath(query=...)` 内部会对同一个查询串
  各自调用一次向量检索（`bucket_mgr.search()` 内部一次、`surface_search()` 直接又一次），
  `hold()`/`grow()` 的 `merge_or_create` → `check_duplicate_for` → `check_plan_resolution`
  三条 fire-and-forget 链路也会对同一段新内容各嵌入一次。同一段文本对同一模型的向量结果恒定，
  缓存后这些短时间内的重复请求不再重新打向量 API。

### 测试 / Tests

- 现有 `tests/test_embedding_api_regression.py` 等回归测试全部通过，确认门面缓存不影响
  既有向量化行为。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.13。

## 2.4.12

### 优化 / Improved

- `pulse()` 顶部统计现在单独显示 feel / plan / letter 数量，避免列表数量和头部统计看起来对不上。
- `grow()` 短内容走 hold 风格单条保存时会明确提示“没有拆分”，减少短日记归档时的误解。
- Dashboard 保存 `OMBRE_HOST_VAULT_DIR` 后直接提示需要重启容器/服务；API 也返回 `restart_required` 和 `message`。
- Dashboard 将单桶、信件和导入审核删除文案改为“删除到档案”，与清理模式里的物理永久删除明确区分。
- `trace(resolved=1)` 与 REST resolve 共用同一套中文提示，Dashboard 会展示“已沉底/已重新激活”的一致说明。
- `config.example.yaml` 移除已废弃的 active `wikilink:` 配置段，只保留 deprecated 说明。

### 测试 / Tests

- 新增 `tests/test_priority4_confusion_cleanup.py` 覆盖上述高频困惑点的回归。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.12。

## 2.4.11

### 修复 / Fixed

- MCP OAuth 支持 `refresh_token` grant：授权码换 token 时会同时返回 refresh token，headless 服务器环境下 access token 失效后可直接刷新，不再必须重新打开浏览器授权页。
- OAuth discovery 与动态客户端注册现在声明 `refresh_token`，并兼容旧版 `.dashboard_mcp_tokens.json` access token 存储格式。
- 修复 v3 legacy 桥接层缺失的 runtime/web/bucket side-channel API，恢复工具调用、Web 路由注册、更新策略评估和 bucket 生命周期事件的只读旁路记录。

### 测试 / Tests

- 新增 `tests/test_oauth_refresh_token.py` 覆盖 refresh token 元数据声明、授权码换 refresh token、刷新 access token、未知 refresh token 拒绝。
- 修复并恢复 `tests/test_v3_legacy_*` 桥接回归，测试用例显式注入 fake embedding，避免绕开当前“写入必须有向量化”的生产约束。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.11。

## 2.4.10

### 新增 / Added

- GitHub 同步现在会在同一次 commit 中写入 `_ombre_backup_manifest.json`，记录备份生成时间、文件数、总字节数、每个 bucket markdown 的大小和 sha256。
- 从 GitHub 导入/恢复时会读取 manifest 摘要并返回给调用方，后续可用于恢复前校验和备份选择。

### 测试 / Tests

- 新增 `tests/test_github_backup_manifest.py` 覆盖 manifest 生成、同步写入和恢复读回。
- 更新 zero-commit 空仓库同步测试，确认首次提交也包含 manifest。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.10。

## 2.4.9

### 新增 / Added

- Dashboard 历史对话导入新增上传前预检：选中文件后先显示识别格式、轮次、分块数、预计 API 调用、文件大小、首个分块预览和警告，再由用户确认开始导入。
- 新增 `POST /api/import/preflight`，复用导入解析/分块逻辑做只读预检，不写 bucket、不启动后台任务。
- 新增 `preview_import()` 纯函数，便于后续把导入体验继续拆成更明确的预检查项。

### 测试 / Tests

- 新增 `tests/test_import_preflight.py` 覆盖导入预检纯函数和 API 路由。
- 新增 `tests/test_dashboard_import_preflight.py` 覆盖 Dashboard 预检入口。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.9。

## 2.4.8

### 新增 / Added

- Dashboard 设置页新增“系统体检”面板，可一键查看数据目录、记忆桶统计、脱水/打标 LLM、向量化、GitHub 备份、访问控制和运行时状态。
- 新增 `GET /api/system/diagnostics` 只读接口，返回结构化 `ok` / `warning` / `error` 检查项；体检不主动请求外部 API，避免设置页被慢网络卡住。

### 测试 / Tests

- 新增 `tests/test_system_diagnostics.py` 覆盖诊断接口和缺配置告警。
- 新增 `tests/test_dashboard_diagnostics_panel.py` 覆盖 Dashboard 体检入口。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.8。

## 2.4.7

### 修复 / Fixed

- 修复 GitHub 新建空仓库（Zero Commit，首页仍是 Quick setup）首次同步时报 `409 Conflict` 的问题。现在 Ombre 会在空仓库中创建初始 tree/commit，并创建 `refs/heads/<branch>`，无需用户先手动添加 README。
- 从空 GitHub 仓库导入时返回“暂无可导入文件”，不再把空仓库 409 当作异常。

### 测试 / Tests

- 新增 `tests/test_github_sync_zero_commit.py` 覆盖 zero-commit 仓库首次存档 bootstrap 流程。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.7。

## 2.4.6

### 优化 / Improved

- Dashboard 批量导入的 LLM 抽取结果解析改为宽松 JSON 清洗：支持 DeepSeek 等模型在 JSON 数组/对象前后附带说明文字，减少 `Import extraction JSON parse failed`。
- 抽出通用 `clean_llm_json()`，让导入解析与 grow/dehydrator 的 JSON 解析共用同一套 code fence/JSON 片段提取逻辑。

### 测试 / Tests

- 新增 `tests/test_import_extraction_json.py` 覆盖模型回复包含说明文字时的导入解析回归。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.6。

## 2.4.5

### 优化 / Improved

- 新增 LLM / embedding 请求超时配置：`dehydration.timeout_seconds`、`embedding.timeout_seconds`，以及环境变量 `OMBRE_COMPRESS_TIMEOUT_SECONDS`、`OMBRE_EMBED_TIMEOUT_SECONDS`。
- 写记忆时的脱水/打标、原生 Gemini、OpenAI 兼容 embedding 请求都会使用配置的超时时长，方便国内自托管服务器连接云端 API 较慢时调大等待时间。

### 测试 / Tests

- 新增 `tests/test_api_timeout_config.py` 覆盖 config/env 覆盖和运行时对象 timeout 传递。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.5。

## 2.4.4

### 修复 / Fixed

- 允许在 Dashboard 清空或修改 `AI_NAME`，避免关闭 OAuth 后仍显示旧的 AI 显示名；清空后回退为默认 `AI`。
- 统一桶元数据读取层的日期时间序列化，将 `created` / `last_active` 中的 `datetime` / `date` 归一化为 ISO 字符串，避免 `dream()`、Dashboard 首页和导入页面 JSON 序列化报错。
- 版本检查优先通过 GitHub Contents API 读取 `VERSION`，避免 raw CDN 在 push 后继续返回旧版本导致热更新检测不到新版本。

### 测试 / Tests

- 新增 `tests/test_env_config_identity.py` 覆盖 AI 显示名清空回归。
- 新增 `tests/test_datetime_metadata_normalization.py` 覆盖 YAML/frontmatter 时间戳被解析为 `datetime` 后的序列化回归。
- 新增 `tests/test_dashboard_update_source.py` 覆盖 Dashboard 版本检查的 GitHub API 优先顺序。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.4。

## 2.4.0

### 架构 / Architecture

- 将当前高级架构线统一作为对外发布版本 `2.4.0`。
- 保留内部 `src/ombrebrain/` 架构层命名：acceptance、eventsourcing、retrieval、microkernel、plugins、distributed 等模块继续作为内部深内核层存在。
- 保持 MCP tool names、bucket markdown、Dashboard existing routes、config/env 语义不变。

### 修复 / Fixed

- 修复 `tests/test_permanent_breath_regression.py` 中写死 Windows 路径分隔符的断言，改为 `os.sep`，避免 Linux / Docker / CI 下出现跨平台假失败。

### 维护 / Chores

- VERSION + `src/VERSION` -> 2.4.0。
- capability catalog 的 manifest version 改为读取项目版本，避免对外元数据继续暴露旧的架构草案版本号。

## 2.3.22

### 前端 / Frontend

- 写信表单「身份」下拉固定为 `user` / `AI`（对面是 AI 这点不必纠结具体模型名）；
  具体署名由用户在旁边的「署名」框自行填写。
- 写信表单的日期选择改造成拟态化「按钮」：点击主动唤起原生日期选择器（`showPicker()`
  + `focus/click` 兜底），选定后按钮显示所选日期；解决了原生小日历图标与提示文字重叠、
  以及透明输入框点击无响应的问题。
- 「服务日志」页右上角的日志文件路径只显示文件名（如 `server.log`），完整路径移到鼠标
  悬停提示，界面更干净、也不在页面上暴露本机绝对路径。

### 维护 / Chores

- VERSION + `src/VERSION` → 2.3.22。

## 2.3.21

### 新增 / Added

- **letter 署名支持自定义 AI 名称。** `letter_write` 的 `author` 不再限定
  `"user"`/`"claude"`，改为接受任意字符串署名：
  - `"user"` → 用户侧（`user_name` 逻辑不变）；
  - `"ai"`、等于 `ai_name` 的值、或历史遗留的 `"claude"` → 统一存为 `ai_name` 的值；
  - 其它任意字符串 → 原样作为署名。
  新增可选参数 `ai_name`（显式传入优先），默认取环境变量 `AI_NAME`，回退 `"AI"`。
  `letter_read` 原样返回存储的署名、不做转换；按 `author` 过滤时 `"ai"` 会同时
  命中新署名与历史 `"claude"` 信件。Dashboard 写信/筛选、SessionStart 钩子的「最近的信」
  同步适配。（`src/tools/plan/core.py`、`src/web/letters.py`、`src/web/hooks.py`、
  `src/server.py`、`frontend/dashboard.html`；回归测试 `tests/test_letter_author_regression.py`）
- 新增共享 helper `utils.get_ai_name()`：统一从环境变量 `AI_NAME` 读取 AI 显示名（回退 `"AI"`）。
- `.env.example` 新增 `AI_NAME=` 条目及说明。

### 变更 / Changed

- **全局去除面向用户文本与注释中的 "Claude" 硬编码。** 面向用户的文案（OAuth 授权页、
  Dashboard 删除确认/提示、配置项说明）改为中性的 "AI"；代码注释中的 "Claude" 统一改为
  "AI"/"LLM"。保留第三方服务/格式/文件的固有名（如 `Claude Desktop`、`claude.ai`、
  `claude_desktop_config.json`、Claude/ChatGPT 导出格式、Anthropic 模型 ID），以及 letter
  存储层对历史 `"claude"` 署名的向后兼容判断。

### 维护 / Chores

- 同步 bump `src/VERSION`（热更新读取的副本）与根 `VERSION` 至 2.3.21。

## 2.3.20

### 修复 / Fixed

- **`breath(importance_min=N)` 在高重要度桶塞满上限时，刚被 `trace` 降级的桶看似「未刷新」**
  之前 `breath(importance_min=N)` 把所有符合阈值的桶按 importance 降序排，直接截取前 20 条。当 `importance=10` 的桶超过 20 个时，一个刚用 `trace` 从 10 降到 9 的桶会被高分桶挤出列表，看起来像「trace 改了 importance 但 breath 没刷新」。
  现在改为先给每个符合阈值的 importance 档位（10、9…）各预留一条最近更新的桶，再按正常排序填满剩余名额，确保降级后的桶在其档位仍可见。
  （`src/tools/breath/importance.py` `_select_importance_buckets`；回归测试见 `tests/test_trace_importance_regression.py`）

  > 说明：`trace` 写入 importance 后，`breath` 是每次从磁盘实时重读、无缓存，本身不存在「需要额外操作触发刷新」。若 `trace` 降级看似无效，请先确认目标桶不是 `pinned`/`protected`——这类核心桶 importance 被锁定为 10，`trace` 会拒绝降级并返回提示，需先 `trace(bucket_id, pinned=0)` 再调整 importance。

### 维护 / Chores

- 修正 `.gitignore`：`docs/secrets/`（复数）此前未被忽略，补上规则，避免本地密钥/设计稿目录被纳入版本控制。
