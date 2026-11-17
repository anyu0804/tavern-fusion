# Tavern Fusion 宿主集成与真机验收清单 (`ACCEPTANCE.md`)

> 本清单定义了代码合入主分支后在 Google Antigravity 宿主环境内的同步步骤、真机交互验收项与日志排查入口。所有涉及用户主目录的路径统一以 `~` 表示。

---

## 1. 权威源同步与重启步骤

依据 `AGENTS.md` §1 双副本纪律，宿主实际从 `~/.gemini/config/plugins/tavern-fusion/` 拉起 `Tavern Stage` sidecar 与 `st-mcp`，严禁直接热改安装副本：

1. 在权威仓库完成提交并通过自动化回归测试：
   ```bash
   node --check sidecars/tavern_stage/ui/app.js
   node --check sidecars/tavern_stage/main.mjs
   node --check st_web_extension/st-ag-sync/index.js
   node scripts/run_py.mjs tests/test_ui_regressions.py
   node scripts/run_py.mjs tests/test_full_suite.py
   node scripts/run_py.mjs tests/test_tool_snapshots.py
   ```
2. 将权威仓库同步至全局插件安装副本：
   ```bash
   node scripts/run_py.mjs scripts/install_local.py --global-plugin
   ```
   （首次绑定或需更换酒馆目录时追加 `--st-root /path/to/SillyTavern`。）
3. 核对权威仓库与安装副本一致性（排除 `.git`、`tests`、`__pycache__`、`*.pyc`、`node_modules` 后应零差异）。
4. 在 Antigravity 中关闭并重新打开右侧 `Tavern Stage` 辅助面板（或重启宿主窗口以重新拉起 `main.mjs` 与 `st-mcp` 子进程）。

---

## 2. Tavern Stage 真机交互验收项

### 2.1 首次运行须知与未成年风险闸滚动可达性
- 将右侧 `Tavern Stage` 面板宽度缩窄至 `320px` 左右，或在 `150%` 系统缩放笔记本视口（高度约 `600px`）下触发 `#first-run-modal` 与 `#minor-risk-modal`。
- **预期**：弹窗卡片限制在视口高度内并支持内部纵向滚动，底部「我已知悉并继续」及风险闸操作按钮完整可见、可点击；旧 `v1` 已确认状态升级至 `v2` 时会重新展示新版条款提示，且小标题保持加粗排版。

### 2.2 草稿保护、原位楼层编辑与行内二次确认
- 在未确认首次须知、未配置完整 BYOK 或 BYOK 处于忙碌态时，在底部输入框填写多行文本并按 `Enter` 发送。
  - **预期**：弹出拦截提示，输入框内的草稿原文完整保留不丢失。
- 点击任意历史楼层的 `✏️` 进入原位编辑，输入未保存文本并等待多轮状态轮询（或在顶栏切换认知透光度档位）。
  - **预期**：编辑框内的未保存文本与光标选区不被轮询重绘冲掉；切换角色、切换存档或删除上方楼层时，编辑状态自动安全复位或修正下标，绝不错写其他楼层。
- 单击楼层 `🗑️`、分支 `✂️定稿` 或世界书「删除条目」。
  - **预期**：按钮先切换为行内二次确认态（如「确认删除？」），超时未点自动恢复，再次点击才执行删除（不调用 `window.confirm`）。

### 2.3 流式输出实时取消与未落盘恢复条
- 在自有 API（BYOK）流式生成期间（或会话处于 `stage_busy` 卡死态时），观察底部操作栏。
  - **预期**：显示 `#btn-cancel-turn`（「⏹ 停止」）；点击后立即调用 `cancel_turn` 终止生成或强制复位忙碌态，舞台流式草稿气泡随即清除，收尾阶段不出现正式楼层与草稿气泡并存的双重残影。
- 当 `Stop` 钩子因外部文件占用等原因写入失败并留下 `session.last_persistence_error.uncommitted_turn` 时：
  - **预期**：舞台顶部展示简洁的未落盘恢复提示条与「重试落盘」按钮；点击「重试落盘」调用 `retry_uncommitted_turn` 补写成功后提示条自动消失。

### 2.4 World Studio 完整槽位、启用开关与跨库隔离
- 在「世界书」Tab 打开含 `an_top`、`an_bottom`、`before_examples`、`after_examples`、`outlet` 等槽位的词条，修改正文并取消勾选「启用条目」后保存，重新载入该世界书。
  - **预期**：`#le-position` 完整保留原注入位置（不被重置为 `at_depth`），词条状态变为「⚪ 已停用」。
- 在「新世界书名」输入框填写新名称并保存词条后，直接点击列表中的「已启用/已停用」或进入编辑点「删除条目」。
  - **预期**：下拉框已同步选中新建世界书，启停与删除均准确作用于新世界书，不误改旧世界书。
- 在「世界书触发与语义召回模拟」输入测试语句并按 `Enter`（或点「测试召回」）。
  - **预期**：命中项清晰显示 `trigger` 触发路径（如 `keyword(...)`、`semantic(...)`、`constant`），且面板展示当前世界书静态体检隐患（如有）。

### 2.5 差分立绘词干解析与全链路防缓存
- 选用文件名词干与角色名不一致的角色卡（例如文件名为 `seraphina_v3.png`、卡内 `name` 为 `Seraphina`，差分立绘位于 `characters/seraphina_v3/`）。
  - **预期**：舞台立绘请求经 `mediaUrl()` 同时携带 `char`、`card` 与 `v=<currentEtag>` 参数并成功加载表情差分；`.json` 纯文本角色卡在舞台与卡库中直接显示内置 SVG 占位图，不触发 `/api/media` 的 `HTTP 415` 报错；隐藏状态下的「卡库」面板不会因每次对话轮询重复拉取整库大图。

### 2.6 Card Doctor 独立会话隔离与透视表单防冲刷
- 在已绑定活跃 RP 会话时，切至「卡库」点击「🔨 自动修复兼容项」。
  - **预期**：始终通过 `startConversation` 新开独立修卡会话，不向当前沉浸式 RP 对话混入 `/card-forge` 指令，也不覆盖舞台当前绑定的 `activeConvId`。
- 切至「透视」Tab 依次改动多个下拉框、复选框与文本框（暂不点保存），等待数次后台轮询。
  - **预期**：未保存的表单改动不被轮询重置；点击「保存配置」后大段中文 Author's Note 等多字节字符经 ASCII `\uXXXX` 安全序列化无损落盘。

---

## 3. 运行态日志与状态排查路径

- **Tavern Stage Sidecar 日志**：`~/.gemini/antigravity/sidecar_data/tavern-fusion/tavern_stage/logs/sidecar.log`
- **Sidecar 事件记录**：`~/.gemini/antigravity/sidecar_data/tavern-fusion/tavern_stage/events/`
- **插件运行态与会话存储**：`~/.gemini/antigravity/tavern-fusion/state/`
  - `active_session.json`（活跃角色、`rp_conv_id`、`stage_busy`、`last_persistence_error`）
  - `plugin_settings.json`（`last_st_data_root`、首次须知确认版本、策略档位、BYOK 配置）
  - `byok_stream_preview.json`（BYOK 流式实时预览临时文件）
- **宿主插件安装副本**：`~/.gemini/config/plugins/tavern-fusion/`
