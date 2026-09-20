#!/usr/bin/env python3
"""
定时任务调度器
支持 cron 表达式和定时执行
"""
import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable
from croniter import croniter

# 导入管理模块
from account_manager import account_manager
from template_manager import template_manager
from log_manager import log_manager
from outreach_policy import outreach_policy, dispatch_outreach, valid_bounds
from telethon import utils


ACCOUNTS_DIR = "./accounts"
SCHEDULE_FILE = os.path.join(ACCOUNTS_DIR, "schedules.json")


def schedule_requirements(schedule):
    action = schedule.get("action")
    if action == "scrape_members":
        return None, [], 0
    if action not in {"send_message", "send_template", "ai_execute", "add_members"}:
        raise ValueError("unsupported_schedule_action")
    if action == "add_members":
        users = schedule.get("add_usernames", [])
        limit, delay = schedule.get("add_limit", 10), schedule.get("add_delay", 35)
        if (schedule.get("source_target") or not isinstance(users, list) or not users
                or not valid_bounds(limit, 10, delay, 35) or len(users) > 10):
            raise ValueError("invalid_member_schedule")
        return "member_add", users[:limit], delay
    friends, strangers = schedule.get("friend_ids", []), schedule.get("stranger_usernames", [])
    interval = schedule.get("interval", 3000)
    if not isinstance(friends, list) or not isinstance(strangers, list):
        raise ValueError("invalid_targets")
    subjects = friends + strangers
    if type(interval) not in (int, float) or not valid_bounds(max(1, len(subjects)), 20, interval / 1000, 3):
        raise ValueError("invalid_send_schedule")
    return "scheduled_send", subjects, interval / 1000


class TaskScheduler:
    """定时任务调度器"""

    def __init__(self):
        self.schedules: Dict[str, Dict] = {}
        self.running = False
        self._load_schedules()

        # 主任务执行器 - 引用 main.py 中的发送功能
        self._send_message_func = None

    def _load_schedules(self):
        """加载定时任务配置"""
        if os.path.exists(SCHEDULE_FILE):
            try:
                with open(SCHEDULE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.schedules = data.get("schedules", {})
            except:
                self.schedules = {}

    def _save_schedules(self):
        """保存定时任务配置"""
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        with open(SCHEDULE_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "schedules": self.schedules,
                "updated_at": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)

    def set_send_message_function(self, func: Callable):
        """设置发送消息函数（从 main.py 导入）"""
        self._send_message_func = func

    def add_schedule(
        self,
        schedule_id: str,
        name: str,
        cron: str,
        action: str,
        target: str,  # chat_id
        message: str = None,
        template_id: str = None,
        accounts: List[str] = None,
        account_ids: List[str] = None,  # 兼容 dashboard.py 传入的参数名
        enabled: bool = True,
        # 新参数
        execute_time: Dict = None,
        repeat: str = None,
        friend_ids: List = None,
        stranger_usernames: List = None,
        interval: int = 3000,
        auto_dedup: bool = True,
        validate_usernames: bool = True,
        approval_id: str = None,
        **kwargs  # 忽略其他未知参数
    ) -> bool:
        """
        添加定时任务

        Args:
            schedule_id: 任务ID
            name: 任务名称
            cron: cron 表达式 (如 "0 9 * * *" 每天早上9点)
            action: 执行动作 (send_message, send_template)
            target: 目标 (chat_id 或 username)
            message: 消息内容（send_message 时使用）
            template_id: 模板ID（send_template 时使用）
            accounts: 账号列表，None表示全部账号
            enabled: 是否启用

        Returns:
            是否成功
        """
        # 验证 cron 表达式
        try:
            croniter(cron)
        except ValueError as e:
            return False

        # 统一账号列表参数（兼容 account_ids 和 accounts）
        accounts_list = account_ids or accounts

        try:
            policy_action, policy_subjects, _ = schedule_requirements({
                "action": action, "friend_ids": friend_ids or [],
                "stranger_usernames": stranger_usernames or [], "interval": interval,
                "source_target": kwargs.get("source_target"),
                "add_usernames": kwargs.get("add_usernames", []),
                "add_limit": kwargs.get("add_limit", 10),
                "add_delay": kwargs.get("add_delay", 35),
            })
        except ValueError:
            return False
        if policy_action and policy_subjects:
            if not accounts_list:
                return False
            decisions = outreach_policy.authorize_many(
                action=policy_action, subjects=policy_subjects,
                account_ids=accounts_list, approval_id=approval_id,
            )
            if any(not decision.allowed and decision.reason != "consent_missing_or_invalid" for decision in decisions):
                return False

        self.schedules[schedule_id] = {
            "id": schedule_id,
            "schedule_id": schedule_id,  # 前端使用的字段名
            "name": name,
            "cron": cron,
            "action": action,
            "target": target,
            "message": message,
            "template_id": template_id,
            "accounts": accounts_list,
            "account_ids": accounts_list,  # 兼容前端使用的字段名
            "enabled": enabled,
            "created_at": datetime.now().isoformat(),
            "last_run": None,
            "lastRun": None,  # 前端使用的字段名（驼峰命名）
            "next_run": self._get_next_run(cron),
            "run_count": 0,
            "fail_count": 0,
            # 新字段
            "execute_time": execute_time,
            "repeat": repeat,
            "friend_ids": friend_ids or [],
            "stranger_usernames": stranger_usernames or [],
            "interval": interval,
            "auto_dedup": auto_dedup,
            "validate_usernames": validate_usernames,
            "approval_id": approval_id,
            "policy_action": policy_action,
            "source_target": kwargs.get("source_target"),
            "dest_target": kwargs.get("dest_target"),
            "add_usernames": kwargs.get("add_usernames", []),
            "add_limit": kwargs.get("add_limit", 10),
            "add_delay": kwargs.get("add_delay", 35),
        }

        self._save_schedules()
        return True

    def _get_next_run(self, cron: str) -> str:
        """获取下次执行时间"""
        try:
            cron_obj = croniter(cron, datetime.now())
            return cron_obj.get_next(datetime).isoformat()
        except:
            return ""

    def remove_schedule(self, schedule_id: str) -> bool:
        """删除定时任务"""
        if schedule_id in self.schedules:
            del self.schedules[schedule_id]
            self._save_schedules()
            return True
        return False

    def delete_schedule(self, schedule_id: str) -> bool:
        """删除定时任务（别名，与 remove_schedule 功能相同）"""
        return self.remove_schedule(schedule_id)

    def get_next_run(self, schedule_id: str) -> Optional[str]:
        """获取指定任务的下次执行时间"""
        schedule = self.get_schedule(schedule_id)
        if schedule:
            return schedule.get("next_run")
        return None

    def toggle_schedule(self, schedule_id: str) -> bool:
        """切换任务状态"""
        if schedule_id in self.schedules:
            self.schedules[schedule_id]["enabled"] = not self.schedules[schedule_id]["enabled"]
            self._save_schedules()
            return True
        return False

    def list_schedules(self) -> List[Dict]:
        """列出所有任务"""
        schedules = []
        for s in self.schedules.values():
            # 确保所有前端需要的字段都存在
            schedule = dict(s)
            # 确保 schedule_id 字段存在
            if "schedule_id" not in schedule and "id" in schedule:
                schedule["schedule_id"] = schedule["id"]
            # 确保 lastRun 字段存在
            if "lastRun" not in schedule and "last_run" in schedule:
                schedule["lastRun"] = schedule["last_run"]
            # 确保 account_ids 字段存在
            if "account_ids" not in schedule and "accounts" in schedule:
                schedule["account_ids"] = schedule["accounts"]
            schedules.append(schedule)
        return schedules

    def get_schedule(self, schedule_id: str) -> Optional[Dict]:
        """获取指定任务"""
        return self.schedules.get(schedule_id)

    async def _execute_schedule(self, schedule: Dict) -> bool:
        """
        执行定时任务

        Args:
            schedule: 任务配置

        Returns:
            是否成功
        """
        try:
            action = schedule["action"]
            message = schedule.get("message", "")
            
            # 获取发送目标
            friend_ids = schedule.get("friend_ids", [])
            stranger_usernames = schedule.get("stranger_usernames", [])
            interval = schedule.get("interval", 3000)  # 毫秒
            
            # 兼容 accounts 和 account_ids 字段
            accounts = schedule.get("accounts") or schedule.get("account_ids")

            # 如果没有指定账号，使用全部账号
            if not accounts:
                accounts = list(account_manager.accounts.keys())

            results = []
            
            # 使用第一个账号发送（通常定时任务只选一个账号）
            account_id = accounts[0] if accounts else None
            if not account_id:
                log_manager.add_log("定时任务", "system", "没有可用账号", "error")
                return False

            try:
                policy_action, policy_subjects, execution_delay = schedule_requirements(schedule)
            except ValueError:
                schedule["enabled"] = False
                schedule["last_error"] = "invalid_schedule_bounds_or_action"
                self._save_schedules()
                return False
            if policy_action and policy_subjects:
                decisions = outreach_policy.authorize_many(
                    action=policy_action, subjects=policy_subjects,
                    account_ids=[account_id], approval_id=schedule.get("approval_id"),
                )
                if any(not decision.allowed and decision.reason != "consent_missing_or_invalid" for decision in decisions):
                    schedule["enabled"] = False
                    schedule["last_error"] = "outreach_denied"
                    self._save_schedules()
                    return False
            if action == "send_template":
                message = template_manager.render_template(schedule.get("template_id"))
                if not message:
                    return False

            try:
                # 获取客户端
                client = await account_manager.get_client(account_id)
                if not client:
                    log_manager.add_log("定时任务", account_id, "获取客户端失败", "error")
                    return False

                # ---- Action: scrape_members ----
                if action == "scrape_members":
                    scrape_target = schedule.get("scrape_target", "")
                    scrape_limit = schedule.get("scrape_limit", 200)
                    filter_bots = schedule.get("filter_bots", True)
                    if not scrape_target:
                        log_manager.add_log("定时任务", account_id, "scrape_members: no target specified", "error")
                        return False
                    try:
                        entity = await client.get_entity(scrape_target)
                        participants = await client.get_participants(entity, limit=scrape_limit)
                        members = []
                        for p in participants:
                            if filter_bots and p.bot:
                                continue
                            members.append({
                                "id": p.id,
                                "username": p.username or "",
                                "first_name": p.first_name or "",
                                "last_name": p.last_name or "",
                            })
                        schedule["last_scrape_result"] = {
                            "target": scrape_target,
                            "total": len(members),
                            "members": members,
                            "scraped_at": datetime.now().isoformat(),
                        }
                        log_manager.add_log("定时任务", account_id,
                            f"Scraped {len(members)} members from {scrape_target}", "success")
                        results.append({"account": account_id, "success": True, "scraped": len(members)})
                    except Exception as e:
                        log_manager.add_log("定时任务", account_id,
                            f"Scrape failed for {scrape_target}: {type(e).__name__}", "error")
                        results.append({"account": account_id, "success": False, "error": type(e).__name__})

                # ---- Action: add_members ----
                elif action == "add_members":
                    from telethon import functions as tl_functions
                    dest_target = schedule.get("dest_target", "")
                    source_target = schedule.get("source_target", "")
                    add_usernames = schedule.get("add_usernames", [])
                    add_limit = schedule.get("add_limit", 10)
                    add_delay = schedule.get("add_delay", 35)
                    if not dest_target:
                        log_manager.add_log("定时任务", account_id, "add_members: no dest_target specified", "error")
                        return False
                    user_list = []
                    if add_usernames:
                        user_list = add_usernames[:add_limit]
                    elif source_target:
                        src_entity = await client.get_entity(source_target)
                        parts = await client.get_participants(src_entity, limit=add_limit)
                        user_list = [p.username for p in parts if not p.bot and p.username]
                    if not user_list:
                        log_manager.add_log("定时任务", account_id, "add_members: no users to add", "warning")
                        results.append({"account": account_id, "success": True, "added": 0})
                    else:
                        dest_entity = await client.get_entity(dest_target)
                        added = 0
                        failed_add = 0
                        for idx, uname in enumerate(user_list):
                            try:
                                user_entity = await client.get_entity(uname)
                                await dispatch_outreach(
                                    action="member_add", subject=utils.get_peer_id(user_entity),
                                    account_id=account_id, approval_id=schedule.get("approval_id"),
                                    delay=execution_delay,
                                    send=lambda: client(tl_functions.channels.InviteToChannelRequest(
                                        channel=dest_entity, users=[user_entity])))
                                added += 1
                                log_manager.add_log("定时任务", account_id, "Approved member addition completed", "success")
                            except Exception as e:
                                failed_add += 1
                                log_manager.add_log("定时任务", account_id, f"Approved member addition failed: {type(e).__name__}", "error")
                            if idx < len(user_list) - 1:
                                await asyncio.sleep(add_delay)
                        results.append({"account": account_id, "success": failed_add == 0, "added": added, "failed": failed_add})
                        log_manager.add_log("定时任务", account_id,
                            f"Add members done: {added} added, {failed_add} failed", "success" if failed_add == 0 else "warning")

                # ---- Action: send_message / ai_execute / default ----
                else:
                    # 合并发送目标
                    targets = []
                    for fid in friend_ids:
                        targets.append({"type": "id", "value": fid})
                    for username in stranger_usernames:
                        targets.append({"type": "username", "value": username})
                    
                    # 如果没有指定目标，发送到 Saved Messages
                    if not targets:
                        targets = [{"type": "id", "value": "me"}]
                    
                    success_count = 0
                    fail_count = 0
                    
                    for i, target in enumerate(targets):
                        try:
                            # 获取目标实体
                            target_value = target["value"]
                            entity = await client.get_entity(target_value)
                            
                            # 发送消息
                            # ai_execute 暂时和 send_message 一样（AI优化需要用户自己调用MCP）
                            if policy_subjects:
                                await dispatch_outreach(
                                    action="scheduled_send", subject=utils.get_peer_id(entity),
                                    account_id=account_id, approval_id=schedule.get("approval_id"),
                                    delay=execution_delay, send=lambda: client.send_message(entity, message))
                            else:
                                # Only the internally constructed Saved Messages target is exempt.
                                await client.send_message(entity, message)
                            success_count += 1
                            
                            log_manager.add_log("定时任务", account_id, 
                                "已批准的定时消息发送成功", "success")
                            
                            # 发送间隔（除了最后一条）
                            if i < len(targets) - 1:
                                await asyncio.sleep(interval / 1000)
                                
                        except Exception as e:
                            fail_count += 1
                            log_manager.add_log("定时任务", account_id, 
                                f"已批准的定时消息发送失败: {type(e).__name__}", "error")
                    
                    results.append({
                        "account": account_id, 
                        "success": fail_count == 0,
                        "sent": success_count,
                        "failed": fail_count
                    })
                    
                    log_manager.add_log("定时任务", account_id, 
                        f"执行完成: {schedule['name']} (成功{success_count}/失败{fail_count})", 
                        "success" if fail_count == 0 else "warning")

            except Exception as e:
                log_manager.add_log("定时任务", account_id, f"执行失败: {type(e).__name__}", "error")
                results.append({"account": account_id, "success": False, "error": type(e).__name__})

            # 更新任务统计
            now_iso = datetime.now().isoformat()
            schedule["last_run"] = now_iso
            schedule["lastRun"] = now_iso  # 前端使用的字段名（驼峰命名）
            schedule["run_count"] = schedule.get("run_count", 0) + 1
            schedule["next_run"] = self._get_next_run(schedule["cron"])

            # 检查是否有失败
            if any(not r.get("success") for r in results):
                schedule["fail_count"] = schedule.get("fail_count", 0) + 1

            self._save_schedules()
            return all(r.get("success") for r in results)

        except Exception as e:
            log_manager.add_log("定时任务", "system", f"执行任务 {schedule['name']} 失败: {type(e).__name__}", "error")
            return False

    async def start(self):
        """启动调度器"""
        if self.running:
            return

        self.running = True
        print("📅 定时任务调度器已启动")

        while self.running:
            try:
                now = datetime.now()
                
                # 重新加载配置（支持动态添加任务）
                self._load_schedules()

                for schedule_id, schedule in list(self.schedules.items()):
                    # 检查是否启用
                    if not schedule.get("enabled", True):
                        continue

                    # 检查执行时间
                    execute_time = schedule.get("execute_time")
                    repeat = schedule.get("repeat", "once")
                    last_run = schedule.get("last_run")
                    
                    should_execute = False
                    
                    if execute_time:
                        # 新格式：精确时间
                        target_time = datetime(
                            execute_time.get("year", now.year),
                            execute_time.get("month", now.month),
                            execute_time.get("day", now.day),
                            execute_time.get("hour", 0),
                            execute_time.get("minute", 0),
                            execute_time.get("second", 0)
                        )
                        
                        # 检查是否应该执行
                        time_diff = (now - target_time).total_seconds()
                        
                        if repeat == "once":
                            # 仅一次：到时间且未执行过
                            if 0 <= time_diff < 30 and not last_run:
                                should_execute = True
                        elif repeat == "daily":
                            # 每天：检查时分秒是否匹配
                            if (now.hour == execute_time.get("hour", 0) and 
                                now.minute == execute_time.get("minute", 0) and
                                abs(now.second - execute_time.get("second", 0)) < 10):
                                # 检查今天是否已执行
                                if last_run:
                                    last_run_time = datetime.fromisoformat(last_run)
                                    if last_run_time.date() < now.date():
                                        should_execute = True
                                else:
                                    should_execute = True
                        elif repeat == "weekly":
                            # 每周：检查星期几+时分秒
                            if (now.weekday() == target_time.weekday() and
                                now.hour == execute_time.get("hour", 0) and
                                now.minute == execute_time.get("minute", 0)):
                                if last_run:
                                    last_run_time = datetime.fromisoformat(last_run)
                                    if (now - last_run_time).days >= 7:
                                        should_execute = True
                                else:
                                    should_execute = True
                        elif repeat == "workday":
                            # 工作日：周一到周五
                            if (now.weekday() < 5 and  # 0-4是周一到周五
                                now.hour == execute_time.get("hour", 0) and
                                now.minute == execute_time.get("minute", 0)):
                                if last_run:
                                    last_run_time = datetime.fromisoformat(last_run)
                                    if last_run_time.date() < now.date():
                                        should_execute = True
                                else:
                                    should_execute = True
                    else:
                        # 旧格式：cron
                        next_run = schedule.get("next_run", "")
                        if next_run:
                            next_time = datetime.fromisoformat(next_run)
                            if 0 <= (now - next_time).total_seconds() < 30:
                                should_execute = True
                    
                    if should_execute:
                        # AI执行类型的任务不自动执行，等待AI通过MCP处理
                        if schedule.get("action") == "ai_execute":
                            print(f"⏰ AI任务已就绪，等待AI润色: {schedule['name']}")
                            log_manager.add_log("定时任务", "system", f"AI任务就绪，等待润色: {schedule['name']}", "info")
                            # 不执行，让AI通过get_pending_ai_tasks获取并润色后执行
                        else:
                            print(f"⏰ 执行定时任务: {schedule['name']}")
                            log_manager.add_log("定时任务", "system", f"开始执行: {schedule['name']}", "info")
                            await self._execute_schedule(schedule)

                # 每10秒检查一次（更精确）
                await asyncio.sleep(10)

            except Exception as e:
                print(f"调度器错误: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(10)

    def stop(self):
        """停止调度器"""
        self.running = False
        print("📅 定时任务调度器已停止")

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "total": len(self.schedules),
            "enabled": sum(1 for s in self.schedules.values() if s.get("enabled", True)),
            "disabled": sum(1 for s in self.schedules.values() if not s.get("enabled", True)),
            "total_runs": sum(s.get("run_count", 0) for s in self.schedules.values()),
            "pending": len([s for s in self.schedules.values() if s.get("enabled", True)])
        }


# 全局实例
task_scheduler = TaskScheduler()
