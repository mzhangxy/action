#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Castle-Host 服务器自动续约脚本
功能：多账号支持 + 自动启动关机服务器 + Cookie自动更新

配置变量:
- CASTLE_COOKIES=PHPSESSID=xxx; uid=xxx,PHPSESSID=yyy; uid=yyy  (多账号用逗号分隔)
- SERVER_ID=117954
"""

import os
import sys
import re
import json
import logging
import asyncio
import aiohttp
from enum import Enum
from base64 import b64encode
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
from playwright.async_api import async_playwright, BrowserContext, Page

# ==================== 配置 ====================

LOG_FILE = "castle_renew.log"
DEFAULT_SERVER_ID = "117954"
REQUEST_TIMEOUT = 10
PAGE_TIMEOUT = 60000

# ==================== 日志配置 ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# ==================== 枚举和数据类 ====================

class RenewalStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    OTHER = "other"

@dataclass
class ServerInfo:
    server_id: str
    expiry_date: Optional[str] = None
    expiry_formatted: Optional[str] = None
    days_left: Optional[int] = None
    balance: str = "0.00"
    url: str = ""

@dataclass
class RenewalResult:
    status: RenewalStatus
    message: str
    new_expiry: Optional[str] = None
    days_added: int = 0
    server_started: bool = False

@dataclass
class Config:
    cookies_list: List[str]
    server_id: str
    tg_token: Optional[str]
    tg_chat_id: Optional[str]
    repo_token: Optional[str]
    repository: Optional[str]

    @classmethod
    def from_env(cls) -> "Config":
        cookies_raw = os.environ.get("CASTLE_COOKIES", "").strip()
        cookies_list = [c.strip() for c in cookies_raw.split(",") if c.strip()]
        return cls(
            cookies_list=cookies_list,
            server_id=os.environ.get("SERVER_ID", DEFAULT_SERVER_ID),
            tg_token=os.environ.get("TG_BOT_TOKEN"),
            tg_chat_id=os.environ.get("TG_CHAT_ID"),
            repo_token=os.environ.get("REPO_TOKEN"),
            repository=os.environ.get("GITHUB_REPOSITORY")
        )

# ==================== 工具函数 ====================

def convert_date_format(date_str: str) -> str:
    if not date_str:
        return "Unknown"
    match = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", date_str)
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else date_str

def parse_date(date_str: str) -> Optional[datetime]:
    for fmt in ["%d.%m.%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None

def calculate_days_left(date_str: str) -> Optional[int]:
    date_obj = parse_date(date_str)
    return (date_obj - datetime.now()).days if date_obj else None

def parse_cookies(cookie_str: str) -> List[Dict]:
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".castle-host.com",
                "path": "/"
            })
    return cookies

def analyze_api_error(error_msg: str) -> Tuple[RenewalStatus, str]:
    error_lower = error_msg.lower()
    if "24 час" in error_lower or "уже продлен" in error_lower:
        return RenewalStatus.RATE_LIMITED, "今日已续期"
    if "недостаточно" in error_lower:
        return RenewalStatus.FAILED, "余额不足"
    if "максимальн" in error_lower:
        return RenewalStatus.FAILED, "已达最大期限"
    return RenewalStatus.FAILED, error_msg

# ==================== 通知模块 ====================

class Notifier:
    def __init__(self, tg_token: Optional[str], tg_chat_id: Optional[str]):
        self.tg_token = tg_token
        self.tg_chat_id = tg_chat_id
    
    def build_message(self, server: ServerInfo, result: RenewalResult, account_idx: int) -> str:
        expiry = convert_date_format(result.new_expiry) if result.new_expiry else server.expiry_formatted
        days = calculate_days_left(result.new_expiry) if result.new_expiry else server.days_left
        started_line = "🟢 服务器已启动\n" if result.server_started else ""
        
        if result.status == RenewalStatus.SUCCESS:
            status_line = f"✅ 续约成功 (+{result.days_added}天)" if result.days_added > 0 else "✅ 续约成功"
        elif result.status == RenewalStatus.FAILED:
            status_line = f"❌ 续约失败: {result.message}"
        elif result.status == RenewalStatus.RATE_LIMITED:
            status_line = "📝 今日已续期"
        else:
            status_line = f"📝 {result.message}"
        
        return f"""🎁 Castle-Host 自动续约通知

👤 账号: #{account_idx + 1}
💻 服务器: {server.server_id}
📅 到期时间: {expiry or 'Unknown'}
⏳ 剩余天数: {days or 'Unknown'} 天
🔗 {server.url}

{started_line}{status_line}"""
    
    async def send(self, message: str) -> bool:
        if not self.tg_token or not self.tg_chat_id:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    json={"chat_id": self.tg_chat_id, "text": message},
                    timeout=REQUEST_TIMEOUT
                ) as resp:
                    if resp.status == 200:
                        logger.info("✅ 通知已发送")
                        return True
                    return False
        except Exception as e:
            logger.error(f"❌ 通知发送异常: {e}")
            return False

# ==================== GitHub模块 ====================

class GitHubSecretsManager:
    def __init__(self, repo_token: Optional[str], repository: Optional[str]):
        self.repo_token = repo_token
        self.repository = repository
        self.headers = {
            "Authorization": f"Bearer {repo_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        } if repo_token else {}
    
    async def update_secret(self, name: str, value: str) -> bool:
        if not self.repo_token or not self.repository:
            return False
        try:
            from nacl import encoding, public
        except ImportError:
            logger.error("❌ 缺少pynacl库")
            return False
        try:
            async with aiohttp.ClientSession() as session:
                key_url = f"https://api.github.com/repos/{self.repository}/actions/secrets/public-key"
                async with session.get(key_url, headers=self.headers) as resp:
                    if resp.status != 200:
                        return False
                    key_data = await resp.json()
                
                public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
                sealed_box = public.SealedBox(public_key)
                encrypted = sealed_box.encrypt(value.encode("utf-8"))
                encrypted_value = b64encode(encrypted).decode("utf-8")
                
                secret_url = f"https://api.github.com/repos/{self.repository}/actions/secrets/{name}"
                async with session.put(
                    secret_url, headers=self.headers,
                    json={"encrypted_value": encrypted_value, "key_id": key_data["key_id"]}
                ) as resp:
                    if resp.status in [201, 204]:
                        logger.info(f"✅ Secret {name} 已更新")
                        return True
                    return False
        except Exception as e:
            logger.error(f"❌ GitHub API异常: {e}")
            return False

# ==================== 浏览器客户端 ====================

class CastleHostClient:
    def __init__(self, context: BrowserContext, page: Page, server_id: str):
        self.context = context
        self.page = page
        self.server_id = server_id
        self.servers_url = "https://cp.castle-host.com/servers"
        self.pay_url = f"https://cp.castle-host.com/servers/pay/index/{server_id}"
    
    async def check_and_start_server(self) -> bool:
        """在服务器列表页检查并启动服务器"""
        try:
            await self.page.goto(self.servers_url, wait_until="networkidle")
            
            # 查找开机按钮: onClick="sendAction(117954,'start')"
            start_btn = self.page.locator(f'button[onclick*="sendAction({self.server_id},\'start\')"]')
            
            if await start_btn.count() > 0:
                logger.info("🔴 服务器已关机，尝试启动...")
                await start_btn.click()
                logger.info("🟢 已点击启动按钮")
                await self.page.wait_for_timeout(5000)
                return True
            
            logger.info("✅ 服务器已在运行")
            return False
        except Exception as e:
            logger.error(f"❌ 检查服务器状态失败: {e}")
            return False
    
    async def get_server_info(self) -> ServerInfo:
        """获取服务器信息"""
        await self.page.goto(self.pay_url, wait_until="networkidle")
        expiry = await self._extract_expiry()
        balance = await self._extract_balance()
        return ServerInfo(
            server_id=self.server_id,
            expiry_date=expiry,
            expiry_formatted=convert_date_format(expiry) if expiry else None,
            days_left=calculate_days_left(expiry) if expiry else None,
            balance=balance,
            url=self.pay_url
        )
    
    async def _extract_expiry(self) -> Optional[str]:
        try:
            text = await self.page.text_content("body")
            for pattern in [r"(\d{2}\.\d{2}\.\d{4})\s*$[^)]*$", r"\b(\d{2}\.\d{2}\.\d{4})\b"]:
                match = re.search(pattern, text)
                if match:
                    return match.group(1)
        except:
            pass
        return None
    
    async def _extract_balance(self) -> str:
        try:
            text = await self.page.text_content("body")
            match = re.search(r"(\d+\.\d+)\s*₽", text)
            return match.group(1) if match else "0.00"
        except:
            return "0.00"
    
    async def renew(self) -> RenewalResult:
        """执行续约"""
        api_response: Dict = {}
        
        async def capture_response(response):
            if "/buy_months/" in response.url or "action" in response.url:
                try:
                    api_response["data"] = await response.json()
                except:
                    pass
        
        self.page.on("response", capture_response)
        
        selectors = [
            "#freebtn",
            'button:has-text("Продлить")',
            'a:has-text("Продлить")',
            'button:has-text("Бесплатно")',
            'a:has-text("Бесплатно")',
        ]
        
        for selector in selectors:
            try:
                button = self.page.locator(selector).first
                if await button.count() > 0 and await button.is_visible():
                    await button.click()
                    logger.info("🖱️ 已点击续约按钮")
                    
                    for _ in range(20):
                        if api_response.get("data"):
                            break
                        await asyncio.sleep(0.5)
                    
                    if api_response.get("data"):
                        data = api_response["data"]
                        if data.get("status") == "error":
                            status, msg = analyze_api_error(data.get("error", ""))
                            return RenewalResult(status, msg)
                        if data.get("status") in ["success", "ok"]:
                            return RenewalResult(RenewalStatus.SUCCESS, "续期成功")
                    
                    await self.page.wait_for_timeout(3000)
                    text = await self.page.text_content("body")
                    if "24 час" in text:
                        return RenewalResult(RenewalStatus.RATE_LIMITED, "今日已续期")
                    
                    return RenewalResult(RenewalStatus.OTHER, "需要验证")
            except:
                continue
        
        return RenewalResult(RenewalStatus.FAILED, "未找到续约按钮")
    
    async def verify_renewal(self, original_expiry: str) -> Tuple[Optional[str], int]:
        """验证续约结果"""
        await asyncio.sleep(2)
        await self.page.reload(wait_until="networkidle")
        await asyncio.sleep(2)
        
        new_expiry = await self._extract_expiry()
        if not new_expiry:
            return None, 0
        
        if original_expiry and new_expiry:
            old_date = parse_date(original_expiry)
            new_date = parse_date(new_expiry)
            if old_date and new_date:
                return new_expiry, (new_date - old_date).days
        return new_expiry, 0
    
    async def extract_cookies(self) -> Optional[str]:
        """提取Cookie"""
        try:
            cookies = await self.context.cookies()
            castle_cookies = [c for c in cookies if "castle-host.com" in c.get("domain", "")]
            if castle_cookies:
                return "; ".join([f"{c['name']}={c['value']}" for c in castle_cookies])
        except:
            pass
        return None

# ==================== 单账号处理 ====================

async def process_account(
    cookie_str: str, 
    account_idx: int, 
    config: Config, 
    notifier: Notifier
) -> Optional[str]:
    """处理单个账号，返回新Cookie"""
    cookies = parse_cookies(cookie_str)
    if not cookies:
        logger.error(f"❌ 账号#{account_idx + 1} Cookie解析失败")
        return None
    
    logger.info(f"{'='*50}")
    logger.info(f"📌 处理账号 #{account_idx + 1}")
    logger.info(f"🔑 已注入 {len(cookies)} 个Cookie")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        await
