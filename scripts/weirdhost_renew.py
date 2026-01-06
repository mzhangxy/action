#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weirdhost-auto - main.py
功能：自动续期 + 智能通知（点击按钮后等待 CF 验证）
"""
import os
import asyncio
import aiohttp
import base64
from datetime import datetime
from playwright.async_api import async_playwright

try:
    from nacl import encoding, public
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False
    print("⚠️ PyNaCl 未安装，无法自动更新 Secrets。pip install pynacl")

DEFAULT_SERVER_URL = "https://hub.weirdhost.xyz/server/d341874c"
DEFAULT_COOKIE_NAME = "remember_web"


# ================== 工具函数 ==================
def calculate_remaining_time(expiry_str: str) -> str:
    try:
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
            try:
                expiry_dt = datetime.strptime(expiry_str.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return "无法解析"
        
        diff = expiry_dt - datetime.now()
        
        if diff.total_seconds() < 0:
            return "⚠️ 已过期"
        
        days = diff.days
        hours, remainder = divmod(diff.seconds, 3600)
        minutes = remainder // 60
        
        parts = []
        if days > 0:
            parts.append(f"{days}天")
        if hours > 0:
            parts.append(f"{hours}小时")
        if minutes > 0 and days == 0:
            parts.append(f"{minutes}分钟")
        
        return " ".join(parts) if parts else "不到1分钟"
    except:
        return "计算失败"


def parse_renew_error(body: dict) -> str:
    try:
        if isinstance(body, dict) and "errors" in body:
            errors = body.get("errors", [])
            if errors and isinstance(errors[0], dict):
                return errors[0].get("detail", str(body))
        return str(body)
    except:
        return str(body)


def is_cooldown_error(error_detail: str) -> bool:
    keywords = [
        "can only once at one time period",
        "can't renew",
        "cannot renew",
        "already renewed"
    ]
    return any(kw in error_detail.lower() for kw in keywords)


# ================== Cloudflare 验证等待 ==================
async def wait_for_cloudflare(page, max_wait: int = 60) -> bool:
    """等待 Cloudflare 验证完成"""
    print("🔄 检查 Cloudflare 验证...")
    
    for i in range(max_wait):
        try:
            # 检查是否在 CF 验证页面
            page_content = await page.content()
            page_text = await page.evaluate("() => document.body.innerText || ''")
            
            cf_indicators = [
                "Checking your browser",
                "Just a moment",
                "Verifying you are human",
                "확인 중",  # 韩文
                "验证中",
            ]
            
            is_cf_page = any(indicator in page_content or indicator in page_text for indicator in cf_indicators)
            
            # 检查是否有 CF challenge iframe
            cf_iframe = await page.query_selector('iframe[src*="challenges.cloudflare.com"]')
            if cf_iframe:
                is_cf_page = True
            
            # 检查 turnstile
            turnstile = await page.query_selector('[class*="cf-turnstile"]')
            if turnstile:
                is_cf_page = True
            
            if not is_cf_page:
                if "/cdn-cgi/" not in page.url and "challenge" not in page.url:
                    print(f"✅ Cloudflare 验证通过 ({i+1}秒)")
                    return True
            
            # 尝试点击 CF 验证框（如果存在）
            try:
                cf_checkbox = await page.query_selector('input[type="checkbox"]')
                if cf_checkbox:
                    await cf_checkbox.click()
                    print("🖱️ 点击了 CF 验证框")
            except:
                pass
            
            if i % 5 == 0:
                print(f"⏳ 等待 CF 验证... ({i+1}/{max_wait}秒)")
            
            await page.wait_for_timeout(1000)
            
        except Exception as e:
            await page.wait_for_timeout(1000)
    
    print("⚠️ Cloudflare 验证超时")
    return False


async def wait_for_page_ready(page, max_wait: int = 15) -> bool:
    """等待页面完全加载"""
    print("🔄 等待页面就绪...")
    
    for i in range(max_wait):
        try:
            ready = await page.evaluate("""
                () => {
                    const hasButton = document.querySelector('button') !== null;
                    const hasContent = document.body.innerText.length > 100;
                    const noSpinner = !document.body.innerText.includes('Loading');
                    return hasButton && hasContent && noSpinner;
                }
            """)
            
            if ready:
                print(f"✅ 页面就绪 ({i+1}秒)")
                await page.wait_for_timeout(1000)
                return True
                
        except:
            pass
        
        await page.wait_for_timeout(1000)
    
    return False


# ================== GitHub Secrets 更新 ==================
def encrypt_secret(public_key: str, secret_value: str) -> str:
    if not NACL_AVAILABLE:
        raise RuntimeError("PyNaCl 未安装")
    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


async def update_github_secret(secret_name: str, secret_value: str) -> bool:
    repo_token = os.environ.get("REPO_TOKEN", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()

    if not repo_token or not repository or not NACL_AVAILABLE:
        return False

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {repo_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with aiohttp.ClientSession() as session:
        try:
            pk_url = f"https://api.github.com/repos/{repository}/actions/secrets/public-key"
            async with session.get(pk_url, headers=headers) as resp:
                if resp.status != 200:
                    return False
                pk_data = await resp.json()

            encrypted_value = encrypt_secret(pk_data["key"], secret_value)
            secret_url = f"https://api.github.com/repos/{repository}/actions/secrets/{secret_name}"
            payload = {"encrypted_value": encrypted_value, "key_id": pk_data["key_id"]}
            
            async with session.put(secret_url, headers=headers, json=payload) as resp:
                if resp.status in (201, 204):
                    print(f"✅ 已更新 Secret: {secret_name}")
                    return True
                return False
        except Exception as e:
            print(f"❌ 更新 Secret 出错: {e}")
            return False


# ================== Telegram 通知 ==================
async def tg_notify(message: str):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(url, json={
                "chat_id": chat_id, 
                "text": message,
                "parse_mode": "HTML"
            })
        except Exception as e:
            print(f"⚠️ TG 通知失败: {e}")


async def tg_notify_photo(photo_path: str, caption: str = ""):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    async with aiohttp.ClientSession() as session:
        try:
            with open(photo_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("chat_id", chat_id)
                data.add_field("photo", f, filename=os.path.basename(photo_path))
                data.add_field("caption", caption)
                data.add_field("parse_mode", "HTML")
                await session.post(url, data=data)
        except Exception as e:
            print(f"⚠️ TG 图片通知失败: {e}")


# ================== Cookie 提取 ==================
async def extract_remember_cookie(context) -> tuple:
    try:
        cookies = await context.cookies()
        for cookie in cookies:
            if cookie["name"].startswith("remember_web"):
                return (cookie["name"], cookie["value"])
        return (None, None)
    except:
        return (None, None)


# ================== 获取到期时间 ==================
async def get_expiry_time(page) -> str:
    try:
        return await page.evaluate("""
            () => {
                const text = document.body.innerText;
                const match = text.match(/유통기한\\s*(\\d{4}-\\d{2}-\\d{2}(?:\\s+\\d{2}:\\d{2}:\\d{2})?)/);
                if (match) return match[1].trim();
                const match2 = text.match(/(?:Expires?|Expiry)[:\\s]*(\\d{4}-\\d{2}-\\d{2}(?:\\s+\\d{2}:\\d{2}:\\d{2})?)/i);
                return match2 ? match2[1].trim() : 'Unknown';
            }
        """)
    except:
        return "Unknown"


# ================== 主逻辑 ==================
async def add_server_time():
    server_url = os.environ.get("SERVER_URL", DEFAULT_SERVER_URL)
    cookie_value = os.environ.get("REMEMBER_WEB_COOKIE", "").strip()
    cookie_name = os.environ.get("REMEMBER_WEB_COOKIE_NAME", DEFAULT_COOKIE_NAME)

    if not cookie_value:
        msg = """🎁 <b>Weirdhost 续订报告</b>

❌ 配置错误
📝 错误: REMEMBER_WEB_COOKIE 未设置"""
        await tg_notify(msg)
        return

    print("🚀 启动 Playwright...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(120000)

        # 捕获续期 API 响应
        renew_result = {"captured": False, "status": None, "body": None}

        async def capture_response(response):
            if "/renew" in response.url and "notfreeservers" in response.url:
                renew_result["captured"] = True
                renew_result["status"] = response.status
                try:
                    renew_result["body"] = await response.json()
                except:
                    renew_result["body"] = await response.text()
                print(f"📡 捕获到 API 响应: {response.status}")

        page.on("response", capture_response)

        try:
            # ========== 1. 注入 Cookie 登录 ==========
            await context.add_cookies([{
                "name": cookie_name,
                "value": cookie_value,
                "domain": "hub.weirdhost.xyz",
                "path": "/",
            }])

            print(f"🌐 访问: {server_url}")
            await page.goto(server_url, timeout=90000)
            
            # 等待初始 CF 验证
            await wait_for_cloudflare(page, max_wait=60)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await wait_for_page_ready(page, max_wait=20)

            # 检查登录状态
            if "/auth/login" in page.url or "/login" in page.url:
                msg = """🎁 <b>Weirdhost 续订报告</b>

❌ 登录失败
📝 错误: Cookie 已失效，请手动更新"""
                await page.screenshot(path="cookie_expired.png", full_page=True)
                await tg_notify_photo("cookie_expired.png", msg)
                return

            print("✅ 登录成功")

            # ========== 2. 获取当前到期时间 ==========
            expiry_time = await get_expiry_time(page)
            remaining_time = calculate_remaining_time(expiry_time)
            print(f"📅 到期时间: {expiry_time} | 剩余: {remaining_time}")

            # ========== 3. 查找续期按钮 ==========
            print("🔍 查找续期按钮...")
            
            selectors = [
                'button:has-text("시간추가")',
                'button:has-text("Add Time")',
                'button:has-text("Renew")',
            ]
            
            add_button = None
            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    if await locator.count() > 0:
                        add_button = locator.nth(0)
                        print(f"✅ 找到按钮: {selector}")
                        break
                except:
                    continue

            if not add_button:
                msg = f"""🎁 <b>Weirdhost 续订报告</b>

⚠️ 未找到续期按钮
📅 到期时间: {expiry_time}
⏳ 剩余时间: {remaining_time}
🔗 {server_url}"""
                await page.screenshot(path="no_button.png", full_page=True)
                await tg_notify_photo("no_button.png", msg)
                return

            # ========== 4. 点击续期按钮 ==========
            print("⏳ 等待按钮可点击...")
            await add_button.wait_for(state="visible", timeout=10000)
            await page.wait_for_timeout(1000)
            
            await add_button.click()
            print("🔄 已点击续期按钮")

            # ========== 5. 【关键】等待按钮点击后的 CF 验证 ==========
            print("🛡️ 等待按钮点击后的 CF 验证...")
            await page.wait_for_timeout(2000)  # 先等待 2 秒让 CF 弹出
            
            # 检测并等待 CF 验证
            cf_passed = await wait_for_cloudflare(page, max_wait=60)
            
            if not cf_passed:
                # CF 验证超时，但可能 API 已经发送，继续检查
                print("⚠️ CF 验证可能超时，继续检查 API 响应...")

            # ========== 6. 等待 API 响应 ==========
            print("⏳ 等待 API 响应...")
            for i in range(30):
                if renew_result["captured"]:
                    print(f"✅ API 响应已捕获 ({i+1}秒)")
                    break
                if i % 5 == 4:
                    print(f"⏳ 仍在等待 API... ({i+1}秒)")
                await page.wait_for_timeout(1000)

            # ========== 7. 处理结果 ==========
            if renew_result["captured"]:
                status = renew_result["status"]
                body = renew_result["body"]

                if status in (200, 201, 204):
                    # ✅ 续期成功
                    await page.wait_for_timeout(2000)
                    await page.reload()
                    await wait_for_cloudflare(page, max_wait=30)
                    await page.wait_for_load_state("networkidle", timeout=30000)
                    new_expiry = await get_expiry_time(page)
                    new_remaining = calculate_remaining_time(new_expiry)
                    
                    msg = f"""🎁 <b>Weirdhost 续订报告</b>

✅ 续期成功！
📅 新到期时间: {new_expiry}
⏳ 剩余时间: {new_remaining}
🔗 {server_url}"""
                    
                    print(f"✅ 续期成功！新到期时间: {new_expiry}")
                    await tg_notify(msg)

                elif status == 400:
                    error_detail = parse_renew_error(body)
                    
                    if is_cooldown_error(error_detail):
                        msg = f"""🎁 <b>Weirdhost 续订报告</b>

ℹ️ 暂无需续期（冷却期内）
📅 到期时间: {expiry_time}
⏳ 剩余时间: {remaining_time}
🔗 {server_url}

💡 下次可续期时会自动尝试"""
                        
                        print(f"ℹ️ 冷却期内，剩余: {remaining_time}")
                        await tg_notify(msg)
                    else:
                        msg = f"""🎁 <b>Weirdhost 续订报告</b>

❌ 续期失败
📝 错误: {error_detail}
📅 到期时间: {expiry_time}
⏳ 剩余时间: {remaining_time}"""
                        
                        print(f"❌ 续期失败: {error_detail}")
                        await tg_notify(msg)

                else:
                    msg = f"""🎁 <b>Weirdhost 续订报告</b>

❌ 续期失败
📝 错误: HTTP {status} - {body}
📅 到期时间: {expiry_time}
⏳ 剩余时间: {remaining_time}"""
                    
                    await tg_notify(msg)

            else:
                # ⚠️ 未捕获到响应 - 可能需要再次点击
                print("⚠️ 未捕获到 API，尝试再次点击...")
                
                # 刷新页面重试一次
                await page.reload()
                await wait_for_cloudflare(page, max_wait=60)
                await page.wait_for_load_state("networkidle", timeout=30000)
                await wait_for_page_ready(page, max_wait=15)
                
                # 再次查找并点击按钮
                for selector in selectors:
                    try:
                        locator = page.locator(selector)
                        if await locator.count() > 0:
                            add_button = locator.nth(0)
                            break
                    except:
                        continue
                
                if add_button and await add_button.count() > 0:
                    await add_button.click()
                    print("🔄 再次点击续期按钮")
                    
                    await page.wait_for_timeout(2000)
                    await wait_for_cloudflare(page, max_wait=60)
                    
                    # 再次等待 API
                    for i in range(30):
                        if renew_result["captured"]:
                            break
                        await page.wait_for_timeout(1000)
                    
                    if renew_result["captured"]:
                        status = renew_result["status"]
                        body = renew_result["body"]
                        
                        if status in (200, 201, 204):
                            new_expiry = await get_expiry_time(page)
                            new_remaining = calculate_remaining_time(new_expiry)
                            msg = f"""🎁 <b>Weirdhost 续订报告</b>

✅ 续期成功！
📅 新到期时间: {new_expiry}
⏳ 剩余时间: {new_remaining}
🔗 {server_url}"""
                            await tg_notify(msg)
                        elif status == 400 and is_cooldown_error(parse_renew_error(body)):
                            msg = f"""🎁 <b>Weirdhost 续订报告</b>

ℹ️ 暂无需续期（冷却期内）
📅 到期时间: {expiry_time}
⏳ 剩余时间: {remaining_time}
🔗 {server_url}

💡 下次可续期时会自动尝试"""
                            await tg_notify(msg)
                        else:
                            msg = f"""🎁 <b>Weirdhost 续订报告</b>

❌ 续期失败
📝 错误: {parse_renew_error(body)}
📅 到期时间: {expiry_time}
⏳ 剩余时间: {remaining_time}"""
                            await tg_notify(msg)
                        
                        # 跳过后面的未捕获通知
                        renew_result["captured"] = True
                
                if not renew_result["captured"]:
                    msg = f"""🎁 <b>Weirdhost 续订报告</b>

⚠️ 未检测到 API 响应（已重试）
📅 到期时间: {expiry_time}
⏳ 剩余时间: {remaining_time}
🔗 {server_url}

💡 可能是 CF 验证未通过，请手动检查"""
                    
                    await page.screenshot(path="no_response.png", full_page=True)
                    await tg_notify_photo("no_response.png", msg)

            # ========== 8. 更新 Cookie ==========
            new_name, new_value = await extract_remember_cookie(context)
            if new_value and new_value != cookie_value:
                print("🔄 检测到新 Cookie，正在更新...")
                await update_github_secret("REMEMBER_WEB_COOKIE", new_value)
                if new_name != cookie_name:
                    await update_github_secret("REMEMBER_WEB_COOKIE_NAME", new_name)

        except Exception as e:
            msg = f"""🎁 <b>Weirdhost 续订报告</b>

❌ 脚本异常
📝 错误: {repr(e)}"""
            print(msg)
            try:
                await page.screenshot(path="error.png", full_page=True)
                await tg_notify_photo("error.png", msg)
            except:
                pass
            await tg_notify(msg)

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(add_server_time())
