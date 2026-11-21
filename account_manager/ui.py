from datetime import datetime, timezone, timedelta


def text(value, tone="default", kind="text"):
    return {"type": kind, "text": str(value), "style": {"tone": tone}}


def button(label, action, tone="default", **params):
    return {"type": "button", "label": label, "action": "run",
            "params": {"action": action, **params}, "style": {"tone": tone}}


def card(label, children, span=1):
    return {"type": "card", "label": label, "children": children, "style": {"span": span}}


def table(label, headers, rows):
    return {"type": "table", "label": label, "headers": headers,
            "rows": [[str(cell) for cell in row] for row in rows]}


def account_status(account):
    if account["expired"]:
        return "登录失效", "danger"
    if not account["logged_in"]:
        return "未登录", "muted"
    if not account["folder_id"]:
        return "接收目录未配置", "warning"
    if not account["folder_ready"]:
        return "接收目录不可用", "danger"
    return "登录及目录已配置，播放待验证", "success"


def render(snapshot, state, scope, params, *, notice=None, preview=None, results=None,
           maintenance=None, folders=None, qr_image=None, qr_status=None, contract_available=False):
    components = [text("115 小号 · 用户分配工作台", kind="heading"),
                  text("账号与绑定由原系统管理，播放继续经过原来的反代入口。", "muted")]
    if notice:
        components.append(text(notice, "warning", "alert"))
    if not contract_available:
        components.append(text("当前实例的扫码、目录和非空账号字段尚未核实；相关写入流程需要先补齐版本契约。", "warning", "alert"))
    rules = snapshot["rules"]
    if not snapshot["rule_id"]:
        rule_page = min(max(0, int(params.get("rule_page", 0))), max(0, (len(rules) - 1) // 10))
        choices = [button(r["name"] or r["id"], "refresh", "primary", rule_id=r["id"])
                   for r in rules[rule_page * 10:rule_page * 10 + 10]]
        if rule_page:
            choices.append(button("上一页规则", "refresh", rule_id="", rule_page=rule_page - 1))
        if (rule_page + 1) * 10 < len(rules):
            choices.append(button("下一页规则", "refresh", rule_id="", rule_page=rule_page + 1))
        components.append(card("选择反代规则", choices or [text("没有可用的反代规则。")]))
        return {"ui": {"width": "full", "layout": "stack", "components": components}}

    accounts, users = snapshot["accounts"], snapshot["users"]
    selected = state.selected(scope)
    target_id = state.target(scope)
    target = next((a for a in accounts if a["id"] == target_id), None)
    rule_name = next((r["name"] for r in rules if r["id"] == snapshot["rule_id"]), snapshot["rule_id"])
    components.append(text(f"当前规则：{rule_name} · 115 小号 {len(accounts)} · 已有用户 {len(users)} · 已选择 {len(selected)}", kind="badge"))
    if snapshot.get("observed_at"):
        read_at = datetime.fromtimestamp(snapshot["observed_at"], timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        components.append(text("列表读取时间：" + read_at + "（北京时间）", "muted"))
    components.append({"type": "grid", "columns": 3, "label": "工作区操作", "children": [
        button("刷新原系统状态", "refresh"), button("清空用户选择", "clear_selection"),
        button("查看其他反代规则", "refresh", rule_id="", rule_page=0),
        button("核实待确认结果（每次 30 项）", "reconcile"), button("查看操作记录", "history")
    ]})

    account_page = max(0, int(params.get("account_page", 0)))
    account_page = min(account_page, max(0, (len(accounts) - 1) // 4))
    pool = []
    for account in accounts[account_page * 4:account_page * 4 + 4]:
        status, tone = account_status(account)
        pool.append(card(account["name"], [
            text(status, tone, "badge"), text(account["masked_login"] or "尚无登录标识", "muted"),
            text("目录：" + (account["folder_path"] or account["folder_id"] or "未设置")),
            text(f"关联 {account['usage_count']} 人"),
            button("当前目标" if account["id"] == target_id else "选择此小号", "choose_account", "primary",
                   cloud_account_id=account["id"]),
        ]))
    if not accounts:
        pool.append(text("当前规则尚无 115 小号，请在上方账号维护表单填写名称后创建。", "muted"))
    if account_page:
        pool.append(button("上一页小号", "refresh", account_page=account_page - 1))
    if (account_page + 1) * 4 < len(accounts):
        pool.append(button("下一页小号", "refresh", account_page=account_page + 1))
    if contract_available:
        pool.append(button("预览创建小号", "preview_create"))

    query = params.get("search", "").strip().casefold()
    filtered = [u for u in users if not query or query in u["name"].casefold() or query in u["id"].casefold()]
    user_page = min(max(0, int(params.get("user_page", 0))), max(0, (len(filtered) - 1) // 8))
    page_users = filtered[user_page * 8:user_page * 8 + 8]
    middle = [text("仅展示原系统已有用户接口返回的用户；邀请码账户沿用原绑定流程。", "muted"),
              table("已有 Emby 用户", ["选择", "用户名", "当前 115 小号", "用户状态"], [
                  ["已选" if u["id"] in selected else "—", u["name"], u["binding"]["name"],
                   "已禁用" if u["disabled"] else "管理员" if u["admin"] else "正常"] for u in page_users
              ])]
    selection_buttons = []
    for user in page_users:
        chosen = user["id"] in selected
        selection_buttons.append(button(("移出选择：" if chosen else "加入选择：") + user["name"],
                                  "remove_user" if chosen else "add_user", user_id=user["id"]))
    if selection_buttons:
        middle.append({"type": "grid", "columns": 2, "children": selection_buttons})
    if not page_users:
        middle.append(text("没有符合条件的用户。", "muted"))
    if user_page:
        middle.append(button("上一页用户", "refresh", user_page=user_page - 1))
    if (user_page + 1) * 8 < len(filtered):
        middle.append(button("下一页用户", "refresh", user_page=user_page + 1))
    right = [text("目标小号：" + (target["name"] if target else "尚未选择")), text(f"已选择 {len(selected)} 位用户"),
             button("预览分配 / 更换", "preview_assign", "primary"), button("预览解除 115 关联", "preview_unbind", "warning")]
    if preview:
        preview_id, payload = preview
        target_name = payload["target"]["name"] if payload["target"] else "解除 115 关联"
        right.extend([table("提交前核对", ["用户名", "原小号", "目标", "变化"], [
            [u["name"], u["old"]["name"], target_name,
             "不变" if u["old"]["id"] == (payload["target"]["id"] if payload["target"] else "") else "切换 / 解除"]
            for u in payload["items"]
        ]), text("预览 5 分钟内有效，确认前会再次读取真实绑定。", "muted"),
            button("确认提交这份预览", "confirm_binding", "primary", preview_id=preview_id)])
    components.append({"type": "grid", "columns": 4, "children": [
        card("115 小号账号池", pool), card("用户选择", middle, 2), card("分配预览", right)
    ]})

    if target:
        status, tone = account_status(target)
        manage = [text(status, tone, "badge"), text("关联用户：" + ("、".join(target["users"][:30]) or "无")),
                  {"type": "link", "label": "打开原系统账号面板", "href": params["base_url"].rstrip("/") + "/api/emby_proxy/cloud-accounts/" + target["id"]},
                  button("预览修改名称", "preview_rename"), button("预览重新扫码登录", "preview_qr_start", "warning"),
                  button("检查并完成登录", "check_login", "primary", cloud_account_id=target["id"]),
                  button("仅检查扫码状态", "qr_status"), button("预览完成登录", "preview_qr_complete"),
                  button("浏览接收目录", "folders"), button("预览设置接收目录", "preview_folder", "warning"),
                  button("预览删除未使用小号", "preview_delete", "danger")]
        manage = manage[:3] + [{"type": "grid", "columns": 3, "children": manage[3:]}]
        manage.append(text("扫码并在 115 App 内确认后，点击“检查并完成登录”。每次检查一次；未授权时可稍后再点。", "muted"))
        components.append(card("维护目标小号：" + target["name"], manage))
    if maintenance:
        preview_id, payload = maintenance
        affected = payload["account"]["usage_count"] if payload["account"] else 0
        labels = {"create":"创建小号", "rename":"修改名称", "delete":"删除未使用小号", "qr_start":"发起扫码登录", "qr_complete":"完成扫码登录", "folder":"修改接收目录"}
        components.append(card("账号维护确认", [
            text(f"操作：{labels[payload['kind']]} · 影响关联用户 {affected} 人", "warning", "alert"),
            text("账号：" + (payload["account"]["name"] if payload["account"] else payload["name"])),
            text(("接收目录：" + payload.get("folder_name", "") + " · ID " + payload["folder_id"])
                 if payload["kind"] == "folder" else "确认后调用原系统账号接口。"),
            button("确认本次账号操作", "confirm_maintenance", "warning", preview_id=preview_id)
        ]))
    if qr_image:
        components.append({"type": "image", "src": qr_image, "alt": "当前独立 115 小号的扫码登录二维码",
                           "caption": "图片最多保留 5 分钟，实际有效状态以检查结果为准；超时后请重新发起登录。"})
    if qr_status:
        labels = {"waiting": "等待扫码", "scanned": "已扫码，等待授权", "authorized": "已授权，请完成登录",
                  "expired": "二维码已过期或尚未生成，请重新发起", "failed": "扫码登录失败"}
        components.append(text(labels[qr_status], "warning" if qr_status in {"expired", "failed"} else "primary", "alert"))
    if folders is not None:
        folder_page = min(max(0, int(params.get("folder_page", 0))), max(0, (len(folders) - 1) // 6))
        page_folders = folders[folder_page * 6:folder_page * 6 + 6]
        current_id = params.get("folder_id", "0")
        current_name = "根目录" if current_id == "0" else params.get("folder_name", "")
        current_parent = "0" if current_id == "0" else params.get("parent_id", "0")
        components.append(table("当前目录下的子目录", ["名称", "目录 ID"], [[f["name"], f["id"]] for f in page_folders]))
        for folder in page_folders:
            components.append(button("进入：" + folder["name"], "folders", folder_id=folder["id"],
                                     folder_name=folder["name"], parent_id=current_id, folder_page=0))
            components.append(button("预览选用：" + folder["name"], "preview_folder",
                                     folder_id=folder["id"], folder_name=folder["name"], parent_id=current_id))
        if folder_page:
            components.append(button("上一页目录", "folders", folder_id=current_id, folder_name=current_name,
                                     parent_id=current_parent, folder_page=folder_page - 1))
        if (folder_page + 1) * 6 < len(folders):
            components.append(button("下一页目录", "folders", folder_id=current_id, folder_name=current_name,
                                     parent_id=current_parent, folder_page=folder_page + 1))
        if current_name:
            components.append(button("预览选用当前目录：" + current_name, "preview_folder", folder_id=current_id,
                                     folder_name=current_name, parent_id=current_parent))
        components.append(button("返回根目录", "folders", folder_id="0", folder_name="根目录", parent_id="0", folder_page=0))
        components.append(text("选择目录后先预览再确认。所有目录 ID 保持字符串，不做数值转换。", "muted"))
    if results:
        labels = {"success": "成功", "failed": "失败", "unknown": "待核实", "not_started": "未执行", "expired": "已过期 / 结束"}
        components.append(table("逐项结果", ["用户", "状态", "说明"], [[r["name"], labels.get(r["status"], r["status"]), r["detail"]] for r in results]))
        for row in results:
            if row["status"] in {"failed", "not_started"}:
                components.append(button("重新选择失败项：" + row["name"], "add_user", user_id=row["id"]))
    if params.get("action") == "history":
        zone = timezone(timedelta(hours=8))
        history = state.history(scope)
        components.append(table("插件操作记录 · 非播放记录", ["时间（北京时间）", "操作", "账号 ID", "用户 ID", "结果"], [
            [datetime.fromtimestamp(r["created"], zone).strftime("%Y-%m-%d %H:%M:%S"), r["action"], r["account_id"], r["user_id"], r["result"]]
            for r in history
        ]))
    if state.unresolved(scope):
        components.append(text("存在结果待核实的操作。请先核实原系统状态，插件不会盲目重放写入。", "warning", "alert"))
    return {"ui": {"width": "full", "layout": "stack", "components": components}}
