# Notion 本机 Agent 执行器

从 Notion 任务看板领取指定给本机的待办，启动本机 Codex 或 Claude Code，将完整结果写入卡片原生评论，再进入「待我验收」或「需处理」。用户决定何时移到「完成」。

Python 3.11+，Mac / Linux，使用标准库直接访问官方 Notion API，无运行时第三方依赖。每机并发 1，默认 30 秒轮询。代码和调研任务均直接在项目目录执行，不创建 worktree 或切换分支。CLI 和用户登录沿用本机环境，不自动绕过权限。

## 快速开始

在本项目目录运行：

```sh
python3 -m notion_agent init
python3 -m notion_agent machine macbook
python3 -m notion_agent project research /absolute/path/to/materials
python3 -m notion_agent project app /absolute/path/to/repo
```

当前工作目录已生成 `config.toml`，无需重复 init。机器名默认为主机名。修改机器名、状态目录或任务数据源后重启服务。

### 在 Notion 添加项目

当前已启用 [Agent 项目配置](https://www.notion.so/YOUR_PAGE_ID)。每行填写：

| 项目 | 执行机器 | 本机目录 |
| --- | --- | --- |
| myapp | 与本机 machine_id 一致 | 本机已存在的绝对目录 |

任务卡的「项目」是关联属性，直接选择此表的一条记录，无需复制或同步项目名。每个任务只关联一个项目；项目记录决定执行机器、目录和项目指令。改名不会断开关联；同名记录也按页面 ID 区分。机器只领取关联项目属于自己的任务。缺目录或相对路径会阻止接单；运行中改派机器或目录会暂停回写，等待人工核对。

`config.toml` 的 `projects_data_source_id` 指定项目表。启用后，本机 `[projects]` 不再参与派单；项目表访问失败时暂停接单，不使用旧目录。未配置项目表时仍支持 `[projects]` 和上面的 `project` 命令。

### 给独立服务授权 Notion

桌面 Notion 插件授权属于插件连接，本程序使用独立的 Notion integration token。无需把 token 发到聊天。

1. 打开 [Notion integrations](https://www.notion.so/profile/integrations)，创建 internal integration，选择看板所在工作区。启用读取、更新、插入内容，以及读取和插入评论权限。
2. 在[任务看板](https://www.notion.so/YOUR_PAGE_ID)的 `… → Connections / 连接` 中添加这个 integration。
3. 在配置文件同目录的 `.env` 中填写 token（已创建空白文件）：

```dotenv
NOTION_TOKEN=你的token
```

程序自动读取 `config.toml` 所在目录的 `.env`，因此后台运行也不依赖终端当前目录。可通过 `env_file` 修改路径。支持单行 `KEY=value`、引号、注释和 `export` 前缀，不执行 shell 命令或变量展开。

凭据读取顺序：系统环境变量 → `.env` → 显式配置的旧版 `token_file`（兼容已有配置）。`.env` 已被 Git 忽略，本机文件权限设为 0600；不要提交真实 token，仓库只提供 `.env.example`。文件内容不会被导出到子进程环境。修改 token 后重启服务生效。

```sh
python3 -m notion_agent doctor
python3 -m notion_agent register
python3 -m notion_agent dry-run
python3 -m notion_agent once
python3 -m notion_agent run
```

关联模式无需运行 `register`；该命令只兼容旧版下拉字段。`dry-run` 只读远端并显示候选和拒绝原因，不领取、写状态或启动 Agent；本地会初始化 SQLite。`once` 先恢复待回写任务，再扫描并最多执行一项，执行结束才退出。`run` 常驻。

项目记录正文作为该项目的长期指令：Codex 通过 `developer_instructions` 注入，Claude Code 通过 `--append-system-prompt` 追加。任务正文单独作为本次任务输入。每次接单保存项目正文到运行快照和 `project-instructions.txt`；正文读取失败或读取时发生修改，不启动任务。空白正文不追加指令；单次指令上限 64000 UTF-8 字节。`default` 是普通兜底项目，不是其他项目的全局默认配置；当前仍需在卡片明确选择项目。

工作区的 [Agent.md](https://www.notion.so/YOUR_PAGE_ID) 解释任务、项目、文档、评论和验收约定。`agent_guide_page_id` 指定其页面 ID；每次接单读取正文并保存为 `workspace-guide.txt`，与项目正文一起传入执行器。配置为空时不读取该页面。`comments = false` 只关闭开始处理通知，最终答复仍通过评论交付。程序通知的评论显示名为「调度程序」，实际回答显示为「Codex」或「Claude Code」。这只是评论显示名，真实创建者仍由 Notion 凭据身份决定。

## 怎么派单

卡片需要有标题、有效正文、状态「待办」、一个关联项目、已启用 Agent 和类型「代码／调研」。任务不再填写执行机器；由关联的项目配置决定。建议先填正文、验收条件与项目，最后设为待办。

新任务可先保留在「草稿」。本机待办校验失败（例如缺少类型、正文为空、项目指令不支持）会退回「草稿」，运行摘要保留未接单原因；修正后需要再次设为「待办」，不会在草稿阶段自动执行。执行已经开始后失败仍进入「需处理」。任务或项目正在编辑时仅跳过当前扫描；网络异常保留状态等待重试。

正文支持普通文本、列表、嵌套、代码块、表格、引用和明确链接；不自动递归读取外链。附件、图片、子页面、同步块、嵌入数据库等暂不支持的块会阻止接单，并在运行摘要说明。结果通过原生评论回复，和接单消息位于同一讨论线程。正文和人工评论保留；运行 ID、会话、进程、日志和协议 JSON 留在本地，任务页面隐藏「最近运行 ID」，运行摘要使用自然语言。过长答复分成连续回复，长篇成果可另放协作文档。

### 评论里派发和续聊

在 issue 的页面级评论或已有页面讨论线程中，直接输入 `@codex 请修正反馈`、`@claude review 当前改动`，或 `@Claude Code 再检查一次`。大小写不敏感；这里的 @ 是文字指令，暂不映射 Notion 菜单里的用户或自定义 Agent 对象。

- 同一 issue 中，目标 Agent 已有本机会话就通过明确的会话 ID 续聊；首次加入则创建会话。新加入者会收到当前正文、项目指令、历史交付和截至触发评论的可见评论。历史交付保留最后 48000 字符，评论保留最后 24000 字符，本次指令完整保留。
- 最早领取本 issue 的 Agent 是主 Agent，负责「执行中」「待我验收」「需处理」、运行摘要及最近运行记录。附属 Agent 只在触发评论所在的讨论中回复，不修改上述属性；附属失败也不会改变主任务状态。主 Agent 的角色固定；不要通过修改执行 Agent 下拉框转交角色，其他 Agent 通过评论调用。
- 评论在草稿、待办、执行中、待我验收、完成、需处理状态都可以派发；仍需标题、有效项目、本机目录、代码或调研类型，以及已启用的目标 CLI。评论可以替代空白任务正文，执行 Agent 下拉框可空。主 Agent 接到评论会进入执行中；附属执行保留当前状态。
- 扫描优先检查评论。常驻服务默认每台机器最多同时执行两个互不重叠的项目目录，可在 `config.toml` 设置 `max_parallel_projects`，设为 `1` 即串行。同一项目、同一实际目录（含符号链接）及父子目录之间仍串行，等待时保留原任务状态。`once` 仍最多执行一项。一条评论可以 @ 多个 Agent，按出现顺序分别派发；相同 Agent 重复出现只派发一次。机器人作者和本机发送回执记录的程序评论不会触发新执行；个人 API token 下程序回复可能归属到用户本人，按评论 ID 排除，保留该用户真正发送的指令。
- SQLite 持久保存评论与 Agent 的派发回执、会话记录和回复回执。首次使用新版本初始化本机状态库时设定启用时间，之前的历史评论不补跑；之后服务离线期间的新评论会在恢复轮询后处理。已派发的同一评论对同一 Agent 不因编辑或重启再次执行，请新增评论表达新的请求。
- 配置缺失等接单失败会在原讨论提示原因，修正后自动重试；同一原因只通知一次。缺少原会话 ID、项目/目录变化或本地运行记录丢失时，不自动另开会话替代，需要人工核对。

例如：Codex 首次写代码 → 评论 `@claude review 当前改动` → Claude 首次创建附属会话并回复 → 评论 `@codex 按 review 修正` → 继续原 Codex 会话。提交代码仍需用户在本次指令中明确授权，完成状态仍由用户验收后设置。

当前轮询页面级开放评论，不递归读取正文块的行内评论；已解决或删除的评论不可依赖此 API 获取，扫描前被解决的指令可能漏过。请在页面评论中派发，并保留讨论开放。评论读取行为依据 [Notion 官方说明](https://developers.notion.com/guides/data-apis/working-with-comments)。全状态扫描会增加 API 请求，规模较大时需进一步优化。

代码和调研任务都直接使用配置的项目目录，保留已有文件和未提交改动。不要求 Git 仓库、干净工作区或基线分支，不自动创建目录、切换分支、提交、推送或合并。每机一次执行一个任务。项目指令、MCP、技能和记忆的加载效果需分别实测，不能由 CLI 已安装推断。

最终回答须包含完整结果和最后一行验收 JSON（提示词由服务生成）。进程退出码 0、CLI 协议成功、明确声明满足验收条件，才进入「待我验收」；否则「需处理」。Claude 的 permission_denials 也会阻止交付成功。AI 的声明是交付依据，成果质量和验收仍由用户判断。

## 耗时统计

任务库可添加三个 number 字段：`最近耗时（秒）`、`累计耗时（秒）`、`执行次数`，并在视图和页面布局中隐藏。三者全部存在时，服务在结果交付后单独写入统计，写入失败不影响评论和状态；全部未添加时保持原行为，部分缺失或类型不符时 doctor 报错。

耗时为本地记录的接单时间到执行结束时间之差，包含启动准备，不包含排队、人工等待和结果回写重试。统计包含同一 issue 的首次执行、评论续办和附属 Agent，成功与失败均计入；未接单、缺少有效起止时间的记录不计入。服务从本机保存的运行历史重算绝对值，重试回写不会重复累加；迁移机器时需一起迁移该 issue 的本地历史。历史记录不完整时统计也不完整，服务中断后恢复的结束时间可能晚于实际进程结束时间。

## 查看和恢复

可选的 `execution_data_source_id` 指向共用执行记录表。字段为：`执行` title、`任务` relation（关联任务库）、`Agent` select、`执行时间` date、`耗时（秒）` number、`结果` select（执行中／待我验收／需处理）、`触发方式` select（任务派单／评论续聊）、`机器` rich_text、`运行 ID` rich_text、`开始时间（原始）` rich_text、`结束时间（原始）` rich_text。可另加分钟公式和原生时间线视图；原始表收纳到后台数据即可。

每次派单或评论续聊创建一行，开始写入时间，结束补齐范围、时长和结果；失败或中断也保留记录。运行记录保存远端行 ID。创建请求被明确拒绝后可以重试；响应丢失时按运行 ID 对账，无法确认时保留在本地待补写队列，不重复建行。

执行记录表不可用时服务仍能启动，`doctor` 会报告问题。执行记录和耗时字段的回写失败不阻止 Agent 执行、评论回复或状态更新，原因记录在本地 SQLite 的 `stats_outbox`，按 60 秒起翻倍、最长 1 小时的间隔重试；`status` 命令会列出待补写项。Notion 日期字段在本次实测中回读到分钟，精确 ISO 起止时间另外保存在隐藏的原始时间字段，耗时数字精确到毫秒；时间线用于看执行分布，不是秒级性能分析图。配置留空不创建执行记录。

```sh
python3 -m notion_agent status
python3 -m notion_agent logs RUN_ID
```

`.state/runs/<run-id>/` 下保留需求快照、实际提示词、结构化事件、stderr 和最终回答；实际代码及产出保存在项目目录。会话 ID 来自 CLI 真实输出。自动续聊通过上面的评论指令完成。手工恢复会话可使用 `codex exec resume SESSION_ID` 或 `claude --resume SESSION_ID`；先停止服务并核对卡片，手动恢复不会自动接回原 worker。

- SQLite 使用 DELETE 日志模式，状态保存在运行目录的 `.state` 下；先记领取，再写 Notion「执行中」和运行 ID，确认成功后启动进程。
- Agent 完成后先保存完整交付，再回复原生评论，最后更新状态。评论回执、发送前快照和作者信息保存在本地；响应丢失时先对账，不在正文添加技术标记，不重新执行 Agent。无法唯一确认评论是否写入时保留待回写状态，不重复发送。
- 评论对账兼容个人 API token 的用户作者与集成机器人作者，仍要求正文、署名、讨论线程和发送前快照匹配。
- 服务中断后检查 PID 和进程启动时间；旧进程尚在时暂停接单。进程已结束时保留工作区并报告「需处理」，不自动重跑。
- 若恰好崩溃在启动进程和保存 PID 之间，进入本地 `hold`，本机暂停接新任务；旧进程仍存活（包括 Agent 已结束但 PID 仍在）时同样暂停。
- 运行已结束且进程确认退出后，回写故障只影响原任务：原卡不可访问、进入回收站、交付写入失败或评论无法确认时保持待交付，失败原因和次数记在本地运行记录（`status`、`logs RUN_ID` 可查），下轮自动重试；运行 ID／路由／状态被人工修改时进入 `hold`。其他待办照常接单。同一任务在对账完成前不会被退回草稿、不提示接单问题，也不会启动新运行。
- `SIGINT`／`SIGTERM` 停止接单，终止所有正在执行的 Agent 进程组并等待清理，保留待回写交付。下次启动补写。
- Notion 网络失败或限流采用退避；旧版结果块回写继续兼容。原生评论没有服务端幂等键，若不确定的评论被解决或删除而无法对账，需要人工核对。Notion 状态不是跨机器原子锁，不承诺全局 exactly-once。

人工核对旧进程、原会话和工作区，确认任务已经结束后，可解除本地阻塞：

```sh
python3 -m notion_agent resolve RUN_ID --confirm-stopped
```

resolve 会保留所有记录与产物，不写 Notion、不杀进程，也不执行任务。若需要重派，先确认旧运行结束和结果已处理，再把原卡移回「待办」，不要清空「最近运行 ID」。同机已结束运行的卡片重置后会生成新 ID。来自另一机器或本地状态丢失的运行不自动接管。

运行约定：每台机器 ID 唯一，每套任务库和机器在本机只运行一个 worker；各机器保留独立的持久状态目录，不共用同一套运行记录或删除状态。状态目录锁和本机按机器/任务库的锁同时约束重复进程。多台机器分别运行同一程序、使用各自项目映射，仅领取指定给自己的任务。

## 后台服务

```sh
python3 -m notion_agent service-files
```

生成包含当前 Python 绝对路径、项目目录和配置路径的服务文件。生成文件本身不安装服务。先通过 doctor、register、dry-run 和真实 once 验证，再安装。

macOS：

```sh
mkdir -p ~/Library/LaunchAgents
cp deploy/generated/top.mclab.notion-agent.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/top.mclab.notion-agent.plist
launchctl print gui/$(id -u)/top.mclab.notion-agent
# 停止
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/top.mclab.notion-agent.plist
```

日志在 `.state/service.log`。服务会在用户登录后启动。关闭终端可继续运行；Mac 睡眠或离线时无法接单。改完机器名后重启服务，并在项目配置中填写匹配的执行机器。

Linux（在 Linux 上重新生成服务文件）：

```sh
mkdir -p ~/.config/systemd/user
cp deploy/generated/notion-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now notion-agent
journalctl --user -u notion-agent -f
# 停止
systemctl --user disable --now notion-agent
```

用户退出登录后是否继续运行取决于机器的 systemd lingering 配置。

## 实际验证范围

```sh
python3 -m unittest discover -s tests -v
```

模拟测试覆盖评论续聊与多 Agent 派发、附属 Agent 不写状态、评论重复扫描与重启去重、原会话缺失与失败重试、全状态评论派发，以及重复领取、写入成功但响应丢失、回写重试不重执行、人工改派、空正文、未知项目、多机筛选、重置重派、进程身份、启动窗口崩溃、子进程超时及项目目录直接执行。

已通过 Notion 插件读需求、读真实 schema 和原卡片，并增量补齐机器、项目、运行 ID、运行摘要及「需处理」状态；已有卡片未修改。本机 Codex 和 Claude Code 参数通过已安装版本 help 核对。

已通过真实 Codex 任务验证接单、执行、测试与结果回写，已安装本机 launchd 服务。Notion 项目表与直接目录执行另有针对性测试。评论功能已实测读取真实 issue 评论和解析作者类型，Codex resume 与 Claude --resume 参数经本机 CLI help 核对，续聊进程调用通过模拟 CLI 子进程验证。尚未完成评论派发的真实双 Agent 跨轮闭环、Claude 真实闭环、后台 MCP/技能/OpenViking 全面验证、第二台机器及 systemd 验收。

Notion 接口依据：[数据源查询](https://developers.notion.com/reference/query-a-data-source)、[追加结果块](https://developers.notion.com/reference/patch-block-children)、[数据源 schema](https://developers.notion.com/reference/update-a-data-source)。
