# Tavern Fusion 3.0.3-preview（预览版）— Antigravity × SillyTavern Native Workbench

> [!WARNING]
> **早期工程版本声明（Early Engineering Preview）**  
> 本项目目前处于**非常早期的工程探索版本**，可能还存在较多 Bug 或边缘环境兼容问题。如果你在使用过程中遇到任何故障，或者有想要补充的功能建议，欢迎直接在仓库提交 [**Issues**](https://github.com/anyu0804/tavern-fusion/issues)，我会尽可能持续跟进完善！

**当前版本：`v3.0.3-preview`（预览版）** · 更新内容见 [Release Notes](https://github.com/anyu0804/tavern-fusion/releases/tag/v3.0.3-preview)。

**Tavern Fusion 3.0.3-preview** 是一款面向 **Google Antigravity (AGY)** 的插件，用于本地编排 SillyTavern 数据与开展角色扮演。它在不修改 SillyTavern 源码、不开启高权限 `enableServerPlugins` 的前提下，使用宿主官方插件扩展点（MCP 与生命周期钩子，以及 `sidecar_sdk`）直接读写本地酒馆数据目录（`characters/*.png`、`worlds/*.json`、`chats/<avatar_stem>/*.jsonl`、`OpenAI Settings/*.json`、`settings.json`），并在 IDE 右侧辅助面板提供常驻可视化的 **Tavern Stage** 剧场工作台。

---

## 核心特性

1. **维度调音台（Dimension Mixer）与四层上下文约束**
   - 每轮按当前档位动态重装完整提示词栈（不冻结旧前缀后只在尾部追加补丁），并在 Tavern Stage 舞台首屏提供三项实时开关：
   - **开关 A · 认知透光度 / 第四面墙感知（顶层 `system_instruction` 模板切换）**：
     - **0% 深潜封闭（默认）**：启用四层上下文约束。① 顶层注入角色世界观认知边界提示词（屏蔽现代科技、代码及 AI 助手身份认知，禁止全知视角）；② 将角色卡 `<START>` 对话示例（`mes_example`）拆分为独立 `<START_LEXICAL_SAMPLE>` 少样本（few-shot）块保留在提示词中锚定语感；③ 世界书与 Author's Note 按 `@Depth` 深度直接插入对应历史楼层位置；④ 提交入库前执行内置防出戏正则组（`BUILTIN_IMMERSION_GUARD_REGEX`），自动剔除“（场外：...）”与 `[OOC: ...]`，以及常见 AI 开场或收尾套话。
     - **50% 隐秘默契**：保留世界观设定、少样本示例与 `@Depth` 插入位置，不直接输出移动互联网或代码术语，但允许角色在设定语境下以双关或隐喻回应玩家的场外暗示。
     - **100% 双轨化身**：顶层切换为“保留角色性格与说话习惯的全知共创模式”，放开现代知识与代码视野，可同时兼顾角色陪伴与剧本或技术讨论。
   - **开关 B · Agentic 权能释放（回合级工具调用门禁）**：
     - **纯对白（关闭）**：在绑定的 RP 会话生成回合中，通过 `PreInvocation` 提示词约束与 `PreToolUse` 钩子（按 `conversationId` 精确隔离，不影响其他并行工程会话调用工具）拦截工具调用，避免模型中途误调工具打断叙事。
     - **权能释放（开启）**：允许模型在演绎过程中按需调用 `st_manage_variables`（更新好感或线索变量）或 `st_evolve_lore_entry`（将新剧情写入世界书）。
   - **开关 C · 超级主控 / 导演穿梭键（`Alt+D` + 场外隔离标记 Backstage Quarantine）**：
     - 一键切入后台导演模式，直接指令模型改写楼层、调整角色卡设定或维护世界书。
     - **场外隔离标记（`extra.is_backstage: true`）**：导演模式下的讨论楼层自动写入 `is_backstage: true` 并在舞台时间线上折叠为 **「后台导演便签」**；切回 0% 或 50% 档位后，提示词装配器与世界书扫描器自动跳过这些场外楼层，避免代码或修卡指令混入后续正式剧情上下文。

2. **常驻右侧剧场工作台 (Tavern Stage Aux Pane Sidecar)**
   - 基于宿主官方 `sidecar_sdk` 构建，通过 `window.sidecar.agent.sendMessage` / `startConversation` 唤醒 Antigravity 内置模型推进回合、重抽平行分支（Swipes）或生成回复参考。
   - **立绘与情绪差分沙箱**：自动检索 `characters/<AvatarStem>/*.png` 差分表情并随最新楼层情绪切换；所有媒体响应均经过二进制栅格文件头（Magic-Byte）与 `realpath` 目录边界校验，拒绝返回伪装为 `.png` 的 SVG/HTML 内容（HTTP 415）。
   - **楼层修订与分支管理**：支持楼层原地编辑和删楼（自动留存 `.bak` 备份），可一键切换「场外隔离」标记、切换或裁剪 Swipe，也能从任意历史楼层分叉（Fork）出新的 `.jsonl` 存档。
   - **玩家导演杆（Player Director Lever）**：提供「回复灵感 x3」与「意图扩写」，生成主角视角的候选台词并经 `st_publish_stage_suggestions` 回填至输入框。

3. **零第三方依赖 Python 控制面、模块化运行时与并发写盘保护**
   - 通过 `scripts/run_py.mjs` 自动跳过 Windows `WindowsApps\python3.exe` 商店占位程序，基于 Python 3.10–3.13 标准库即可运行。
   - 控制面按职责拆分为 `server/protocol/`（JSON-RPC stdio）和 `server/runtime/`（`RuntimeContext` 与 `SessionStore` 单一会话状态入口），并由 `server/tools/`（14 个 MCP 工具注册表 + `inputSchema.required` 参数校验 + Sidecar 操作）、`server/policy/`（合规护栏与未成年风险闸入口）以及 `server/prompt/`（宏与预设、正则管线及提示词帧编译器）分别承担对应模块；生命周期钩子在常规工程会话路径下零导入重型编解码与向量模块。
   - SQLite 稀疏/稠密语义向量缓存采用懒加载与短连接模式避免句柄泄漏；`.jsonl` 聊天记录和世界书，以及 `active_session.json` 与 `settings.json` 全局变量，均采用跨平台文件锁与 `os.replace` 原子替换，锁范围覆盖完整的「读-改-写（RMW）」周期，并对损坏 JSON 行报错阻断以防静默丢楼或并发丢更新。

4. **兼容酒馆常用数据格式与 Card Doctor 静态体检**
   - 解析并同步双写 PNG `chara`（CCv2）+ `ccv3`（CCv3）数据块，落实 **Card-is-Data** 字段清洗。
   - 支持按头像文件名（`avatar_stem`）对齐 SillyTavern `chats/` 目录，兼容 `settings.json` 全局变量（`extension_settings.variables.global`）和全局世界书（`world_info_settings.world_info.globalSelect`），支持 JS 风格正则捕获组替换（`$1` / `$&`）、`OpenAI Settings/*.json` 的 `prompt_order` 顺序栈，以及 4 种 `selectiveLogic` 次级词门控。
   - 内置 **Card & Lore Doctor (`st_lint_card_and_lore`)**：除了静态检查缺失 `ccv3` 块或 `<START>` 示例、未带 `{{original}}` 覆盖预设的 `system_prompt`，以及 `selective: false` 次级词失效与残缺宏之外，还内置了 **未成年风险闸（`evaluate_minor_risk_gate`）**。该风险闸会扫描年龄字段与文本（`<18` 岁）、学年/校服/低龄身高阈值，以及 `loli/shota` 类标签和“外表看起来未成年/合法萝莉”等启发式特征；命中高危或多项叠加时**默认拒绝导入与装载**，单一模糊特征触发**强制红屏警告**。

5. **内置双档内容安全护栏 (`policy_mode`)**
   - `host_gentle`（默认）：对绝对安全红线执行零 Token 硬熔断；对暗黑奇幻或悬疑冲突题材附加温和的边界内敛引导，不误伤正常文学创作。
   - `strict_block`：对绝对安全红线与成人敏感边界词统一执行零 Token 硬熔断。
   - 宿主 MCP 模式下**不开放**环境变量或配置文件级的无过滤透传（`passthrough`）开关，任何未识别或篡改的配置值均自动回落至 `host_gentle`（注：本地护栏仅作输入侧基础前置拦截，无法替代或绕过云端模型服务端的强制过滤）。

6. **可选“自有 API Key”生成后端（BYOK）**
   - 在 Tavern Stage 的「文风预设、Author's Note 与内容边界」中把「生成后端」切换为「自有 API Key」，回合不经过宿主内置模型，直接把同一份 `ST_RUNTIME_FRAME` 发送到任意 OpenAI 兼容的 `/chat/completions` 接口（默认 Google AI Studio 的 Gemini OpenAI 兼容端点 `https://generativelanguage.googleapis.com/v1beta/openai`），并直接落盘到 `.jsonl`。
   - **密钥只从环境变量 `TAVERN_BYOK_API_KEY` 读取**（或你手动创建的插件状态目录 `byok_api_key` 文件第一行）；插件不保存、不回显，也不上传密钥。远程端点强制 `https://`。可选环境变量包括 `TAVERN_BYOK_BASE_URL` 与 `TAVERN_BYOK_MODEL`。
   - 当前版本支持「发送回合」与「重抽（Swipe）」；回复灵感和意图扩写，以及 Kartoittaja 分支与导演模式仍需宿主后端，BYOK 模式下不会静默回退到宿主模型。
   - **前缀缓存模式（可选，默认关闭）**：在同一面板把「前缀缓存模式」设为开启后，请求会拆成「静态前缀 system + 只追加的历史轮次 + 末尾动态块」，便于 DeepSeek、OpenAI 式接口和 Gemini 隐式缓存等按前缀自动缓存的服务复用前文。代价是不再严格还原酒馆插入位置：关键词触发的世界书、`@Depth` 词条和 Author's Note 会移到末尾，`{{time}}`、`{{date}}`、`{{getvar}}` 在前缀中改为指向末尾的实时值，历史按每 12 条整块截断。面板会显示上一回合接口返回的缓存命中 token 数（接口不返回时只显示输入 token 数）。实际命中率和折扣取决于服务商与中转站。
   - 「附加 Claude 式 cache_control 断点」只在你的网关明确支持该字段时开启，部分严格的 OpenAI 兼容接口会因此报错。
   - **流式输出**：自有 API 模式默认用流式请求，舞台边生成边显示正文，生成完成后才写入 `.jsonl`；中途出错不会落盘半截回复。接口不接受流式请求时自动退回一次普通请求。可在同一面板关闭。
   - 宿主模型模式下回复由 Antigravity 生成，舞台拿不到中间结果，因此改为在回复提交后用打字机效果逐字展开（点击正文立即显示全文，可在面板关闭）。

7. **浏览器端实时同步扩展（`st-ag-sync`，可选）**
   - 安装至 SillyTavern 的 `extensions/third-party/st-ag-sync/` 后（无需 `enableServerPlugins`），Antigravity 侧每提交一层，打开中的酒馆标签页会自动重新载入聊天。变更检测走 `/api/chats/search` 增量比对（比对消息条数、时间与预览，不整文件下载）；轮询频率自适应，出错时带抖动退避，标签页隐藏或生成期间自动暂停，重新可见时立即复查。
   - 服务端每次写盘都会轮换 `chat_metadata.integrity` 来防止误覆盖，两者配合构成「过期标签页拒绝静默覆盖 + 恢复可见自动刷新」的双向保护（详见下方已知限制第一条）。

---

## 专家子代理组（Specialist Subagents）与 Slash 技能

| 组件 | 名称 | 职责说明 |
| :--- | :--- | :--- |
| Subagent | `kertoja-director` (**Kertoja**) | 叙事主导演，把控角色声线、感官张力与 `*动作*` / `"台词"` 排版纪律 |
| Subagent | `kartoittaja-brancher` (**Kartoittaja**) | 剧情分支测绘师，为当前楼层构思 3 条平行 Swipe，保持角色性格自洽而剧情走向不同 |
| Subagent | `seppa-cardsmith` (**Seppä**) | CCv2/CCv3 角色卡锻造与 Card Doctor 修复专家 |
| Subagent | `toimittaja-state` (**Toimittaja**) | 状态审计官，维护 `/getvar` 聊天变量、好感/线索追踪与长线记忆摘要 |
| Subagent | `dramaturgi-lore` (**Dramaturgi**) | 世界书织命师，管理 `worlds/*.json` 条目演化、门控逻辑与 `@D` 深度预算 |
| Skill | `/rp` | 启动或恢复指定角色扮演会话并联动 Tavern Stage |
| Skill | `/tavern-stage` | 打开右侧 Tavern Stage 工作台面板或导出 CSP 合规的 `/generative_ui` 快照 |
| Skill | `/card-forge` | 运行角色卡无损拆解、Card Doctor 体检与双区块 PNG 铸造 |
| Skill | `/lore-weaver` | 演化世界书条目并模拟混合语义召回 |
| Skill | `/dump-prompt` | 透视当前回合 `prompt_order` 拼装栈、激活词条与 Prefix Cache 命中率 |

---

## 快速挂载与验证

```bash
# 1. 安装至全局 ~/.gemini/config/plugins/tavern-fusion 并绑定本地 SillyTavern 数据目录
#    （全局安装会自动清理同级工作区重复的 .agents/plugins.json 挂载项，避免双重触发 Hook）
node scripts/run_py.mjs scripts/install_local.py --global-plugin --st-root /path/to/SillyTavern

# 2. (二选一，勿与全局安装混用) 仅注册到指定工作区 (.agents/plugins.json) 并生成示例角色卡 Seraphina.png
node scripts/run_py.mjs scripts/install_local.py --workspace /path/to/workspace

# 3. 运行全量自动化验收套件与 14 工具黄金特征快照测试
node scripts/run_py.mjs tests/test_full_suite.py
node scripts/run_py.mjs tests/test_tool_snapshots.py
```

---

## 已知限制（Known Limitations）

- **与正在运行的 SillyTavern 同时编辑同一存档**：SillyTavern 把当前聊天保存在浏览器内存中，保存时整文件重写。Tavern Fusion 每次写盘都会轮换 `chat_metadata.integrity`，因此旧标签页再保存时会被 SillyTavern 自身的完整性校验拒绝并弹出“是否覆盖”提示，此时**请选择不覆盖并重新载入聊天**，否则 Antigravity 侧写入的楼层会被覆盖。安装 `st-ag-sync` 扩展可在标签页重新可见时自动重新载入。
- **宿主环境依赖**：插件依赖 Google Antigravity 2.0+ 的 Lifecycle Hooks（`SessionStart` / `PreInvocation` / `PreToolUse` / `Stop`）与 `sidecar_sdk` 右侧面板协议，无法脱离 Antigravity 在普通独立 MCP 客户端中直接获得完整的舞台联动与自动落盘体验。
- **单活跃舞台状态**：`active_session.json` 当前设计为维护单个绑定 RP 会话（`rp_conv_id`）；其他工程对话与该 RP 会话完全隔离，但暂不支持在同一工作区内同时开启两个不同角色的 RP 会话并自动落盘。
- **SillyTavern 特性覆盖范围**：目前已覆盖主流单人角色卡（CCv2/CCv3）和世界书门控，支持 `OpenAI Settings` 顺序栈与常用宏、正则替换，但尚未完整复刻 SillyTavern 前端全部 STscript 斜杠脚本命令、复杂群聊发言调度器及第三方前端 UI 扩展。

---

## 常见问题排查 (Troubleshooting)

1. **Windows 中文系统用户名下宿主 Node 启动器报 `exit status 1`**
   - 若 Windows 宿主环境的 `~/.gemini/antigravity/bin/agy-node.cmd` 因 LF 换行符与代码页 936（GBK）解析中文路径发生错位，可将 `agy-node.cmd` 换行符转换为 `CRLF` 并使用 `%LOCALAPPDATA%` 纯 ASCII 环境变量路径。
2. **侧栏面板与工作区数据目录绑定**
   - 首次安装时传入 `--st-root /path/to/SillyTavern`（或在 `mcp_config.json` 中配置 `ST_USER_DATA_DIR`），插件会将数据根路径持久化记录于 `~/.gemini/antigravity/tavern-fusion/state/plugin_settings.json`，确保 MCP 服务、Lifecycle Hooks 与右侧 Tavern Stage 舞台面板共享同一份状态与存档目录。

---

## 架构边界、安全隔离与免责声明 (Technical Boundaries & Disclaimer)

### 接入边界、预设限制与风险承担 (Integration Scope & Risk Allocation)

* **技术接入边界**：我们**不提取/不中转凭证、不架反代、不做多租户；使用官方插件扩展点**（插件清单与本地 `stdio` MCP 服务、生命周期钩子及 `sidecar_sdk` 面板接口）在本地运行；**内容政策与账号风险由使用者承担**（建议使用独立的专用或测试账号体验预览版本；首次运行将弹出一次性风险与边界告知）。
* **预设与内容限制**：**不支持、不建议导入破限/NSFW 预设，云端模型侧无法绕过服务端强制过滤**。导入越狱破限（Jailbreak / DAN）或色情露骨（NSFW / ERP）等违规预设与角色卡无法突破云端模型服务端的安全拦截，还可能直接触发上游平台风控。[server/policy/compliance.py](https://github.com/anyu0804/tavern-fusion/blob/main/server/policy/compliance.py) 默认启用 `host_gentle` 本地前置护栏（宿主 MCP 模式仅保留 `host_gentle` 与 `strict_block` 两档，不提供关闭护栏的 `passthrough` 开关），该机制仅用于本地输入侧自检，**不构成对任何上游平台合规性或账号安全的保证**。
* **永不实现清单与 PR 红线**：本项目永久拒绝实现凭证读取、反向代理与多账号轮询，也不提供 `passthrough` 透传或内容过滤关闭开关，相关 PR 一律不予合并；详见 **[POLICY.md](./POLICY.md)** 与 [.github/PULL_REQUEST_TEMPLATE.md](./.github/PULL_REQUEST_TEMPLATE.md)。

### Antigravity 条款与对话隐私 (Antigravity Terms & Conversation Privacy)

* **条款风险**：[Google Antigravity 附加条款](https://antigravity.google/terms)第 6 条禁止“将本服务与非 Google 提供的产品结合使用”，违反可能导致 Antigravity 及 Gemini CLI 账号被暂停或终止。使用宿主内置模型运行 SillyTavern 角色扮演存在被认定违规的风险，本项目无法消除这一风险；如需规避，请使用上文的「自有 API Key」后端。
* **交互数据会被记录**：依据同一条款，宿主会记录交互数据，Google 员工或承包商可能人工审阅，并可能用于改进产品和训练模型。角色扮演对话通常较为私密，请勿输入不希望被审阅的内容，并建议在 Antigravity 设置中关闭交互数据用于改进的选项（即使关闭，Google 仍会为提供服务和安全目的使用数据）。

### 本地数据安全与沙箱隔离 (Local Data & Sandbox Safety)

* **Card-is-Data 零特权原则**：外部导入的角色卡与世界书均被视为不可信纯数据，在解析阶段自动剥离潜在的命令执行钩子与危险扩展字段。
* **文件系统边界锁定**：所有文件读写均受 [server/fs_safety.py](https://github.com/anyu0804/tavern-fusion/blob/main/server/fs_safety.py) 目录包含校验（`ensure_under_dir`）与生命周期拦截钩子保护，严格限制在本地回环地址（`127.0.0.1`）以及用户显式配置的本地 `ST_DATA_ROOT` 目录内。

### 开源许可证与洁净室隔离声明 (Open-Source Licensing & Clean-Room Notice)

本项目核心代码基于 [MIT License](https://github.com/anyu0804/tavern-fusion/blob/main/LICENSE) 开源，完整第三方致谢与协议文本详见 [THIRD_PARTY_NOTICES.md](https://github.com/anyu0804/tavern-fusion/blob/main/THIRD_PARTY_NOTICES.md)。

| 关联生态 / 规范 | 上游许可证 / 性质 | 本项目集成与隔离方式 |
| :--- | :--- | :--- |
| [SillyTavern](https://github.com/SillyTavern/SillyTavern/blob/release/LICENSE) | AGPL-3.0 | **洁净室（Clean-Room）零源码引用**：未复制、修改或捆绑任何 SillyTavern 前后端源码；仅基于公开文件结构独立使用 Python 标准库与原生 ESM 实现本地 `.jsonl` / 世界书读写及本地 HTTP 同步（[st-ag-sync](https://github.com/anyu0804/tavern-fusion/tree/main/st_web_extension/st-ag-sync) 未静态导入任何 AGPL 内部模块）。 |
| [Terva (terva-sh/terva)](https://github.com/terva-sh/terva) | MIT | 致谢并沿用其芬兰语叙事专家角色命名体系与上下文分层理念，所有业务代码均在 [Tavern Fusion](https://github.com/anyu0804/tavern-fusion) 内以 Python / ESM 独立重写，并完整保留上游 MIT 版权声明。 |
| [Character Card Spec V2 / V3](https://github.com/kwaroran/character-card-spec-v3) | 公开格式规范 | 依据公开规范利用 Python 标准库（`struct` / `zlib` / `base64` / `json`）独立实现 PNG `tEXt` / `zTXt` / `iTXt` 双区块无损读写。 |
| 开源字体栈（Noto Sans SC / Source Han Sans SC / Inter / JetBrains Mono） | SIL OFL 1.1 | 前端面板与样式表仅声明 SIL Open Font License 1.1 开源字体及 W3C 标准通用族关键字（`system-ui`、`sans-serif`、`ui-monospace`、`monospace`），不内嵌、不引用微软雅黑（Microsoft YaHei）等商业专有字库，无商业字体侵权风险。 |

### 商标声明与免责条款 (Trademark & Liability Disclaimer)

* **非官方关联声明 (Non-Affiliation)**：Tavern Fusion 是由社区独立开发的开源工程项目，**与 Google LLC（含 Google Antigravity 团队）、SillyTavern 官方团队或 Anthropic PBC 不存在任何隶属与合作关系，也未获得其授权、赞助或官方背书**。文档中提及的 Google Antigravity 和 SillyTavern、Claude 等产品或项目名称仅用于客观说明技术兼容性与运行环境，相关商标及名称权利均归其各自所有者所有。
* **使用风险自担 (Use at Your Own Risk)**：本软件按 [MIT License](https://github.com/anyu0804/tavern-fusion/blob/main/LICENSE) 规定的“原样（AS IS）”提供，不附带任何明示或暗示担保。使用者须自行确保所加载的角色卡、世界书与提示词预设，以及对话内容符合所在地法律法规，并遵守所接入模型服务商的内容与服务政策；因使用本插件或导入外部预设/角色卡、触发上游平台风控而导致的账号受限、配额冻结或数据损失，均由使用者自行承担，项目作者与贡献者概不承担责任。

*Non-Affiliation Disclaimer: Tavern Fusion is an independent, community-driven open-source project and is not affiliated with, endorsed by, or sponsored by Google LLC, the SillyTavern project, or Anthropic PBC. All product names and trademarks mentioned in this repository are the property of their respective owners and are used solely to describe interoperability.*

---

## 开源协议与社区致谢

- 本仓库原创插件代码与 Sidecar 前端，以及脚本和架构文档采用 [MIT License](./LICENSE) 开源（Copyright (c) 2026 安钰 (AnYu)）。
- 架构与戏剧专家组设计参考并致谢以下开源项目（完整许可声明与上游 `LICENSE` 原文见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)）：
  - [`terva-sh/terva`](https://github.com/terva-sh/terva)（[terva.sh](https://terva.sh/)），Copyright (c) Drew Short, Patric Eckhart（MIT License）。本项目的五位芬兰语戏剧专家子代理命名（Kertoja / Kartoittaja / Seppä / Toimittaja / Dramaturgi）与 `--play` / `--chat` 双模交互契约，以及 `--dump-prompt` 注入帧透视、Card-is-Data 安全边界和 Card Doctor 静态体检理念参考自 Terva。
- 酒馆数据格式与生态标准引用自以下项目与规范：
  - [`SillyTavern/SillyTavern`](https://github.com/SillyTavern/SillyTavern)（[官方文档](https://docs.sillytavern.app/)，AGPL-3.0）：本项目的角色卡与世界书（4 种 `selectiveLogic` 门控与 `@Depth` 深度注入）、`OpenAI Settings` 提示词装配栈，以及 Regex 后置清洗和 `.jsonl` 聊天记录读写器均依据公开数据格式规范，以洁净室（Clean-Room）方式全新独立实现，**不包含、不分发任何受 AGPL-3.0 约束的 SillyTavern 源码副本**。
  - [`TavernAI/TavernAI`](https://github.com/TavernAI/TavernAI)：初代酒馆开创了在 PNG `tEXt`（`chara`）块内嵌角色卡元数据的做法，特此致敬。
  - [`kwaroran/character-card-spec-v3`](https://github.com/kwaroran/character-card-spec-v3)：遵循 Character Card V2（`chara`）与 V3（`ccv3`）开放规范。
- 社区致谢：本项目认可并致谢 [LINUX DO 社区](https://linux.do/)（https://linux.do），感谢社区佬友在 AI Agent 工程实践与工具链上的交流分享与严谨审阅。
