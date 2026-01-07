#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# scripts/lunes_renew.py

import os
import sys
import re
import logging
import asyncio
import aiohttp
from base64 import b64encode
from datetime import datetime
from dataclasses import dataclass, field
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
    short_id: str
    is_active: bool
    cpu: str = ""
    ram: str = ""
    disk: str = ""

@dataclass
class AccountResult:
    index: int
    servers: List[ServerInfo] = field(default_factory=list)
    started: List[dict] = field(default_factory=list)
    cookie_changed: bool = False
    new_cookie: str = ""
    error: str = ""

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
            # 为不同域名添加cookie
            for domain in [".lunes.host", "betadash.lunes.host", "ctrl.lunes.host"]:
                cookies.append({"name": n.strip(), "value": v.strip(), "domain": domain, "path": "/"})
    return cookies

def mask_cookie(s: str, show: int = 8) -> str:
    if len(s) <= show * 2:
        return s
    return f"{s[:show]}...{s[-show:]}"

class Notifier:
    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token, self.chat_id = token, chat_id
    
    async def send(self, msg: str) -> Optional[int]:
        if not self.token or not self.chat_id:
            logger.info("[Telegram] 未配置，跳过通知")
            return None
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": msg, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as r:
                    if r.status == 200:
                        logger.info("✅ Telegram通知已发送")
                        data = await r.json()
                        return data.get('result', {}).get('message_id')
                    else:
                        logger.error(f"❌ Telegram通知失败: {r.status}")
        except Exception as e:
            logger.error(f"❌ Telegram异常: {e}")
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
                    if r.status == 200:
                        logger.info("✅ 截图已发送")
                        return True
                    logger.error(f"❌ 截图发送失败: {r.status}")
        except Exception as e:
            logger.error(f"❌ 截图发送异常: {e}")
        return False

class GitHubManager:
    def __init__(self, token: Optional[str], repo: Optional[str]):
        self.token, self.repo = token, repo
    
    async def update_secret(self, name: str, value: str) -> bool:
        if not self.token or not self.repo:
            logger.info("[GitHub] 未配置REPO_TOKEN，跳过Secret更新")
            return False
        try:
            from nacl import encoding, public
            headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://api.github.com/repos/{self.repo}/actions/secrets/public-key", headers=headers) as r:
                    if r.status != 200:
                        logger.error(f"❌ 获取GitHub公钥失败: {r.status}")
                        return False
                    kd = await r.json()
                pk = public.PublicKey(kd["key"].encode(), encoding.Base64Encoder())
                enc = b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()
                async with s.put(
                    f"https://api.github.com/repos/{self.repo}/actions/secrets/{name}",
                    headers=headers, 
                    json={"encrypted_value": enc, "key_id": kd["key_id"]}
                ) as r:
                    if r.status in [201, 204]:
                        logger.info(f"✅ GitHub Secret [{name}] 已更新")
                        return True
                    logger.error(f"❌ 更新Secret失败: {r.status}")
        except ImportError:
            logger.error("❌ 缺少pynacl库，无法更新Secret")
        except Exception as e:
            logger.error(f"❌ GitHub异常: {e}")
        return False

class LunesClient:
    def __init__(self, ctx: BrowserContext, page: Page):
        self.ctx, self.page = ctx, page
        self.dashboard_url = "https://betadash.lunes.host/"
        self.ctrl_url = "https://ctrl.lunes.host/server"
    
    async def get_servers(self) -> List[ServerInfo]:
        servers = []
        try:
            logger.info(f"🌐 访问: {self.dashboard_url}")
            
            # 使用 domcontentloaded 而不是 networkidle，更快
            resp = await self.page.goto(self.dashboard_url, wait_until="domcontentloaded", timeout=30000)
            logger.info(f"📡 响应状态: {resp.status if resp else 'None'}")
            
            # 等待页面加载
            await self.page.wait_for_timeout(3000)
            
            current_url = self.page.url
            logger.info(f"📍 当前URL: {current_url}")
            
            # 检查登录状态
            if "/login" in current_url:
                logger.error("❌ Cookie已失效，重定向到登录页")
                return []
            
            # 等待服务器卡片出现
            try:
                await self.page.wait_for_selector("a.server-card", timeout=10000)
            except:
                logger.warning("⚠️ 未找到服务器卡片，可能没有服务器")
                # 检查是否有"Create Server"按钮确认页面已加载
                if await self.page.locator('a[href="/servers/create"]').count() > 0:
                    logger.info("✅ 页面已加载，但没有服务器")
                    return []
                logger.error("❌ 页面加载异常")
                return []
            
            # 解析服务器
            cards = await self.page.locator("a.server-card").all()
            logger.info(f"📋 找到 {len(cards)} 个服务器卡片")
            
            for card in cards:
                try:
                    href = await card.get_attribute("href") or ""
                    match = re.search(r"/servers/(\d+)", href)
                    if not match:
                        continue
                    
                    server_id = match.group(1)
                    
                    # 提取信息
                    short_id = ""
                    meta = card.locator(".server-meta")
                    if await meta.count() > 0:
                        meta_text = await meta.text_content() or ""
                        id_match = re.search(r"ID\s*·\s*(\w+)", meta_text)
                        if id_match:
                            short_id = id_match.group(1)
                    
                    name_el = card.locator(".server-title")
                    name = await name_el.text_content() if await name_el.count() > 0 else server_id
                    
                    status_el = card.locator(".server-status")
                    status_text = await status_el.text_content() if await status_el.count() > 0 else ""
                    is_active = "Active" in status_text
                    
                    # 提取资源信息
                    pills = await card.locator(".server-pill").all()
                    cpu, ram, disk = "", "", ""
                    for pill in pills:
                        text = await pill.text_content() or ""
                        if "CPU" in text:
                            cpu = text.strip()
                        elif "RAM" in text:
                            ram = text.strip()
                        elif "Disk" in text:
                            disk = text.strip()
                    
                    server = ServerInfo(
                        server_id=server_id,
                        name=name.strip(),
                        short_id=short_id,
                        is_active=is_active,
                        cpu=cpu, ram=ram, disk=disk
                    )
                    servers.append(server)
                    
                    status_icon = "🟢" if is_active else "🔴"
                    logger.info(f"  {status_icon} [{server_id}] {name.strip()} (ID: {short_id}) - {'Active' if is_active else 'Inactive'}")
                    
                except Exception as e:
                    logger.warning(f"  ⚠️ 解析服务器卡片失败: {e}")
            
        except Exception as e:
            logger.error(f"❌ 获取服务器列表失败: {e}")
        
        return servers
    
    async def start_server(self, server: ServerInfo) -> Tuple[bool, Optional[bytes]]:
        try:
            url = f"{self.ctrl_url}/{server.server_id}"
            logger.info(f"🌐 访问控制台: {url}")
            
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self.page.wait_for_timeout(3000)
            
            # 查找 Start 按钮
            start_btn = self.page.locator('button:has-text("Start")').first
            if await start_btn.count() == 0:
                logger.info(f"  ℹ️ 未找到Start按钮")
                return False, None
            
            disabled = await start_btn.get_attribute("disabled")
            if disabled is not None:
                logger.info(f"  ✅ Start按钮已禁用（服务器运行中）")
                return False, None
            
            logger.info(f"  🔴 服务器已停止，点击启动...")
            await start_btn.click()
            logger.info(f"  ⏳ 等待5秒...")
            await self.page.wait_for_timeout(5000)
            
            # 截图
            logger.info(f"  📸 截图中...")
            screenshot = await self.page.screenshot(full_page=True)
            logger.info(f"  🟢 启动完成")
            
            return True, screenshot
            
        except Exception as e:
            logger.error(f"  ❌ 启动服务器失败: {e}")
            return False, None
    
    async def extract_cookies(self) -> Tuple[str, bool]:
        """提取Cookie，返回(cookie_str, is_changed)"""
        try:
            cookies = await self.ctx.cookies()
            lunes_cookies = {}
            for c in cookies:
                if "lunes.host" in c.get("domain", ""):
                    # 去重，只保留一个
                    lunes_cookies[c['name']] = c['value']
            
            if lunes_cookies:
                new_cookie = "; ".join([f"{k}={v}" for k, v in lunes_cookies.items()])
                logger.info(f"🍪 提取到Cookie: {list(lunes_cookies.keys())}")
                return new_cookie, True
        except Exception as e:
            logger.error(f"❌ 提取Cookie失败: {e}")
        return "", False

async def process_account(cookie_str: str, idx: int, notifier: Notifier) -> AccountResult:
    result = AccountResult(index=idx + 1)
    
    cookies = parse_cookies(cookie_str)
    if not cookies:
        result.error = "Cookie解析失败"
        logger.error(f"❌ 账号#{idx+1} Cookie解析失败")
        return result
    
    logger.info(f"{'='*60}")
    logger.info(f"📌 处理账号 #{idx+1}")
    logger.info(f"🍪 Cookie: {mask_cookie(cookie_str)}")
    logger.info(f"{'='*60}")
    
    async with async_playwright() as p:
        logger.info("🚀 启动浏览器...")
        browser = await p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.97 Safari/537.36",
            viewport={"width": 1366, "height": 768}
        )
        
        logger.info("🍪 注入Cookie...")
        await ctx.add_cookies(cookies)
        
        page = await ctx.new_page()
        client = LunesClient(ctx, page)
        
        try:
            # 获取服务器列表
            servers = await client.get_servers()
            result.servers = servers
            
            if not servers:
                if "/login" in page.url:
                    result.error = "Cookie已失效"
                else:
                    result.error = "无服务器"
                return result
            
            # 统计
            active_count = sum(1 for s in servers if s.is_active)
            inactive_count = len(servers) - active_count
            logger.info(f"📊 统计: {active_count} 运行中, {inactive_count} 已停止")
            
            # 处理未运行的服务器
            for server in servers:
                if server.is_active:
                    continue
                
                logger.info(f"🔄 启动服务器 [{server.server_id}] {server.name}")
                started, screenshot = await client.start_server(server)
                
                if started:
                    result.started.append({
                        "server": server,
                        "screenshot": screenshot
                    })
                
                await asyncio.sleep(2)
            
            # 提取新Cookie
            new_cookie, has_cookie = await client.extract_cookies()
            if has_cookie and new_cookie:
                # 比较关键cookie是否变化
                old_session = re.search(r'session=([^;]+)', cookie_str)
                new_session = re.search(r'session=([^;]+)', new_cookie)
                
                if old_session and new_session:
                    if old_session.group(1) != new_session.group(1):
                        result.cookie_changed = True
                        result.new_cookie = new_cookie
                        logger.info(f"🔄 Cookie已变化!")
                        logger.info(f"   旧: {mask_cookie(old_session.group(1))}")
                        logger.info(f"   新: {mask_cookie(new_session.group(1))}")
                    else:
                        result.new_cookie = cookie_str
                        logger.info(f"✅ Cookie未变化")
                else:
                    result.new_cookie = new_cookie
                    result.cookie_changed = new_cookie != cookie_str
            else:
                result.new_cookie = cookie_str
            
        except Exception as e:
            result.error = str(e)
            logger.error(f"❌ 账号#{idx+1} 异常: {e}")
        finally:
            await ctx.close()
            await browser.close()
            logger.info("🔒 浏览器已关闭")
    
    return result

async def main():
    start_time = datetime.now()
    
    logger.info("=" * 60)
    logger.info("🚀 Lunes Host 自动启动脚本")
    logger.info(f"⏰ 开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    config = Config.from_env()
    
    # 检查配置
    logger.info("\n📋 配置检查:")
    logger.info(f"  LUNES_COOKIES: {'✅ 已设置' if config.cookies_list else '❌ 未设置'}")
    logger.info(f"  TG_BOT_TOKEN: {'✅ 已设置' if config.tg_token else '⚠️ 未设置'}")
    logger.info(f"  TG_CHAT_ID: {'✅ 已设置' if config.tg_chat_id else '⚠️ 未设置'}")
    logger.info(f"  REPO_TOKEN: {'✅ 已设置' if config.repo_token else '⚠️ 未设置'}")
    logger.info(f"  GITHUB_REPOSITORY: {config.repository or '⚠️ 未设置'}")
    
    if not config.cookies_list:
        logger.error("\n❌ 未设置 LUNES_COOKIES 环境变量")
        return
    
    logger.info(f"\n📊 共 {len(config.cookies_list)} 个账号待处理")
    
    notifier = Notifier(config.tg_token, config.tg_chat_id)
    github = GitHubManager(config.repo_token, config.repository)
    
    results: List[AccountResult] = []
    
    for i, cookie in enumerate(config.cookies_list):
        result = await process_account(cookie, i, notifier)
        results.append(result)
        
        if i < len(config.cookies_list) - 1:
            logger.info("\n⏳ 等待5秒后处理下一个账号...")
            await asyncio.sleep(5)
    
    # 汇总
    logger.info("\n" + "=" * 60)
    logger.info("📊 执行汇总")
    logger.info("=" * 60)
    
    total_servers = sum(len(r.servers) for r in results)
    total_started = sum(len(r.started) for r in results)
    total_errors = sum(1 for r in results if r.error)
    cookie_changed = any(r.cookie_changed for r in results)
    
    logger.info(f"  账号总数: {len(results)}")
    logger.info(f"  服务器总数: {total_servers}")
    logger.info(f"  本次启动: {total_started}")
    logger.info(f"  错误数: {total_errors}")
    logger.info(f"  Cookie变化: {'是' if cookie_changed else '否'}")
    
    # 构建通知消息
    msg_lines = [
        "🎁 <b>Lunes Host 自动检查报告</b>",
        "",
        f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"📊 账号: {len(results)} | 服务器: {total_servers} | 启动: {total_started}",
        ""
    ]
    
    for r in results:
        msg_lines.append(f"<b>👤 账号 #{r.index}</b>")
        if r.error:
            msg_lines.append(f"  ❌ 错误: {r.error}")
        else:
            for s in r.servers:
                icon = "🟢" if s.is_active else "🔴"
                started_mark = " ⚡已启动" if any(st['server'].server_id == s.server_id for st in r.started) else ""
                msg_lines.append(f"  {icon} {s.name} ({s.server_id}){started_mark}")
        
        if r.cookie_changed:
            msg_lines.append(f"  🔄 Cookie已更新")
        msg_lines.append("")
    
    # 发送通知
    await notifier.send("\n".join(msg_lines))
    
    # 发送截图
    for r in results:
        for st in r.started:
            if st.get("screenshot"):
                server = st["server"]
                caption = f"📸 账号#{r.index} - {server.name} ({server.server_id})"
                await notifier.send_photo(st["screenshot"], caption)
    
    # 更新Cookie
    if cookie_changed:
        new_cookies = []
        for i, r in enumerate(results):
            if r.new_cookie:
                new_cookies.append(r.new_cookie)
            else:
                new_cookies.append(config.cookies_list[i])
        
        logger.info("\n🔄 更新GitHub Secret...")
        await github.update_secret("LUNES_COOKIES", ",".join(new_cookies))
    else:
        logger.info("\n✅ Cookie无变化，无需更新Secret")
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    
    logger.info("\n" + "=" * 60)
    logger.info(f"👋 执行完成，耗时: {duration:.1f}秒")
    logger.info("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
