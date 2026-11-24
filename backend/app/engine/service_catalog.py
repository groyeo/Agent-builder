"""外部服务注册表：把"需求里提到的能力"翻译成"用户要去哪注册、填哪个变量"。

这是把"我需要什么"讲清楚的底座。规则很简单：
关键词命中 → 出一张资源卡（注册入口 + 步骤 + 免费额度 + 缺失后果）。

刻意不做的事：不让模型现编注册流程。注册流程是**事实**，模型编出来的
URL 和步骤看起来合理但会把人带到 404，那比没有清单更糟。
因此这里是一张人工维护的表，命中不了的服务会退化成"待补充"条目并明确标注。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceEntry:
    key: str
    name: str
    kind: str
    keywords: tuple[str, ...]
    env_vars: tuple[str, ...]
    signup_url: str
    steps: tuple[str, ...]
    cost: str
    fallback: str
    example: str = ""
    optional: bool = False
    env_defaults: tuple[tuple[str, str], ...] = ()


# 顺序即输出顺序：模型 → 数据/知识 → 业务系统 → 基础设施
CATALOG: tuple[ServiceEntry, ...] = (
    ServiceEntry(
        key="llm",
        name="大模型服务（唯一硬依赖）",
        kind="llm",
        keywords=(),  # 恒定注入，不靠关键词
        env_vars=("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"),
        signup_url="https://platform.deepseek.com/api_keys",
        steps=(
            "打开注册页，用手机号或邮箱注册（国内可直接访问）",
            "左侧「API keys」→ 创建 API Key → 复制（只显示一次）",
            "充值少量额度（10 元够调试很久），或换用其他 OpenAI 兼容服务",
            "把 Key 填进 .env 的 OPENAI_API_KEY；Base URL 保持 https://api.deepseek.com/v1；模型填 deepseek-chat",
        ),
        cost="按 token 计费；调试期通常几元级。任何 OpenAI 兼容服务（通义 / Kimi / 智谱 / 本地 Ollama）都能替换。",
        fallback="没配 Key 时程序启动即报「缺少 OPENAI_API_KEY」，这是唯一无法降级的依赖。",
        example="Key 形如 sk-xxxx…（只显示一次，丢了就重新创建一个）",
        env_defaults=(
            ("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
            ("OPENAI_MODEL", "deepseek-chat"),
        ),
    ),
    ServiceEntry(
        key="amap",
        name="高德地图开放平台 Key",
        kind="api_key",
        keywords=("地图", "导航", "路线", "经纬度", "定位", "POI", "地址解析", "周边", "行程规划", "景点"),
        env_vars=("AMAP_API_KEY",),
        signup_url="https://console.amap.com/dev/key/app",
        steps=(
            "注册高德开放平台账号（个人即可，需实名认证，1 个工作日左右）",
            "控制台 →「应用管理」→ 创建新应用（名称随意，类型选出行/生活服务）",
            "在应用下「添加 Key」，服务平台选 **Web 服务**（不是 Web端 JS API）",
            "复制 Key 填进 .env 的 AMAP_API_KEY",
        ),
        cost="个人开发者有免费日配额（地理编码/路径规划数千次/日），调试阶段基本用不完。",
        fallback="地图类工具会抛 NotImplementedError，规划和展示降级为纯文本。",
        example="32 位十六进制字符串",
    ),
    ServiceEntry(
        key="tencent_map",
        name="腾讯位置服务 Key",
        kind="api_key",
        keywords=("腾讯地图", "微信小程序地图"),
        env_vars=("TENCENT_MAP_KEY",),
        signup_url="https://lbs.qq.com/dev/console/application/mine",
        steps=(
            "注册腾讯位置服务账号并完成实名认证",
            "控制台 →「应用管理」→ 创建应用 → 添加 Key",
            "启用「WebServiceAPI」，勾选需要的接口（地点搜索/路线规划）",
            "复制 Key 填进 .env 的 TENCENT_MAP_KEY",
        ),
        cost="个人开发者每日免费配额；商用需按调用量付费。",
        fallback="地图工具不可用，输出退化为文字行程。",
    ),
    ServiceEntry(
        key="baidu_map",
        name="百度地图开放平台 AK",
        kind="api_key",
        keywords=("百度地图", "百度导航"),
        env_vars=("BAIDU_MAP_AK",),
        signup_url="https://lbsyun.baidu.com/apiconsole/key",
        steps=(
            "注册百度地图开放平台（需实名认证）",
            "控制台 →「应用管理」→ 创建应用，类型选「服务端」",
            "勾选「地点检索」「路线规划」等接口，提交后得到 AK",
            "把 AK 填进 .env 的 BAIDU_MAP_AK",
        ),
        cost="个人认证开发者有免费日配额。",
        fallback="地图工具不可用。",
    ),
    ServiceEntry(
        key="serper",
        name="Serper 网页搜索 API",
        kind="api_key",
        # 注意：不要用「搜索」「检索」这种泛词 —— 内部知识库检索会被误判成需要联网搜索，
        # 结果用户白注册一个用不上的 Key。只认明确的"去网上找"语义。
        keywords=(
            "联网搜索",
            "网页搜索",
            "搜索引擎",
            "网上查",
            "实时信息",
            "最新动态",
            "竞品",
            "舆情",
            "查资料",
            "互联网搜索",
        ),
        env_vars=("SERPER_API_KEY",),
        signup_url="https://serper.dev",
        steps=(
            "打开 serper.dev，用 Google 账号登录",
            "进入 Dashboard，直接看到 API Key（免费 2500 次查询，无需绑卡）",
            "复制 Key 填进 .env 的 SERPER_API_KEY",
        ),
        cost="免费 2500 次；之后约 $50/5 万次。",
        fallback="搜索工具抛 NotImplementedError，Agent 只能依靠模型已有知识。",
    ),
    ServiceEntry(
        key="tavily",
        name="Tavily 搜索 / 网页抓取 API",
        kind="api_key",
        keywords=("网页抓取", "网页正文", "深度检索", "内容抽取"),
        env_vars=("TAVILY_API_KEY",),
        signup_url="https://app.tavily.com",
        steps=(
            "注册 Tavily 账号（邮箱即可）",
            "登录后在首页复制 API Key（每月 1000 次免费）",
            "填进 .env 的 TAVILY_API_KEY",
        ),
        cost="每月 1000 次免费额度。",
        fallback="网页抓取不可用，检索结果只剩标题与摘要。",
    ),
    ServiceEntry(
        key="smtp",
        name="邮件发送账号（SMTP 授权码）",
        kind="credential",
        keywords=("邮件", "发信", "发送邮件", "邮件草稿", "通知邮件", "周报邮件"),
        env_vars=("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"),
        signup_url="https://mail.qq.com",
        steps=(
            "用你的常用邮箱登录网页版（QQ 邮箱 / 163 / 企业邮箱均可）",
            "QQ 邮箱：「设置 → 账号」开启「POP3/SMTP 服务」；163：「设置 → POP3/SMTP/IMAP」开启",
            "按提示获取**授权码**（不是邮箱登录密码），复制保存",
            "填进 .env：SMTP_HOST=smtp.qq.com、SMTP_PORT=465、SMTP_USER=你的邮箱、SMTP_PASSWORD=授权码",
        ),
        cost="免费（用自己已有邮箱）。",
        fallback="仅生成邮件草稿写入本地文件，不实际发送 —— 先跑通流程再补授权码也可行。",
        example="SMTP_PASSWORD 填的是授权码，不是邮箱登录密码",
        optional=True,
        env_defaults=(
            ("SMTP_HOST", "smtp.qq.com"),
            ("SMTP_PORT", "465"),
            ("SMTP_USER", "you@example.com"),
        ),
    ),
    ServiceEntry(
        key="wecom",
        name="企业微信群机器人 Webhook",
        kind="credential",
        keywords=("企业微信", "企微", "群机器人", "内部通知", "工作群"),
        env_vars=("WECOM_WEBHOOK",),
        signup_url=None,
        steps=(
            "在目标企业微信群右上角「…」→「群机器人」→「添加机器人」",
            "起个名字，创建后会得到一个 Webhook 地址（含 key，形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx）",
            "把整个地址填进 .env 的 WECOM_WEBHOOK —— 不需要注册开放平台，也不需要管理员审批",
        ),
        cost="免费。",
        fallback="不推送，只在终端打印结果。",
        optional=True,
        example="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx",
    ),
    ServiceEntry(
        key="feishu",
        name="飞书自建应用凭证",
        kind="credential",
        keywords=("飞书", "多维表格", "飞书文档", "审批流"),
        env_vars=("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
        signup_url="https://open.feishu.cn/app",
        steps=(
            "登录飞书开放平台，进入「开发者后台」→ 创建企业自建应用",
            "「凭证与基础信息」页复制 App ID 与 App Secret",
            "「权限管理」里开通需要的权限（如 bitable:app、im:message）并发布版本",
            "把两个值填进 .env 的 FEISHU_APP_ID / FEISHU_APP_SECRET",
        ),
        cost="免费（企业已开通飞书即可）。",
        fallback="飞书相关工具不可用，结果落本地文件。",
    ),
    ServiceEntry(
        key="wechat_pay",
        name="微信支付商户号与 API 凭证",
        kind="credential",
        keywords=("支付", "收款", "付款", "退款", "微信支付", "支付宝"),
        env_vars=("WXPAY_MCH_ID", "WXPAY_API_V3_KEY", "WXPAY_CERT_SERIAL"),
        signup_url="https://pay.weixin.qq.com",
        steps=(
            "注册微信支付商户号（需营业执照，个人无法开通）",
            "商户平台 →「账户中心 → API 安全」设置 APIv3 密钥",
            "「API 安全」→ 申请 API 证书，记录证书序列号",
            "把商户号 / 密钥 / 序列号填进 .env",
        ),
        cost="按笔收取手续费（通常 0.6%）。",
        fallback="支付流程无法联调；建议先用沙箱或跳过该分支。",
    ),
    ServiceEntry(
        key="oss",
        name="对象存储（阿里云 OSS / 腾讯云 COS）",
        kind="infra",
        keywords=("上传文件", "图片存储", "附件存储", "生成图片", "导出文件", "下载链接"),
        env_vars=("OSS_ENDPOINT", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET"),
        signup_url="https://oss.console.aliyun.com",
        steps=(
            "登录阿里云 → 开通对象存储 OSS（按量付费，几毛钱起）",
            "创建 Bucket，读写权限选「私有」，记下地域 endpoint",
            "右上角头像 →「AccessKey 管理」→ 创建 AccessKey（建议用子账号 + 只授权该 Bucket）",
            "把 endpoint / Key / Bucket 名填进 .env",
        ),
        cost="按存储量与流量计费，调试期每月几毛到几元。",
        fallback="产物只写本地目录，不生成外链。",
        optional=True,
        env_defaults=(
            ("OSS_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com"),
            ("OSS_BUCKET", "your-bucket"),
        ),
    ),
    ServiceEntry(
        key="sms",
        name="短信服务（阿里云 / 腾讯云）",
        kind="api_key",
        keywords=("短信", "验证码", "触达用户"),
        env_vars=("SMS_ACCESS_KEY_ID", "SMS_ACCESS_KEY_SECRET", "SMS_SIGN_NAME", "SMS_TEMPLATE_CODE"),
        signup_url="https://dysms.console.aliyun.com",
        steps=(
            "登录阿里云短信服务控制台并开通",
            "「国内消息」→ 申请签名与模板（审核 1~2 小时）",
            "创建 AccessKey（建议子账号，只授权 SMS）",
            "把 AccessKey、签名、模板号填进 .env",
        ),
        cost="约 0.045 元/条。",
        fallback="短信分支跳过，只写日志。",
    ),
    ServiceEntry(
        key="asr",
        name="语音识别服务",
        kind="api_key",
        keywords=("语音", "录音", "转写", "会议纪要", "音频"),
        env_vars=("ASR_APP_ID", "ASR_SECRET_ID", "ASR_SECRET_KEY"),
        signup_url="https://console.cloud.tencent.com/asr",
        steps=(
            "登录腾讯云 → 开通「语音识别 ASR」",
            "访问管理 CAM → 创建 API 密钥（SecretId / SecretKey）",
            "把 AppID / SecretId / SecretKey 填进 .env",
        ),
        cost="按时长计费，调试期有免费额度。",
        fallback="音频输入不可用，只处理文本。",
    ),
    ServiceEntry(
        key="ocr",
        name="OCR 文字识别服务",
        kind="api_key",
        keywords=("OCR", "发票识别", "票据", "扫描件", "图片文字", "截图识别"),
        env_vars=("OCR_API_KEY", "OCR_SECRET_KEY"),
        signup_url="https://console.bce.baidu.com/ai/#/ai/ocr/overview/index",
        steps=(
            "登录百度智能云 → 进入「文字识别 OCR」控制台",
            "创建应用，勾选需要的识别类型（通用/发票/表格）",
            "在应用列表里拿到 API Key 与 Secret Key",
            "填进 .env 的 OCR_API_KEY / OCR_SECRET_KEY",
        ),
        cost="每月有一定免费调用量。",
        fallback="扫描件无法解析，需要用户提供文本版。",
    ),
    ServiceEntry(
        key="mcp_server",
        name="MCP 服务地址与凭证",
        kind="infra",
        keywords=(),  # 由架构里的 mcp 组件触发
        env_vars=("MCP_SERVER_URL", "MCP_TOKEN"),
        signup_url="https://modelcontextprotocol.io",
        steps=(
            "确认要用哪个 MCP Server（自建或社区现成的，如 filesystem / postgres / 浏览器）",
            "本地跑：npx -y @modelcontextprotocol/server-xxx，把命令记下来",
            "远程跑：拿到服务地址与访问 token",
            "把地址填进 .env 的 MCP_SERVER_URL，凭证填 MCP_TOKEN；并在 requirements.txt 打开 mcp 依赖注释",
        ),
        cost="取决于所选 Server，多数开源本地 Server 免费。",
        fallback="MCP 客户端保持桩实现，相关工具抛 NotImplementedError。",
    ),
    ServiceEntry(
        key="vector_db",
        name="向量库（可选，默认本地文件检索）",
        kind="infra",
        keywords=("语义检索", "向量", "embedding", "相似度"),
        env_vars=("QDRANT_URL", "QDRANT_API_KEY"),
        signup_url="https://cloud.qdrant.io",
        steps=(
            "先不用配：脚手架默认用本地内存 + 关键词检索，够跑通",
            "语料上万条再考虑：注册 Qdrant Cloud 免费层，或本地 docker run qdrant/qdrant",
            "拿到 URL 与 API Key 填进 .env",
        ),
        cost="Qdrant Cloud 有永久免费层。",
        fallback="自动降级为本地关键词检索。",
        optional=True,
        env_defaults=(("QDRANT_URL", "http://localhost:6333"),),
    ),
)
