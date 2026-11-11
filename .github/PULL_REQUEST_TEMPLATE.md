## 变更说明 (Summary)

<!-- 简述本次 PR 解决的问题或改进的技术细节 -->

## 验证方式 (Verification)

- [ ] 已在本地运行并通过全量测试套件：`node scripts/run_py.mjs tests/test_full_suite.py`
- [ ] 已在本地运行并通过特征快照测试：`node scripts/run_py.mjs tests/test_tool_snapshots.py`

## 技术中立与边界红线核对 (Mandatory Policy Checklist)

> 提交前请逐项核对 [`POLICY.md`](../POLICY.md)。触碰以下任一红线的 PR 将直接关闭不予合并：

- [ ] **无凭证读取或中转**：未读取、提取、落盘或转发任何宿主 OAuth Token、Cookie 或本地登录态凭证。
- [ ] **无反向代理或外部网关**：未将宿主会话包装为供外部程序调用的 HTTP 反代或兼容 API 端口。
- [ ] **无多账号或多租户逻辑**：未引入多账号轮询、切号池化、限流规避或多租户共享功能。
- [ ] **无 `passthrough` 或内容过滤开关**：未添加 `passthrough` 透传模式，未提供任何通过环境变量、配置文件或 UI 关闭安全护栏的开关，未附带破限（Jailbreak）或 NSFW/ERP 预设及角色卡。
- [ ] **无诱导性营销措辞**：新增或修改的文档、UI 微文案与注释中不含“免费无限”“无需 API Key”“白嫖订阅”等表述。
