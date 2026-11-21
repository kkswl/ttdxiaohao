# 用户中心流程对照与插件复用

核对日期：2026-11-21。证据来自当前实例的用户中心 HTML / JavaScript、独立账号面板、OpenAPI 及脱敏绑定摘要。没有导出浏览器 Cookie，没有改动邀请码用户的个人绑定。

## 原生页面实际分工

| 模式 | 原生入口 / 标识 | 115 凭据与关联 |
| --- | --- | --- |
| 邀请码用户 | 反代端口 /emby-signup；ADMIN_BINDING=false、CLOUD_POOL=false，account_source=invite | 该用户自行授权；实际绑定摘要不含 cloud_account_id |
| 独立账号池 | /api/emby_proxy/cloud-accounts/{cloud_account_id}；ADMIN_BINDING=true、CLOUD_POOL=true | 独立小号保存授权及接收目录，可供多位已有用户关联 |
| 已有用户 | prepare 返回的 /api/emby_proxy/existing-users/{account_id}/cloud | 页面根据 account_source=existing 进入账号选择分支，assign 提交 cloud_account_id |

已指定的邀请码测试用户当前有 ready=true、status=active 的个人 115 绑定和接收目录。当前 invites 返回的是邀请码记录，不含另行指定的已有测试用户；不能据此认定已有用户管理记录不存在。

模板同时包含个人授权和账号选择代码，不代表同一用户可以混用两条流程。源码实际通过 account_source 分支选择。插件继续管理独立账号池及已有用户引用，不将邀请码用户传给 existing-users/prepare，也不复制个人 115 Cookie。

## 可以直接复用的扫码协议

两种登录页面使用各自 BASE 下相同的协议：

1. POST api/115/qr/start，提交 app，取得 flow_id、qr 和有效期。
2. GET api/115/qr/status，提交 flow_id；0 为等待扫码、1 为等待 App 确认、2 为已授权。
3. 状态为 2 时，原生页面立即 POST api/115/qr/complete，提交同一 flow_id。
4. 读取原系统账号摘要，后续通过该账号的 folders / folder 接口配置接收目录。

原生网页使用浏览器定时器，至少每 5 秒检查一次。插件协议不执行自定义 JavaScript，插件任务也应有界，因此 0.2.3 使用「检查并完成登录」：每次点击只检查一次，已授权则完成，其他状态立即返回。它不会启动后台轮询，也不会每次播放执行插件。

## 合并操作的前置条件

生成二维码前仍须预览并确认影响人数。加密短期流程关联到这份已确认预览，合并操作会核对规则、明确的小号 ID、流程版本及当时的账号状态。

扫码检查返回已授权后，再读取原系统账号及关联人数；发生变化时要求重新预览。目标切换、扫码流程替换、旧按钮、未核实写入和缺少原确认记录的旧流程均不会直接完成登录。正常合并操作仅调用一次状态接口和一次完成接口；不再为了连续预览而重复轮询。

旧版流程没有新增的确认引用时，继续使用「预览完成登录」；升级不重置账号、选择或原系统绑定。新增字段仅位于插件自己的 data_dir 数据库。

## 播放链路仍需单独验证

本次借鉴的是原生扫码协议与页面的账号来源分工。已有用户分配仍写入原生 prepare / assign / unbind 系统，播放继续由原网关处理。

已有 HTTP 200 / playbackinfo_rewrite 的播放请求，也已完成邀请码用户与账号池用户的 HEAD 对照：后者通过插件临时分配小号、回读确认后，以自身登录身份取得 302 → 115 CDN。原网关记录均为 invite_user_personal_cloud，账号池用户的设备和请求身份匹配。该旧名称覆盖了本次已有用户账号池路径；不应因名称带 invite 就自行搬迁或转换用户记录。

详细任务与记录号见 TEST-REPORT.md。该对照尚未核实小号目标文件或取链账号，不能视为秒传已通过。最新《野蛮师姐》同为 /play115share 来源，邀请用户活动为 share_direct_playback / 115share，个人绑定仍就绪；具体见 RAPID-TRANSFER-DIAGNOSIS.md。新合并按钮的真实扫码完成仍单独验收；HEAD 302 不代表所有客户端的帧播放已经通过。
