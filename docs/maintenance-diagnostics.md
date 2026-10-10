# Maintenance 诊断与待决事项（2026-10-10）

本次范围为诊断、安全预览和草稿 PR；不合并、部署或重跑生产工作流。

## 已确认事实

[Maintenance 38001044557](https://github.com/joe1chief/ai-research-radar/actions/runs/38001044557/job/114058895584)
在 2026-10-09 22:47:44 UTC 输出统计后主动 exit 1，三个条件同时触发：

- 数据库 371,494,035 bytes，约 354.3 MiB，超过应用 350 MiB 预警线约 4.28 MiB。
  日志还有 33,846 items、36,869 versions、4,606 events。没有证据证明已到托管硬限额。
- 11 个源连续失败至少 3 次：bair-blog、runway-blog、allen-ai-news、dwarkesh-podcast、
  sierra-news、langchain-blog、perplexity-blog、hebbia-blog、factory-blog、
  evolutionary-scale-blog、arxiv-oai-reconcile。
- 677 条过期 raw 路径记录、0 条清理。maintenance.yml 未传上传开关，Settings 默认为 false，
  `_raw_store` 因此返回 None：这次没有尝试删除对象。677 不是桶内已确认对象数。

旧命令也会不受 RADAR_DRY_RUN 保护地删除 60 天前 ledger；若启用 raw 开关，还会删除对象并
清空路径。本 PR 将这些默认副作用改成只读统计，所有健康告警保留。不会自动启用存储或清理。

## 持续失败源证据

[Collect and Alert 37990150011](https://github.com/joe1chief/ai-research-radar/actions/runs/37990150011)
采用 main `1876b7e…`，21:03:28 UTC perplexity-blog 返回 403，21:04:33 UTC
 evolutionary-scale-blog 返回 404。应分别核实允许访问的官方订阅/API 与官方最新地址，
不要靠盲重试、绕过访问控制或未经核实替换 URL 解决。

该运行的 tech 组最终 failed=4、degraded=2、budget_exhausted=1，只遍历 13 个源；capital
和 standards 则正常结束。因此绿色工作流不表示所有源健康；其他旧失败源没有在本轮被访问，
不能声称都再次失败或已经恢复。建议对照新诊断的 last_attempt_at/last_success_at、实际
HTTP 状态和配置逐源复核；确认最新地址和解析契约后，再单独提交源修复。本 PR 不禁用源或告警。

[Paper Sweep 37924234640](https://github.com/joe1chief/ai-research-radar/actions/runs/37924234640)
11:35:48 UTC arxiv-oai-reconcile 获取 20 页 HTTP 200 后进入 persist，items=19,845；
11:36:03 UTC 抛 CollectionBudgetExceeded，11:36:06 UTC 单源结束，耗时 122.737 秒。
这支持“大批次入库耗尽预算”，不支持 arXiv 服务不可用。papers 最终 failed=4，
另有 ACL/PMLR 预算失败。建议后续评估持久分页/更小可提交批次和 cursor 原子性，
用实际吞吐量决定预算；本 PR 不抬预算或隐藏失败。

## 容量判断与证据缺口

当前只有 pg_database_size 总量，不能归因于 raw、embedding、正文、索引、死元组或某张表。
新增只读关系诊断提供最大的 20 张用户表/物化视图、table/TOAST 与索引大小、live/dead
估计数及 autovacuum 时间；尚未在生产运行，真实容量构成仍未知。总数据库容量可能包括
多个 schema，关系大小与估算行数也不能直接视为可回收字节。需核对托管套餐的真实额度和
计量口径，再制定容量计划；本次没有读取新凭据、修改权限、清理数据库或购买扩容。

raw 在独立对象存储，删除对象不保证缩小数据库。即使删掉数据库行，PostgreSQL 也通常
先复用空闲空间；本次不执行 VACUUM FULL 或数据保留政策变更。

## 验证和回滚

回归涵盖 live/non-dry-run 设置仍无 SQL 写入和对象删除、issuer/schema 不初始化、ledger/
raw 指针不改变、近期引用保护、孤儿区分、缺失/失败存储列表、全部健康原因和错误脱敏。
隔离 PostgreSQL 验证只读事务以 SQLSTATE 25006 拒绝 INSERT、连接随后恢复，以及
实际表/索引容量查询。本次没有运行生产预览或清理，也未改变 cron、并发锁或通知。

合并部署后可以观察自然 Maintenance 的新 JSON；如需要具体私有清单，应在受控本地
环境显式执行 runbook 中的只读预览，不把路径输出到公开日志。
回滚代码提交无需数据库迁移，但旧命令会恢复 ledger 自动删除以及 raw 开关开启时的删除
能力；回滚前须确认开关状态和下一自然运行影响。

## 需要用户决定

1. 是否合并并部署本草稿，让自然 Maintenance 使用只读诊断。
2. 审阅新容量构成后确定保留政策或扩容方案；350 MiB 预警保持原值。
3. 取得新鲜 raw 清单、对象字节影响、备份恢复条件后，是否另行批准精确删除计划。
   当前没有生产对象清单、字节估算或已验证备份，不具备删除执行条件。
4. 是否另开源 URL/访问契约修复和 papers 分页吞吐改造；这些尚未由本 PR 解决。
