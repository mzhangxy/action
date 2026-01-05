#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Castle-Host 服务器自动续约脚本
功能：自动续期 + 提取新Cookie + 更新GitHub Secrets
"""

import os
import asyncio
import aiohttp
import re
import json
import logging
from datetime import datetime
from playwright.async_api import async_playwright
from base64 import b64encode
import sys

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('castle_renew.log')
    ]
)
logger = logging.getLogger(__name__)

# 续约数据
renewal_data = {
    "server_id": "",
    "before_expiry": "",
    "after_expiry": "",
    "renewal_time": "",
    "success": False,
    "status": "",
    "error_message": "",
    "cookie_updated": False
}

# ------------------ 日期格式转换 ------------------
def convert_date_format(date_str):
    """将 DD.MM.YYYY 转换为 YYYY-MM-DD"""
    if not date_str or date_str == "Unknown":
        return date_str
    try:
        if re.match(r'\d{2}\.\d{2}\.\d{4}', date_str):
            parts = date_str.split('.')
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        return date_str
    except:
        return date_str

def parse_date(date_str):
    """解析日期字符串"""
    try:
        for fmt in ['%d.%m.%Y', '%Y-%m-%d']:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None
    except:
        return None

def calculate_days_left(date_str):
    """计算剩余天数"""
    date_obj = parse_date(date_str)
    if date_obj:
        return (date_obj - datetime.now()).days
    return None

# ------------------ GitHub Secrets 更新 ------------------
async def encrypt_secret(public_key: str, secret_value: str) -> str:
    """使用 GitHub 公钥加密 secret"""
    try:
        from nacl import encoding, public
        
        public_key_bytes = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
        sealed_box = public.SealedBox(public_key_bytes)
        encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
        return b64encode(encrypted).decode("utf-8")
    except ImportError:
        logger.error("❌ 需要安装 pynacl: pip install pynacl")
        return None
    except Exception as e:
        logger.error(f"❌ 加密失败: {e}")
        return None

async def update_github_secret(secret_name: str, secret_value: str, repo_token: str = None, repository: str = None):
    """更新 GitHub Repository Secret"""
    repo_token = repo_token or os.environ.get("REPO_TOKEN")
    repository = repository or os.environ.get("GITHUB_REPOSITORY")
    
    if not repo_token:
        logger.info("ℹ️ 未设置 REPO_TOKEN，跳过 GitHub Secrets 更新")
        return False
    
    if not repository:
        logger.warning("⚠️ 未设置 GITHUB_REPOSITORY")
        return False
    
    logger.info(f"🔄 更新 GitHub Secret: {secret_name} (仓库: {repository})")
    
    headers = {
        "Authorization": f"Bearer {repo_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            # 1. 获取仓库公钥
            key_url = f"https://api.github.com/repos/{repository}/actions/secrets/public-key"
            async with session.get(key_url, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"❌ 获取公钥失败: {resp.status} - {error_text}")
                    return False
                key_data = await resp.json()
            
            public_key = key_data["key"]
            key_id = key_data["key_id"]
            
            # 2. 加密 secret
            encrypted_value = await encrypt_secret(public_key, secret_value)
            if not encrypted_value:
                return False
            
            # 3. 更新 secret
            secret_url = f"https://api.github.com/repos/{repository}/actions/secrets/{secret_name}"
            payload = {
                "encrypted_value": encrypted_value,
                "key_id": key_id
            }
            
            async with session.put(secret_url, headers=headers, json=payload) as resp:
                if resp.status in [201, 204]:
                    logger.info(f"✅ GitHub Secret {secret_name} 更新成功")
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"❌ 更新 Secret 失败: {resp.status} - {error_text}")
                    return False
                    
    except Exception as e:
        logger.error(f"❌ GitHub API 错误: {e}")
        return False

# ------------------ Cookie 操作 ------------------
def parse_cookie_string(cookie_str: str):
    """解析Cookie字符串"""
    cookies = []
    for part in cookie_str.split(';'):
        part = part.strip()
        if '=' in part:
            name, value = part.split('=', 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".castle-host.com",
                "path": "/"
            })
    logger.info(f"✅ 解析 {len(cookies)} 个Cookie")
    return cookies

async def extract_cookies(context) -> str:
    """从浏览器上下文提取Cookie"""
    try:
        cookies = await context.cookies()
        
        # 过滤 castle-host.com 的 Cookie
        castle_cookies = [c for c in cookies if 'castle-host.com' in c.get('domain', '')]
        
        if not castle_cookies:
            logger.warning("⚠️ 未找到 Castle-Host Cookie")
            return None
        
        # 转换为字符串格式
        cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in castle_cookies])
        
        logger.info(f"✅ 提取到 {len(castle_cookies)} 个Cookie")
        logger.debug(f"Cookie: {cookie_str[:100]}...")
        
        return cookie_str
        
    except Exception as e:
        logger.error(f"❌ 提取Cookie失败: {e}")
        return None

# ------------------ Telegram 通知 ------------------
async def tg_notify(message: str, token=None, chat_id=None):
    """发送Telegram通知"""
    token = token or os.environ.get("TG_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TG_CHAT_ID")
        
    if not token or not chat_id:
        return False
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=10
            ) as resp:
                if resp.status == 200:
                    logger.info("✅ Telegram通知已发送")
                    return True
                return False
    except Exception as e:
        logger.error(f"⚠️ TG通知失败: {e}")
        return False

# ------------------ 页面操作 ------------------
async def extract_expiry_date(page):
    """提取到期时间"""
    try:
        body_text = await page.text_content('body')
        for pattern in [r'(\d{2}\.\d{2}\.\d{4})\s*\([^)]*\)', r'\b(\d{2}\.\d{2}\.\d{4})\b']:
            match = re.search(pattern, body_text)
            if match:
                return match.group(1)
        return None
    except:
        return None

async def extract_balance(page):
    """提取余额"""
    try:
        body_text = await page.text_content('body')
        match = re.search(r'(\d+\.\d+)\s*₽', body_text)
        return match.group(1) if match else "0.00"
    except:
        return "0.00"

def analyze_error_message(error_msg):
    """分析错误信息"""
    error_lower = error_msg.lower()
    
    if '24 час' in error_lower:
        return "rate_limited", "今日已续期"
    if 'уже продлен' in error_lower:
        return "already_renewed", "今日已续期"
    if 'недостаточно' in error_lower:
        return "insufficient_funds", "余额不足"
    if 'максимальн' in error_lower:
        return "max_period", "已达最大期限"
    
    return "unknown", error_msg

# ------------------ 续约执行 ------------------
async def perform_renewal(page, server_id):
    """执行续约"""
    logger.info(f"🔄 续约服务器: {server_id}")
    
    api_response = {"body": None}
    
    try:
        for selector in ['#freebtn', 'button:has-text("Продлить")']:
            button = page.locator(selector)
            if await button.count() > 0:
                logger.info(f"🖱️ 点击: {selector}")
                
                if await button.get_attribute("disabled"):
                    return {"success": False, "error_type": "disabled", "message": "按钮已禁用"}
                
                async def handle_response(response):
                    if "/buy_months/" in response.url:
                        try:
                            api_response["body"] = await response.json()
                        except:
                            pass
                
                page.on("response", handle_response)
                await button.click()
                
                for _ in range(20):
                    if api_response["body"]:
                        break
                    await asyncio.sleep(0.5)
                
                if api_response["body"] and isinstance(api_response["body"], dict):
                    body = api_response["body"]
                    if body.get("status") == "error":
                        error_type, error_desc = analyze_error_message(body.get("error", ""))
                        return {"success": False, "error_type": error_type, "message": error_desc}
                    if body.get("status") in ["success", "ok"]:
                        return {"success": True, "message": "续期成功"}
                
                await page.wait_for_timeout(3000)
                
                page_text = await page.text_content('body')
                if '24 час' in page_text:
                    return {"success": False, "error_type": "rate_limited", "message": "今日已续期"}
                
                return {"success": None, "message": "需要验证"}
        
        return {"success": False, "error_type": "no_button", "message": "未找到按钮"}
        
    except Exception as e:
        return {"success": False, "error_type": "exception", "message": str(e)}

async def verify_renewal(page, original_expiry):
    """验证续约结果"""
    try:
        await asyncio.sleep(2)
        await page.reload(wait_until="networkidle")
        await asyncio.sleep(2)
        
        new_expiry = await extract_expiry_date(page)
        if not new_expiry:
            return None, 0
        
        if original_expiry and new_expiry:
            old_date = parse_date(original_expiry)
            new_date = parse_date(new_expiry)
            if old_date and new_date:
                return new_expiry, (new_date - old_date).days
        
        return new_expiry, 0
    except:
        return None, 0

# ------------------ 主函数 ------------------
async def main():
    logger.info("=" * 60)
    logger.info("Castle-Host 自动续约 + Cookie自动更新")
    logger.info("=" * 60)
    
    # 环境变量
    cookie_str = os.environ.get("CASTLE_COOKIES", "").strip()
    server_id = os.environ.get("SERVER_ID", "117954")
    tg_token = os.environ.get("TG_BOT_TOKEN")
    tg_chat_id = os.environ.get("TG_CHAT_ID")
    repo_token = os.environ.get("REPO_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    force_renew = os.environ.get("FORCE_RENEW", "false").lower() == "true"
    renew_threshold = int(os.environ.get("RENEW_THRESHOLD", "3"))
    
    if not cookie_str:
        logger.error("❌ 未设置 CASTLE_COOKIES")
        return
    
    renewal_data["server_id"] = server_id
    renewal_data["renewal_time"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    cookies = parse_cookie_string(cookie_str)
    if not cookies:
        logger.error("❌ Cookie解析失败")
        return
    
    server_url = f"https://cp.castle-host.com/servers/pay/index/{server_id}"
    
    logger.info("🚀 启动浏览器...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        
        await context.add_cookies(cookies)
        page = await context.new_page()
        page.set_default_timeout(60000)
        
        try:
            logger.info(f"🌐 访问: {server_url}")
            await page.goto(server_url, wait_until="networkidle")
            
            # 检查登录
            if "login" in page.url or "auth" in page.url:
                logger.error("❌ Cookie已失效")
                await tg_notify(f"❌ Castle-Host Cookie已失效\n\n🆔 服务器: {server_id}", tg_token, tg_chat_id)
                return
            
            logger.info("✅ 登录成功")
            
            # 提取信息
            original_expiry = await extract_expiry_date(page)
            balance = await extract_balance(page)
            renewal_data["before_expiry"] = original_expiry
            
            days_left = calculate_days_left(original_expiry) if original_expiry else None
            expiry_formatted = convert_date_format(original_expiry) if original_expiry else "Unknown"
            
            logger.info(f"📅 到期: {expiry_formatted}, 剩余: {days_left} 天")
            
            # 检查是否需要续约
            if days_left and days_left > renew_threshold and not force_renew:
                logger.info(f"ℹ️ 剩余 {days_left} 天，跳过续约")
                
                message = f"""ℹ️ Castle-Host 状态正常

🆔 服务器: {server_id}
📅 到期时间: {expiry_formatted}
⏳ 剩余: {days_left} 天
💰 余额: {balance} ₽"""
                
                await tg_notify(message, tg_token, tg_chat_id)
                renewal_data["success"] = True
                renewal_data["status"] = "skipped"
                renewal_data["after_expiry"] = original_expiry
                
            else:
                # 执行续约
                result = await perform_renewal(page, server_id)
                renewal_data["status"] = result.get("error_type", "unknown")
                
                if result["success"] == True:
                    new_expiry, days_added = await verify_renewal(page, original_expiry)
                    new_expiry_formatted = convert_date_format(new_expiry) if new_expiry else "Unknown"
                    renewal_data["after_expiry"] = new_expiry
                    renewal_data["success"] = True
                    
                    message = f"""✅ Castle-Host 续约成功

🆔 服务器: {server_id}
📅 到期时间: {new_expiry_formatted}
📈 续期: +{days_added} 天
💰 余额: {balance} ₽"""
                    
                    logger.info("🎉 续约成功！")
                    
                elif result["success"] == False:
                    error_type = result.get("error_type", "unknown")
                    error_msg = result.get("message", "未知错误")
                    
                    renewal_data["success"] = False
                    renewal_data["after_expiry"] = original_expiry
                    renewal_data["error_message"] = error_msg
                    
                    icon = "⏰" if error_type in ["rate_limited", "already_renewed"] else "⚠️"
                    
                    message = f"""{icon} Castle-Host 续约提示

🆔 服务器: {server_id}
📅 到期时间: {expiry_formatted}
⏳ 剩余: {days_left} 天
💰 余额: {balance} ₽

📋 {error_msg}"""
                    
                else:
                    new_expiry, days_added = await verify_renewal(page, original_expiry)
                    new_expiry_formatted = convert_date_format(new_expiry) if new_expiry else "Unknown"
                    renewal_data["after_expiry"] = new_expiry
                    
                    if new_expiry and new_expiry != original_expiry and days_added > 0:
                        renewal_data["success"] = True
                        message = f"""✅ Castle-Host 续约成功

🆔 服务器: {server_id}
📅 到期时间: {new_expiry_formatted}
📈 续期: +{days_added} 天
💰 余额: {balance} ₽"""
                    else:
                        renewal_data["success"] = False
                        message = f"""⏰ Castle-Host 续约提示

🆔 服务器: {server_id}
📅 到期时间: {expiry_formatted}
⏳ 剩余: {days_left} 天
💰 余额: {balance} ₽

📋 今日已续期"""
                
                await tg_notify(message, tg_token, tg_chat_id)
            
            # ========== 提取并更新 Cookie ==========
            logger.info("🍪 提取新Cookie...")
            new_cookie_str = await extract_cookies(context)
            
            if new_cookie_str:
                # 检查Cookie是否有变化
                if new_cookie_str != cookie_str:
                    logger.info("🔄 Cookie已更新，准备同步到GitHub...")
                    
                    if repo_token and repository:
                        update_success = await update_github_secret(
                            "CASTLE_COOKIES", 
                            new_cookie_str,
                            repo_token,
                            repository
                        )
                        renewal_data["cookie_updated"] = update_success
                        
                        if update_success:
                            logger.info("✅ GitHub Secret CASTLE_COOKIES 已更新")
                        else:
                            logger.warning("⚠️ GitHub Secret 更新失败")
                    else:
                        logger.info("ℹ️ 未配置 REPO_TOKEN，跳过 GitHub 更新")
                else:
                    logger.info("ℹ️ Cookie未变化，无需更新")
            
            # 保存记录
            with open("renewal_history.json", "a", encoding="utf-8") as f:
                json.dump(renewal_data, f, ensure_ascii=False)
                f.write("\n")
            
        except Exception as e:
            logger.error(f"❌ 错误: {e}", exc_info=True)
            await tg_notify(f"❌ Castle-Host 脚本错误\n\n{str(e)}", tg_token, tg_chat_id)
            
        finally:
            await context.close()
            await browser.close()
            logger.info("👋 完成")
            
            # 输出总结
            logger.info("=" * 60)
            logger.info(f"续约结果: {'✅ 成功' if renewal_data['success'] else '❌ 失败'}")
            logger.info(f"Cookie更新: {'✅ 已更新' if renewal_data.get('cookie_updated') else '⏭️ 跳过'}")
            logger.info("=" * 60)

if __name__ == "__main__":
    print("Castle-Host 自动续约 + Cookie自动更新")
    
    if not os.environ.get("CASTLE_COOKIES"):
        print("❌ 请设置 CASTLE_COOKIES 环境变量")
        sys.exit(1)
    
    asyncio.run(main())
