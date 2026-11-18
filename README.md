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

正文支持普通文本、列表、嵌套、代码块、表格、引用和明确链接；不自动递归读取外链。附件、图片、子页面、同步块、嵌入数据库等暂不支持的块会阻止接单，并在运行摘要说明。结果通过原生评论回复，和接单消息位于同一讨论线程。正文和人工评论保留；运行 ID、会话、进程、日志和协议 JSON 留在本地，任务页面隐藏「最近运行 ID」，运行摘要使用自然语言。过长答复分成连续回复，长篇成果可另放协作文档。

代码和调研任务都直接使用配置的项目目录，保留已有文件和未提交改动。不要求 Git 仓库、干净工作区或基线分支，不自动创建目录、切换分支、提交、推送或合并。每机一次执行一个任务。项目指令、MCP、技能和记忆的加载效果需分别实测，不能由 CLI 已安装推断。

最终回答须包含完整结果和最后一行验收 JSON（提示词由服务生成）。进程退出码 0、CLI 协议成功、明确声明满足验收条件，才进入「待我验收」；否则「需处理」。Claude 的 permission_denials 也会阻止交付成功。AI 的声明是交付依据，成果质量和验收仍由用户判断。

## 查看和恢复

```sh
python3 -m notion_agent status
python3 -m notion_agent logs RUN_ID
```

`.state/runs/<run-id>/` 下保留需求快照、实际提示词、结构化事件、stderr 和最终回答；实际代码及产出保存在项目目录。会话 ID 来自 CLI 真实输出。恢复会话可使用 `codex exec resume SESSION_ID` 或 `claude --resume SESSION_ID`；先停止服务并核对卡片，手动恢复不会自动接回原 worker。

- SQLite 先记领取，再写 Notion「执行中」和运行 ID，确认成功后启动进程。
- Agent 完成后先保存完整交付，再回复原生评论，最后更新状态。评论回执、发送前快照和作者信息保存在本地；响应丢失时先对账，不在正文添加技术标记，不重新执行 Agent。无法唯一确认评论是否写入时保留待回写状态，不重复发送。
- 服务中断后检查 PID 和进程启动时间；旧进程尚在时暂停接单。进程已结束时保留工作区并报告「需处理」，不自动重跑。
- 若恰好崩溃在启动进程和保存 PID 之间，或运行 ID／路由／状态被人工修改，会进入本地 `hold`；本机暂停接新任务，避免错写或重复执行。
- `SIGINT`／`SIGTERM` 停止接单并终止正在执行的进程组，保留待回写交付。下次启动补写。
- Notion 网络失败或限流采用退避；旧版结果块回写继续兼容。原生评论没有服务端幂等键，若不确定的评论被解决或删除而无法对账，需要人工核对。Notion 状态不是跨机器原子锁，不承诺全局 exactly-once。

人工核对旧进程、原会话和工作区，确认任务已经结束后，可解除本地阻塞：

```sh
python3 -m notion_agent resolve RUN_ID --confirm-stopped
```

resolve 会保留所有记录与产物，不写 Notion、不杀进程，也不执行任务。若需要重派，先确认旧运行结束和结果已处理，再把原卡移回「待办」，不要清空「最近运行 ID」。同机已结束运行的卡片重置后会生成新 ID。来自另一机器或本地状态丢失的运行不自动接管。

运行约定：每台机器 ID 唯一，每套任务库和机器在本机只运行一个 worker；状态目录必须放在本地持久磁盘，不能在两个主机共享或删除。状态目录锁和本机按机器/任务库的锁同时约束重复进程。多台机器分别运行同一程序、使用各自项目映射，仅领取指定给自己的任务。

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

模拟测试覆盖重复领取、写入成功但响应丢失、回写重试不重执行、人工改派、空正文、未知项目、多机筛选、重置重派、进程身份、启动窗口崩溃、子进程超时及项目目录直接执行。

已通过 Notion 插件读需求、读真实 schema 和原卡片，并增量补齐机器、项目、运行 ID、运行摘要及「需处理」状态；已有卡片未修改。本机 Codex 和 Claude Code 参数通过已安装版本 help 核对。

已通过真实 Codex 任务验证接单、执行、测试与结果回写，已安装本机 launchd 服务。Notion 项目表与直接目录执行另有针对性测试。尚未完成 Claude 真实闭环、后台 MCP/技能/OpenViking 全面验证、第二台机器及 systemd 验收。

Notion 接口依据：[数据源查询](https://developers.notion.com/reference/query-a-data-source)、[追加结果块](https://developers.notion.com/reference/patch-block-children)、[数据源 schema](https://developers.notion.com/reference/update-a-data-source)。
