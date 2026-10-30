"""GitHub commands run on a dedicated worker, independent of chat/media."""
import re
from services.github_monitor import HELP


def handle_github(handler, message, gid, uid):
    match = re.fullmatch(r"(?:github|gh)\s*(订阅列表|列表|帮助|退订|订阅)(?:\s+(.+))?", message.strip(), re.I)
    if not match:
        # 裸命令只接受 GitHub 链接，避免抢占其它服务的订阅指令。
        match = re.fullmatch(r"(退订|订阅)\s+((?:https://github\.com/|\[|<https://github\.com/).+)", message.strip(), re.I)
    if not match:
        return None

    def work():
        try:
            action, value = match.groups()
            if value:
                value = value.strip()
                link = re.fullmatch(r"\[[^\]\r\n]*\]\((https://github\.com/[^\s)]+)\)", value, re.I)
                if link:
                    value = link.group(1)
                elif value.startswith("<") and value.endswith(">"):
                    value = value[1:-1]
            target = ("g:" + str(gid)) if gid is not None else ("u:" + str(uid))
            service = handler.github_monitor_service
            if action == "帮助":
                result = HELP
            elif action in ("列表", "订阅列表"):
                result = service.list_text(target)
            elif not value:
                result = HELP
            else:
                if gid is not None and str(uid) != str(handler.admin_qq):
                    role = (handler.qq_api.get_group_member_info(gid, uid) or {}).get("role")
                    if role not in ("owner", "admin"):
                        raise ValueError("仅群主、群管理员或机器人管理员可修改群订阅")
                result = service.subscribe(target, value) if action == "订阅" else service.unsubscribe(target, value)
            handler.qq_api.send_message(gid, result, uid, at=False)
        except ValueError as error:
            handler.qq_api.send_message(gid, str(error), uid, at=False)
        except Exception as error:
            print("GitHub command failed:", type(error).__name__)
            handler.qq_api.send_message(gid, "GitHub 操作失败，可能网络异常或接口限流，请稍后重试", uid, at=False)

    if not handler.github_job_queue.submit(work):
        handler.qq_api.send_message(gid, "GitHub 任务队列已满，请稍后重试", uid, at=False)
    return "ok"
