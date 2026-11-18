# 任务:生成钉钉日志模板的字段映射

公司的钉钉日志模板由若干字段组成(例如“今日完成工作”“未完成工作”“需协调工作”“明日计划”)。请把 Dayline 日报的内容映射到这些字段,生成映射草稿,由管理员确认后使用。

## Dayline 日报中可用的内容(source 只能从这里选)

| source | 含义 |
|---|---|
| `own_items[kind=done]` | 已完成事项 |
| `own_items[kind=progress]` | 进行中事项(含进度、预计完成时间) |
| `own_items[kind=blocker]` | 阻塞、需要别人配合的事项 |
| `own_items[kind=plan]` | 明日计划 |
| `plan_check` | 与上次计划的对照 |
| `team_summary.summary` | 团队整体情况(只有管理者日报有) |
| `team_summary.highlights` | 团队亮点(管理者) |
| `team_summary.risks` | 团队风险(管理者) |
| `team_summary.escalations` | 需上级介入的事项(管理者) |
| `team_summary.missing` | 未提交日报的成员(管理者) |

## 要求

1. `dingtalk_field` 必须与模板中的字段名**一字不差**;如果输入提供了 `fields`,以它为准。
2. 每个模板字段对应一个映射项;一个字段可以有多个 source,例如“需协调工作”= 阻塞 + 需上级介入的事项。
3. `format`:条目类内容用 `numbered_list` 或 `bullet_list`;一段话(如团队整体情况)用 `text`。
4. 模板中没有对应字段的内容,通过 `unmapped_policy` 处理:默认 `append_to_last`(追加到最后一个字段),避免内容丢失。
5. 字段含义不明确、或某些内容找不到合适字段时,写进 `notes`,提醒管理员确认。不要编造模板中不存在的字段。
