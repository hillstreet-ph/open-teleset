#!/usr/bin/env python3
"""
批量操作工具
支持批量发送消息、批量操作账号等
"""
import asyncio
from typing import List, Dict, Optional
from datetime import datetime

# 导入管理模块
from account_manager import account_manager
from template_manager import template_manager
from log_manager import log_manager
from health_monitor import health_monitor
from outreach_policy import outreach_policy, dispatch_outreach, valid_bounds
from telethon import utils
from stats_tracker import stats_tracker


class BatchOperations:
    """批量操作器"""

    def __init__(self, default_delay: float = 3.0):
        """
        初始化批量操作器

        Args:
            default_delay: 默认操作间隔（秒），防止频繁操作被封号
        """
        self.default_delay = default_delay

    async def batch_send_message(
        self,
        chat_id: str,
        message: str,
        account_ids: List[str] = None,
        delay: float = None,
        approval_id: str = None,
    ) -> Dict:
        """
        批量发送消息

        Args:
            chat_id: 目标聊天ID
            message: 消息内容
            account_ids: 账号ID列表，None表示全部账号
            delay: 操作间隔（秒）

        Returns:
            执行结果
        """
        if account_ids is None:
            account_ids = list(account_manager.accounts.keys())

        if not account_ids:
            return {"success": False, "error": "没有可用账号"}

        delay = self.default_delay if delay is None else delay
        if not valid_bounds(len(account_ids), 5, delay, 3):
            return {"success": False, "error": "Batch-send safety bounds exceeded"}

        decisions = outreach_policy.authorize_many(
            action="batch_send",
            subjects=[chat_id],
            account_ids=account_ids,
            approval_id=approval_id,
        )
        denied = next((decision for decision in decisions if not decision.allowed and decision.reason != "consent_missing_or_invalid"), None)
        if denied:
            log_manager.add_log("OutreachPolicy", "system", denied.audit_summary(), "warning")
            return {"success": False, "error": f"Outreach denied: {denied.reason}"}

        results = []
        success_count = 0
        fail_count = 0

        for account_id in account_ids:
            try:
                # 获取客户端
                client = await account_manager.get_client(account_id)
                if not client:
                    results.append({
                        "account": account_id,
                        "success": False,
                        "error": "客户端不可用"
                    })
                    fail_count += 1
                    log_manager.add_log("批量发送", account_id, "发送失败: 客户端不可用", "error")
                    continue

                # 发送消息
                entity = await client.get_entity(chat_id)
                await dispatch_outreach(action="batch_send", subject=utils.get_peer_id(entity), account_id=account_id, approval_id=approval_id, delay=delay, send=lambda: client.send_message(entity, message))

                results.append({
                    "account": account_id,
                    "success": True
                })
                success_count += 1
                log_manager.add_log("批量发送", account_id, "已批准的批量消息发送成功", "success")
                stats_tracker.record_message_sent(account_id)

            except Exception as e:
                results.append({
                    "account": account_id,
                    "success": False,
                    "error": type(e).__name__
                })
                fail_count += 1
                log_manager.add_log("批量发送", account_id, f"发送失败: {type(e).__name__}", "error")
                health_monitor.record_message_failure(account_id, type(e).__name__)

            # 延迟
            await asyncio.sleep(delay)

        return {
            "success": fail_count == 0,
            "total": len(account_ids),
            "success_count": success_count,
            "fail_count": fail_count,
            "results": results
        }

    async def batch_send_template(
        self,
        chat_id: str,
        template_id: str,
        account_ids: List[str] = None,
        template_vars: Dict = None,
        delay: float = None,
        approval_id: str = None,
    ) -> Dict:
        """
        批量发送模板消息

        Args:
            chat_id: 目标聊天ID
            template_id: 模板ID
            account_ids: 账号ID列表
            template_vars: 模板变量
            delay: 操作间隔

        Returns:
            执行结果
        """
        # 渲染模板
        if template_vars is None:
            template_vars = {}

        # 为每个账号添加默认变量
        messages = {}
        accounts = list(account_manager.accounts.keys()) if account_ids is None else account_ids
        if not valid_bounds(len(accounts), 5, self.default_delay if delay is None else delay, 3):
            return {"success": False, "error": "Batch-send safety bounds exceeded"}

        for account_id in accounts:
            vars_for_account = {
                **template_vars,
                "account": account_id,
                "time": datetime.now().strftime("%H:%M"),
                "date": datetime.now().strftime("%Y-%m-%d")
            }
            messages[account_id] = template_manager.render_template(template_id, **vars_for_account)

        # 批量发送
        results = []
        errors = []
        for account_id, message in messages.items():
            if message:
                result = await self.batch_send_message(
                    chat_id=chat_id,
                    message=message,
                    account_ids=[account_id],
                    delay=delay,
                    approval_id=approval_id,
                )
                results.extend(result.get("results", []))
                if not result.get("success"):
                    errors.append(result.get("error", "Batch send failed"))

        return {
            "success": not errors,
            "template_id": template_id,
            "results": results,
            "errors": errors,
        }

    async def batch_check_health(self, account_ids: List[str] = None) -> Dict:
        """
        批量检查账号健康状态

        Args:
            account_ids: 账号ID列表

        Returns:
            健康检查结果
        """
        if account_ids is None:
            account_ids = list(account_manager.accounts.keys())

        results = []

        for account_id in account_ids:
            result = await health_monitor.check_account_health(account_id)
            results.append({
                "account": account_id,
                "status": result.get("status", "unknown"),
                "details": result
            })

        return {
            "success": True,
            "results": results
        }

    async def batch_export_sessions(self, account_ids: List[str] = None) -> Dict:
        """
        批量导出 Session

        Args:
            account_ids: 账号ID列表

        Returns:
            Session 数据
        """
        if account_ids is None:
            account_ids = list(account_manager.accounts.keys())

        sessions = {}
        for account_id in account_ids:
            session = account_manager.export_session(account_id)
            if session:
                sessions[account_id] = session
                log_manager.add_log("导出Session", account_id, "批量导出", "info")

        return {
            "success": True,
            "sessions": sessions,
            "total": len(sessions)
        }

    async def batch_delete_accounts(self, account_ids: List[str]) -> Dict:
        """
        批量删除账号

        Args:
            account_ids: 账号ID列表

        Returns:
            删除结果
        """
        results = []
        success_count = 0

        for account_id in account_ids:
            if account_id == "default":
                results.append({
                    "account": account_id,
                    "success": False,
                    "error": "无法删除默认账号"
                })
                continue

            success = await account_manager.remove_account(account_id)
            results.append({
                "account": account_id,
                "success": success
            })

            if success:
                success_count += 1
                log_manager.add_log("删除账号", account_id, "批量删除", "warning")

        return {
            "success": success_count == len(account_ids),
            "total": len(account_ids),
            "success_count": success_count,
            "results": results
        }

    async def batch_get_dialogs(
        self,
        account_ids: List[str] = None,
        limit: int = 20
    ) -> Dict:
        """
        批量获取对话列表

        Args:
            account_ids: 账号ID列表
            limit: 每个账号获取的数量

        Returns:
            对话列表
        """
        if account_ids is None:
            account_ids = list(account_manager.accounts.keys())

        dialogs = {}

        for account_id in account_ids:
            try:
                client = await account_manager.get_client(account_id)
                if client:
                    result = await client.get_dialogs(limit=limit)
                    dialogs[account_id] = [
                        {
                            "id": d.id,
                            "name": d.name,
                            "unread": d.unread_count,
                            "type": "user" if d.is_user else "chat" if d.is_group else "channel"
                        }
                        for d in result
                    ]
                    log_manager.add_log("获取对话", account_id, f"获取 {len(result)} 个对话", "info")
            except Exception as e:
                dialogs[account_id] = []
                log_manager.add_log("获取对话", account_id, f"获取失败: {type(e).__name__}", "error")

        return {
            "success": True,
            "dialogs": dialogs
        }


# 全局实例
batch_operations = BatchOperations()
