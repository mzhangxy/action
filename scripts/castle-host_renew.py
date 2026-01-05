#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Castle-Host 服务器自动续约脚本 (增强版)
兼容 Playwright 1.48.0+ 版本
修复：text_content() 必须传入 selector 参数的问题
"""

import os
import asyncio
import aiohttp
import re
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse
from playwright.async_api import async_playwright
import sys

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('castle_renew_enhanced.log')
    ]
)
logger = logging.getLogger(__name__)

# 存储续约前后的时间
renewal_data = {
    "server_id": "",
    "before_expiry": "",
    "after_expiry": "",
    "renewal_time": "",
    "success": False,
    "error_message": ""
}

# ------------------ Telegram 通知 ------------------
async def tg_notify(message: str, token=None, chat_id=None):
    """发送Telegram通知"""
    if not token or not chat_id:
        token = os.environ.get("TG_BOT_TOKEN")
        chat_id = os.environ.get("TG_CHAT_ID")
        
    if not token or not chat_id:
        logger.info("ℹ️ Telegram通知未配置")
        return False
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            data = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            async with session.post(url, json=data, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("✅ Telegram通知已发送")
                    return True
                else:
                    logger.warning(f"⚠️ Telegram通知发送失败: {resp.status}")
                    return False
    except Exception as e:
        logger.error(f"⚠️ TG通知失败: {e}")
        return False

# ------------------ Cookie 解析 ------------------
def parse_cookie_string(cookie_str: str):
    """解析Cookie字符串为字典列表，用于Playwright"""
    cookies = []
    parts = cookie_str.split(';')
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
            
        # 分割键值对
        if '=' in part:
            name, value = part.split('=', 1)
            name = name.strip()
            value = value.strip()
            
            # 为每个Cookie创建字典
            cookie_dict = {
                "name": name,
                "value": value,
                "domain": ".castle-host.com",
                "path": "/"
            }
            
            # 如果是PHPSESSID，确保路径正确
            if name == "PHPSESSID":
                cookie_dict["path"] = "/"
                
            cookies.append(cookie_dict)
            logger.debug(f"🍪 解析Cookie: {name}={value[:30]}...")
    
    logger.info(f"✅ 成功解析 {len(cookies)} 个Cookie")
    return cookies

# ------------------ 到期时间提取 ------------------
async def extract_expiry_date(page):
    """从页面提取服务器到期时间（兼容Playwright 1.48.0+）"""
    try:
        # 方法1: 从整个页面body获取文本（新版API必须传入selector）
        body_text = await page.text_content('body')
        
        # 尝试多种模式匹配
        patterns = [
            r'Сервер действует до (\d{2}\.\d{2}\.\d{4})',
            r'Оплачено до (\d{2}\.\d{2}\.\d{4})',
            r'(\d{2}\.\d{2}\.\d{4})\s*\([^)]*\)',  # 格式: 12.01.2026 (6 д.)
            r'有效期至(\d{4}年\d{1,2}月\d{1,2}日)',
            r'该服务器有效期至(\d{4}年\d{1,2}月\d{1,2}日)',
            r'\b(\d{2}\.\d{2}\.\d{4})\b'  # 通用日期格式
        ]
        
        for pattern in patterns:
            match = re.search(pattern, body_text)
            if match:
                date_str = match.group(1)
                logger.info(f"📅 提取到到期时间: {date_str}")
                return date_str
        
        # 方法2: 使用JavaScript提取（备用方案）
        date_from_js = await page.evaluate("""
            () => {
                // 查找包含日期的元素
                const elements = document.querySelectorAll('*');
                for (let el of elements) {
                    const text = el.textContent || '';
                    const match = text.match(/\\d{2}\\.\\d{2}\\.\\d{4}/);
                    if (match) {
                        return match[0];
                    }
                }
                return null;
            }
        """)
        
        if date_from_js:
            logger.info(f"📅 JavaScript提取到到期时间: {date_from_js}")
            return date_from_js
        
        logger.warning("⚠️ 未找到到期时间")
        return None
        
    except Exception as e:
        logger.error(f"❌ 提取到期时间失败: {e}")
        return None

# ------------------ 服务器信息提取 ------------------
async def extract_server_info(page):
    """提取服务器详细信息（兼容Playwright 1.48.0+）"""
    info = {
        "status": "Unknown",
        "expiry_date": "Unknown",
        "server_name": "Unknown",
        "balance": "Unknown",
        "tariff": "Unknown",
        "days_until_expiry": "Unknown"
    }
    
    try:
        # 获取页面文本（新版API必须传入selector）
        text_content = await page.text_content('body')
        
        # 提取状态
        status_patterns = [
            r'Сервер запущен',
            r'Server running',
            r'Сервер остановлен',
            r'Server stopped'
        ]
        
        for pattern in status_patterns:
            if re.search(pattern, text_content, re.IGNORECASE):
                if "запущен" in pattern or "running" in pattern:
                    info["status"] = "运行中"
                else:
                    info["status"] = "已停止"
                break
        
        # 提取到期时间（使用专门的函数）
        expiry_date = await extract_expiry_date(page)
        if expiry_date:
            info["expiry_date"] = expiry_date
        
        # 提取剩余天数
        days_pattern = r'До продления: ≈ (\d+) дней?'
        days_match = re.search(days_pattern, text_content, re.IGNORECASE)
        if days_match:
            info["days_until_expiry"] = days_match.group(1)
        
        # 提取服务器名称
        name_pattern = r'MineCraft: PE.*?>\s*(.*?)\s*<'
        name_match = re.search(name_pattern, text_content, re.DOTALL)
        if name_match:
            info["server_name"] = name_match.group(1).strip()
        
        # 提取余额
        balance_pattern = r'(\d+\.\d+)\s*₽'
        balance_match = re.search(balance_pattern, text_content)
        if balance_match:
            info["balance"] = balance_match.group(1)
        
        # 提取套餐
        tariff_pattern = r'Бесплатный|Бесплатно|Free'
        if re.search(tariff_pattern, text_content, re.IGNORECASE):
            info["tariff"] = "免费"
        else:
            info["tariff"] = "付费"
        
        logger.info(f"📊 服务器信息: 状态={info['status']}, 到期={info['expiry_date']}, 剩余天数={info['days_until_expiry']}")
        
    except Exception as e:
        logger.error(f"⚠️ 提取服务器信息失败: {e}")
    
    return info

# ------------------ 日期验证和计算 ------------------
def parse_date(date_str):
    """解析日期字符串为datetime对象"""
    try:
        # 尝试不同格式
        formats = [
            '%d.%m.%Y',  # 12.01.2026
            '%Y年%m月%d日',  # 2026年1月12日
            '%Y-%m-%d',  # 2026-01-12
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        
        # 尝试从字符串中提取数字
        numbers = re.findall(r'\d+', date_str)
        if len(numbers) >= 3:
            # 假设格式为 日.月.年
            if len(numbers[2]) == 4:  # 年份为4位
                return datetime(int(numbers[2]), int(numbers[1]), int(numbers[0]))
        
        return None
    except Exception as e:
        logger.error(f"❌ 解析日期失败: {date_str}, 错误: {e}")
        return None

def calculate_date_difference(date1_str, date2_str):
    """计算两个日期之间的天数差"""
    date1 = parse_date(date1_str)
    date2 = parse_date(date2_str)
    
    if not date1 or not date2:
        return None
    
    difference = (date2 - date1).days
    return difference

# ------------------ 续约执行 ------------------
async def perform_renewal(page, server_id):
    """执行续约操作"""
    logger.info(f"🔄 开始续约流程，服务器ID: {server_id}")
    
    try:
        # 查找续约按钮
        renew_button_selectors = [
            '#freebtn',
            'button:has-text("Продлить")',
            'button:has-text("Renew")',
            'button:has-text("续约")',
            'button:has-text("продлить")',
            'button[onclick*="freePay"]'
        ]
        
        button_found = False
        for selector in renew_button_selectors:
            button = page.locator(selector)
            if await button.count() > 0:
                logger.info(f"🖱️ 找到续约按钮: {selector}")
                
                # 检查按钮是否禁用
                is_disabled = await button.get_attribute("disabled")
                if is_disabled:
                    logger.error("❌ 续约按钮已禁用，无法点击")
                    return False
                
                # 监听网络请求
                request_sent = False
                request_success = False
                
                def handle_request(request):
                    nonlocal request_sent
                    if "/buy_months/" in request.url:
                        logger.info(f"📡 检测到续约请求: {request.method} {request.url}")
                        request_sent = True
                
                def handle_response(response):
                    nonlocal request_success
                    if "/buy_months/" in response.url:
                        logger.info(f"📡 续约响应状态: {response.status}")
                        request_success = response.status == 200
                
                page.on("request", handle_request)
                page.on("response", handle_response)
                
                # 点击按钮
                await button.click()
                
                # 等待请求发送
                for i in range(10):
                    if request_sent:
                        break
                    await asyncio.sleep(0.5)
                
                if not request_sent:
                    logger.warning("⚠️ 未检测到续约请求，可能按钮点击无效")
                
                # 等待可能的弹窗或提示
                await page.wait_for_timeout(3000)
                
                # 检查是否有成功提示
                success_indicators = [
                    "успех", "success", "продлен", "renewed", "续约成功",
                    "Сервер продлен", "Server renewed"
                ]
                
                page_text = await page.text_content('body')
                for indicator in success_indicators:
                    if indicator.lower() in page_text.lower():
                        logger.info(f"✅ 检测到成功提示: {indicator}")
                        request_success = True
                        break
                
                # 检查是否有错误提示
                error_indicators = [
                    "ошибка", "error", "失败", "не удалось",
                    "Уже продлен", "Already renewed",
                    "Недостаточно средств", "Insufficient funds"
                ]
                
                for indicator in error_indicators:
                    if indicator.lower() in page_text.lower():
                        logger.warning(f"⚠️ 检测到错误提示: {indicator}")
                        return False
                
                button_found = True
                
                if request_sent and request_success:
                    logger.info("✅ 续约请求发送成功")
                    return True
                elif request_success:
                    logger.info("✅ 续约可能成功（有成功提示）")
                    return True
                else:
                    logger.warning("⚠️ 续约状态不确定")
                    return True  # 假设成功，继续验证
                
                break
        
        if not button_found:
            logger.error("❌ 未找到续约按钮")
            
            # 尝试通过JavaScript调用freePay函数
            try:
                result = await page.evaluate("""
                    () => {
                        if (typeof freePay === 'function') {
                            freePay();
                            return true;
                        }
                        return false;
                    }
                """)
                
                if result:
                    logger.info("✅ 通过JavaScript调用freePay函数")
                    await page.wait_for_timeout(3000)
                    return True
                else:
                    logger.error("❌ freePay函数不存在")
                    return False
            except Exception as e:
                logger.error(f"❌ 调用freePay函数失败: {e}")
                return False
            
    except Exception as e:
        logger.error(f"❌ 续约过程中出现错误: {e}")
        return False
    
    return False

# ------------------ 验证续约是否成功 ------------------
async def verify_renewal(page, original_expiry):
    """验证续约是否成功，返回新的到期时间（兼容Playwright 1.48.0+）"""
    try:
        # 等待一段时间让页面更新
        await asyncio.sleep(2)
        
        # 重新加载页面获取最新信息
        await page.reload(wait_until="networkidle")
        await asyncio.sleep(2)
        
        # 提取新的到期时间
        new_expiry = await extract_expiry_date(page)
        
        if not new_expiry:
            logger.warning("⚠️ 无法获取续约后的到期时间")
            return None
        
        logger.info(f"📅 续约前到期时间: {original_expiry}")
        logger.info(f"📅 续约后到期时间: {new_expiry}")
        
        # 对比日期
        if original_expiry and new_expiry:
            original_date = parse_date(original_expiry)
            new_date = parse_date(new_expiry)
            
            if original_date and new_date:
                days_added = (new_date - original_date).days
                logger.info(f"📊 续期增加了 {days_added} 天")
                
                # 免费服务器通常增加7天
                if days_added >= 1:
                    logger.info("✅ 续约成功验证通过")
                    return new_expiry
                else:
                    logger.warning(f"⚠️ 续期天数异常: 增加了 {days_added} 天")
                    return new_expiry
        
        return new_expiry
        
    except Exception as e:
        logger.error(f"❌ 验证续约结果失败: {e}")
        return None

# ------------------ 主函数 ------------------
async def main():
    """主执行函数"""
    logger.info("=" * 60)
    logger.info("Castle-Host 服务器自动续约脚本 (增强版)")
    logger.info("兼容 Playwright 1.48.0+ 版本")
    logger.info("=" * 60)
    
    # 获取环境变量
    cookie_str = os.environ.get("CASTLE_COOKIES", "").strip()
    server_id = os.environ.get("SERVER_ID", "117954")
    tg_token = os.environ.get("TG_BOT_TOKEN")
    tg_chat_id = os.environ.get("TG_CHAT_ID")
    
    if not cookie_str:
        error_msg = "❌ 错误：未设置 CASTLE_COOKIES 环境变量"
        logger.error(error_msg)
        await tg_notify(error_msg, tg_token, tg_chat_id)
        return
    
    # 初始化续约数据
    renewal_data["server_id"] = server_id
    renewal_data["renewal_time"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 解析Cookie
    cookies = parse_cookie_string(cookie_str)
    if not cookies:
        error_msg = "❌ 错误：无法解析Cookie字符串"
        logger.error(error_msg)
        await tg_notify(error_msg, tg_token, tg_chat_id)
        return
    
    # 服务器URL
    server_url = f"https://cp.castle-host.com/servers/pay/index/{server_id}"
    
    # 启动浏览器
    logger.info("🚀 启动浏览器...")
    async with async_playwright() as p:
        # 使用Chromium浏览器
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage'
            ]
        )
        
        # 创建浏览器上下文
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        
        # 添加Cookie
        await context.add_cookies(cookies)
        logger.info("✅ Cookie已添加到浏览器")
        
        # 创建页面
        page = await context.new_page()
        page.set_default_timeout(60000)
        page.set_default_navigation_timeout(60000)
        
        try:
            # 访问服务器页面
            logger.info(f"🌐 访问服务器页面: {server_url}")
            await page.goto(server_url, wait_until="networkidle")
            
            # 检查是否登录成功
            current_url = page.url
            if "login" in current_url or "auth" in current_url:
                error_msg = "❌ Cookie失效，无法登录"
                logger.error(error_msg)
                
                # 截图保存
                screenshot_path = "login_failed.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.info(f"📸 截图已保存: {screenshot_path}")
                
                await tg_notify(error_msg, tg_token, tg_chat_id)
                return
            
            logger.info("✅ 登录成功")
            
            # 提取服务器信息
            server_info = await extract_server_info(page)
            
            # 提取原始到期时间
            original_expiry = server_info.get("expiry_date", "Unknown")
            renewal_data["before_expiry"] = original_expiry
            
            # 检查是否需要续约
            days_until_expiry = server_info.get("days_until_expiry", "Unknown")
            if days_until_expiry != "Unknown":
                try:
                    days = int(days_until_expiry)
                    if days > 3:
                        logger.info(f"ℹ️ 距离到期还有 {days} 天，无需立即续约")
                except:
                    pass
            
            # 执行续约
            renewal_success = await perform_renewal(page, server_id)
            
            # 验证续约结果
            new_expiry = None
            if renewal_success:
                new_expiry = await verify_renewal(page, original_expiry)
                renewal_data["after_expiry"] = new_expiry if new_expiry else "Unknown"
            
            # 更新续约状态
            if new_expiry and new_expiry != "Unknown":
                # 对比日期确认是否成功
                if original_expiry != new_expiry:
                    renewal_data["success"] = True
                    days_added = calculate_date_difference(original_expiry, new_expiry)
                    if days_added:
                        logger.info(f"✅ 续约成功！增加了 {days_added} 天")
                    else:
                        logger.info("✅ 到期时间已更新，续约成功")
                else:
                    renewal_data["success"] = False
                    renewal_data["error_message"] = "到期时间未变化"
                    logger.warning("⚠️ 到期时间未变化，续约可能未成功")
            else:
                renewal_data["success"] = renewal_success
                if not renewal_success:
                    renewal_data["error_message"] = "续约操作失败"
            
            # 准备通知消息
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            if renewal_data["success"]:
                message = f"""✅ Castle-Host 服务器续约成功！

🆔 服务器ID: {server_id}
📛 服务器名称: {server_info.get('server_name', 'Unknown')}
📊 当前状态: {server_info.get('status', 'Unknown')}
💰 账户余额: {server_info.get('balance', 'Unknown')} ₽
🎫 当前套餐: {server_info.get('tariff', 'Unknown')}
📅 续约前到期: {original_expiry}
📅 续约后到期: {new_expiry if new_expiry else 'Unknown'}
⏰ 续约时间: {current_time}
🔗 管理页面: {server_url}"""
                
                # 计算增加的天数
                if original_expiry != "Unknown" and new_expiry and new_expiry != "Unknown":
                    days_added = calculate_date_difference(original_expiry, new_expiry)
                    if days_added:
                        message += f"\n📈 续期增加: {days_added} 天"
                
                logger.info("🎉 续约成功！")
                
            else:
                message = f"""⚠️ Castle-Host 服务器续约失败！

🆔 服务器ID: {server_id}
📛 服务器名称: {server_info.get('server_name', 'Unknown')}
📊 当前状态: {server_info.get('status', 'Unknown')}
💰 账户余额: {server_info.get('balance', 'Unknown')} ₽
🎫 当前套餐: {server_info.get('tariff', 'Unknown')}
📅 当前到期: {original_expiry}
⏰ 操作时间: {current_time}
❌ 错误信息: {renewal_data.get('error_message', '未知错误')}
🔗 管理页面: {server_url}

💡 可能原因：
1. Cookie已过期
2. 已续约过，需等待24小时
3. 服务器已达到最大续期天数
4. 网络或系统问题
5. VK群组验证未通过"""

                logger.error("❌ 续约失败")
            
            # 发送Telegram通知
            await tg_notify(message, tg_token, tg_chat_id)
            
            # 保存续约数据到文件
            with open("renewal_history.json", "a", encoding="utf-8") as f:
                json.dump(renewal_data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            logger.info("💾 续约记录已保存到 renewal_history.json")
            
            # 保存成功截图
            screenshot_path = "renewal_result.png"
            await page.screenshot(path=screenshot_path, full_page=True)
            logger.info(f"📸 结果截图已保存: {screenshot_path}")
            
        except Exception as e:
            error_msg = f"❌ 脚本执行过程中出现错误: {str(e)}"
            logger.error(error_msg, exc_info=True)
            renewal_data["success"] = False
            renewal_data["error_message"] = str(e)
            
            try:
                screenshot_path = "error.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                logger.info(f"📸 错误截图已保存: {screenshot_path}")
            except:
                pass
            
            # 发送错误通知
            await tg_notify(error_msg, tg_token, tg_chat_id)
            
            # 保存错误数据
            with open("renewal_history.json", "a", encoding="utf-8") as f:
                json.dump(renewal_data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            
        finally:
            # 关闭浏览器
            await context.close()
            await browser.close()
            logger.info("👋 浏览器已关闭")
            
            # 输出总结
            logger.info("=" * 60)
            logger.info("续约结果总结:")
            logger.info(f"  服务器ID: {renewal_data['server_id']}")
            logger.info(f"  续约时间: {renewal_data['renewal_time']}")
            logger.info(f"  续约前到期: {renewal_data['before_expiry']}")
            logger.info(f"  续约后到期: {renewal_data['after_expiry']}")
            logger.info(f"  是否成功: {'✅ 是' if renewal_data['success'] else '❌ 否'}")
            logger.info("=" * 60)

# ------------------ 入口点 ------------------
if __name__ == "__main__":
    print("=" * 60)
    print("Castle-Host 服务器自动续约脚本 (增强版)")
    print("兼容 Playwright 1.48.0+ 版本")
    print("修复了 text_content() API 兼容性问题")
    print("=" * 60)
    
    # 检查环境变量
    cookie_str = os.environ.get("CASTLE_COOKIES", "").strip()
    
    if not cookie_str:
        print("❌ 错误：未设置 CASTLE_COOKIES 环境变量")
        print()
        print("💡 使用方法：")
        print("1. 从浏览器复制Cookie字符串：")
        print("   - 打开 https://cp.castle-host.com 并登录")
        print("   - 按F12打开开发者工具")
        print("   - 进入Application/Storage/Cookies")
        print("   - 复制所有Cookie值")
        print()
        print("2. 设置环境变量：")
        print("   export CASTLE_COOKIES=\"PHPSESSID=xxx; uid=xxx; ...\"")
        print()
        print("3. 运行脚本：")
        print("   python castle_renew_enhanced.py")
        print()
        print("4. 可选：设置Telegram通知")
        print("   export TG_BOT_TOKEN=\"your_token\"")
        print("   export TG_CHAT_ID=\"your_chat_id\"")
        print()
        print("5. 可选：指定服务器ID（默认为117954）")
        print("   export SERVER_ID=\"117954\"")
        sys.exit(1)
    
    # 运行主函数
    asyncio.run(main())
