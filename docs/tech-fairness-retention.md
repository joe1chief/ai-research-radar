# Tech 调度与历史 payload 只读估算

## 证据和范围

当前基线 main `1c09e0c` 已有单源、组、SQL、HTTP 和重试预算。本改动不增加预算、
不修改 cron、并发锁、发送代码或生产配置，不处理 papers 分页入库。

[Collect 37990150011](https://github.com/joe1chief/ai-research-radar/actions/runs/37990150011)
只访问 13 个 tech 源后耗尽组预算。后段源没有本轮新鲜失败证据，固定顺序会重复把它们留到预算之后。
现在 tech 优先处理已到期、未暂停源，按持久化 last_attempt_at 从旧到新排列；从未尝试优先，
相同时间保留配置顺序。尝试时间在外部请求前提交，使下一轮能优先处理其他源。
原 collect_source 仍决定 force、Retry-After、next_due_at 和 disabled 行为。
排序查询也受组 deadline 约束，失败时 rollback、输出脱敏失败统计，停止该组，不回退固定顺序。
预算是合作式检查，不承诺硬中断所有 CPU 或底层阻塞。

## 四个入口

2026-10-10 保存的公开列表响应及离线解析证明以下迁移入口可用：

| 源 | 官方入口 | 文章限制 |
| --- | --- | --- |
| Runway | https://runway.com/news | news/research 或 research 单篇，排除 publications |
| Hebbia | https://www.hebbia.com/blog | blog 单篇，排除已见分类入口 |
| Sierra | https://sierra.ai/blog | blog 单篇，排除分类和分页 |
| Factory | https://factory.com/news | news 单篇，排除分类和分页 |

测试 fixture 只保留每个列表的三个公开文章链接和标题，不包含原始页面或凭据。
这些测试证明受限列表/详情解析，不证明生产源已经恢复。
LangChain 新页面的文章锚文本为空，现有通用解析器不能直接使用，保持旧配置；
BAIR 样本 RSS 可解析，保持配置；Allen 403、Dwarkesh TLS 仍未解决，不停用或绕过访问控制。

## 90 天候选政策

估算保留所有 items 和版本行，包括 ID、hash、标题、时间、metadata。以下版本保留完整 payload：

- 当前 content_hash 对应的版本；
- 每个 item 最近一个非当前历史版本；
- 任何 event_items 或 evidence 引用的版本；
- fetched_at 距估算时间不超过 90 天的版本（恰好 90 天也保留）。

其余超过 90 天的版本仅列为未来清空 normalized_text、abstract_text、embedding、
embedding_vector 的候选。本改动没有 apply、删除、压缩或 VACUUM 入口。
raw 的 14 天政策及 events/revisions/delivery/webhook 均不改变。

PostgreSQL 必须可读取 public.evidence、event_items 和版本字段，vector 必须为 1024 维。
检测到未知版本外键、缺失当前版本或查询权限错误时失败关闭，候选数量为 unavailable，
不会将缺失证据当成没有引用。无声明外键的额外应用引用无法自动发现，正式清空方案前仍需人工核对。
SQLite 仅供隔离测试，可没有 evidence，并明确报告该表不存在。

输出包括年龄分桶、版本数分布、可重叠的保留原因，以及所有候选的列级逻辑字节。
文本按 UTF-8 字节、两份嵌入各按 float32 元素的四字节计算；不含 headers、索引、TOAST 或死元组。
physical_reclaimable_bytes 永远为 null，不能据此承诺数据库文件或托管计量减少。

## 受控查看与验证

`radar maintenance --retention-estimate` 只输出聚合，不输出候选 ID、路径或正文。
`radar maintenance --retention-preview --retention-limit 200` 显式输出私有候选版本 ID，
limit 为 1–1000，只限制清单长度，不截断聚合。应在受控终端使用，避免公开 Actions 日志。
Maintenance 的手动 boolean 输入 retention_estimate 默认 false，仅可启用聚合；
自然 schedule 仍运行原只读诊断，本 PR 不触发该工作流。

隔离回归覆盖多轮轮转、正常及 force 冷却、停用、非 tech 顺序、排序超时/错误、
文章导航与站外过滤、年龄边界、版本与两类引用保护，以及 CLI 没有 SQL/存储写入。
CI 使用固定版本 pgvector PostgreSQL 镜像，验证实际 vector/ARRAY 字节、只读事务、
缺失 evidence、未知外键及权限不足；CI 缺失 vector 时失败，不允许跳过。
生产版本年龄、引用分布和可回收物理空间未验证，不能从 versions-items 推算候选容量。

获准合并后观察自然 collect 的各源 last_attempt_at/last_success_at 和预算统计。
生产聚合估算需单独获准执行，不以此作为交付本 PR 的前置条件。
回滚本 PR 无 schema 迁移：恢复这四个入口和配置顺序，移除可选估算功能即可；
不要整体回滚此前 Maintenance 只读安全修复。
