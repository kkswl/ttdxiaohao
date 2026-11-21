# 原生接口证据 · v8.9.2

核查日期：2026-11-20 至 2026-11-21。所有写入限定于已授权的独立 115 测试记录，未修改其他账号或用户。真实凭据和二维码授权内容不写入此文件。

开发指南的权限说明已重新核实：Python 插件使用服务账户，可访问文件、环境变量和网络，独立进程不构成权限沙箱；没有插件独立用户权限系统。管理员 API 鉴权与这些进程权限是不同层次，运行插件不会自动继承浏览器 Cookie。本实现继续遵守仅使用现有 HTTP API 的项目约束。

## 已有现场证据

- Bearer GET /api/emby_proxy/config 成功，核实指定反代规则。
- GET cloud-accounts 和 existing-users 成功；后者返回 users 数组。
- POST cloud-accounts/create 使用 rule_id、provider="115"、display_name，成功创建待登录测试记录。
- 已读取该账号原生页面、api/session，核对客户端实际请求；正式插件入口也已成功读取真实账号工作台。
- POST 独立账号 api/115/qr/start，app="web"，成功返回 expires_in=120、flow_id、qr。
- 超时后 GET qr/status 返回 HTTP 400，error 为“登录流程已过期或已取消，请重新发起”，插件映射为固定过期提示。
- 指定 Emby 测试用户在 invites 查询中 account_source="invite"，不在 existing-users 中。没有调用 prepare 强行转换或创建重复记录。
- 用户通过原生账号面板完成登录和接收目录配置后，账号池与会话均回读 authorized=true、ready=true、status="active"，接收目录是字符串 ID，关联人数为 0。
- 独立小号目录接口真实读取成功：根目录 6 条子目录，已配置接收目录下 30 条子目录，所有返回 ID 均为字符串。
- 宿主正式插件通过目录按钮预览并确认重新保存当前接收目录；原接口接受操作，随后账号池回读名称和 ID 一致，仍然就绪、关联人数为 0。没有更换到其他目录。
- 另行指定的已有 Emby 测试用户已通过正式插件完成 prepare / assign / unbind。分配后 existing-users.bindings 出现同一小号，账号 user_count 从 0 变 1；解绑后该用户 bindings 恢复为空，user_count 回到 0，小号授权及目录仍然就绪。其他用户和账号摘要与测试前一致。

## 账号摘要

cloud_accounts 条目实际字段：

    id, cloud_account_id, rule_id, provider, display_name, account_label,
    authorized, ready, folder_id, folder_name, status, shared,
    user_count, users, created_at, updated_at, last_validated_at

已验证待登录记录 authorized=false、ready=false、folder_id=""、status="draft"、user_count=0；也已验证登录并配置目录后的 authorized=true、ready=true、status="active"、user_count=0。目录 ID 是字符串，updated_at 作为预览版本信息。authorized / ready 是原系统摘要，不代表已通过真实播放验证。登录失效账号的真实摘要语义仍未验证。

独立账号 api/session 返回：

    {"success": true, "account": {
        "account_source": "cloud_pool",
        "cloud_account": {上述摘要},
        "bindings": [], ...}}

真实分配后，账号摘要 users 条目已确认是包含 emby_user_id、emby_username 的对象。

## 请求契约

独立账号基础路径：

    /api/emby_proxy/cloud-accounts/{cloud_account_id}

| 操作 | 方法和后缀 | 请求 |
| --- | --- | --- |
| 会话 | GET /api/session | 无 |
| 开始扫码 | POST /api/115/qr/start | JSON app |
| 检查扫码 | GET /api/115/qr/status | query flow_id |
| 完成登录 | POST /api/115/qr/complete | JSON flow_id |
| 浏览目录 | GET /api/cloud/115/folders | query parent_id |
| 设置目录 | POST /api/cloud/115/folder | JSON folder_id、folder_name、parent_id |

扫码开始响应 qr 包含 qrcode、uid、time、sign。插件仅在内存使用 qrcode 生成 PNG，flow_id 作为加密短期上下文；不回传这些授权字段。

状态响应 status.status：原生客户端将 0 作为待扫码、1 作为已扫码、2 作为已授权、负数作为过期或取消；原生页面还读取 cooldown 和 retry_after。插件使用显式检查按钮，不在任务中无限轮询。

目录响应 folders 条目的 id / name 由原生页面使用。接收目录需实际调用设置接口，不能只保存显示路径。

## 已有用户分配

- GET /api/emby_proxy/existing-users，query id 指定规则。
- POST /api/emby_proxy/existing-users/prepare，JSON rule_id、emby_user_id。
- 使用返回 binding_url 的 api/session 和 api/cloud/115/assign、unbind。
- assign 使用 cloud_account_id；解绑空 JSON 已通过正式插件实际调用验证。
- 原生页面显示 binding.cloud_account_id、display_name 或 account_label。
- 严格区分 rule_id、emby_user_id、account_id、cloud_account_id；不猜测它们相同。
- binding_url 必须同源且匹配已有用户绑定路径，不向其他地址转发 Token。

真实非空 bindings 条目已确认包含：

    provider, cloud_account_id, display_name, account_label, folder_id,
    folder_name, ready, shared, status, created_at, updated_at, last_validated_at

插件使用 provider=115、cloud_account_id 和 display_name / account_label 读取关联；真实分配和解绑后的 GET 已验证该映射。测试用户属于已有用户接口，未使用邀请码账户。

插件内完整扫码、多人批量 / 切换及真实直链缓存行为仍待验证。单用户 prepare / assign / unbind、目录读写和指定用户的 302 路径已有宿主任务、接口回读及原网关记录；没有将原生页面登录成功当成插件完成扫码的证据。源码中的 HTTP mock 使用匿名合成值，不能替代现场证据。

## 播放观测接口及证据边界

GET /api/emby_proxy/playback_records 使用 id 指定规则，支持 page、page_size，返回 success、records、total 等字段。真实请求已在 records 中出现，user_id / extra.context.user_id 匹配测试用户，status_code=200、redirect_reason=playbackinfo_rewrite、direct_url 为 115 CDN URL。此记录没有 cloud_account_id，不能单凭直链域名确定账号来源，也不能把 PlaybackInfo 的 200 响应写成 302 验收通过。

该接口的 extra.authorization、query_args、request_url、direct_url 等可能含客户端凭据和签名，不能直接展示或写入插件日志。现场仅筛选必要字段进行判断，交付包不包含原始播放记录。

随后已观察到两次 HEAD 302：邀请码用户记录 13，以及通过插件分配小号的已有用户记录 14，均为 invite_user_personal_cloud。后者同时具备原系统账号池引用回读、实际登录用户、请求上下文及测试设备匹配，补足了账号池关联接入原网关用户云盘路径的证据。两次来源均为分享 STRM，没有核实小号目标文件或取链账号，不能据此认定资源已秒传进小号。没有把 HEAD 结果写成客户端视频播放流畅性已通过。最新分享来源诊断见 RAPID-TRANSFER-DIAGNOSIS.md。
