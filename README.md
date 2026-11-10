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

## 致谢、灵感来源与标准引用 (Acknowledgments & Citations)

### 1. LINUX DO（L站）社区致谢
本项目在构思与打磨过程中，深受 [**LINUX DO (linux.do)**](https://linux.do) 社区开源互助氛围的启发。谨依 L 站开源分享与致谢规范，向社区及各位佬友致以诚挚谢意：
- **酒馆提示词工程与「四重维度锁」沉淀**：感谢 L 站酒馆（SillyTavern）专区佬友们在 Gemini / Claude 模型驯服、原生 `system_instruction` 认知切断、`<START>` 对话样本词表收敛、世界书 `@Depth` 临界深度锁喉，以及前端 Regex 防出戏物理清洗等实践上的无私分享——本项目的「四重维度锁」与「维度调音台（Dimension Mixer）」正是将这些社区民间智慧工程化、可视化的结晶。
- **反重力（Antigravity）生态折腾与共创**：感谢 L 站反重力板块佬友们在 Google Antigravity IDE 扩展体系、MCP 工具链、Sidecar 面板及多模型协同玩法上的早期探路与经验沉淀。
- *“真诚、友善、团结、专业，共建你我引以为荣之社区。”* 欢迎各位 L 站佬友体验、拍砖、提 Issue 或 PR；若在社区二创、打包或分享，烦请保留本节上游与社区致谢说明。

### 2. 反重力 IDE / Coding Agent 酒馆先驱项目致谢
本项目从“在反重力 IDE 内嵌运行酒馆”的早期构想走向完整工作台，参考并致敬了以下先驱项目与原型：
- **[`terva-sh/terva`](https://github.com/terva-sh/terva)（[terva.sh](https://terva.sh/)，MIT License，作者 Drew Short & Patric Eckhart）**  
  将 Coding Agent Harness / IDE 与 SillyTavern 角色卡（`--card`）、世界书引擎同构融合的开创性项目。本项目的五位芬兰语戏剧专家子代理命名（`Kertoja`、`Kartoittaja`、`Seppä`、`Toimittaja`、`Dramaturgi`）、`--play` / `--chat` 双模契约、`--dump-prompt` 注入帧透视、`Card-is-Data` 零特权边界以及 Card Doctor 体检理念均直接致敬 Terva。
- **反重力 IDE × 酒馆 MCP 桥接探索（`st-antigravity-mcp` / `sillytavern-bridge` 及社区同路项目）**  
  本项目的第一代原型脱胎于面向 Google Antigravity IDE 的轻量级 MCP 桥接探索（`st-antigravity-mcp`），并参考了社区将 Coding IDE（Antigravity / Cursor / Claude Code）通过 MCP 与本地酒馆数据目录直连的开源实践（如 [`Chaunyzhang/sillytavern-mcp`](https://github.com/Chaunyzhang/sillytavern-mcp)、[`Inversity/SillyTavern-MCP-Servers-Universal`](https://github.com/Inversity/SillyTavern-MCP-Servers-Universal) 等），最终演进为自带右侧常驻 `Tavern Stage` 舞台与「维度调音台」的 **Tavern Fusion 3.0**。

### 3. SillyTavern 与 TavernAI 标准引用格式 (Standard Upstream Attribution)
本项目作为与酒馆生态数据格式深度互通的伴生插件，向酒馆生态的缔造者与维护者致敬。在文档、衍生项目或技术报告中引用酒馆上游生态与数据规范时，遵循以下标准引用格式：

- **SillyTavern（现行主流前端与高级提示词编排标准）**
  - **官方仓库**：[https://github.com/SillyTavern/SillyTavern](https://github.com/SillyTavern/SillyTavern)
  - **官方文档**：[https://docs.sillytavern.app/](https://docs.sillytavern.app/)
  - **核心维护团队**：Cohee, RossAscends, Wolfsblvt, and the SillyTavern Community
  - **开源协议与洁净室边界**：上游采用 `AGPL-3.0` 协议。**Tavern Fusion 3.0 采用 100% 洁净室（Clean-Room）独立实现（Python 3.12 + 原生 ES Module），不包含、不分发任何受 AGPL-3.0 约束的 SillyTavern 源码副本**，仅通过公开的文件规范读写用户本地的 `characters/*.png`、`worlds/*.json`、`chats/**/*.jsonl` 与 `OpenAI Settings/*.json`。
- **TavernAI（初代酒馆鼻祖与 PNG 角色卡范式奠基者）**
  - **官方仓库**：[https://github.com/TavernAI/TavernAI](https://github.com/TavernAI/TavernAI)
  - **致敬说明**：致敬 Hum 及早期贡献者开创的 PNG `tEXt`（`chara`）块内嵌角色元数据与经典酒馆交互范式。
- **Character Card V2 / V3 开放规范**
  - **规范仓库**：[https://github.com/kwaroran/character-card-spec-v3](https://github.com/kwaroran/character-card-spec-v3)（`chara_card_v2` / `chara_card_v3` 双区块无损读写标准）。

<details>
<summary>📄 <b>展开查看标准 BibTeX 引用格式（SillyTavern / TavernAI / Terva / Tavern Fusion）</b></summary>

```bibtex
@software{sillytavern,
  author       = {Cohee and RossAscends and Wolfsblvt and {SillyTavern Contributors}},
  title        = {{SillyTavern}: LLM Frontend for Power Users},
  url          = {https://github.com/SillyTavern/SillyTavern},
  note         = {Documentation: https://docs.sillytavern.app/},
  license      = {AGPL-3.0}
}

@software{tavernai,
  author       = {Hum and {TavernAI Contributors}},
  title        = {{TavernAI}: Atmospheric Adventure Chat for AI Language Models},
  url          = {https://github.com/TavernAI/TavernAI}
}

@software{terva,
  author       = {Short, Drew and Eckhart, Patric},
  title        = {{Terva}: Coding Agent Harness with Character Card and Lore Engine},
  url          = {https://github.com/terva-sh/terva},
  license      = {MIT}
}

@software{tavern_fusion,
  author       = {AnYu (安钰)},
  title        = {{Tavern Fusion 3.0}: Native SillyTavern Workbench and Dimension Mixer for Google Antigravity},
  year         = {2026},
  url          = {https://github.com/anyu0804/tavern-fusion},
  license      = {MIT}
}
```
</details>

完整第三方许可声明与洁净室合规说明见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。

---

## License

Released under the [MIT License](./LICENSE). Copyright (c) 2026 安钰 (AnYu).

---

## 常见问题排查 (Troubleshooting)

1. **Windows 中文系统用户名下宿主 Node 启动器报 `exit status 1`**
   - 若 Windows 宿主环境的 `~/.gemini/antigravity/bin/agy-node.cmd` 因 LF 换行符与代码页 936（GBK）解析中文路径发生错位，可将 `agy-node.cmd` 换行符转换为 `CRLF` 并使用 `%LOCALAPPDATA%` 纯 ASCII 环境变量路径。
2. **侧栏面板与工作区数据目录绑定**
   - 首次安装时传入 `--st-root /path/to/SillyTavern`（或在 `mcp_config.json` 中配置 `ST_USER_DATA_DIR`），插件会将数据根路径持久化记录于 `~/.gemini/antigravity/tavern-fusion/state/plugin_settings.json`，确保 MCP 服务、Lifecycle Hooks 与右侧 `Tavern Stage` 舞台面板共享同一份状态与存档目录。
