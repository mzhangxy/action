#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# scripts/lunes_renew.py

import os
import sys
import re
import io
import logging
import asyncio
import aiohttp
from base64 import b64encode
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from playwright.async_api import async_playwright, BrowserContext, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

@dataclass
class ServerInfo:
    server_id: str
    name: str
    is_active: bool

@dataclass
class Config:
    cookies_list: List[str]
    tg_token: Optional[str]
    tg_chat_id: Optional[str]
    repo_token: Optional[str]
    repository: Optional[str]

    @classmethod
    def from_env(cls) -> "Config":
        raw = os.environ.get("LUNES_COOKIES", "").strip()
        return cls(
            cookies_list=[c.strip() for c in raw.split(",") if c.strip()],
            tg_token=os.environ.get("TG_BOT_TOKEN"),
            tg_chat_id=os.environ.get("TG_CHAT_ID"),
            repo_token=os.environ.get("REPO_TOKEN"),
            repository=os.environ.get("GITHUB_REPOSITORY")
        )

def parse_cookies(s: str) -> List[Dict]:
    cookies = []
    for p in s.split(";"):
        p = p.strip()
        if "=" in p:
            n, v = p.split("=", 1)
            cookies.append({"name": n.strip(), "value": v.strip(), "domain": ".lunes.host", "path": "/"})
    return cookies

class Notifier:
    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token, self.chat_id = token, chat_id
    
    async def send(self, msg: str) -> Optional[int]:
        if not self.token or not self.chat_id:
            return None
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data.get('result', {}).get('message_id')
        except Exception as e:
            logger.error(f"通知失败: {e}")
        return None
    
    async def send_photo(self, photo_bytes: bytes, caption: str = "") -> bool:
        if not self.token or not self.chat_id:
            return False
        try:
            async with aiohttp.ClientSession() as s:
                data = aiohttp.FormData()
                data.add_field('chat_id', str(self.chat_id))
                data.add_field('photo', photo_bytes, filename='screenshot.png', content_type='image/png')
                if caption:
                    data.add_field('caption', caption)
                async with s.post(
                    f"https://api.telegram.org/bot{self.token}/sendPhoto",
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as r:
                    return r.status == 200
        except Exception as e:
            logger.error(f"发送图片失败: {e}")
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
                async with s.put(
                    f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                    headers=self.headers, 
                    json={"encrypted_value": enc, "key_id": kd["key_id"]}
                ) as r:
                    if r.status in [201, 204]:
                        logger.info(f"✅ Secret {name} 已更新")
                        return True
        except Exception as e:
            logger.error(f"GitHub异常: {e}")
        return False

class LunesClient:
    def __init__(self, ctx: BrowserContext, page: Page):
        self.ctx, self.page = ctx, page
        self.dashboard_url = "https://betadash.lunes.host/"
        self.ctrl_url = "https://ctrl.lunes.host/server"
    
    async def get_servers(self) -> List[ServerInfo]:
        """获取所有服务器信息"""
        servers = []
        try:
            await self.page.goto(self.dashboard_url, wait_until="networkidle", timeout=60000)
            await self.page.wait_for_timeout(2000)
            
            # 检查是否需要登录
            if "/login" in self.page.url:
                logger.error("Cookie已失效，需要重新登录")
                return []
            
            # 解析服务器卡片
            cards = await self.page.locator("a.server-card").all()
            for card in cards:
                href = await card.get_attribute("href") or ""
                match = re.search(r"/servers/(\d+)", href)
                if not match:
                    continue
                
                server_id = match.group(1)
                name_el = card.locator(".server-title")
                name = await name_el.text_content() if await name_el.count() > 0 else server_id
                
                status_el = card.locator(".server-status")
                status_text = await status_el.text_content() if await status_el.count() > 0 else ""
                is_active = "Active" in status_text
                
                servers.append(ServerInfo(server_id, name.strip(), is_active))
                logger.info(f"📋 服务器: {server_id} ({name.strip()}) - {'Active' if is_active else 'Inactive'}")
            
            logger.info(f"共找到 {len(servers)} 个服务器")
        except Exception as e:
            logger.error(f"获取服务器列表失败: {e}")
        return servers
    
    async def start_server(self, server_id: str) -> Tuple[bool, Optional[bytes]]:
        """启动服务器并截图，返回(是否成功, 截图)"""
        try:
            url = f"{self.ctrl_url}/{server_id}"
            logger.info(f"访问控制台: {url}")
            await self.page.goto(url, wait_until="networkidle", timeout=60000)
            await self.page.wait_for_timeout(3000)
            
            # 查找 Start 按钮
            start_btn = self.page.locator('button:has-text("Start")').first
            if await start_btn.count() > 0:
                disabled = await start_btn.get_attribute("disabled")
                if disabled is None:  # 按钮可点击
                    logger.info(f"🔴 服务器 {server_id} 已停止，正在启动...")
                    await start_btn.click()
                    await self.page.wait_for_timeout(5000)
                    logger.info(f"🟢 服务器 {server_id} 启动命令已发送")
                    
                    # 截图
                    screenshot = await self.page.screenshot(full_page=True)
                    return True, screenshot
                else:
                    logger.info(f"✅ 服务器 {server_id} Start按钮已禁用（可能正在运行）")
            else:
                logger.info(f"✅ 服务器 {server_id} 未找到Start按钮")
            
            return False, None
        except Exception as e:
            logger.error(f"启动服务器 {server_id} 失败: {e}")
            return False, None
    
    async def extract_cookies(self) -> Optional[str]:
        """提取当前Cookie"""
        try:
            cookies = await self.ctx.cookies()
            lunes_cookies = [c for c in cookies if "lunes.host" in c.get("domain", "")]
            if lunes_cookies:
                return "; ".join([f"{c['name']}={c['value']}" for c in lunes_cookies])
        except Exception as e:
            logger.error(f"提取Cookie失败: {e}")
        return None

async def process_account(cookie_str: str, idx: int, notifier: Notifier) -> Tuple[Optional[str], List[dict]]:
    """处理单个账号，返回(新Cookie, 启动的服务器列表)"""
    cookies = parse_cookies(cookie_str)
    if not cookies:
        logger.error(f"❌ 账号#{idx+1} Cookie解析失败")
        return None, []
    
    logger.info(f"{'='*50}")
    logger.info(f"📌 处理账号 #{idx+1}")
    
    started_servers = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.97 Safari/537.36",
            viewport={"width": 1366, "height": 768}
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        client = LunesClient(ctx, page)
        
        try:
            servers = await client.get_servers()
            if not servers:
                await notifier.send(f"❌ 账号#{idx+1} 获取服务器失败或Cookie已失效")
                return None, []
            
            for server in servers:
                if server.is_active:
                    logger.info(f"✅ 服务器 {server.server_id} ({server.name}) 已在运行，跳过")
                    continue
                
                logger.info(f"🔄 服务器 {server.server_id} ({server.name}) 未运行，尝试启动")
                started, screenshot = await client.start_server(server.server_id)
                
                if started:
                    started_servers.append({
                        "server_id": server.server_id,
                        "name": server.name,
                        "screenshot": screenshot
                    })
                
                await asyncio.sleep(2)
            
            # 提取新Cookie
            new_cookie = await client.extract_cookies()
            return new_cookie, started_servers
            
        except Exception as e:
            logger.error(f"❌ 账号#{idx+1} 异常: {e}")
            await notifier.send(f"❌ 账号#{idx+1} 处理异常: {e}")
            return None, []
        finally:
            await ctx.close()
            await browser.close()

async def main():
    logger.info("=" * 50)
    logger.info("Lunes Host 自动启动脚本")
    logger.info("=" * 50)
    
    config = Config.from_env()
    if not config.cookies_list:
        logger.error("❌ 未设置 LUNES_COOKIES")
        return
    
    logger.info(f"📊 共 {len(config.cookies_list)} 个账号")
    
    notifier = Notifier(config.tg_token, config.tg_chat_id)
    github = GitHubManager(config.repo_token, config.repository)
    
    new_cookies = []
    changed = False
    all_started = []
    
    for i, cookie in enumerate(config.cookies_list):
        new_cookie, started = await process_account(cookie, i, notifier)
        all_started.extend([(i+1, s) for s in started])
        
        if new_cookie:
            new_cookies.append(new_cookie)
            if new_cookie != cookie:
                changed = True
                logger.info(f"🔄 账号#{i+1} Cookie已变化")
        else:
            new_cookies.append(cookie)
        
        if i < len(config.cookies_list) - 1:
            await asyncio.sleep(5)
    
    # 发送汇总通知
    if all_started:
        summary = f"🎁 Lunes Host 自动启动通知\n\n"
        summary += f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        summary += f"📊 启动了 {len(all_started)} 个服务器:\n\n"
        
        for acc_idx, server in all_started:
            summary += f"• 账号#{acc_idx}: {server['name']} ({server['server_id']})\n"
        
        await notifier.send(summary)
        
        # 发送截图
        for acc_idx, server in all_started:
            if server.get("screenshot"):
                caption = f"📸 账号#{acc_idx} - {server['name']} ({server['server_id']}) 控制台截图"
                await notifier.send_photo(server["screenshot"], caption)
    else:
        await notifier.send(f"✅ Lunes Host 检查完成\n\n所有服务器均在运行中，无需启动。\n\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 更新Cookie
    if changed and new_cookies:
        await github.update_secret("LUNES_COOKIES", ",".join(new_cookies))
    
    logger.info("👋 完成")

if __name__ == "__main__":
    asyncio.run(main())
