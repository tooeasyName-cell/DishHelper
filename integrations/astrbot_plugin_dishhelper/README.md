# DishHelper AstrBot 插件

独立安装在 AstrBot 中的可选插件。它只负责绑定私聊的文字收发和投递进度；场景判断、业务模型、C/R/U 及所有日常动作均由同机 DishHelper 处理。

当前插件按 AstrBot 4.29 系列公开 API 对接，声明支持 >=4.29,<5；真实 AstrBot 与 QQ 仍需配置后联调。

## 安装和配置

1. 在电脑上分别安装、运行 AstrBot 与包含手机桥接功能的 DishHelper。AstrBot 平台配置按其官方文档操作，本插件不实现 QQ 登录或平台 SDK。
2. 把本目录的内容打成 ZIP，确保 ZIP 根目录直接包含 main.py、bridge.py、metadata.yaml 等文件，在 AstrBot 的插件管理中选择本地 ZIP 安装；也可将完整目录放入 AstrBot/data/plugins/astrbot_plugin_dishhelper 后重载。不要把整个 DishHelper 仓库作为插件上传。
3. 在 DishHelper 电脑侧配置桥接端口、连接密钥、决策模型档案和单个私聊来源 UMO，并启用本机接入。
4. 在插件配置中填写相同端口、连接密钥和 UMO，启用插件并重载。UMO 可用 AstrBot 的会话来源选择器查找；需要绑定私聊，不是群聊或QQ号字符串。

默认地址 http://127.0.0.1:8765。连接密钥与模型 API Key 是两回事，不写入插件日志或收发记录。凭据在 AstrBot 插件配置中保存；请按 AstrBot 配置管理方式保管。插件使用 AstrBot 现有 aiohttp 异步请求，DishHelper 无需安装 AstrBot。

## 消息和恢复

绑定私聊会被插件接管并停止 AstrBot 默认回答，不再额外请求 AstrBot 的模型。其它会话保持原有流程。当前只处理文字；回复复用 DishHelper 已保存的内容，按段落拆分，不另调用摘要模型。

每个平台消息编号对应一个本机任务。提交响应丢失时沿用原编号查询或重新提交，由 DishHelper 去重；已生成的回答不会因投递失败重新生成。未完成任务及分段进度位于 AstrBot/data/plugin_data/astrbot_plugin_dishhelper/transport.json，插件更新不会覆盖它。确认完成后清除该条缓存的正文，仅保留投递编号和状态。

插件重载或 AstrBot 重启后会继续查询未完成任务。已经开始发送、但结果不明的分段会暂停，不自动重发。AstrBot 管理员可在绑定私聊中执行 `/dishhelper_resend`（最近任务）或 `/dishhelper_resend 任务编号`，恢复已有内容的投递，不调用回答模型；未知的上一段可能已送达。两端配置修正后重载插件可以恢复之前因配置错误而暂停的接口请求。

本插件不开放 DishHelper 的调试、反馈、配置管理或临时分支；日常的场景/日期、会话、查看参考和纠正回放由 DishHelper 决策。纠正回放不自动撤回原误路由轮已经写入的 C/R/U 或共享信息。

接口实现见主程序源码的 dishhelper/mobile_server.py 和 dishhelper/mobile.py。仅做模拟接口和框架替身验证，未以此声明实际 QQ 登录、长连接或发送已经验收。

实现根据 [AstrBot 插件文档](https://docs.astrbot.app/dev/star/plugin-new.html)、[消息事件](https://docs.astrbot.app/dev/star/guides/listen-message-event.html) 和 [消息发送](https://docs.astrbot.app/dev/star/guides/send-message.html) 对接，未移植其它插件代码。
