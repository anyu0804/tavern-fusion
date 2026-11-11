# Tavern Fusion 技术边界与永不实现清单 (`POLICY.md`)

Tavern Fusion 的工程定位严格限定为：**基于宿主官方插件扩展点（MCP、Lifecycle Hooks、`sidecar_sdk`）运行的本地 SillyTavern 数据格式（角色卡、世界书、`.jsonl` 聊天记录）读写与舞台编排工具**。

为保持纯粹的本地工具属性与技术中立边界，以下功能属于本项目的**永久非目标（Never-Implemented Features）**。核心仓库永不实现，任何试图引入下述能力或相关开关的 Issue 提议与 Pull Request 将**直接关闭，不予合并**。

---

## 一、永不实现的功能清单（Never-Implemented List）

1. **凭证读取、提取与中转（No Credential Access or Forwarding）**
   - 永不读取、解析、导出、持久化或转发宿主客户端的 OAuth 登录令牌（Access / Refresh Token）、Session Cookie、本地凭据数据库、系统钥匙串或进程内存凭证。
2. **反向代理与算力外泄（No Reverse Proxy or External API Wrapper）**
   - 永不架设将宿主内置模型包装为 OpenAI / Anthropic 兼容接口的 HTTP 反向代理服务，永不向外部第三方客户端、脚本或局域网/公网暴露推理调用端口。
3. **多账号轮询与多租户共享（No Multi-Account Pooling or Multi-Tenancy）**
   - 永不实现多账号自动切号、账号池轮询、配额聚合、限流规避调度或跨用户多租户共享机制。
4. **`passthrough` 无过滤透传模式（No Unfiltered `passthrough` Mode）**
   - 永不在宿主 MCP 服务与生命周期钩子（`PreInvocation` / `Stop`）路径中提供绕过本地输入侧检查的无过滤透传（`passthrough`）模式；任何未知或被篡改的策略值一律强制回落至默认受控档位。
5. **内容过滤关闭开关与破限注入（No Filter Kill-Switch or Jailbreak Presets）**
   - 永不提供通过环境变量、配置文件、命令行参数或 UI 控件关闭安全护栏或关闭 Card Doctor「未成年风险闸」的开关；永不内置、推荐或分发越狱破限（Jailbreak / DAN）、色情露骨（NSFW / ERP）及未成年/低幼化（`loli/shota` 等）预设与角色卡。云端模型侧受上游服务端强制安全过滤约束，客户端侧无法亦不会尝试绕过。

---

## 二、文档与沟通措辞准则（Communication Guardrails）

- 仓库文档、UI 文案、代码注释、Release 说明及 PR 描述中，**禁止使用**“免费无限”“无需 API Key”“白嫖订阅”“零成本调用商业模型”“绕过限制”等诱导性营销表述。
- 所有说明一律客观陈述本地数据读写范围、官方扩展点调用方式及使用者风险自担原则：**内容合规与账号风控风险均由使用者自行承担**，建议使用独立的专用/测试账号体验早期预览版本。

---

## 三、Pull Request 审查执行规则（PR Enforcement）

所有向本仓库提交的 Pull Request 必须完成 [`.github/PULL_REQUEST_TEMPLATE.md`](./.github/PULL_REQUEST_TEMPLATE.md) 中的边界核对清单：

- 触及上述「永不实现清单」任一条目的 PR，维护者将不做代码细节评审，直接标记拒绝并关闭。
- 修改 `server/st_runtime_compat.py`、`hooks/`、`mcp_config.json` 或 `sidecars/` 交互入口的 PR，必须保持全量自动化测试（`node scripts/run_py.mjs tests/test_full_suite.py`）通过，且不得削弱现有路径沙箱（`ensure_under_dir`）与策略回落逻辑。
