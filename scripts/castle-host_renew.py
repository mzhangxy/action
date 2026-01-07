#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Castle-Host 自动续约脚本
- 多账号支持（逗号分隔）
- 自动获取服务器ID
- 自动启动关机服务器
- 自动续约
"""

import os
import sys
import re
import logging
import asyncio
import aiohttp
from enum import Enum
from base64 import b64encode
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
from playwright.async_api import async_playwright, BrowserContext, Page

LOG_FILE = "castle_renew.log"
REQUEST_TIMEOUT = 10
PAGE_TIMEOUT = 60000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")]
)
logger = logging.getLogger(__name__)

class RenewalStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"

@dataclass
class ServerResult:
    server_id: str
    status: RenewalStatus
    message: str
    expiry: str = ""
    days: int = 0
    started: bool = False

@dataclass
class Config:
    cookies_list: List[str]
    tg_token: Optional[str]
    tg_chat_id: Optional[str]
    repo_token: Optional[str]
    repository: Optional[str]

    @classmethod
    def from_env(cls) -> "Config":
        raw = os.environ.get("CASTLE_COOKIES", "").strip()
        return cls(
            cookies_list=[c.strip() for c in raw.split(",") if c.strip()],
            tg_token=os.environ.get("TG_BOT_TOKEN"),
            tg_chat_id=os.environ.get("TG_CHAT_ID"),
            repo_token=os.environ.get("REPO_TOKEN"),
            repository=os.environ.get("GITHUB_REPOSITORY")
        )

def mask_id(sid: str) -> str:
    """隐藏ID: 117987 -> 1***87"""
    return f"{sid[0]}***{sid[-2:]}" if len(sid) > 3 else sid

def convert_date(s: str) -> str:
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s) if s else None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else "Unknown"

def days_left(s: str) -> int:
    try:
        d = datetime.strptime(s, "%d.%m.%Y")
        return (d - datetime.now()).days
    except:
        return 0

def parse_cookies(s: str) -> List[Dict]:
    cookies = []
    for p in s.split(";"):
        p = p.strip()
        if "=" in p:
            n, v = p.split("=", 1)
            cookies.append({"name": n.strip(), "value": v.strip(), "domain": ".castle-host.com", "path": "/"})
    return cookies

def analyze_error(msg: str) -> Tuple[RenewalStatus, str]:
    m = msg.lower()
    if "24 час" in m or "уже продлен" in m:
        return RenewalStatus.RATE_LIMITED, "今日已续期"
    if "недостаточно" in m:
        return RenewalStatus.FAILED, "余额不足"
    return RenewalStatus.FAILED, msg

class Notifier:
    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token, self.chat_id = token, chat_id
    
    async def send(self, msg: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": msg}, timeout=REQUEST_TIMEOUT) as r:
                    if r.status == 200:
                        logger.info("✅ 通知已发送")
                        return True
        except Exception as e:
            logger.error(f"❌ 通知异常: {e}")
        return False
    
    async def send_file(self, content: str, filename: str, caption: str = "") -> bool:
        """发送txt文件"""
        if not self.token or not self.chat_id:
            return False
        try:
            async with aiohttp.ClientSession() as s:
                data = aiohttp.FormData()
                data.add_field('chat_id', self.chat_id)
                data.add_field('document', content.encode('utf-8'), filename=filename, content_type='text/plain')
                if caption:
                    data.add_field('caption', caption)
                async with s.post(f"https://api.telegram.org/bot{self.token}/sendDocument",
                    data=data, timeout=REQUEST_TIMEOUT) as r:
                    if r.status == 200:
                        logger.info("✅ 文件已发送")
                        return True
        except Exception as e:
            logger.error(f"❌ 文件发送异常: {e}")
        return False

class GitHubManager:
    def __init__(self, token: Optional[str], repo: Optional[str]):
        self.token, self.repo = token, repo
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"} if token else {}
    
    async def update_secret(self, name: str, value: str) -> bool:
        if not self.token or not self.repo:
            return False
        try:
            from nacl import encoding, public
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key", headers=self.headers) as r:
                    if r.status != 200:
                        return False
                    kd = await r.json()
                pk = public.PublicKey(kd["key"].encode(), encoding.Base64Encoder())
                enc = b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()
                async with s.put(f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                    headers=self.headers, json={"encrypted_value": enc, "key_id": kd["key_id"]}) as r:
                    if r.status in [201, 204]:
                        logger.info(f"✅ Secret {name} 已更新")
                        return True
        except Exception as e:
            logger.error(f"❌ GitHub异常: {e}")
        return False

class CastleClient:
    def __init__(self, ctx: BrowserContext, page: Page):
        self.ctx, self.page = ctx, page
        self.base = "https://cp.castle-host.com"
    
    async def get_server_ids(self) -> List[str]:
        """从/servers页面提取服务器ID"""
        try:
            await self.page.goto(f"{self.base}/servers", wait_until="networkidle")
            content = await self.page.content()
            match = re.search(r'var\s+ServersID\s*=\s*\[([\d,\s]+)\]', content)
            if match:
                ids = [x.strip() for x in match.group(1).split(",") if x.strip()]
                # 日志中隐藏ID
                masked = [mask_id(x) for x in ids]
                logger.info(f"📋 找到 {len(ids)} 个服务器: {masked}")
                return ids
        except Exception as e:
            logger.error(f"❌ 获取服务器ID失败: {e}")
        return []
    
    async def start_if_stopped(self, sid: str) -> bool:
        """如果服务器关机则启动"""
        masked = mask_id(sid)
        try:
            if "/servers" not in self.page.url:
                await self.page.goto(f"{self.base}/servers", wait_until="networkidle")
            btn = self.page.locator(f'button[onclick*="sendAction({sid},\'start\')"]')
            if await btn.count() > 0:
                logger.info(f"🔴 服务器 {masked} 已关机，启动中...")
                await btn.click()
                await self.page.wait_for_timeout(5000)
                logger.info(f"🟢 服务器 {masked} 已启动")
                return True
            logger.info(f"✅ 服务器 {masked} 运行中")
        except Exception as e:
            logger.error(f"❌ 启动服务器失败: {e}")
        return False
    
    async def get_expiry(self, sid: str) -> str:
        """获取到期时间"""
        try:
            await self.page.goto(f"{self.base}/servers/pay/index/{sid}", wait_until="networkidle")
            text = await self.page.text_content("body")
            match = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
            return match.group(1) if match else ""
        except:
            return ""
    
    async def renew(self, sid: str) -> Tuple[RenewalStatus, str]:
        """执行续约"""
        masked = mask_id(sid)
        api_resp: Dict = {}
        
        async def capture(resp):
            if "/buy_months/" in resp.url:
                try:
                    api_resp["data"] = await resp.json()
                except:
                    pass
        
        self.page.on("response", capture)
        
        for sel in ["#freebtn", 'button:has-text("Продлить")', 'a:has-text("Продлить")', 
                    'button:has-text("Бесплатно")', 'a:has-text("Бесплатно")']:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    logger.info(f"🖱️ 服务器 {masked} 已点击续约")
                    
                    for _ in range(20):
                        if api_resp.get("data"):
                            break
                        await asyncio.sleep(0.5)
                    
                    if api_resp.get("data"):
                        data = api_resp["data"]
                        if data.get("status") == "error":
                            return analyze_error(data.get("error", ""))
                        if data.get("status") in ["success", "ok"]:
                            return RenewalStatus.SUCCESS, "续约成功"
                    
                    await self.page.wait_for_timeout(2000)
                    text = await self.page.text_content("body")
                    if "24 час" in text:
                        return RenewalStatus.RATE_LIMITED, "今日已续期"
                    return RenewalStatus.SUCCESS, "续约成功"
            except:
                continue
        
        return RenewalStatus.FAILED, "未找到续约按钮"
    
    async def extract_cookies(self) -> Optional[str]:
        try:
            cookies = await self.ctx.cookies()
            cc = [c for c in cookies if "castle-host.com" in c.get("domain", "")]
            return "; ".join([f"{c['name']}={c['value']}" for c in cc]) if cc else None
        except:
            return None

async def process_account(cookie_str: str, idx: int, config: Config, notifier: Notifier) -> Tuple[Optional[str], List[str]]:
    """处理单个账号，返回(新Cookie, 启动的服务器ID列表)"""
    cookies = parse_cookies(cookie_str)
    if not cookies:
        logger.error(f"❌ 账号#{idx+1} Cookie解析失败")
        return None, []
    
    logger.info(f"{'='*50}")
    logger.info(f"📌 处理账号 #{idx+1}")
    
    started_servers: List[str] = []  # 记录启动的服务器
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)
        client = CastleClient(ctx, page)
        results: List[ServerResult] = []
        
        try:
            server_ids = await client.get_server_ids()
            if not server_ids:
                if "login" in page.url:
                    logger.error(f"❌ 账号#{idx+1} Cookie已失效")
                    await notifier.send(f"❌ 账号#{idx+1} Cookie已失效")
                return None, []
            
            for sid in server_ids:
                masked = mask_id(sid)
                logger.info(f"--- 处理服务器 {masked} ---")
                
                started = await client.start_if_stopped(sid)
                if started:
                    started_servers.append(sid)
                
                expiry = await client.get_expiry(sid)
                d = days_left(expiry)
                logger.info(f"📅 到期: {convert_date(expiry)} ({d}天)")
                
                status, msg = await client.renew(sid)
                logger.info(f"📝 结果: {msg}")
                
                results.append(ServerResult(sid, status, msg, expiry, d, started))
                await asyncio.sleep(2)
            
            # 发送详细通知
            for r in results:
                if r.status == RenewalStatus.SUCCESS:
                    stat = "✅ 续约成功 (+1天)"
                elif r.status == RenewalStatus.RATE_LIMITED:
                    stat = "📝 今日已续期"
                else:
                    stat = f"❌ 续约失败: {r.message}"
                
                started_line = "🟢 服务器已启动\n" if r.started else ""
                
                msg = f"""🎁 Castle-Host 自动续约通知

👤 账号: #{idx+1}
💻 服务器: {r.server_id}
📅 到期时间: {convert_date(r.expiry)}
⏳ 剩余天数: {r.days} 天
🔗 https://cp.castle-host.com/servers/pay/index/{r.server_id}

{started_line}{stat}"""
                await notifier.send(msg)
            
            new_cookie = await client.extract_cookies()
            if new_cookie and new_cookie != cookie_str:
                logger.info(f"🔄 账号#{idx+1} Cookie已变化")
                return new_cookie, started_servers
            return cookie_str, started_servers
            
        except Exception as e:
            logger.error(f"❌ 账号#{idx+1} 异常: {e}")
            await notifier.send(f"❌ 账号#{idx+1} 异常: {e}")
            return None, []
        finally:
            await ctx.close()
            await browser.close()

async def main():
    logger.info("=" * 50)
    logger.info("Castle-Host 自动续约")
    logger.info("=" * 50)
    
    config = Config.from_env()
    if not config.cookies_list:
        logger.error("❌ 未设置 CASTLE_COOKIES")
        return
    
    logger.info(f"📊 共 {len(config.cookies_list)} 个账号")
    
    notifier = Notifier(config.tg_token, config.tg_chat_id)
    github = GitHubManager(config.repo_token, config.repository)
    
    new_cookies = []
    changed = False
    all_started: List[str] = []  # 所有启动的服务器
    
    for i, cookie in enumerate(config.cookies_list):
        new, started = await process_account(cookie, i, config, notifier)
        all_started.extend(started)
        if new:
            new_cookies.append(new)
            if new != cookie:
                changed = True
        else:
            new_cookies.append(cookie)
        
        if i < len(config.cookies_list) - 1:
            await asyncio.sleep(5)
    
    # 如果有服务器启动，发送txt文件
    if all_started:
        content = "Castle-Host 已启动的服务器\n" + "=" * 30 + "\n\n"
        for sid in all_started:
            content += f"服务器ID: {sid}\n"
            content += f"控制面板: https://cp.castle-host.com/servers/control/index/{sid}\n\n"
        await notifier.send_file(content, "castle-host-started.txt", "🟢 已启动的服务器列表")
    
    if changed:
        await github.update_secret("CASTLE_COOKIES", ",".join(new_cookies))
    
    logger.info("👋 完成")

if __name__ == "__main__":
    asyncio.run(main())
