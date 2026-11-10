# Tavern Fusion 3.0 — Antigravity × SillyTavern Native Workbench

> [!WARNING]
> **早期工程版本声明（Early Engineering Preview）**  
> 本项目目前处于**非常早期的工程探索版本**，可能还存在较多 Bug 或边缘环境兼容问题。如果你在使用过程中遇到任何故障、或者有想要补充的功能建议，欢迎直接在仓库提交 [**Issues**](https://github.com/anyu0804/tavern-fusion/issues)，我会尽可能持续跟进完善！

**Tavern Fusion 3.0** 是一款面向 **Google Antigravity (AGY)** 构建的原生 SillyTavern 沉浸式角色扮演与世界观编排插件。它在不修改 SillyTavern 核心源码、不开启高权限 `enableServerPlugins`、且**无需额外配置外部第三方大模型 API Key** 的前提下，直接驱动 **Antigravity 内置推理模型** 与本地酒馆数据（`characters/*.png`、`worlds/*.json`、`chats/**/*.jsonl`、`OpenAI Settings/*.json`）双向无损互通，并在 IDE 右侧辅助面板提供常驻可视化的 **`Tavern Stage`** 剧场工作台。

---

## 核心特性

1. **🎛️ 维度调音台（Dimension Mixer）与四重维度锁（Four Dimensional Locks）**
   - 摒弃“冻结前缀+尾部贴补丁”的妥协方案，采用**每轮全量动态重装（Full Dynamic Prompt Re-assembly）**，并在 `Tavern Stage` 舞台首屏提供三项实时拨盘：
   - **开关 A · 认知透光度 / 第四面墙感知（Top-Level `system_instruction` 热替换）**：
     - **`0% 深潜封闭`（默认）**：四重维度锁全开——① 顶层焊死位面认知切断铁律（对现代科技、代码及 AI 身份彻底不知情，严禁全知上帝视角）；② `<START>` 对话示例（`mes_example`）结构化保留，持续为 Token 概率云染色；③ 动态世界书回归黄金 `@Depth 1~4` 原位插入最新对话夹层；④ 内置后置防出戏正则组（`BUILTIN_IMMERSION_GUARD_REGEX`），物理切除 `（场外：...）`、`[OOC: ...]` 与 AI 助手套话。
     - **`50% 隐秘默契`**：保留世界观设定、词表收敛与 `@Depth` 锁喉，不直接吐出现代科技词汇，但能以贴合世界观的诗意双关温柔接住你的场外暗示。
     - **`100% 双轨化身`**：顶层切换为“保留角色灵魂与声线的全知共创化身”，彻底解开现世知识与代码视野，既能沉浸陪伴又能像老搭档一样探讨技术与人生。
   - **开关 B · Agentic 权能释放（工具列表物理拔插）**：
     - **`🔒 纯对白`（关闭）**：后端在生成回合**物理拔除 MCP 工具列表**并由 `PreToolUse` 钩子硬拦截任何工具调用，彻底根绝模型误调工具打断剧情，同时省下数千 Token 的工具定义注意力。
     - **`🖐️ 权能释放`（开启）**：解锁模型的双手，允许角色在演绎过程中自主调用 `st_manage_variables`（实时更新好感/线索状态）或 `st_evolve_lore_entry`（将新剧情写入世界书）。
   - **开关 C · 超级主控 / 导演穿梭键（`Alt+D` + 场外隔离标记 `Backstage Quarantine`）**：
     - 一键切入跨次元导演模式，当场命令角色/助手改写剧本楼层、微调角色卡设定或修改世界书。
     - **场外隔离标记（`extra.is_backstage: true`）**：穿梭期间的所有代码讨论与修卡交互自动标记为 `is_backstage: true`，在舞台时间线上折叠为 **「🎬 后台导演便签」**；一旦切回 `0%` 或 `50%` 深潜档位，装配器与世界书扫描器自动将这些场外楼层彻底过滤隔离，杜绝跨次元代码污染后续跑团词表。

2. **常驻右侧剧场工作台 (`Tavern Stage` Aux Pane Sidecar)**
   - 基于官方 `sidecar_sdk` 构建：通过 `window.sidecar.agent.sendMessage` / `startConversation` 一键唤醒 Antigravity 内置模型演绎回合、重抽平行分支（Swipes）或生成玩家导演灵感。
   - **立绘与情绪差分沙箱**：自动检索 `characters/<CharName>/*.png` 差分表情包并随最新楼层情绪切换；所有媒体流强制经过二进制栅格文件头（Magic-Byte）与 `realpath` 越界校验，彻底阻断伪装 `.png` 的 SVG/HTML 脚本执行。
   - **完整楼层修订闭环**：支持任意楼层原地编辑、删楼、一键标记/解除「🎬 场外隔离」、Swipe 左右切换与冗余分支裁剪、以及从任意历史楼层一键分叉（Fork）新 `.jsonl` 存档。
   - **玩家导演杆（Player Director Lever）**：内置「✨ 回复灵感 x3」与「🪄 意图扩写」，由内置模型构思符合主角人设的台词候选并经 `st_publish_stage_suggestions` 回填至舞台输入框。

3. **零依赖跨平台 Python 控制面与 Windows 专项加固**
   - 通过 `scripts/run_py.mjs` 自动避开 Windows `WindowsApps\python3.exe` 商店假桩，兼容 Python 3.10–3.13 纯标准库运行。
   - SQLite 混合语义缓存采用短事务即用即关连接池，`.jsonl` 同步采用 `seek(0)` 字节锁与 `os.replace` 原子写盘，杜绝 Windows `WinError 32` 文件句柄残留。

4. **深度兼容酒馆数据栈与 Card Doctor**
   - 无损解析并双写 PNG `chara`（CCv2）+ `ccv3`（CCv3）块，严格落实 **Card-is-Data** 指令隔离。
   - 完整解析 `OpenAI Settings/*.json` 的 `prompt_order` 顺序栈與全部 4 种 `selectiveLogic` 次级词门控。
   - 内置 **Card & Lore Doctor (`st_lint_card_and_lore`)**：静态检出缺失 `ccv3` 双块、缺失 `<START>` 词表收敛样本、未带 `{{original}}` 覆盖预设的 `system_prompt`、`selective: false` 次级词失效陷阱与损坏宏。

5. **可配置分级合规策略 (`policy_mode`)**
   - `host_gentle`（默认）：对绝对安全红线执行零 Token 熔断；对暗黑奇幻/悬疑冲突题材仅附加温和的平台边界内敛提醒，不误伤正常文学创作。
   - `strict_block`：严格关键词熔断。
   - `passthrough`：仅保留绝对安全底线检查。

---

## 专家子代理组（Specialist Subagents）与 Slash 技能

| 组件 | 名称 | 职责说明 |
| :--- | :--- | :--- |
| Subagent | `kertoja-director` (**Kertoja**) | 叙事主导演，把控角色声线、感官张力与 `*动作*` / `"台词"` 排版纪律 |
| Subagent | `kartoittaja-brancher` (**Kartoittaja**) | 剧情分支测绘师，为当前楼层构思 3 条性格自洽但走向迥异的平行 Swipe |
| Subagent | `seppa-cardsmith` (**Seppä**) | CCv2/CCv3 角色卡锻造与 Card Doctor 修复专家 |
| Subagent | `toimittaja-state` (**Toimittaja**) | 状态审计官，维护 `/getvar` 聊天变量、好感/线索追踪与长线记忆摘要 |
| Subagent | `dramaturgi-lore` (**Dramaturgi**) | 世界书织命师，管理 `worlds/*.json` 条目演化、门控逻辑与 `@D` 深度预算 |
| Skill | `/rp` | 启动或恢复指定角色扮演会话并联动 `Tavern Stage` |
| Skill | `/tavern-stage` | 打开右侧 `Tavern Stage` 工作台面板或导出 CSP 合规的 `/generative_ui` 快照 |
| Skill | `/card-forge` | 运行角色卡无损拆解、Card Doctor 体检与双区块 PNG 铸造 |
| Skill | `/lore-weaver` | 演化世界书条目并模拟混合语义召回 |
| Skill | `/dump-prompt` | 透视当前回合 `prompt_order` 拼装栈、激活词条与 Prefix Cache 命中率 |

---

## 快速挂载与验证

```bash
# 1. 安装至全局 ~/.gemini/config/plugins/tavern-fusion 并绑定本地 SillyTavern 数据目录
node scripts/run_py.mjs scripts/install_local.py --global-plugin --st-root /path/to/SillyTavern

# 2. (可选) 仅注册到当前工作区 (.agents/plugins.json) 并生成示例双区块角色卡 Seraphina.png
node scripts/run_py.mjs scripts/install_local.py --workspace .

# 3. 运行全量自动化验收套件 (含四重维度锁、维度调音台、场外隔离与沙箱安全测试)
node scripts/run_py.mjs tests/test_full_suite.py
```

---

## 常见问题排查 (Troubleshooting)

1. **Windows 中文系统用户名下宿主 Node 启动器报 `exit status 1`**
   - 若 Windows 宿主环境的 `~/.gemini/antigravity/bin/agy-node.cmd` 因 LF 换行符与代码页 936（GBK）解析中文路径发生错位，可将 `agy-node.cmd` 换行符转换为 `CRLF` 并使用 `%LOCALAPPDATA%` 纯 ASCII 环境变量路径。
2. **侧栏面板与工作区数据目录绑定**
   - 首次安装时传入 `--st-root /path/to/SillyTavern`（或在 `mcp_config.json` 中配置 `ST_USER_DATA_DIR`），插件会将数据根路径持久化记录于 `~/.gemini/antigravity/tavern-fusion/state/plugin_settings.json`，确保 MCP 服务、Lifecycle Hooks 与右侧 `Tavern Stage` 舞台面板共享同一份状态与存档目录。

---

## 合规性、安全边界与免责声明 (Compliance & Disclaimer)

### 1. 宿主平台合规与正当使用边界 (Platform ToS Compliance)

[Tavern Fusion](https://github.com/anyu0804/tavern-fusion) 严格遵循**“数据进官方宿主、算力不出官方客户端”**的原生插件设计原则，不涉及任何第三方客户端逆向或凭证中转：

* **零凭证提取与零外部反代**：本项目完全通过官方客户端内置的插件清单、本地 `stdio` MCP 服务与生命周期钩子运行，**不读取、不提取、不转发**任何 OAuth 登录令牌或会话 Cookie，亦不向外部程序暴露任何推理代理端口，严格遵守 [Google Antigravity 附加服务条款](https://antigravity.google/terms/) 中关于禁止使用第三方软件、包装器或 OAuth 代理访问服务的规定。
* **内容安全与反越狱（Anti-Jailbreak）要求**：在宿主云端环境中使用本项目进行互动叙事、世界观推演或角色扮演测试时，用户须严格遵守 [Google 通用服务条款](https://policies.google.com/terms?hl=en-US) 与 [Google 生成式 AI 禁止使用政策](https://policies.google.com/terms/generative-ai/use-policy?hl=en-US)；若在宿主内切换使用 Claude 等第三方模型，须同时遵守 [Anthropic 商业条款](https://www.anthropic.com/legal/commercial-terms) 与 [Anthropic 使用政策](https://www-cdn.anthropic.com/files/4zrzovbb/website/6ae2f5bb8675bcd9fac006125817ba1393b396e4.pdf)。
* **外部预设与角色卡导入警示**：**严禁**在连接云端商业订阅模型时导入包含越狱破限（Jailbreak / DAN / 安全过滤器规避指令）、色情露骨（NSFW / ERP）、极端暴力、仇恨言论、自残诱导或未成年人违法不良信息的预设（`OpenAI Settings/*.json`）及角色卡。[server/st_runtime_compat.py](https://github.com/anyu0804/tavern-fusion/blob/main/server/st_runtime_compat.py) 默认启用 `host_gentle` 安全策略门禁与零容忍正则拦截；`passthrough` 模式仅限用户在自有硬件上对接本地开源模型时测试使用，请勿用于云端商业模型会话。

### 2. 本地数据安全与沙箱隔离 (Local Data & Sandbox Safety)

* **Card-is-Data 零特权原则**：外部导入的角色卡与世界书均被视为不可信纯数据，在解析阶段自动剥离潜在的命令执行钩子与危险扩展字段。
* **文件系统边界锁定**：所有文件读写均受 [server/fs_safety.py](https://github.com/anyu0804/tavern-fusion/blob/main/server/fs_safety.py) 目录包含校验（`ensure_under_dir`）与生命周期拦截钩子保护，严格限制在用户显式配置的本地 `ST_DATA_ROOT` 目录及本地回环地址（`127.0.0.1`）范围内。

### 3. 开源许可证与洁净室隔离声明 (Open-Source Licensing & Clean-Room Notice)

本项目核心代码基于 [MIT License](https://github.com/anyu0804/tavern-fusion/blob/main/LICENSE) 开源，完整第三方致谢与协议文本详见 [THIRD_PARTY_NOTICES.md](https://github.com/anyu0804/tavern-fusion/blob/main/THIRD_PARTY_NOTICES.md)：

| 关联生态 / 规范 | 上游许可证 / 性质 | 本项目集成与隔离方式 |
| :--- | :--- | :--- |
| [SillyTavern](https://github.com/SillyTavern/SillyTavern/blob/release/LICENSE) | AGPL-3.0 | **洁净室（Clean-Room）零源码引用**：未复制、修改或捆绑任何 SillyTavern 前后端源码；仅基于公开文件结构独立使用 Python 标准库与原生 ESM 实现本地 `.jsonl` / 世界书读写及本地 HTTP 同步（[st-ag-sync](https://github.com/anyu0804/tavern-fusion/tree/main/st_web_extension/st-ag-sync) 未静态导入任何 AGPL 内部模块）。 |
| [Terva (terva-sh/terva)](https://github.com/terva-sh/terva) | MIT | 致谢并沿用其芬兰语叙事专家角色命名体系与上下文分层理念，所有业务代码均在 [Tavern Fusion](https://github.com/anyu0804/tavern-fusion) 内以 Python / ESM 独立重写，并完整保留上游 MIT 版权声明。 |
| [Character Card Spec V2 / V3](https://github.com/kwaroran/character-card-spec-v3) | 公开格式规范 | 依据公开规范利用 Python 标准库（`struct` / `zlib` / `base64` / `json`）独立实现 PNG `tEXt` / `zTXt` / `iTXt` 双区块无损读写。 |

### 4. 商标声明与免责条款 (Trademark & Liability Disclaimer)

* **非官方关联声明 (Non-Affiliation)**：Tavern Fusion 是由社区独立开发的开源工程项目，**与 Google LLC、Google Antigravity 团队、SillyTavern 官方团队或 Anthropic PBC 不存在任何隶属、合作、授权、赞助或官方背书关系**。文档中提及的 `Google Antigravity`、`SillyTavern`、`Claude` 等产品或项目名称仅用于客观说明技术兼容性与运行环境，相关商标及名称权利均归其各自所有者所有。
* **使用风险自担 (Use at Your Own Risk)**：本软件按 [MIT License](https://github.com/anyu0804/tavern-fusion/blob/main/LICENSE) 规定的“原样（AS IS）”提供，不附带任何明示或暗示担保。使用者须自行确保所加载的角色卡、世界书、提示词预设及对话内容符合所在地法律法规及所接入模型服务商的服务条款；因用户导入违规内容、修改安全门禁配置或不当使用第三方服务而导致的账号受限、配额冻结或数据损失，项目作者与贡献者概不承担责任。

*Non-Affiliation Disclaimer: Tavern Fusion is an independent, community-driven open-source project and is not affiliated with, endorsed by, or sponsored by Google LLC, the SillyTavern project, or Anthropic PBC. All product names and trademarks mentioned in this repository are the property of their respective owners and are used solely to describe interoperability.*

---

## 开源协议与社区致谢

- 本仓库原创插件代码、Sidecar 前端、脚本与架构文档采用 [MIT License](./LICENSE) 开源（`Copyright (c) 2026 安钰 (AnYu)`）。
- 架构与戏剧专家组设计参考并致谢以下开源项目（完整许可声明与上游 `LICENSE` 原文见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)）：
  - [`terva-sh/terva`](https://github.com/terva-sh/terva)（[terva.sh](https://terva.sh/)）— `Copyright (c) Drew Short, Patric Eckhart`（MIT License）。本项目的五位芬兰语戏剧专家子代理命名（`Kertoja`、`Kartoittaja`、`Seppä`、`Toimittaja`、`Dramaturgi`）、`--play` / `--chat` 双模交互契约、`--dump-prompt` 注入帧透视、`Card-is-Data` 安全边界及 Card Doctor 静态体检理念参考自 Terva。
- 酒馆数据格式与生态标准引用：
  - [`SillyTavern/SillyTavern`](https://github.com/SillyTavern/SillyTavern)（[官方文档](https://docs.sillytavern.app/)，AGPL-3.0）— 本项目的角色卡、世界书（4 种 `selectiveLogic` 门控与 `@Depth` 深度注入）、`OpenAI Settings` 提示词装配栈、Regex 后置清洗与 `.jsonl` 聊天记录读写器均为基于公开数据格式规范的全新洁净室（Clean-Room）独立实现，**不包含、不分发任何受 AGPL-3.0 约束的 SillyTavern 源码副本**。
  - [`TavernAI/TavernAI`](https://github.com/TavernAI/TavernAI) — 致敬初代酒馆开创的 PNG `tEXt`（`chara`）角色卡元数据内嵌范式。
  - [`kwaroran/character-card-spec-v3`](https://github.com/kwaroran/character-card-spec-v3) — 遵循 Character Card V2（`chara`）与 V3（`ccv3`）开放规范。
- 社区致谢：本项目认可并致谢 [LINUX DO 社区](https://linux.do/)（[https://linux.do](https://linux.do)），感谢社区佬友在 AI Agent 工程实践与工具链上的交流分享与严谨审阅。
