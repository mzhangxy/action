#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KataBump 自动续订脚本 (Playwright 版本)
cron: 0 9,21 * * *
new Env('KataBump续订');
"""

import os
import sys
import re
import asyncio
import requests
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

# 配置
DASHBOARD_URL = 'https://dashboard.katabump.com'
SERVER_ID = os.environ.get('KATA_SERVER_ID', '185829')
KATA_EMAIL = os.environ.get('KATA_EMAIL', '')
KATA_PASSWORD = os.environ.get('KATA_PASSWORD', '')
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID = os.environ.get('TG_USER_ID', '')

SCREENSHOT_DIR = os.environ.get('SCREENSHOT_DIR', '/tmp')


def log(msg):
    tz = timezone(timedelta(hours=8))
    t = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{t}] {msg}')


def tg_notify(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        requests.post(
            f'https://telegram.alist.fr.cr/bot{TG_BOT_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': message, 'parse_mode': 'HTML'},
            timeout=30
        )
        log('✅ Telegram 通知已发送')
        return True
    except Exception as e:
        log(f'❌ Telegram 错误: {e}')
    return False


def tg_notify_photo(photo_path, caption=''):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        with open(photo_path, 'rb') as f:
            requests.post(
                f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto',
                data={'chat_id': TG_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'},
                files={'photo': f},
                timeout=60
            )
        log('✅ Telegram 截图已发送')
        return True
    except Exception as e:
        log(f'❌ Telegram 图片发送错误: {e}')
    return False


def get_expiry_from_text(text):
    match = re.search(r'Expiry[\s\S]*?(\d{4}-\d{2}-\d{2})', text, re.IGNORECASE)
    return match.group(1) if match else None


def days_until(date_str):
    try:
        exp = datetime.strptime(date_str, '%Y-%m-%d')
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return (exp - today).days
    except:
        return None


async def run():
    log('🚀 KataBump 自动续订 (Playwright)')
    log(f'🖥 服务器 ID: {SERVER_ID}')
    
    server_url = f'{DASHBOARD_URL}/servers/edit?id={SERVER_ID}'
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ]
        )
        
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        page = await context.new_page()
        
        try:
            # ========== 登录 ==========
            log('🔐 正在登录...')
            
            await page.goto(f'{DASHBOARD_URL}/auth/login', timeout=60000)
            await page.wait_for_load_state('networkidle', timeout=30000)
            
            # 填写登录表单
            email_input = page.locator('input[name="email"], input[type="email"]')
            await email_input.wait_for(timeout=10000)
            await email_input.fill(KATA_EMAIL)
            
            password_input = page.locator('input[name="password"], input[type="password"]')
            await password_input.fill(KATA_PASSWORD)
            
            login_btn = page.locator('button[type="submit"], input[type="submit"]')
            await login_btn.first.click()
            
            await page.wait_for_timeout(3000)
            await page.wait_for_load_state('networkidle', timeout=30000)
            
            if '/auth/login' in page.url:
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'login_failed.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                tg_notify_photo(screenshot_path, '❌ 登录失败，请检查账号密码')
                raise Exception('登录失败')
            
            log('✅ 登录成功')
            
            # ========== 打开服务器页面 ==========
            log(f'📄 打开服务器页面...')
            
            await page.goto(server_url, timeout=90000)
            await page.wait_for_load_state('networkidle', timeout=30000)
            
            # 获取当前到期时间
            page_content = await page.content()
            old_expiry = get_expiry_from_text(page_content) or '未知'
            days = days_until(old_expiry)
            log(f'📅 当前到期: {old_expiry} (剩余 {days} 天)')
            
            # ========== 第一步：点击主页面 Renew 按钮（打开模态框） ==========
            log('🔍 查找主页面 Renew 按钮...')
            
            # 定位主页面的 Renew 按钮（有 data-bs-target="#renew-modal" 属性）
            main_renew_btn = page.locator('button[data-bs-target="#renew-modal"]')
            
            if await main_renew_btn.count() == 0:
                # 备用选择器
                main_renew_btn = page.locator('button.btn-outline-primary:has-text("Renew")')
            
            if await main_renew_btn.count() == 0:
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'no_renew_button.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                tg_notify_photo(screenshot_path, f'❌ 未找到 Renew 按钮\n\n🖥 服务器: {SERVER_ID}')
                raise Exception('未找到主页面 Renew 按钮')
            
            log('🖱 点击主页面 Renew 按钮（打开模态框）...')
            await main_renew_btn.first.click()
            
            # 等待模态框出现
            await page.wait_for_timeout(1000)
            
            # ========== 第二步：点击模态框内的 Renew 按钮（确认续期） ==========
            log('🔍 等待模态框出现...')
            
            # 等待模态框显示
            modal = page.locator('#renew-modal')
            try:
                await modal.wait_for(state='visible', timeout=5000)
                log('✅ 模态框已打开')
            except:
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'modal_not_found.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                tg_notify_photo(screenshot_path, '❌ 模态框未打开')
                raise Exception('模态框未打开')
            
            # 定位模态框内的确认 Renew 按钮（type="submit"）
            modal_renew_btn = page.locator('#renew-modal button[type="submit"]')
            
            if await modal_renew_btn.count() == 0:
                # 备用选择器
                modal_renew_btn = page.locator('#renew-modal .modal-footer button.btn-primary')
            
            if await modal_renew_btn.count() == 0:
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'no_confirm_button.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                tg_notify_photo(screenshot_path, '❌ 未找到确认按钮')
                raise Exception('未找到模态框确认按钮')
            
            log('🖱 点击模态框内 Renew 按钮（确认续期）...')
            await modal_renew_btn.first.click()
            
            # 等待页面响应
            await page.wait_for_timeout(3000)
            await page.wait_for_load_state('networkidle', timeout=30000)
            
            # ========== 检查结果 ==========
            log('🔍 检查续订结果...')
            
            current_url = page.url
            page_content = await page.content()
            
            if 'renew=success' in current_url:
                new_expiry = get_expiry_from_text(page_content) or '未知'
                log(f'🎉 续订成功！新到期: {new_expiry}')
                
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'renew_success.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                tg_notify_photo(
                    screenshot_path,
                    f'✅ KataBump 续订成功\n\n'
                    f'🖥 服务器: <code>{SERVER_ID}</code>\n'
                    f'📅 原到期: {old_expiry}\n'
                    f'📅 新到期: {new_expiry}'
                )
                return
            
            elif 'renew-error' in current_url:
                error_match = re.search(r'renew-error=([^&]+)', current_url)
                error_msg = '未知错误'
                if error_match:
                    from urllib.parse import unquote
                    error_msg = unquote(error_match.group(1).replace('+', ' '))
                
                log(f'⏳ 续订受限: {error_msg}')
                
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'renew_limited.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                
                if days is not None and days <= 2:
                    tg_notify_photo(
                        screenshot_path,
                        f'ℹ️ KataBump 续订提醒\n\n'
                        f'🖥 服务器: <code>{SERVER_ID}</code>\n'
                        f'📅 到期: {old_expiry}\n'
                        f'⏰ 剩余: {days} 天\n'
                        f'📝 {error_msg}'
                    )
                return
            
            elif 'captcha' in current_url.lower() or 'captcha' in page_content.lower():
                log('❌ 需要验证码')
                
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'captcha_required.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                
                if days is not None and days <= 2:
                    tg_notify_photo(
                        screenshot_path,
                        f'⚠️ KataBump 需要手动续订\n\n'
                        f'🖥 服务器: <code>{SERVER_ID}</code>\n'
                        f'📅 到期: {old_expiry}\n'
                        f'⏰ 剩余: {days} 天\n'
                        f'❗ 需要验证码\n\n'
                        f'👉 <a href="{server_url}">手动续订</a>'
                    )
                return
            
            # 重新检查到期时间
            await page.goto(server_url, timeout=60000)
            await page.wait_for_load_state('networkidle', timeout=30000)
            page_content = await page.content()
            new_expiry = get_expiry_from_text(page_content) or '未知'
            
            if new_expiry != '未知' and old_expiry != '未知' and new_expiry > old_expiry:
                log(f'🎉 续订成功！新到期: {new_expiry}')
                
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'renew_success.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                tg_notify_photo(
                    screenshot_path,
                    f'✅ KataBump 续订成功\n\n'
                    f'🖥 服务器: <code>{SERVER_ID}</code>\n'
                    f'📅 原到期: {old_expiry}\n'
                    f'📅 新到期: {new_expiry}'
                )
            else:
                log(f'⚠️ 续订状态未知，当前到期: {new_expiry}')
                
                screenshot_path = os.path.join(SCREENSHOT_DIR, 'renew_unknown.png')
                await page.screenshot(path=screenshot_path, full_page=True)
                
                if days is not None and days <= 2:
                    tg_notify_photo(
                        screenshot_path,
                        f'⚠️ KataBump 请检查续订状态\n\n'
                        f'🖥 服务器: <code>{SERVER_ID}</code>\n'
                        f'📅 到期: {new_expiry}\n\n'
                        f'👉 <a href="{server_url}">查看详情</a>'
                    )
        
        except Exception as e:
            log(f'❌ 错误: {e}')
            tg_notify(f'❌ KataBump 出错\n\n🖥 服务器: <code>{SERVER_ID}</code>\n❗ {e}')
            raise
        
        finally:
            await browser.close()


def main():
    log('=' * 50)
    log('   KataBump 自动续订 (Playwright)')
    log('=' * 50)
    
    if not KATA_EMAIL or not KATA_PASSWORD:
        log('❌ 请设置 KATA_EMAIL 和 KATA_PASSWORD')
        sys.exit(1)
    
    asyncio.run(run())
    log('🏁 完成')


if __name__ == '__main__':
    main()
