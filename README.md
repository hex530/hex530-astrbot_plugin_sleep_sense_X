# hex530-astrbot_plugin_sleep_sense_X
☆˶> x <˶.⁺ 让 Bot 拥有真实睡眠感知 —— 睡觉、慵懒、熬夜、补觉、吵醒、睡眠周期，像真人一样。

版本: 1.3.0 | 作者: 夕小柠 | 平台: AstrBot

快速开始
安装
将插件文件夹放入 AstrBot 的 plugins/ 目录。
打开 webui_config.html（用浏览器或 QQ 内置浏览器），配置完成后点「保存配置」，会自动下载 config.yaml 和 prompts.yaml。
将这两个文件放入 data/sleep_sense/ 目录。
在 QQ 中发送 /睡眠 重载配置 使配置生效。
依赖

pyyaml

功能模块
模块	说明
慵懒模式	到点后回复简短，注入慵懒提示词
睡觉	满足时间+管理员空闲+私聊无消息三条件后入睡，关闭 LLM
起床	指定时间自动起床，支持误差波动
熬夜	概率/周限额模式，AI 自主决定，夜晚感性提示词
熬夜疲劳	连续熬夜梯度提示词，第2天起有明显疲劳感
补觉/午休	睡眠不足触发，或每日午休窗口，可配置概率
吵醒机制	按私聊消息数/群聊艾特数触发，两阶段提示词（懵→清醒）
警戒词	管理员专属，立刻唤醒
二次入睡	被吵醒后满足条件可重新入睡
多次吵醒	管理员可多次唤醒，提示词风格不同
睡眠周期	浅睡/深睡期吵醒难度变化，配合提示词模拟真实感
睡眠日志	WebUI 实时展示每晚入睡/各阶段时间点/起床时间、近7天时长趋势、梦境时间线
周作息	每天可独立配置睡觉/起床时间
特殊起床	上学/早起等场景，指定日期特殊唤醒时间
临时闹钟	AI 工具调用 sleep_set_alarm 设置
噩梦惊醒	极低概率触发，可联动情绪插件提高概率
被吵醒汇报	被别人吵醒后发消息告诉管理员
跨插件事件	发出 sleep_plugin_state_change 供其他插件监听
指令

/睡眠 状态        → 查看当前状态、睡眠时长、熬夜天数

/睡眠 清醒        → 强制切换为清醒

/睡眠 慵懒        → 强制切换为慵懒

/睡眠 睡觉        → 强制切换为睡眠

/睡眠 熬夜        → 强制切换为熬夜

/睡眠 重载配置    → 重新加载 config.yaml 和 prompts.yaml

/睡眠 日志 on     → 开启 debug 日志

/睡眠 日志 off    → 关闭 debug 日志

文件结构

data/sleep_sense/

├── config.yaml           # 主配置（由 WebUI 生成）

├── prompts.yaml          # 提示词（由 WebUI 生成）

├── state.json            # 运行状态（自动维护）

├── alarms.json           # 临时闹钟

├── dreams.json            # 梦境记录（最近 30 条）

├── daily_log.json         # 每日睡眠日志（入睡/各阶段时间点/起床/时长，最近 60 天）

├── logs/

│   └── sleep.log

├── stats/                # 统计数据（每周一清）

│   └── 2026-W23.json

└── overtime/

    ├── active.yaml

    └── history/

WebUI 睡眠日志
「当前状态」页新增三块实时展示，每 30 秒自动刷新：

睡眠周期卡：今晚入睡时间、当前所处阶段（浅睡/深睡/正常）、已睡分钟数、预计起床时间。
近 7 天睡眠时长柱状图。
梦境时间线：按日期列出梦境类型与清晰度，点击展开查看内容。
对应只读接口：


GET /astrbot_plugin_sleep_sense/status    → 当前状态实时数据

GET /astrbot_plugin_sleep_sense/history?days=7   → 最近N天每日睡眠日志

GET /astrbot_plugin_sleep_sense/dreams?limit=20  → 梦境记录列表

webui_config.html（独立单文件版）需要先在页面顶部「连接到 AstrBot」处填入服务器访问地址才能拉取实时数据；pages/config/index.html（AstrBot 内嵌版）通过内置 bridge 自动连接，无需额外配置。

跨插件联动
其他插件监听睡眠状态变化：

python
# 在你的插件中

@context.on_event("sleep_plugin_state_change")

async def on_sleep_change(data):

    if data["state"] == "sleep":

        # 关闭主动回复

        pass

    elif data["state"] == "awake":

        # 开启主动回复

        pass

WebUI 配置说明
用浏览器打开 webui_config.html，所有配置均有说明。 修改完成后点「💾 保存配置」自动下载两个 yaml 文件， 放入 data/sleep_sense/ 后在 QQ 内发 /睡眠 重载配置 即可。

扩展开发
新增功能请在 main.py 中添加对应的 _handle_* 或 _check_* 方法
提示词统一在 prompts.yaml 管理，无需改代码
所有开关均支持热重载，无需重启 AstrBot
如果有报错，可以联系我。感谢使用！
联系方式：QQ：1591793025
