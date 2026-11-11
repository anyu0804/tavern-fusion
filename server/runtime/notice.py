"""First-run notice shown once per notice version (Tavern Stage + `st_session_bootstrap`)."""

from __future__ import annotations

from typing import Any

FIRST_RUN_NOTICE_VERSION = 2

FIRST_RUN_NOTICE: dict[str, Any] = {
    "version": FIRST_RUN_NOTICE_VERSION,
    "title": "首次运行须知（Preview）",
    "items": [
        "条款与风险摘要：本工具仅通过宿主官方扩展点在本地读写酒馆数据，不提取凭证、不架反代、不做多租户。模型调用受宿主平台及上游模型服务商条款约束，内容政策与账号风险由使用者自行承担。",
        "建议使用专用账号：当前处于早期预览版（Preview），建议使用独立的专用/测试账号运行，避免在主力生产账号上加载未经审阅的外部预设或角色卡。",
        "不支持破限 / ERP：不支持、不建议导入越狱破限（Jailbreak）或色情露骨（NSFW / ERP）预设与角色卡；云端模型侧无法绕过服务端强制安全过滤。",
        "Antigravity 条款风险：Google Antigravity 附加条款第 6 条禁止“将本服务与非 Google 提供的产品结合使用”，违反可能导致 Antigravity 及 Gemini CLI 账号被暂停或终止。使用宿主内置模型运行酒馆角色扮演存在被认定违规的风险；如需规避，可在舞台配置中切换为“自有 API Key”后端。",
        "对话隐私：依据 Antigravity 条款，宿主会记录交互数据，Google 员工或承包商可能人工审阅，并可能用于改进产品和训练模型。角色扮演对话通常较为私密，请勿输入不希望被审阅的内容，并建议在 Antigravity 设置中关闭交互数据用于改进的选项（即使关闭，Google 仍会为提供服务和安全目的使用数据）。",
    ],
}

# Backwards-compatible alias (older imports / tests).
FIRST_RUN_NOTICE_V1 = FIRST_RUN_NOTICE
