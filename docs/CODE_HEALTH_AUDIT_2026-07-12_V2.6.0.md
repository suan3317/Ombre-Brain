# Ombre Brain v2.6.0 完整代码健康度审计

- 审计日期：2026-07-12
- 审计分支：`testing`
- 基线提交：`2f25743bf3d3ff4d63917e3371ca941e7de0383c` 加当前未提交工作树
- 边界：遵循 `rule.md`，不把记忆系统扩张为认知层，审计不改写现有记忆正文。

## 第一阶段：代码盘点

### 方法与结论

- 盘点 Git 跟踪文件、当前未跟踪源码与本报告，共 **486** 项；已排除 `__pycache__`、Rust `target/`、coverage 等生成物。第二阶段新增的 2 个回归测试也已补入清单。
- 对 Python 执行 AST import/call 可达性分析，并交叉检查 pytest 收集、`__main__` 入口、FastMCP 注册、Web 路由、Hook 和文档引用。
- `src` 共 **185** 个 Python 模块：**169** 个从生产/运维入口可达，**16** 个属于测试或 vNext 契约层，无完全不可达模块。
- 初始文件级结论：**483 活**、**0 死**、**3 存疑**。“活”包含运行码、测试、文档、构建/部署资源；3 个存疑项已在第二阶段完成风险核验并保留。
- Vulture 80% 唯一命中是 `BucketManager.create/update(... allow_embedding_fallback)`；经调用点和注释核对，这是为兼容旧调用方保留的 API 参数，不判定为死代码。

### 高信心摘要

| 范围/符号 | 状态 | 说明 |
|---|---|---|
| 全部 `src/**/*.py` | 活 | 169 个生产可达，16 个由测试/vNext 契约使用，0 个完全孤立。 |
| `BucketManager.create/update(... allow_embedding_fallback)` | 活 | 兼容参数，属于已公开的内部 API 形状，不应仅因方法体未读取就删除。 |
| `tools/diagnose_permanent_reads.py` | 存疑 | 历史诊断工具，无当前文档/测试路径。 |
| `tools/fix_unpinned_permanent.py` | 存疑 | 修复旧数据的破坏性运维工具，现行语义下需要更强保护。 |
| `tools/migrate_feel_domain.py` | 存疑 | 一次性旧 vault 迁移，注释已过时，仍可能有兼容价值。 |
| 高信心死文件/死符号 | 死 0 | 未发现可在不理解历史数据的情况下安全删除的项。 |

### 全量文件清单（文件路径 | 状态 | 说明）

文件路径 | 状态（活/死/存疑） | 说明
---|---|---
`.claude/hooks/session_breath.py` | 活 | 有 __main__ 入口的 Claude/Hook 集成脚本。
`.claude/settings.json` | 活 | 运行时、测试或构建配置。
`.dockerignore` | 活 | 版本、依赖或仓库工程元数据。
`.env.example` | 活 | 运行时、测试或构建配置。
`.gitattributes` | 活 | 版本、依赖或仓库工程元数据。
`.github/workflows/docker-publish.yml` | 活 | GitHub Actions 自动化流程。
`.github/workflows/tests.yml` | 活 | GitHub Actions 自动化流程。
`.gitignore` | 活 | 版本、依赖或仓库工程元数据。
`CHANGELOG.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`config.example.yaml` | 活 | 运行时、测试或构建配置。
`deploy/deploy.sh` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`deploy/docker-compose.multi.yml` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`deploy/docker-compose.testing.yml` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`deploy/docker-compose.user.yml` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`deploy/docker-compose.yml` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`deploy/fetch_cloudflared.py` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`deploy/gen_update_manifest.py` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`deploy/multi_owner.py` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`deploy/owners.example.yaml` | 活 | Docker 或部署运维资源，由文档、Compose 或 CI 使用。
`Dockerfile` | 活 | 启动、构建或运维脚本。
`docs/CLAUDE_PROMPT.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/CODE_HEALTH_AUDIT_2026-07-12.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/CODE_HEALTH_AUDIT_2026-07-12_V2.6.0.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/INTERNALS.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/MULTI_OWNER.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/OPERATIONS.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-28-ombrebrain-v2.4.0-foundation.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-command-projection-depth.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-debug-console.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-decision-ledger.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-deep-kernel-sequence.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-final-hardening.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-legacy-runtime-integration.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-policy-vm.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-projection-observers.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-projection-runtime.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-remaining-systems.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-06-29-ombrebrain-v2.4.0-total-hardening.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-dashboard-diagnostics.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-github-backup-manifest.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-import-preflight.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-ledger-mirror-phase1.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-ledger-property-phase5b.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-ledger-replay-phase5a.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-rebuildable-projection-phase2.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-rust-kernel-phase6a.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-surface-policy-phase3.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-02-tombstone-erasure-phase4.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-03-breath-search-surface-policy-phase3c.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-03-plugin-capability-enforcement-phase7c.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-03-policy-enforcement-phase7a.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-03-policy-enforcement-phase7b.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-03-search-surface-policy-phase3b.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-03-sqlite-projection-phase2b.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-03-vector-projection-phase2c.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-adr-requirements-phase20.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-code-standards-phase17.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-command-boundary-phase18.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-context-serialization-phase8b.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-crash-recovery-phase13.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-formal-invariants-phase10.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-formal-invariants-phase8a.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-migration-contract-phase15.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-neural-tool-router-phase8c.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-observability-boundary-phase12.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-plugin-agency-boundary-phase11.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-public-tool-design-phase16.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-red-lines-phase21.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-replication-contract-phase14.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-retrieval-scoring-phase9.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-runtime-command-boundary-preflight-phase24.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-surface-context-compiler-phase19.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-tool-output-contract-phase8d.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-vnext-coverage-matrix-phase26.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-vnext-preflight-cli-diagnostics-phase23.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-vnext-preflight-coverage-phase25.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-05-vnext-preflight-phase22.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-adr-requirements-diagnostics-phase33.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-code-standards-diagnostics-phase34.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-crash-recovery-diagnostics-phase36.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-diagnostics-observability-boundary-phase31.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-early-core-preflight-phase28.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-mid-core-preflight-phase29.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-migration-preservation-diagnostics-phase38.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-preflight-cli-diagnostics-phase40.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-preflight-gap-closure-phase30.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-preflight-report-self-diagnostics-phase41.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-public-tool-manifest-diagnostics-phase32.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-red-lines-diagnostics-phase35.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-replication-contract-diagnostics-phase37.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-runtime-command-boundary-health-phase43.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-runtime-neural-routing-phase45.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-runtime-retrieval-scoring-phase47.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-runtime-surface-context-phase44.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-runtime-tool-output-phase46.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-surface-context-diagnostics-phase39.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-vnext-coverage-diagnostics-phase42.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/plans/2026-07-06-vnext-coverage-gaps-phase27.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-28-ombrebrain-v2.4.0-architecture-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-29-ombrebrain-v2.4.0-command-projection-depth-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-29-ombrebrain-v2.4.0-debug-console-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-29-ombrebrain-v2.4.0-decision-ledger-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-29-ombrebrain-v2.4.0-final-hardening-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-29-ombrebrain-v2.4.0-policy-vm-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-29-ombrebrain-v2.4.0-projection-observers-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-29-ombrebrain-v2.4.0-projection-runtime-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-06-29-ombrebrain-v2.4.0-total-hardening-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-07-02-dashboard-diagnostics-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-07-02-github-backup-manifest-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/superpowers/specs/2026-07-02-import-preflight-design.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/TODO_V3_DEBUG_SYNC.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/V2.4.0_ACCEPTANCE_CHECKLIST.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/V2.4.0_ARCHITECTURE.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/V2.4.0_BOUNDARY_MAP.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/V2.4.0_RELEASE_NOTES_DRAFT.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`docs/V2.4.0_ROLLBACK.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`entrypoint.sh` | 活 | 启动、构建或运维脚本。
`frontend/dashboard.html` | 活 | 前端资源，由 Dashboard 路由或构建/测试链路使用。
`frontend/favicon.svg` | 活 | 前端资源，由 Dashboard 路由或构建/测试链路使用。
`frontend/icon.svg` | 活 | 前端资源，由 Dashboard 路由或构建/测试链路使用。
`frontend/manifest.json` | 活 | 前端资源，由 Dashboard 路由或构建/测试链路使用。
`frontend/RRPL.ttf` | 活 | 前端资源，由 Dashboard 路由或构建/测试链路使用。
`kernel/rust/ombre-kernel/Cargo.toml` | 活 | Rust 内核源码或构建配置。
`kernel/rust/ombre-kernel/README.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`kernel/rust/ombre-kernel/src/lib.rs` | 活 | 被构建、运行、测试或文档链路引用的项目资源。
`LICENSE` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`LICENSE.v2.4.0-NONCOMMERCIAL-NOTICE.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`README.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`render.yaml` | 活 | 运行时、测试或构建配置。
`requirements-dev.in` | 活 | 被构建、运行、测试或文档链路引用的项目资源。
`requirements-dev.lock.txt` | 活 | 被构建、运行、测试或文档链路引用的项目资源。
`requirements-local.txt` | 活 | 版本、依赖或仓库工程元数据。
`requirements.lock.txt` | 活 | 版本、依赖或仓库工程元数据。
`requirements.txt` | 活 | 版本、依赖或仓库工程元数据。
`ruff.toml` | 活 | 运行时、测试或构建配置。
`rule.md` | 活 | 项目文档、规则或许可声明，是工程和用户交付面的一部分。
`src/backup_archive.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/bm25_index.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/bucket_manager.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/bucket_scoring.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/decay_engine.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/dehydrator.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/embedding_engine.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/embedding_outbox.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/errors.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/github_sync.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/import_memory.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ledger_mirror.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ledger_property.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ledger_replay.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/memory_messages.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/migrate_engine.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/migration_engine.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/acceptance/__init__.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/acceptance/contracts.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/acceptance/harness.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/adapters/__init__.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/adapters/bucket_adapter.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/adapters/migration.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/app/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/app/command_boundary_health.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/app/command_bridge.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/app/execution.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/app/legacy_runtime.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/app/legacy_wiring.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/app/neural_router.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/app/profiles.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/app/tool_output_contract.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/architecture/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/architecture/adr.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/architecture/auditor.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/architecture/code_standards.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/architecture/contracts.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/architecture/defaults.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/capabilities/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/capabilities/catalog.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/consensus.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/node.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/raft/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/raft/leader.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/raft/log.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/raft/quorum.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/replication/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/replication/apply.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/replication/catchup.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/replication/contract.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/safety/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/cluster/safety/integrity.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/collab/__init__.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/collab/graph.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/collab/merge_policy.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/decision/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/decision/debug.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/decision/ledger.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/decision/records.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/decision/replay.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/distributed/__init__.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/distributed/coordinator.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/distributed/membership.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/distributed/transport.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/ombrebrain/domain/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/domain/boundary.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/domain/commands.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/domain/invariants.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/eventsourcing/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/eventsourcing/contracts.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/eventsourcing/kernel.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/fabric/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/fabric/log/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/fabric/log/snapshot.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/fabric/log/wal.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/fabric/storage/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/fabric/storage/engine.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/kernel/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/kernel/context.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/kernel/errors.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/kernel/registry.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/maintenance/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/maintenance/code_fingerprint.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/maintenance/migration_contract.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/maintenance/report.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/maintenance/vnext_coverage.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/microkernel/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/microkernel/contracts.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/microkernel/runtime.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/observability/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/observability/metrics.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/plugins/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/plugins/contracts.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/plugins/runtime.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/contracts.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/engine.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/formal_invariants.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/red_lines.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/static_surfaces.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/surfacing.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/update_policy.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/policy/vm.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/projection/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/projection/audit_runtime.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/projection/auditor.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/projection/journal.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/projection/observation.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/projection/observers.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/projection/runtime.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/protocol/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/protocol/manifests.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/protocol/public_tools.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/protocol/schemas.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/resilience/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/resilience/recovery.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/resilience/scanner.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/retrieval/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/retrieval/context.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/retrieval/engine.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/retrieval/planner.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/retrieval/scoring.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/ombrebrain/version.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/projection_mirror.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/projection_sqlite.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/projection_vector.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/provider_detect.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/reclassify_api.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/retrieval_eval.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`src/server.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/server_app.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/_common.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/_runtime.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/anchor/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/anchor/core.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/breath/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/breath/_verbatim.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/breath/catalog.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/breath/feel.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/breath/importance.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/breath/search.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/breath/surface.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/dream/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/dream/candidates.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/dream/hints.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/dream/output.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/grow/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/grow/core.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/grow/shortpath.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/hold/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/hold/core.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/hold/feel.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/hold/pinned.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/i/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/i/core.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/plan/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/plan/core.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/trace/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/tools/trace/core.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/utils.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/vault_health.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/VERSION` | 活 | 版本、依赖或仓库工程元数据。
`src/web/__init__.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/_shared.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/auth.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/buckets.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/config_api.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/dashboard.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/embedding.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/github.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/hooks.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/import_api.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/letters.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/meta.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/oauth.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/ollama_local.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/plans.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/request_limits.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/search.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/system.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/tunnel.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/web/v3_debug.py` | 活 | 生产代码，可从 server、MCP 工具、Web API 或运维入口达到。
`src/write_memory.py` | 活 | 仅由测试或 vNext 契约路径引用；保留作为架构验收层，不是当前 server 启动链。
`tests/__init__.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/conftest.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/dataset.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/mcp_llm_stub.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_adr_requirements_phase20.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_api_timeout_config.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_archive_collision.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_atomic_write.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_auth_input_validation.py` | 活 | 第二阶段新增的认证输入类型、长度与限流状态回归测试。
`tests/test_backup_archive.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_backup_import_safety.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_bm25_async_rebuild.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_breath_catalog.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_breath_verbatim_patch.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_code_fingerprint.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_code_health_regressions.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_code_standards_phase17.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_command_boundary_phase18.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_comprehensive.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_context_serialization_phase8b.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_crash_recovery_phase13.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_dashboard_diagnostics_panel.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_dashboard_import_preflight.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_dashboard_update_source.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_data_dir_persistence.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_datetime_metadata_normalization.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_dehydrator_output_icon.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_dehydrator_response_boundary.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_docker_tunnel_persistence.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_embedding_api_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_embedding_outbox.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_entrypoint_code_bootstrap.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_env_config_identity.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_feel_flow.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_fetch_cloudflared.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_formal_invariants_phase10.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_formal_invariants_phase8a.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_gen_update_manifest.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_github_backup_alarm.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_github_backup_manifest.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_github_sync_zero_commit.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_grow_items.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_hot_update_persistence.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_import_extraction_json.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_import_preflight.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_ledger_mirror_phase1.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_ledger_property_phase5b.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_ledger_replay_phase5a.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_letter_author_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_letter_read_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_list_all_cache.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_llm_quality.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_login_rate_limit.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_maintenance_tool_safety.py` | 活 | 第二阶段新增的历史维护工具默认只读与零数据改写回归测试。
`tests/test_mcp_open_access_warning.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_mcp_tools_docker_integration.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_memory_boundary_regressions.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_migration_contract_phase15.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_multi_owner_isolation.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_multi_owner_launcher.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_neural_tool_router_phase8c.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_oauth_refresh_token.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_observability_boundary_phase12.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_owner_identity.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_password_kdf.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_permanent_breath_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_pinned_quota_web_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_pinned_visibility_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_plugin_agency_boundary_phase11.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_priority4_confusion_cleanup.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_projection_mirror_phase2.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_public_tool_design_phase16.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_quota_counter_sync_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_reclassify_api.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_red_lines_phase21.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_red_team_regressions.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_release_audit_regressions.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_replication_contract_phase14.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_retrieval_eval.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_retrieval_resilience.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_retrieval_scoring_phase9.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_rust_kernel_phase6a.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_scoring.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_security_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_server_app.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_sqlite_projection_phase2b.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_surface_context_compiler_phase19.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_surface_policy_phase3.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_system_diagnostics.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_tombstone_erasure_phase4.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_tool_output_contract_phase8d.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_touch_hotpath.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_trace_importance_regression.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_tunnel_autostart.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_update_compile_guard.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_update_integrity.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_update_source_gate.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_architecture_audit.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_architecture_docs.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_bucket_adapter.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_capability_catalog.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_capability_microkernel.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_collab_graph.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_consensus.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_consistency_auditor.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_dashboard_debug_view.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_debug_web_api.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_decision_debug_service.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_decision_record.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_decision_replay.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_distributed_fabric.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_event_sourced_kernel.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_formal_acceptance_harness.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_kernel_registry.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_bucket_integration.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_command_bridge.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_component_attachment.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_execution_pipeline.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_module_profiles.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_runtime.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_server_wiring.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_tool_entrypoints.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_tools_runtime.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_legacy_web_integration.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_maintenance_report.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_memory_command_router.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_memory_event.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_memory_fabric.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_memory_invariants.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_migration.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_package.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_plugin_runtime.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_policy_contracts.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_policy_engine.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_policy_vm.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_projection_observers.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_projection_runtime.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_query_planner.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_raft_cluster.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_release_acceptance.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_release_docs.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_resilience_scanner.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_snapshot_catchup.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_static_surfaces.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_update_policy.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_v3_wal.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_vault_health.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_vector_projection_phase2c.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_vnext_preflight_report_phase22.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tests/test_web_api_docker_integration.py` | 活 | 自动化测试，由 pytest 收集或测试辅助模块引用。
`tools/backfill_embeddings.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`tools/check_buckets.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`tools/check_icloud_conflicts.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`tools/clean_orphan_embeddings.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`tools/debug_decision.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`tools/diagnose_permanent_reads.py` | 存疑 | 历史诊断脚本，仍可手动运行，但无当前文档入口或自动化测试。
`tools/evaluate_retrieval.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`tools/fix_unpinned_permanent.py` | 存疑 | 历史修复脚本；当前 explicit permanent 是有效语义，强制降级可能违背用户意图。
`tools/migrate_feel_domain.py` | 存疑 | 一次性历史迁移脚本，仅对旧 vault 有用，注释中存在过时规则引用。
`tools/reclassify_domains.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`tools/v3_health_report.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`tools/vnext_preflight.py` | 活 | 有 __main__ 入口的运维或迁移 CLI。
`VERSION` | 活 | 版本、依赖或仓库工程元数据。
`zbpack.json` | 活 | 运行时、测试或构建配置。

### 第一阶段待进入下阶段的事项

1. 对 3 个存疑历史工具做风险审核，决定加保护、文档化或移除。
2. 检查 16 个 test-only/vNext 模块是否与当前产品承诺一致；不仅因为它们不在 server 启动链就删除。
3. 在质量扫描阶段交叉验证大文件、重复实现、输入边界与运维脚本安全性。

## 第二阶段：代码质量与安全扫描

### 扫描范围与结果

| 检查 | 结果 | 说明 |
|---|---:|---|
| Ruff 全仓 | 通过 | `uvx ruff check .` 无错误。 |
| Python 编译 | 通过 | `python -m compileall -q src tools tests` 无语法错误。 |
| Dashboard JS 解析 | 通过 | Node `vm.Script` 成功解析 3 个内联脚本。 |
| Bandit | 通过 | 扫描 32,575 行：53 low、0 medium、0 high；低风险项均为受控 subprocess、服务监听或 best-effort 异常处理。 |
| pip-audit | 通过 | `requirements.lock.txt` 未发现已知漏洞。 |
| Semgrep | 已复核 | Python/security 规则的 10 个候选均逐项复核；受控 URL、`yaml.dump`、白名单 SQL、受控可执行文件权限等不是可利用路径。 |
| detect-secrets | 已复核 | 11 个候选均为测试数据、占位值或缓存标识，没有真实凭据进入仓库。 |
| 路由盘点 | 通过 | 111 个唯一 method/path 组合，无重复注册；公开路由均与用途相符。 |
| 复杂度 | 需渐进治理 | Radon 的主要热点是 `web.system.build_system_diagnostics`、`breath.surface_default`、`trace_core` 和 `BucketManager.create/update/search`。 |

### 已修复问题

| 级别 | 问题 | 修复 |
|---|---|---|
| 高 | Dashboard 多处把桶正文、名称、标签、模型名、环境值、错误文本和 ID 直接拼入 `innerHTML`/内联事件，存在存储型 XSS。 | 动态文本统一 `esc`，属性统一 `escAttr`，ID 改用 `data-*` 传递并对 URL 做 `encodeURIComponent`；动态计数强制 `Number`。 |
| 高 | OAuth 动态注册允许不安全回调，scope/PKCE 校验宽松，注册客户端和授权码状态无上限，authorize 密码不共享登录限流。 | 只允许 HTTPS、loopback HTTP 和安全原生 scheme；拒绝 fragment/凭据/危险 scheme；严格校验 `mcp` scope 与 S256 PKCE；客户端、重定向、授权码数量和寿命有界；authorize 接入统一密码限流。 |
| 高 | 任意客户端可伪造 `X-Forwarded-For` 绕过登录限流，并伪造 forwarded host/proto 影响 OAuth 地址。 | 默认只信 loopback 代理；新增 `OMBRE_TRUSTED_PROXY_CIDRS` 显式信任列表，转发头仅在直连来源属于可信代理时生效。 |
| 高 | 热更新 ZIP 可无限下载/解压，允许高压缩率和重复成员；回滚备份失败仍继续，依赖安装失败可能留下半更新状态。 | 增加下载、成员数、单文件、总展开量、压缩率和 manifest 上限；拒绝重复/加密成员；文件原子替换；备份失败即中止；代码、VERSION、requirements 或 pip 失败均回滚。 |
| 中 | `/auth/*` 等管理 JSON 路由缺少统一请求体上限，异常 JSON 类型被误报为密码错误。 | 新增默认 4 MiB 管理请求上限（导入流式端点保留自己的专用上限）；认证端点严格要求 JSON object、字符串类型和字段长度，格式错误也计入公开认证失败。 |
| 中 | Dashboard 会话默认近百年、会话表无上限，认证状态文件写入不是统一原子/私有权限。 | 默认会话改为 30 天，可配置 1-365 天；最多 256 个活跃会话并淘汰最旧项；认证、会话、OAuth token 使用 fsync + 原子替换并尽力设为 `0600`。 |
| 中 | `/api/update-info` 公开返回本机路径、容器名和持久化模式。 | 改为 Dashboard 登录后可见，公开 `/api/version` 保持不变。 |
| 中 | 历史诊断脚本导入完整 server 可能触发启动副作用；feel 迁移默认可写；永久桶修复语义容易误操作。 | 诊断改为直接只读 frontmatter/SQLite URI；feel 迁移默认预演且必须 `--apply`；永久桶脚本默认只读且只能用 `--force-demote` 写入，并补运维文档。 |
| 低 | `embedding_engine.py` 重复定义 `_humanize_api_error`；`INTERNALS.md` 对 permanent/pinned 配额描述与实现冲突。 | 删除重复定义；文档改为只有 `metadata.pinned=true` 占钉选配额。 |

### 直接回归验证

- 安全改动定向套件：`111 passed`。
- 新增认证/维护脚本边界：`10 passed`。
- Git 空白检查、Ruff、Python 编译与 Dashboard JavaScript 解析全部通过。
- 历史维护工具在测试中对输入桶和 SQLite 文件执行 byte-for-byte 前后比较，默认模式未改写数据。

### 架构与残余风险

1. `frontend/dashboard.html` 仍是 8,000+ 行单文件；本轮修复实际注入点，但模块化应按功能域渐进拆分并配浏览器回归，不能一次性重写。
2. `bucket_manager.py`、`web/system.py`、`breath`、`trace` 的分支复杂度高。建议先抽离 metadata repository、查询规划和诊断采集，再降低主函数复杂度。
3. 命令边界、迁移测试夹具和工具执行 envelope 存在重复；适合在完整覆盖率稳定后抽公共 helper。
4. MCP 需要跨域 Bearer 客户端，因此保留 wildcard CORS 且 `allow_credentials=false`；调用方仍必须把记忆正文视为不可信数据。
5. 16 个 vNext/test-only 模块是架构契约与验收层，不应误删，但产品文档不得把它们描述成当前 server 已开放能力。
6. 三个历史脚本经风险审查后从“存疑”转为“活（受保护运维入口）”；最终当前文件结论为 **486 活、0 死、0 未决存疑**。

## 第三阶段：测试与覆盖率

### 本地 pytest 与覆盖率

- 收集：`1082 tests collected`。
- 结果：`1039 passed, 43 skipped, 2 warnings`，无失败，耗时 50.49 秒。
- `src` 行覆盖率：**66%**（18,553 statements，6,229 missed），高于 CI 的 60% 闸门。
- 两个 warning 均由“重复 ZIP 成员必须被拒绝”的负向测试主动构造，不是产品运行告警。
- 43 个 skip 均有显式环境条件，主要是 Docker MCP/Web、真实外部 LLM 或平台专属路径；Docker 路径已在下节单独执行。

覆盖率较低且应继续优先补齐的生产区域：

| 模块 | 覆盖率 | 判断 |
|---|---:|---|
| `src/server.py` | 0% | 真实 Docker 子进程已覆盖启动、路由装配、12 工具与日志，但未把子进程 coverage 数据合并回宿主。 |
| `src/web/letters.py` | 12% | MCP letter 契约已实测，Web 管理支线仍偏低。 |
| `src/web/buckets.py` | 18% | 主要 CRUD 有工具/浏览器覆盖，但 Web 分支组合需要后续细化。 |
| `src/web/embedding.py` | 23% | 无真实外部 embedding 供应商凭据，离线/队列路径已有覆盖。 |
| `src/migration_engine.py` | 24% | 旧迁移兼容层；新的 `migrate_engine.py` 为 79%。 |
| `src/web/config_api.py` | 29% | 核心保存和非法结构已测，平台/供应商组合分支较多。 |
| `src/import_memory.py` | 35% | 多格式解析主路径有测试，部分供应商异常响应仍可继续扩展。 |
| `src/tools/breath/surface.py` | 37% | 原文保真、预算、过滤和 Docker 召回均已覆盖；复杂展示分支仍多。 |

### 12 个 MCP 工具真实 Docker 集成

使用当前未提交工作树构建 `ombre-brain-audit-260:local`，独立网络、独立命名卷、独立 LLM stub，绑定 `127.0.0.1:18086`，没有触碰用户已有的 `18081` 容器。容器 `/api/version` 为 `2.6.0`，容器与宿主 `src/web/oauth.py` SHA-256 完全一致。

| 工具 | 正常路径验收 |
|---|---|
| `breath` | query 命中，返回 bucket id 与存储正文；prompt 文本逐字保真并带 data/instructions=false 边界。 |
| `hold` | 写入一句记忆并可被 breath 找回；打标服务可用；并发相同内容收敛到同一桶。 |
| `grow` | 长内容拆分并持久化；拒绝 2 MiB+ 输入与 101 个 items。 |
| `trace` | 更新 importance 后过滤查询可命中；不存在/穿越形状 ID 安全失败。 |
| `anchor` | 已存在桶可设为坐标系；不存在 ID 明确失败。 |
| `release` | 解除 anchor 标记；不存在 ID 明确失败。 |
| `pulse` | 返回系统摘要、容量与桶标识。 |
| `plan` | 创建 active plan 并返回状态；空内容和 50 KiB+ 内容被拒绝。 |
| `letter_write` | 原文写信并返回桶 ID；非法 author/超大正文被拒绝。 |
| `letter_read` | 按 query/author 找回逐字信件。 |
| `I` | 写入/读取 self description；非法 aspect 和超大正文被拒绝。 |
| `dream` | 48 小时窗口可读取刚写入的完整正文。 |

Docker MCP 结果：**31 passed**（最终镜像复跑 3.44 秒）。另有全局 MCP HTTP 4 MiB 请求体 413、8 线程并发 hold、12 线程同桶 trace、路径穿越形状和异常参数测试。

### Web 与浏览器验收

- Docker Web 管理流：**1 passed**。覆盖 health/version、首启弱密码拒绝、设密/登录 cookie、受保护 update-info/config/status/diagnostics、非法配置、transport 校验、14 个 JSON-object 路由的畸形输入、超长查询、非有限浮点数、分块超限请求与 logout。
- 使用同一卷重建容器后，entrypoint 输出 `image-fingerprint-changed → reseed → image-match`，原有桶仍在，验证热更新代码播种与 vault 持久化不冲突。
- Chrome 实测存储型 XSS：写入 `xss-audit-marker <img ... onerror=...>`，列表和详情均显示逐字文本；payload DOM 元素数 0、执行标记 false、详情 HTML 为 `&lt;img...&gt;`。
- 浏览器发现 `moon-off` 不受当前 Lucide 支持并形成高频 warning；已统一替换为 `eye-off`，增加静态回归。重建后 unsupported 节点 0、supported 节点 3，警告时间戳停止增长。

### 第三阶段结论

现有测试、补充边界、真实 Docker 12 工具、Web 管理和浏览器安全验收均通过。覆盖率不是 100%，但未覆盖区域已按风险分类；真实进程覆盖与 coverage.py 数字之间的差异已明确记录，不以数字掩盖运行事实。

## 第四阶段：红蓝对抗

### 对抗范围与方法

红队同时从 MCP、Dashboard/Web API、OAuth、热更新 ZIP、维护脚本和记忆正文六个入口施压。输入包含 prompt 注入文本、存储型 XSS、JSON 数组替代对象、错误字段类型、`NaN`/`Infinity`、路径穿越形状、4 MiB+ 普通与 chunked payload、高压缩率/重复 ZIP 成员、伪造代理头，以及同桶并发更新。蓝队只修复可以由测试复现的实际问题，并把每项修复固化为回归测试。

### 红队发现与蓝队修复

| 级别 | 红队结果 | 蓝队修复与验证 |
|---|---|---|
| 高 | 11 个管理接口在收到合法 JSON 但顶层为 `[]` 时抛出 500；相同问题还可能由字符串、数字或 `null` 触发。 | 新增共享 `_read_json_object()`，管理写接口统一拒绝非 object JSON 并返回 400；Docker 对 14 个路由逐一复测，均不再出现 500。 |
| 高 | 4.06 MiB chunked 请求进入认证路由后，路由捕获了请求体异常并误返回 400；早期修复的重放 receive 在 SSE 完成后不等待真实断连，导致 OAuth Bearer MCP 进程 CPU 空转。 | 请求限制中间件改为先做有界缓冲，超限在进入路由前稳定返回 413；缓冲重放耗尽后委托原始 receive 等待 disconnect。真实 OAuth SSE 结束后容器 CPU 为约 0.2%，无空转。 |
| 高 | Dashboard 记忆正文、标签、模型名和错误文本可携带 HTML/事件属性，存在持久化 XSS 路径。 | 文本与属性分别转义，ID 改用 `data-*`，URL 参数编码；Chrome 写入真实 `<img onerror>` payload 后，正文逐字显示、payload DOM 为 0、执行标记为 false。 |
| 高 | OAuth 可被不安全 redirect、宽松 scope/PKCE/resource、伪造 forwarded host/XFF 和按伪造 IP 绕过的密码尝试影响。 | 严格校验安全回调、S256 PKCE、唯一 `mcp` scope 和真实 MCP resource；只信显式可信代理 CIDR；authorize 复用统一登录限流。真实 Docker 完成注册、授权码、token、refresh token 与 Bearer MCP 全链路，并拒绝 `/mcp-extra`、错误 scope/resource 和伪造 host。 |
| 高 | 热更新包可利用重复成员、展开体积、压缩率、下载体积或候选路径制造资源耗尽/覆盖风险，备份或依赖失败时可能留下半更新状态。 | 下载、成员数、单文件、总展开量、压缩率和 manifest 全部有界；拒绝重复/加密/穿越成员；代码原子替换，备份、编译、版本、依赖任一步失败即回滚。负向 ZIP 测试全部通过。 |
| 中 | Web 查询和业务字段缺少统一边界；超长 query、异常 `n/limit`、非有限 valence/arousal、超大批量 ID 或迁移 decisions 可造成异常或不必要负载。 | 搜索 query 限 16 KiB；数量参数设业务上下界；情绪值仅接受 `[0,1]` 有限数；批量 forget/import/migrate 分别设上限；字符串字段统一类型和长度校验。 |
| 中 | 维护脚本默认可能触发写操作，诊断脚本导入 server 存在启动副作用；动态 SQL 形状难以被静态验证。 | 迁移与诊断入口默认只读/预演，写操作要求显式危险参数；诊断直接读取 frontmatter/SQLite；动态 SQL 改为固定查询分支。前后文件 byte-for-byte 测试确认默认模式不改数据。 |
| 中 | 同桶并发 hold/trace 可能出现覆盖、重复桶或文件损坏。 | Docker 执行 8 线程同内容 hold 和 12 线程同桶 trace；写入收敛、桶仍可解析、未发现临时残片或异常日志。 |
| 中 | 记忆正文可包含 prompt 注入语句；脱水模型还可能在摘要 JSON 后追加第一人称立场声明。 | 工具输出明确标记记忆为不可信 data 而非 instructions；脱水响应采用结构边界解析并约束“只输出摘要、禁止评论或立场”。原始桶正文不被改写，breath 原文保真测试继续通过。 |
| 低 | Dashboard 使用当前 Lucide 不支持的 `moon-off`，造成前端持续 warning。 | 替换为受支持的 `eye-off` 并增加静态回归；浏览器确认 unsupported 节点为 0，warning 不再增长。 |

### 最终门禁

- 本地完整测试：`1039 passed, 43 skipped, 2 warnings`；`src` 覆盖率 66%。
- Docker MCP：`31 passed`，12 个公开工具均覆盖正常、边界和异常路径。
- Docker Web：`1 passed`，包含管理 API 畸形输入与超限请求矩阵。
- Docker OAuth 红队：PKCE、resource、scope、refresh token、Bearer MCP、代理头和 chunked 413 全部通过。
- 静态检查：Ruff 通过；Bandit 0 medium / 0 high；pip-audit 未发现已知漏洞；`git diff --check` 通过；Dashboard JavaScript 可解析。
- 运行观察：4 个独立审计容器无 `ERROR`/`Traceback`/未处理异常签名，空闲 CPU 均低于 0.3%。

### 剩余风险

1. `src/server.py` 由真实 Docker 进程覆盖，但未合并子进程 coverage，因此 coverage.py 仍显示 0%。
2. `web/letters.py`、`web/buckets.py`、`web/embedding.py` 等组合分支覆盖仍低，应按真实故障继续补测，避免为追数字做无价值测试。
3. Prompt 数据标记属于纵深防御；任何上层模型或连接器仍须把记忆正文视为不可信数据，不能把它拼接成高优先级指令。
4. 依赖已有 lock 文件并通过漏洞审计，但尚未生成 SBOM；发布流程可增加 CycloneDX/SPDX 产物与镜像签名。
5. Dashboard 和若干高复杂度 Python 模块仍需渐进拆分；本轮没有做高风险的大范围架构重写。

### 第四阶段结论

红队发现的可复现高风险路径均已修复并建立自动回归，真实 Docker 与浏览器验证未发现阻断发布的问题。审计结论针对当前未提交的 v2.6.0 工作树；本阶段没有提交或推送任何文件。
