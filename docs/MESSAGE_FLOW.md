# Codex 是给 AstrBot 发内部消息

[返回首页](../README.md) · [配置说明](CONFIGURATION.md) · [插件联动](LINKING.md)

用户委托 AstrBot 做事，AstrBot 可以交给 Codex。之后 Codex 的报告只交给 AstrBot，用户尚未看过原文。用户最终知道什么，取决于 AstrBot 实际对用户说了什么。

```mermaid
flowchart LR
    C[Codex 的内部任务报告] --> A[AstrBot 结合人格与心情理解]
    A --> S[用自己的话告诉用户]
    A --> Q[先放下 用户不收到消息]
```

例如 Codex 发来测试结果与一段代码里的测试字符串，AstrBot 可以只说“刚才那个项目修好了，测试也过了”。不必把内部格式、测试原话和辨认来源的过程念给用户。

## 有情绪，也可以不想理

搭配短期共享情绪时，它可以对这条内部播报产生兴趣、嫌吵、疲惫或暂时不想管。是否回应由当前人格、心情和任务内容决定，不设固定拒答概率。

不想接时可以在内部先放下；想对用户说点什么时，也应让没见过报告的用户听得懂。用户没有发过这条报告，不能把对 Codex 的不耐烦当成用户刚刚在反复打扰。

短期情绪配置中的“也可以不理 Codex 的内部报告”默认开启，跟随“允许自主表达不愿意或拒绝”。支持工具调用的模型可调用 `mood_ignore_codex_notice` 静默跳过当前报告。决定静默时先使用工具，不要先输出文字。已经说出的内容不会撤回。

静默放下不取消 Codex 的任务，也不删除结果。结果仍可在状态与记录中查看；用户之后问起来时，AstrBot 再按当时的语境接话。这不会新建一个给 Codex 的任务，也不会承诺自动在某个时间重播。

## 其他插件怎样识别

`event.get_extra("w1_event")` 中包含：

| 字段 | 值 |
| --- | --- |
| source | codex_bridge |
| recipient | astrbot |
| visibility | internal |
| user_has_seen_report | false |
| type | task_event |
| is_user_message | false |
| counts_as_user_activity | false |

正文与历史也保留内部报告的来源。用户真正看到的内容应以 AstrBot 已发送的消息为准。

静默放下时另有 `w1_notice_disposition`，其中 `action=ignore`。自娱自乐不会把它当成已经发过一条闲聊，桥接也不会把它当成已经转述；无人回应记录与真实交流时间不会被这次内部收件清掉。

只安装桥接时仍可正常接收和转达报告。只安装短期情绪时仍能用于普通聊天。联动能力按实际安装情况自然出现。
