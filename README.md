<div align="center">

<h1>AstrBot 短期共享情绪</h1>
<p>让 AI 有玩心、小脾气，也有慢慢心软的情绪余韵。</p>

<p>
  <a href="https://github.com/1972124398/astrbot_plugin_short_term_mood/releases/latest"><img src="https://img.shields.io/github/v/release/1972124398/astrbot_plugin_short_term_mood?style=flat-square&color=2563eb" alt="最新版本"></a>
  <a href="https://github.com/1972124398/astrbot_plugin_short_term_mood/actions/workflows/test.yml"><img src="https://github.com/1972124398/astrbot_plugin_short_term_mood/actions/workflows/test.yml/badge.svg?branch=main" alt="测试状态"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-0BSD-64748b?style=flat-square" alt="0BSD 许可证"></a>
</p>

<p>
  <a href="https://github.com/1972124398/astrbot_plugin_short_term_mood/releases/latest/download/astrbot_plugin_short_term_mood.zip"><strong>下载 ZIP</strong></a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="docs/CONFIGURATION.md">配置文档</a> ·
  <a href="https://github.com/1972124398/astrbot_plugin_short_term_mood/issues/new/choose">问题反馈</a>
</p>

</div>

## 聊天体验

- 开心时更热情，好奇时想接着问，疲惫时可能少说几句。
- 熟人间可以接梗、轻怼或嘴硬，也可以坦率承认高兴。
- 生气、委屈与不愿意可以直接表达，得到关心后逐步心软。
- 私聊、群聊和不同人格共享一份心境，各人格保留自己的措辞。

支持十种情绪混合，以及最近五次抽象心境的连续变化。

| 互动 | 实际生成片段 |
| --- | --- |
| 善意叫它“小笨蛋” | “反正你也没聪明到哪去。” |
| 被认真道歉 | “刚才那几句确实挺扎人的。” |
| 后续被认真倾听 | “你能这么讲我气消大半。” |

片段来自模拟人格的连续聊天，实际措辞随人设、模型和对话变化。

## 快速开始

需要 **AstrBot `>=4.29.4,<5`、Python 3.12+**，并已配置可用的聊天模型。

1. 在 AstrBot 插件管理中使用下方仓库地址安装，或下载 ZIP 后从文件安装。
2. 打开 **插件 → 短期共享情绪 → 配置**。
3. 保留默认配置开始聊天，也可选择独立的情绪检测模型。
4. 使用 `/mood` 查看当前共享心境。

```text
https://github.com/1972124398/astrbot_plugin_short_term_mood
```

安装与更新支持自动加载或单独重载。首次遇到一份主提示词时，会在后台生成人格化的表达提示。

## 常用配置

| 配置 | 默认体验 |
| --- | --- |
| 聊天气质 | 随性自然，可选活泼外露或含蓄细腻 |
| 玩心与表达 | 玩心 `0.6`，表达强度 `1.0`，情绪惯性 `0.35` |
| 持续聊天 | 保留心境，当前消息可立即引起表达上的反应 |
| 空闲恢复 | 停聊 `300` 秒后开始恢复，每 `300` 秒强度减半 |
| 恢复期限 | 累计自然恢复 `1200` 秒后归于平稳 |

在配置中填写表达偏好，例如“亲近时多逗趣，生气时直说，少写动作描写”，可以进一步调整口吻。

<details>
<summary><strong>支持哪些情绪？后台检测如何影响聊天？</strong></summary>

支持开心、亲近、失落、生气、不安、疲惫、好奇、害羞、得意、委屈。多种情绪可同时存在，主要取最强的三种影响表达。

前台直接使用已有心境，并允许即时回应当前刺激。后台检测完成后，变化延续到后续对话。

普通对话会额外产生一次后台模型调用。使用同一服务时共享并发和额度，可选择独立 Provider。默认会评估图片，检测模型需支持图片，也可以关闭图片评估。

[全部配置与升级说明](docs/CONFIGURATION.md)

</details>

## 命令

| 命令 | 权限 | 用途 |
| --- | --- | --- |
| `/mood` | 所有人 | 查看共享心境、余韵和互动倾向 |
| `/mood_reset` | 管理员 | 恢复平稳 |
| `/mood_set 生气 0.8` | 管理员 | 设置情绪以调试，也支持英文名称 |
| `/mood_refresh` | 管理员 | 清除人格表达缓存，下次后台重新生成 |

## 常见问题

<details>
<summary>情绪表达太淡、太明显，或第一次聊天变化不大</summary>

首次检测和人格表达生成需要后台完成。可通过 `/mood` 查看心境，再调整表达鲜明程度、聊天气质和表达偏好。

</details>

<details>
<summary>持续聊天一直有情绪，或希望减少拒绝</summary>

默认活跃期间保持心境，停聊超过空闲时间后恢复。可关闭“持续聊天保持情绪”，改为按经过时间恢复。关闭“允许自主表达不愿意或拒绝”后，仍可保留其他情绪表达。

</details>

<details>
<summary>后台检测失败，或微信底层 API 报错</summary>

先检查 Provider、图片支持、超时配置和插件日志。微信 API 返回 HTTP 500 等环境问题，见 [兼容与排错](docs/COMPATIBILITY.md)。

</details>

## 文档与反馈

| 文档 | 内容 |
| --- | --- |
| [配置参考](docs/CONFIGURATION.md) | 全部参数、默认值与升级说明 |
| [兼容与排错](docs/COMPATIBILITY.md) | 已检查的插件和 Windows 微信环境 |
| [工作方式与开发](docs/DEVELOPMENT.md) | 情绪变化、后台任务和验证方法 |
| [更新记录](CHANGELOG.md) | 版本变化与发布内容 |

反馈时请附上插件与 AstrBot 版本、聊天模型、预期表现和实际回复，并隐藏个人信息。

[提交问题](https://github.com/1972124398/astrbot_plugin_short_term_mood/issues/new/choose) · [查看发布版本](https://github.com/1972124398/astrbot_plugin_short_term_mood/releases)

## 相关插件

| 插件 | 用途 |
| --- | --- |
| [微信原生表情转图片](https://github.com/1972124398/astrbot_plugin_wechat_sticker_receiver) | 理解微信表情传达的语气与情绪 |
| [Codex 任务桥接](https://github.com/1972124398/astrbot_plugin_codex_bridge) | 从私聊提交本机 Codex 后台任务 |

采用 [0BSD 许可证](LICENSE)，允许使用、修改、分发和商用，无需保留署名。
