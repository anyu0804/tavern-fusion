# tavern-fusion — 开发与部署纪律（Agent 必读）

> 本文件面向在本仓库工作的 AI Agent 与维护者。动代码前先读这里。

## 1. 权威源与运行副本（最高优先级）

- **本仓库是唯一权威源**（main 分支）。GitHub 远端：`anyu0804/tavern-fusion`。
- Antigravity 宿主实际运行的是**安装副本**：`~/.gemini/config/plugins/tavern-fusion/`。
  - sidecar（Tavern Stage）与 st-mcp 都由宿主从该副本拉起，**不是**从任何开发工作区拉起。
  - 修改运行行为的唯一正确路径：**先改本仓库 → 提交 → 拷贝同步到安装副本 → 重启 sidecar / 重开面板**。
  - **严禁只热改安装副本不回灌仓库**。2026-11-12 的"绿块头像"事故即由双副本分叉导致（安装副本带防缓存热修、仓库没有，且 git 历史从未包含该修复）。

## 2. 防缓存纪律（2026-11-12 事故后固化，不得回退）

背景：Antigravity 经 `plugin://` 自定义协议代理 sidecar UI。sidecar 启动窗口期，宿主可能对 `<img>` 子资源请求返回 256×256 纯色占位图——它是合法 PNG，**能成功解码、不触发 onerror**，旧 UI 的 srcKey 逻辑对未变化的 URL 永不重载，导致占位图永久卡死在舞台上。

固化规则：

1. `ui/app.js` 的 `mediaUrl()` 必须给所有媒体 URL 追加 `v=<currentEtag>` 缓存参数。
2. `main.mjs` 的 `computeDiskEtag`/`dirStamp` 签名必须包含目录 totalSize（同名同mtime不同内容的文件变更也能被感知）。
3. 媒体响应（`serveMediaAsset`）与页面路由（`/`、`/styles.css`、`/app.js`）一律 `Cache-Control: no-cache`。禁止给任何 UI 资产加 max-age。
4. UI 逻辑不得假设"img 失败会触发 onerror"——占位图是可解码的合法图片。

## 3. 本地环境事实（Windows）

- 酒馆数据根默认：`~/../../d/SillyTavern/SillyTavern/data/default-user`（即 `D:\SillyTavern\SillyTavern\data\default-user`），由插件状态文件 `last_st_data_root` 记忆；sidecar 数据根解析顺序见 `sidecars/tavern_stage/main.mjs` 的 `resolveStDataRoot`。
- sidecar 进程 = Antigravity.exe 以 Electron-as-Node 方式拉起（`--import=sidecar_sdk/register_loader.js`），日志在 `~/.gemini/antigravity/sidecar_data/tavern-fusion/tavern_stage/logs/sidecar.log`。
- 调试运行中的面板：Antigravity 的 CDP 端口在 `%APPDATA%/Antigravity/DevToolsActivePort`，Tavern Stage 面板是 `plugin://tavern-fusion--tavern_stage-<port>` iframe 目标，可用 `Runtime.evaluate` 只读探查。

## 4. 陈旧副本清理记录

以下散落副本已于 2026-11-12 逐一核验（无独有提交/修复，权威源全量领先）后**永久删除**：

- `D:\SillyTavern\tavern-fusion\` — git 旧快照，头提交 `a7cf070` 经核验为主线祖先且存在于 GitHub 远端，删除无历史损失
- `D:\WB-Workspaces\_archive\sidecar-legacy-tavern-fusion-20261111-130122\` — 2026-11-11 归档副本
- `%TEMP%\tavern-fusion-{inspect, review-20261111, fix-20261111-122453, isolate-20261111-130122, reinstall-20261111-124631}\` — 临时检查/修复/隔离/重装片段（`fix` 片段为删除时新发现，属同批产物一并清理）

再次发现新的散落副本时：先核对其角色与内容差异（有无独有提交、有无领先修复），确认无独有内容后删除，并更新本节记录。
