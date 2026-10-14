#!/usr/bin/env python3
"""
代理管理核心模块
支持全局代理和独立代理，真实生效
"""
import asyncio
import json
import os
import ipaddress
import ssl
import tempfile
import time
from copy import deepcopy
from urllib.parse import urlsplit
from datetime import datetime
from typing import Dict, Optional
import aiohttp


ACCOUNTS_DIR = "./accounts"
FREE_PROXY_SOURCE = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/protocols/socks5/data.txt"
VERIFIED_PROXY_SOURCE = "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt"
PROXIES_FILE = os.path.join(ACCOUNTS_DIR, "proxies.json")


class ProxyManager:
    """代理管理器"""

    def __init__(self):
        self.proxies: Dict[str, Dict] = {}  # 代理配置
        self.global_proxy: Optional[Dict] = None  # 全局代理
        self.proxy_stats: Dict[str, Dict] = {}  # 代理统计
        self.free_global_enabled = True
        self.auto_failover_enabled = True
        self.maintenance = {}
        self._free_proxy_lock = asyncio.Lock()
        self._load_proxies()

    def _load_proxies(self):
        """加载代理配置"""
        if os.path.exists(PROXIES_FILE):
            with open(PROXIES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.free_global_enabled = data.get("free_global_enabled", True)
                self.auto_failover_enabled = data.get("auto_failover_enabled", True)
                self.maintenance = data.get("maintenance", {})
                self.global_proxy = data.get("global")
                self.proxies = data.get("proxies", {})
                self.proxy_stats = data.get("stats", {})

    def _save_proxies(self):
        """保存代理配置"""
        directory = os.path.dirname(os.path.abspath(PROXIES_FILE))
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".proxies-", dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({
                    "free_global_enabled": self.free_global_enabled,
                    "auto_failover_enabled": self.auto_failover_enabled,
                    "maintenance": self.maintenance,
                    "global": self.global_proxy,
                    "proxies": self.proxies,
                    "stats": self.proxy_stats
                }, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, PROXIES_FILE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list_proxies(self) -> Dict:
        """
        列出所有代理

        Returns:
            {
                "global": 全局代理配置,
                "proxies": 账号独立代理列表,
                "stats": 代理统计信息
            }
        """
        return {
            "free_global_enabled": self.free_global_enabled,
            "auto_failover_enabled": self.auto_failover_enabled,
            "maintenance": self.maintenance,
            "global": self.global_proxy,
            "proxies": self.proxies,
            "stats": self.proxy_stats
        }

    def add_proxy(
        self,
        proxy_id: str,
        protocol: str,
        host: str,
        port: int,
        username: str = None,
        password: str = None
    ) -> bool:
        """
        添加代理

        Args:
            proxy_id: 代理ID
            protocol: 协议 (socks5, http, https, socks4)
            host: 主机地址
            port: 端口
            username: 用户名
            password: 密码

        Returns:
            是否成功
        """
        if (protocol.lower() not in ["socks5", "http", "socks4"] or not host.strip()
                or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
            return False

        self.proxies[proxy_id] = {
            "proxy_id": proxy_id,
            "protocol": protocol.lower(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "created_at": datetime.now().isoformat()
        }

        # 初始化统计
        if proxy_id not in self.proxy_stats:
            self.proxy_stats[proxy_id] = {
                "success_count": 0,
                "fail_count": 0,
                "avg_response_time": 0,
                "last_test": None
            }

        self._save_proxies()
        return True

    def delete_proxy(self, proxy_id: str) -> bool:
        """
        删除代理

        Args:
            proxy_id: 代理ID

        Returns:
            是否成功
        """
        if proxy_id not in self.proxies:
            return False

        del self.proxies[proxy_id]

        # 清除相关账号的代理引用
        # (实际使用时会从 account_manager 读取)

        self._save_proxies()
        return True

    def set_global_proxy(
        self,
        protocol: str,
        host: str,
        port: int,
        username: str = None,
        password: str = None
    ) -> bool:
        """
        设置全局代理

        Args:
            protocol: 协议
            host: 主机
            port: 端口
            username: 用户名
            password: 密码

        Returns:
            是否成功
        """
        if (protocol.lower() not in ["socks5", "http", "socks4"] or not host.strip()
                or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
            return False

        self.global_proxy = {
            "protocol": protocol.lower(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "updated_at": datetime.now().isoformat()
        }
        self.maintenance = {}

        self._save_proxies()
        return True

    def remove_global_proxy(self) -> bool:
        """
        移除全局代理

        Returns:
            是否成功
        """
        self.global_proxy = None
        self.free_global_enabled = False
        self.maintenance = {"status": "disabled"}
        self._save_proxies()
        return True

    def assign_proxy_to_account(self, account_id: str, proxy_id: str) -> bool:
        """
        为账号分配代理

        Args:
            account_id: 账号ID
            proxy_id: 代理ID

        Returns:
            是否成功
        """
        if proxy_id not in self.proxies:
            return False

        # 这个关联关系应该存储在账号配置中
        # 但为了统一管理，我们也在代理配置中记录
        if "assigned_to" not in self.proxies[proxy_id]:
            self.proxies[proxy_id]["assigned_to"] = []

        if account_id not in self.proxies[proxy_id]["assigned_to"]:
            self.proxies[proxy_id]["assigned_to"].append(account_id)

        self._save_proxies()
        return True

    def unassign_proxy_from_account(self, account_id: str, proxy_id: str) -> bool:
        """
        取消账号的代理分配

        Args:
            account_id: 账号ID
            proxy_id: 代理ID

        Returns:
            是否成功
        """
        if proxy_id not in self.proxies:
            return False

        if "assigned_to" in self.proxies[proxy_id]:
            if account_id in self.proxies[proxy_id]["assigned_to"]:
                self.proxies[proxy_id]["assigned_to"].remove(account_id)

        self._save_proxies()
        return True

    def to_telethon_format(self, proxy_config: Dict) -> Dict:
        """
        转换为 Telethon 代理格式

        智能处理各种代理配置组合：
        - 有用户名和密码
        - 只有密码
        - 只有用户名
        - 都没有

        Args:
            proxy_config: 代理配置

        Returns:
            Telethon 格式的代理配置
        """
        protocol = proxy_config.get("protocol", "socks5")
        if protocol not in {"socks5", "socks4", "http"}:
            raise ValueError("Telegram supports SOCKS5, SOCKS4 and HTTP CONNECT proxies. Choose a supported proxy.")
        host = proxy_config.get('host')
        port = proxy_config.get('port')
        username = proxy_config.get('username')
        password = proxy_config.get('password')

        # 基础配置
        config = {
            'proxy_type': protocol,
            'addr': host,
            'port': port
        }

        # 根据不同协议添加认证信息
        if protocol == "socks5":
            config['rdns'] = True
            # SOCKS5 支持：有用户名+密码、只有密码、都没有
            if username or password:
                config['username'] = username or ''
                config['password'] = password or ''

        elif protocol == "http" or protocol == "https":
            # HTTP/HTTPS 支持：有用户名+密码
            if username and password:
                config['username'] = username
                config['password'] = password

        elif protocol == "socks4":
            # SOCKS4 只支持用户名，不支持密码
            if username:
                config['username'] = username

        return config

    def get_global_proxy(self) -> Optional[Dict]:
        """
        获取全局代理配置（Telethon格式）

        Returns:
            Telethon 格式的代理配置，如果无全局代理则返回 None
        """
        if self.global_proxy and self.global_proxy.get("host") and self.global_proxy.get("port"):
            return self.to_telethon_format(self.global_proxy)
        return None

    def get_proxy(self, proxy_id: str) -> Optional[Dict]:
        """
        获取指定代理配置（Telethon格式）

        Args:
            proxy_id: 代理ID

        Returns:
            Telethon 格式的代理配置，如果代理不存在则返回 None
        """
        if proxy_id in self.proxies:
            proxy_config = self.proxies[proxy_id]
            if proxy_config.get("host") and proxy_config.get("port"):
                return self.to_telethon_format(proxy_config)
        return None

    def get_proxy_for_account(self, account_id: str) -> Optional[Dict]:
        """
        获取账号的代理配置（Telethon格式）

        Args:
            account_id: 账号ID

        Returns:
            Telethon 格式的代理配置
        """
        # 1. 先检查账号是否有独立代理
        for proxy_id, proxy_config in self.proxies.items():
            if "assigned_to" in proxy_config:
                if account_id in proxy_config["assigned_to"]:
                    # 验证代理配置有效
                    if proxy_config.get("host") and proxy_config.get("port"):
                        return self.to_telethon_format(proxy_config)

        # 2. 使用全局代理（必须有有效的host和port）
        if self.global_proxy and self.global_proxy.get("host") and self.global_proxy.get("port"):
            return self.to_telethon_format(self.global_proxy)

        # 3. 无代理
        return None

    def resolve_login_proxy(self, mode="default", proxy_id=None, account_id=None):
        """Resolve an explicit route. Never silently replace a selected proxy."""
        if mode == "direct":
            return None
        if mode == "default":
            return self.get_proxy_for_account(account_id) if account_id else self.get_global_proxy()
        if mode == "selected" and isinstance(proxy_id, str) and proxy_id:
            proxy = self.get_proxy(proxy_id)
            if proxy:
                return proxy
            raise ValueError("Selected proxy is unavailable. Choose another saved proxy.")
        raise ValueError("Choose Global default, Direct connection, or a saved proxy.")

    @staticmethod
    def free_candidates(text):
        """Accept only public SOCKS5 IPv4/IPv6 addresses from the fixed source."""
        candidates = []
        seen = set()
        for line in text.splitlines():
            try:
                url = urlsplit(line.strip())
                address = ipaddress.ip_address(url.hostname or "")
                if (url.scheme != "socks5" or not address.is_global or not url.port
                        or url.username or url.password or url.path or url.query or url.fragment):
                    continue
                key = (str(address), url.port)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({"protocol": "socks5", "host": str(address), "port": url.port})
            except ValueError:
                continue
        return candidates

    async def fetch_free_candidates(self):
        # Prefer the short, hourly checked list, sorted by response time. Keep
        # the existing source as a fallback when that list cannot be fetched.
        for source in (VERIFIED_PROXY_SOURCE, FREE_PROXY_SOURCE):
            try:
                candidates = await self._fetch_free_source(source)
                if candidates:
                    return candidates
            except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
                continue
        raise ValueError("No public SOCKS5 candidates available")

    async def _fetch_free_source(self, source):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(source, allow_redirects=False) as response:
                response.raise_for_status()
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        raise ValueError("Free proxy list exceeds size limit")
                    chunks.append(chunk)
        return self.free_candidates(b"".join(chunks).decode("utf-8"))

    async def ensure_free_global_proxy(self, enable=False):
        """Use one tested free proxy as a stable default; never rotate on restrictions."""
        async with self._free_proxy_lock:
            if enable:
                self.free_global_enabled = True
                self._save_proxies()
            if self.global_proxy:
                return {"success": True, "status": "existing_global_preserved"}
            if not self.free_global_enabled:
                return {"success": False, "status": "disabled"}
            try:
                candidates = await self.fetch_free_candidates()
                selected = candidates[:24]
                for start in range(0, len(selected), 4):
                    group = selected[start:start + 4]
                    results = await asyncio.gather(*(self.test_proxy(p, timeout=3) for p in group))
                    for config, result in zip(group, results):
                        if not result.get("success"):
                            continue
                        # A manual setting created while probing always wins.
                        if self.global_proxy or not self.free_global_enabled:
                            return {"success": bool(self.global_proxy), "status": "configuration_changed"}
                        self.set_global_proxy(**config)
                        self.global_proxy.update({"source": "verified_public_list", "free": True,
                                                  "verified_at": datetime.now().isoformat()})
                        self._save_proxies()
                        return {"success": True, "status": "verified_free_global", "tested": start + len(group)}
                return {"success": False, "status": "no_verified_free_proxy", "tested": len(selected)}
            except Exception as error:
                return {"success": False, "status": "free_proxy_source_unavailable", "error": type(error).__name__}

    async def maintain_free_global_proxy(self):
        """Replace only an unhealthy managed proxy, never on a Telegram RPC error.

        Keep live sessions connected. Disconnected sessions select the new proxy
        through the existing serialized account reconnect path. Never replay sends.
        """
        async with self._free_proxy_lock:
            if not self.free_global_enabled or not self.auto_failover_enabled:
                return {"status": "disabled"}
            if self.global_proxy and not self.global_proxy.get("free"):
                return {"status": "manual_global_preserved"}
            now = time.time()
            if now < self.maintenance.get("next_check", 0):
                return {"status": "backoff"}
            original = deepcopy(self.global_proxy)
            previous = deepcopy(self.maintenance)
            published = False

            def unchanged():
                return (self.global_proxy == original and self.free_global_enabled
                        and self.auto_failover_enabled)

            try:
                if original:
                    result = await self.test_proxy(original, timeout=3)
                    if not unchanged():
                        return {"status": "configuration_changed"}
                    failures = 0 if result.get("success") else previous.get("consecutive_failures", 0) + 1
                    self.maintenance = {**previous, "last_check": now, "next_check": now + 300,
                                        "consecutive_failures": failures,
                                        "status": "healthy" if not failures else "probe_failed"}
                    if failures < 2:
                        self._save_proxies()
                        return {"status": self.maintenance["status"]}

                # Discovery is bounded and excludes the failed endpoint.
                candidates = await self.fetch_free_candidates()
                candidates = [p for p in candidates if not original or
                              (p["host"], p["port"]) != (original["host"], original["port"])][:24]
                replacement = None
                for start in range(0, len(candidates), 4):
                    group = candidates[start:start + 4]
                    results = await asyncio.gather(*(self.test_proxy(p, timeout=3) for p in group))
                    replacement = next((p for p, r in zip(group, results) if r.get("success")), None)
                    if replacement:
                        break
                if not unchanged():
                    return {"status": "configuration_changed"}
                if replacement:
                    self.global_proxy = {**replacement, "free": True, "source": "verified_public_list",
                                         "verified_at": datetime.now().isoformat()}
                    published = True
                    self.maintenance = {"status": "replaced" if original else "initialized",
                                        "last_check": now, "next_check": now + 300,
                                        "last_failover": now, "consecutive_failures": 0}
                else:
                    self.maintenance.update(status="no_verified_replacement", last_check=now, next_check=now + 900)
                self._save_proxies()
                return {"status": self.maintenance["status"]}
            except Exception as exc:
                # Never discard the saved endpoint or change to direct transport
                # when a source, candidate or disk operation fails.
                if unchanged() or published:
                    self.global_proxy = original
                    self.maintenance = {**previous, "status": "maintenance_failed", "last_check": now,
                                        "next_check": now + 900, "error": type(exc).__name__}
                    try:
                        self._save_proxies()
                    except OSError:
                        pass
                return {"status": "maintenance_failed", "error": type(exc).__name__}

    async def test_socks_proxy(self, proxy_config, timeout):
        from python_socks import ProxyType
        from python_socks.async_.asyncio import Proxy

        proxy = Proxy(proxy_type=ProxyType.SOCKS5 if proxy_config["protocol"] == "socks5" else ProxyType.SOCKS4,
                      host=proxy_config["host"], port=proxy_config["port"],
                      username=proxy_config.get("username"), password=proxy_config.get("password"), rdns=True)
        # Verify Telegram transport connectivity, without authentication or RPCs.
        sock = await proxy.connect(dest_host="149.154.167.51", dest_port=443, timeout=timeout)
        sock.close()
        # Verify an authenticated TLS tunnel as well; a connect-only proxy can lie.
        sock = await proxy.connect(dest_host="telegram.org", dest_port=443, timeout=timeout)
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                sock=sock, ssl=ssl.create_default_context(), server_hostname="telegram.org"), timeout)
            writer.write(b"HEAD / HTTP/1.1\r\nHost: telegram.org\r\nConnection: close\r\n\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout)
            return b" 200 " in status or b" 302 " in status or b" 301 " in status
        finally:
            if writer:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout)
                except (TimeoutError, OSError):
                    pass
            else:
                sock.close()

    async def test_proxy(self, proxy_config: Dict, timeout: int = 10) -> Dict:
        """
        测试代理连接

        Args:
            proxy_config: 代理配置
            timeout: 超时时间（秒）

        Returns:
            {
                "success": True/False,
                "response_time": 响应时间(ms),
                "error": 错误信息
            }
        """
        start_time = datetime.now()

        try:
            if proxy_config.get("protocol") in {"socks5", "socks4"}:
                success = await asyncio.wait_for(self.test_socks_proxy(proxy_config, timeout), timeout * 3)
                return {"success": success, "response_time": (datetime.now() - start_time).total_seconds() * 1000}
            # 构建代理URL
            protocol = proxy_config.get("protocol", "http")
            host = proxy_config.get("host")
            port = proxy_config.get("port")
            username = proxy_config.get("username")
            password = proxy_config.get("password")

            if username and password:
                proxy_url = f"{protocol}://{username}:{password}@{host}:{port}"
            else:
                proxy_url = f"{protocol}://{host}:{port}"

            # 测试连接（访问Google）
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://www.google.com",
                    proxy=proxy_url,
                    timeout=timeout
                ) as response:
                    if response.status == 200:
                        elapsed = (datetime.now() - start_time).total_seconds() * 1000
                        return {
                            "success": True,
                            "response_time": elapsed,
                            "status_code": response.status
                        }

            return {"success": False, "error": f"HTTP {response.status}"}

        except asyncio.TimeoutError:
            return {"success": False, "error": "Connection timed out"}
        except Exception as e:
            return {"success": False, "error": type(e).__name__}

    async def test_all_proxies(self) -> Dict:
        """
        测试所有代理

        Returns:
            {
                "global": 全局代理测试结果,
                "proxies": 独立代理测试结果列表
            }
        """
        results = {"global": None, "proxies": {}}

        # 测试全局代理
        if self.global_proxy:
            results["global"] = await self.test_proxy(self.global_proxy)
            self._update_proxy_stats("global", results["global"])

        # 测试独立代理
        for proxy_id, proxy_config in self.proxies.items():
            result = await self.test_proxy(proxy_config)
            results["proxies"][proxy_id] = result
            self._update_proxy_stats(proxy_id, result)

        self._save_proxies()
        return results

    def _update_proxy_stats(self, proxy_id: str, test_result: Dict):
        """更新代理统计信息"""
        if proxy_id not in self.proxy_stats:
            self.proxy_stats[proxy_id] = {
                "success_count": 0,
                "fail_count": 0,
                "avg_response_time": 0,
                "last_test": None
            }

        stats = self.proxy_stats[proxy_id]

        if test_result.get("success"):
            stats["success_count"] += 1
            # 更新平均响应时间
            response_time = test_result.get("response_time", 0)
            if stats["avg_response_time"] == 0:
                stats["avg_response_time"] = response_time
            else:
                stats["avg_response_time"] = (stats["avg_response_time"] + response_time) / 2
        else:
            stats["fail_count"] += 1

        stats["last_test"] = datetime.now().isoformat()


# 全局实例
proxy_manager = ProxyManager()
