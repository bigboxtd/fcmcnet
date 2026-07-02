"""
FreeMcServer.net 自动登录 + 续期（Extended Renewal / 看广告续期）
浏览器引擎: CloakBrowser (Playwright 接口，C++ 层指纹伪装，可过 CF Turnstile)

整体流程（对应人工操作步骤）:
  1. 打开登录页 -> 填用户名密码 -> 等待/点击 Cloudflare Turnstile 通过 -> 提交登录
  2. 登录成功后直接跳转 /server/{id}/renew
  3. 页面上有大量弹窗广告，全程用后台清理函数「实时」检测并关闭，避免遮挡点击
  4. 向下滚动找到 "Choose Extended Renewal" 按钮并点击，进入 /renew-with-ads
  5. 该页面加载后会先弹出一个 "Captcha is required" 的错误提示框，点击 OK 关掉
  6. 等待该页面内嵌的 Turnstile 验证通过（自动或点击验证框）
  7. 点击 "Watch Ad and Renew!" 按钮，触发广告
  8. 广告播放期间会有各种插页广告弹出，持续清理；等广告右上角出现 Close 就点掉
  9. 广告结束后站点自动完成续期，弹出 "Success / Your server was renewed" 提示框，点击 OK

如果扩展续期（看广告）流程失败（比如广告不可用/按钮找不到/被识别为拦截器），
不会回退到 Normal Renewal，而是重新回到续期页面，重试 Extended Renewal
（次数由 FMC_EXTENDED_RETRIES 控制），因为 Normal Renewal 续的时长更短，
优先级低于「多试几次 Extended Renewal」。

Profile 持久化: GitHub Actions Cache（不写入 git 历史，公开仓库安全）
代理: Xray SOCKS5 本地代理，透传给 CloakBrowser
通知: WxPusher + Telegram 双通道，任一未配置则自动跳过，互不影响
"""

import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

import requests

# 强制无缓冲输出，GitHub Actions 才能实时看到日志
sys.stdout.reconfigure(line_buffering=False)
sys.stderr.reconfigure(line_buffering=False)

# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------
BASE_URL   = os.getenv("FMC_BASE_URL", "https://panel.freemcserver.net").rstrip("/")
SERVER_ID  = os.getenv("FMC_SERVER_ID", "").strip()
RENEW_URL       = f"{BASE_URL}/server/{SERVER_ID}/renew" if SERVER_ID else None
RENEW_ADS_URL   = f"{BASE_URL}/server/{SERVER_ID}/renew-with-ads" if SERVER_ID else None
RENEW_BASIC_URL = f"{BASE_URL}/server/{SERVER_ID}/renew-basic" if SERVER_ID else None
# 服务器主页：Server Expiration 到期时间卡片在这个页面上，不在 /renew 页面上
# （从 DevTools 截图确认：<script>window.fmcs.server_expires_at = "..."</script>
#  和 #server-expiration .badge-light 都只出现在 /server/{id} 主页）
SERVER_HOME_URL = f"{BASE_URL}/server/{SERVER_ID}" if SERVER_ID else None

USERNAME  = os.getenv("FMC_USERNAME")
PASSWORD  = os.getenv("FMC_PASSWORD")
LOGIN_URL = os.getenv("FMC_LOGIN_URL", f"{BASE_URL}/user/login")

# persistent_context profile 目录（由 workflow 的 actions/cache 在运行间持久化）
PROFILE_DIR = os.getenv("FMC_PROFILE_DIR", "state/freemcserver_profile")

# SOCKS5 代理，空字符串 = 直连
PROXY = os.getenv("PROXY", "socks5://127.0.0.1:10808").strip()

# Extended Renewal 失败后不再回退到 Normal Renewal，而是重试 Extended Renewal 的次数
# （1 表示总共尝试 1+1=2 次，以此类推）
EXTENDED_RENEWAL_RETRIES = int(os.getenv("FMC_EXTENDED_RETRIES", "1"))

BEFORE_SCREENSHOT_FILE = "freemcserver_before_renew.png"
AFTER_SCREENSHOT_FILE  = "freemcserver_after_renew.png"

# 录屏开关（GitHub Actions 里配合 Xvfb 有头模式使用）
ENABLE_RECORDING = os.getenv("ENABLE_RECORDING", "true").strip().lower() == "true"
RECORD_FILE      = "freemcserver_record.mp4"
SCREENSHOT_FILE  = "freemcserver_debug_screenshot.png"

XVFB_WIDTH  = 1366
XVFB_HEIGHT = 768

# 固定 fingerprint seed：让每次运行看起来像同一台设备（returning visitor）
FINGERPRINT_SEED = os.getenv("FMC_FINGERPRINT_SEED", "778899")

TURNSTILE_SITEKEY = "0x4AAAAAAAGCtSTbw9pROsNY"

LOGIN_SUCCESS = False
RENEW_SUCCESS = False


# ---------------------------------------------------------------------------
# 通知：WxPusher + Telegram 双通道
# 任一渠道的环境变量没配置就自动跳过该渠道，不影响另一个，也不影响主流程。
# ---------------------------------------------------------------------------
def _send_wxpusher(message, photo_path=None):
    uid   = os.getenv("WXPUSHER_UID")
    token = os.getenv("WXPUSHER_APP_TOKEN")
    if not uid or not token:
        print("未配置 WxPusher 变量 (WXPUSHER_UID / WXPUSHER_APP_TOKEN)，跳过 WxPusher 推送。")
        return
    try:
        resp = requests.post(
            "https://wxpusher.zjiecode.com/api/send/message",
            json={
                "appToken": token,
                "content": message,
                "summary": re.sub(r"<[^>]+>", "", message)[:20] or "FreeMcServer 通知",
                "contentType": 2,  # 2 = HTML
                "uids": [uid],
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("success"):
            print("WxPusher 通知发送成功。")
        else:
            print(f"WxPusher 通知返回失败: {data}")
    except Exception as e:
        print(f"发送 WxPusher 消息异常: {e}")
    # WxPusher 消息接口不直接支持图片，附上截图链接说明改为"仅文字通知"，
    # 如需图片可自行接入图床后把 URL 拼进 content，这里保持简单不额外引入依赖。


def _send_telegram(message, photo_path=None):
    token   = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        print("未配置 Telegram 变量 (TG_BOT_TOKEN / TG_CHAT_ID)，跳过 Telegram 推送。")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        print("Telegram 状态通知发送成功。")
    except Exception as e:
        print(f"发送 Telegram 消息异常: {e}")
    if photo_path and os.path.exists(photo_path):
        try:
            with open(photo_path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": "FreeMcServer 实时画面"},
                    files={"photo": f},
                    timeout=15,
                )
            print("Telegram 截图发送成功。")
        except Exception as e:
            print(f"发送 Telegram 截图异常: {e}")


def send_notification(message, photo_path=None):
    """同时尝试 WxPusher 和 Telegram 两个渠道，互不阻塞。"""
    _send_wxpusher(message, photo_path)
    _send_telegram(message, photo_path)


# ---------------------------------------------------------------------------
# 录屏（ffmpeg x11grab）
# ---------------------------------------------------------------------------
def get_display_resolution(display):
    try:
        out = subprocess.check_output(
            ["xdpyinfo", "-display", display], stderr=subprocess.DEVNULL
        ).decode()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("dimensions:"):
                dims = line.split()[1]
                w, h = dims.split("x")
                return int(w), int(h)
    except Exception as e:
        print(f"探测显示器分辨率失败，使用默认 {XVFB_WIDTH}x{XVFB_HEIGHT}: {e}")
    return XVFB_WIDTH, XVFB_HEIGHT


def start_recording():
    display = os.environ.get("DISPLAY", ":99")
    width, height = get_display_resolution(display)
    print(f"开启录屏，目标显示器: {display}，分辨率: {width}x{height}")
    try:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-video_size", f"{width}x{height}",
                "-framerate", "15",
                "-f", "x11grab",
                "-i", display,
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                RECORD_FILE,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        return proc
    except Exception as e:
        print(f"启动 ffmpeg 录屏失败，本次跳过录屏: {e}")
        return None


def stop_recording(proc):
    if not proc:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
        print("录屏已正常停止并保存。")
    except Exception as e:
        print(f"停止录屏异常，强制 kill: {e}")
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------
def screenshot(page, path=SCREENSHOT_FILE):
    try:
        page.screenshot(path=path)
    except Exception as e:
        print(f"截图失败: {e}")


# ---------------------------------------------------------------------------
# 续期前 / 续期后状态快照：读取页面上的到期时间 + 截图
#
# 从 DevTools 截图确认的真实结构（在服务器主页 /server/{id}，不在 /renew 页面）：
#   <script>window.fmcs.server_expires_at = "2026-07-02 09:11:06";</script>
#   <div id="server-expiration">
#     <span class="badge badge-light">Server Expires on: 2026-07-02 09:11:06 UTC</span>
#   </div>
# 优先直接读 window.fmcs.server_expires_at 这个 JS 变量（最干净，纯 "YYYY-MM-DD HH:MM:SS"），
# 读不到再退化到读 badge 文本，最后再退化到全文正则搜 "expire" 兜底。
# ---------------------------------------------------------------------------
EXPIRY_TEXT_PATTERN = re.compile(
    r"[^\n]{0,40}\b(expir\w*|renews?\s+(on|in)|valid\s+until)\b[^\n]{0,60}", re.I
)
EXPIRY_DT_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")


def get_expiry_snapshot_text(page):
    """
    抓取到期时间，按可信度从高到低依次尝试：
      1. window.fmcs.server_expires_at（JS 变量，最干净）
      2. #server-expiration 卡片里的 .badge-light 文本
      3. 全文正则搜 "expire" 兜底（可能是别的措辞时的最后手段）
    抓不到返回 None。
    """
    try:
        js_val = page.evaluate("() => (window.fmcs && window.fmcs.server_expires_at) || null")
        if js_val:
            return str(js_val).strip()
    except Exception as e:
        print(f"  读取 window.fmcs.server_expires_at 失败: {e}")

    try:
        badge = page.locator("#server-expiration .badge-light").first
        if badge.count() > 0 and badge.is_visible(timeout=1000):
            text = badge.inner_text(timeout=3000).strip()
            if text:
                return text
    except Exception as e:
        print(f"  读取 #server-expiration badge 文本失败: {e}")

    try:
        body_text = page.locator("body").inner_text(timeout=5000)
        m = EXPIRY_TEXT_PATTERN.search(body_text)
        if m:
            return " ".join(m.group(0).split())
    except Exception as e:
        print(f"  读取页面正文失败: {e}")

    return None


def parse_expiry_dt(text):
    """从抓取到的到期时间文本里解析出 datetime（UTC，naive），解析不出返回 None。"""
    if not text:
        return None
    m = EXPIRY_DT_PATTERN.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def capture_renew_snapshot(page, label, screenshot_path, goto_home_page=True):
    """
    续期前/续期后各调用一次：截图 + 抓到期时间，打印到日志。
    到期时间卡片（Server Expiration）只在服务器主页 SERVER_HOME_URL 上，不在
    /renew 页面上，所以默认会先跳转到主页再抓取；跳转失败则直接在当前页面尝试。

    同时算出「抓取这一刻，距离到期还剩多少分钟」（remaining_minutes）。
    这个字段比"续期前后到期时间的差值"更能反映续期是否真的成功——freemcserver
    的到期时间是重置到固定的 6 小时窗口，不是无限累加，所以续期前的剩余时间
    本来就可能已经接近 6 小时（比如快到期前又被别的机制刷新过），这种情况下
    真续期成功后 delta 也可能很小甚至看不出明显跳变。真正可靠的信号是「续期后
    这一刻剩余时间是不是被重置回接近满窗口」，跟续期前剩多少完全无关。
    """
    if goto_home_page and SERVER_HOME_URL:
        try:
            page.goto(SERVER_HOME_URL, wait_until="domcontentloaded", timeout=20000)
            time.sleep(1.5)
            try_close_overlays(page)
        except Exception as e:
            print(f"  [{label}] 跳转服务器主页失败（改为在当前页面尝试抓取）: {e}")

    expiry_text = get_expiry_snapshot_text(page)
    screenshot(page, path=screenshot_path)

    captured_at_utc = datetime.utcnow()
    expiry_dt = parse_expiry_dt(expiry_text)
    remaining_minutes = None
    if expiry_dt:
        remaining_minutes = (expiry_dt - captured_at_utc).total_seconds() / 60

    if expiry_text:
        if remaining_minutes is not None:
            print(f"[{label}] 服务器到期时间: {expiry_text}（此刻剩余约 {remaining_minutes:.0f} 分钟）")
        else:
            print(f"[{label}] 服务器到期时间: {expiry_text}（剩余时间解析失败）")
    else:
        print(f"[{label}] 未能提取到到期时间（已截图，可人工核对）。")

    return {
        "label": label,
        "expiry_text": expiry_text,
        "expiry_dt": expiry_dt,
        "captured_at_utc": captured_at_utc,
        "remaining_minutes": remaining_minutes,
        "screenshot": screenshot_path,
        "captured_at": time.time(),
    }


# ---------------------------------------------------------------------------
# 弹窗广告 / SweetAlert 实时清理
# ---------------------------------------------------------------------------
CLOSE_BUTTON_SELECTORS = [
    ".swal2-confirm",                      # SweetAlert2 确认/OK 按钮（验证码提示、续期成功提示都用它）
    # Welcome / GDPR / Cookie 同意弹窗
    "button.fc-button.fc-cta-consent",     # Funding Choices（freemcserver 用的 CMP）"Consent" 按钮
    "button.fc-button.fc-cta-do-not-consent",
    ".fc-footer-buttons button",           # Funding Choices 底部按钮（关闭/拒绝）
    "button[aria-label*='consent' i]",
    "button[aria-label*='agree' i]",
    "button[aria-label*='accept' i]",
    ".qc-cmp2-summary-buttons button",     # Quantcast CMP
    "#onetrust-accept-btn-handler",        # OneTrust
    ".cc-btn.cc-dismiss",                  # Cookie Consent
    "[id*='cookie'] button",
    "[class*='cookie-banner'] button",
    "[class*='gdpr'] button",
    "[class*='consent'] button[class*='close' i]",
    "[class*='welcome'] button[class*='close' i]",
    # 通用广告关闭
    "[aria-label='Close ad' i]",
    "[aria-label*='close' i]",
    "[title*='close' i]",
    "#dismiss-button",                     # Google 插页广告关闭按钮
    ".ytp-ad-skip-button",
    "button[class*='close' i]",
    "div[class*='close-button' i]",
    "div[id*='close-button' i]",
    "svg[class*='close' i]",
    ".abgc",                                # Google ads 关闭图标容器
    "ins.adsbygoogle-close-btn",
    "[data-dismiss='modal']",
]

CLOSE_TEXT_PATTERN = re.compile(
    r"^(close|skip ad|skip|×|✕|agree|accept|got it|i agree|consent|manage settings|ok)$", re.I
)


#  单次调用整体超时保护:
#  Google Vignette 插页广告会往页面里注入大量跨域 iframe(doubleclick.net 等)。
#  在这些 frame 上调用 frm.locator(sel).count() / is_visible(timeout=250) 等接口，
#  某些 Playwright/CDP 场景下遇到"正在导航中"的跨域 frame 会不遵守单次调用自己声明
#  的 timeout，导致单次调用被拖到远超预期。之前卡死 8 分钟大概率就是这里——每次
#  循环调用 try_close_overlays 本身没有整体时间上限，多个 frame * 多个 selector 的
#  overhead 叠加起来就会失控。这里给整个函数加一个硬性 max_duration，保证无论内部
#  单个调用多慢，这个函数本身最多跑 max_duration 秒就必须返回。
def try_close_overlays(page, verbose=False, max_duration=6.0):
    closed_any = False
    call_deadline = time.time() + max_duration

    try:
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    except Exception:
        frames = [page.main_frame]

    for frm in frames:
        if time.time() > call_deadline:
            if verbose:
                print(f"  [清理弹窗] 已达单次调用上限 {max_duration}s，提前返回，剩余 frame 留到下一轮再处理。")
            break

        # 跳过已经 detach/关闭的 frame，避免在其上调用接口时抛异常或卡住
        try:
            if frm.is_detached():
                continue
        except Exception:
            continue

        for sel in CLOSE_BUTTON_SELECTORS:
            if time.time() > call_deadline:
                break
            try:
                loc = frm.locator(sel)
                cnt = loc.count()
                if cnt == 0:
                    continue
                cnt = min(cnt, 4)
                for i in range(cnt):
                    if time.time() > call_deadline:
                        break
                    el = loc.nth(i)
                    if not el.is_visible(timeout=250):
                        continue
                    box = el.bounding_box()
                    if not box or box["width"] <= 0 or box["height"] <= 0:
                        continue
                    el.click(timeout=1200, force=True)
                    closed_any = True
                    if verbose:
                        print(f"  [清理弹窗] 点击关闭元素: {sel}")
                    time.sleep(0.25)
            except Exception:
                continue

        if time.time() > call_deadline:
            break

        try:
            txt_loc = frm.get_by_text(CLOSE_TEXT_PATTERN)
            cnt = min(txt_loc.count(), 3)
            for i in range(cnt):
                el = txt_loc.nth(i)
                if el.is_visible(timeout=250):
                    el.click(timeout=1200, force=True)
                    closed_any = True
                    if verbose:
                        print("  [清理弹窗] 按文本关闭一个弹窗")
                    time.sleep(0.25)
        except Exception:
            pass

    return closed_any


def keep_closing_ads(page, duration_s, interval=1.5):
    deadline = time.time() + duration_s
    while time.time() < deadline:
        try_close_overlays(page)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# 专门针对「Watch Ad and Renew!」点击后出现的插页广告（右上角纯文字/图标 "Close"）
# 之前观察到的问题：这个 "Close" 有时候不在 CLOSE_BUTTON_SELECTORS 能命中的
# class/aria-label 里，纯粹是一段文本；且它可能位于层层嵌套的广告 iframe
# （doubleclick / googlesyndication 的 safeframe）里，普通 Playwright click()
# 有时会因为元素被判定为"暂时不可交互"而抛异常，然后被上层 except 悄悄吞掉，
# 导致这个广告永远关不掉，脚本干等到 150s 超时。
# 这里做三件事强化命中率：
#   1) 不再限制只处理前 3 个匹配，把所有匹配到的 "Close" 都试一遍
#   2) 普通 click() 失败后，回退用 JS 原生 el.click() 再点一次
#   3) 再失败的话，用 page.mouse.click(x, y) 点它的中心坐标（真实鼠标事件，
#      对某些需要 "trusted event" 才会响应的广告 iframe 更有效，
#      和脚本里处理 Turnstile 用的是同一个思路）
#   4) verbose 日志默认开着，方便下次运行时在 Actions 日志里确认到底有没有
#      找到、点没点、点的结果如何
# ---------------------------------------------------------------------------
AD_CLOSE_TEXT_PATTERN = re.compile(r"^close$", re.I)


def force_close_ad_overlay(page, verbose=True):
    closed_any = False
    try:
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    except Exception:
        frames = [page.main_frame]

    for frm in frames:
        try:
            if frm.is_detached():
                continue
        except Exception:
            continue

        try:
            txt_loc = frm.get_by_text(AD_CLOSE_TEXT_PATTERN)
            cnt = txt_loc.count()
        except Exception:
            cnt = 0

        for i in range(cnt):
            try:
                el = txt_loc.nth(i)
                if not el.is_visible(timeout=300):
                    continue
                box = el.bounding_box()
                if not box or box["width"] <= 0 or box["height"] <= 0:
                    continue
            except Exception:
                continue

            ok = False
            try:
                el.click(timeout=1500, force=True)
                ok = True
                if verbose:
                    print(f"  [关闭广告] Playwright click 成功命中 'Close' (frame={frm.url[:60]!r})")
            except Exception as e:
                if verbose:
                    print(f"  [关闭广告] Playwright click 失败: {e}，尝试 JS click...")

            if not ok:
                try:
                    el.evaluate("(e) => e.click()")
                    ok = True
                    if verbose:
                        print("  [关闭广告] JS click() 成功。")
                except Exception as e:
                    if verbose:
                        print(f"  [关闭广告] JS click 也失败: {e}，尝试鼠标坐标点击...")

            if not ok:
                try:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    page.mouse.click(cx, cy)
                    ok = True
                    if verbose:
                        print(f"  [关闭广告] 坐标点击 ({cx:.0f}, {cy:.0f}) 完成。")
                except Exception as e:
                    if verbose:
                        print(f"  [关闭广告] 坐标点击也失败: {e}")

            if ok:
                closed_any = True
                time.sleep(0.3)

    return closed_any


# ---------------------------------------------------------------------------
# 专门处理 Google Rewarded / Vignette 插页广告的 Close 按钮
#
# 从 DevTools 截图拿到的真实 DOM 结构（在 googlesyndication.com safeframe 里）：
#   div#dismiss-button  aria-label="Close ad" role="button"
#     div.close-button-outer
#       div.close-button  (id="dismiss-button-element")
#         div.continue-prompt-text > "Close"
#
# 注意事项：
#   1. 广告有 10-15s 倒计时（count-down-container），倒计时未结束时 dismiss-button
#      存在但不响应点击（DOM 上没有 disabled，只是 JS 层面忽略事件）。
#   2. 倒计时结束后 count-down-container 变成 display:none，dismiss-button 变得可交互。
#   3. 这个 div 在 safeframe iframe 里，必须用 frm.locator() 遍历 frames 才能命中。
# ---------------------------------------------------------------------------
# 广告遮罩清除：用 JS 删除广告的「遮罩层」，不删广告 iframe 本体
#
# 背景：Google Rewarded/Vignette 插页广告嵌在 safeframe iframe 里，会先弹出一层
# 全屏遮罩（#google_vignette）挡住整个页面。之前的版本连广告 iframe 本体
# （googlesyndication / doubleclick / aswift 等）一起删掉了，这些 iframe 是
# 广告 SDK 自己的心跳检测节点，删掉后大概率被判定为开了广告拦截器，导致
# 后续被导向 we-need-your-support 拦截页，续期永远无法触发。
#
# 现在只做：
#   1. 删除 Google Interstitial / Vignette 的全屏遮罩容器本身（不碰里面的 iframe）
#   2. 解锁 body overflow（广告弹出时通常会设 overflow:hidden 防止滚动）
#   3. 清除 #google_vignette / #goog_rewarded hash，让页面 URL 恢复干净状态
# ---------------------------------------------------------------------------
#
# 重要修正（2026-07）：之前这里连广告 iframe 本体（safeframe / googlesyndication /
# doubleclick / aswift 等）一起物理删除了，这些 iframe 正是广告 SDK 自己用来做
# "心跳/存活检测"的节点——很多广告平台会定期检查这些节点是否还在 DOM 里，
# 一旦发现被删除，就会判定用户开了广告拦截器，把后续请求导向
# /we-need-your-support?r=adblock2 这个页面，导致 Extended Renewal 永远无法
# 触发续期回调。这不是点击坐标准不准的问题，是「删除广告本体」这个动作本身
# 有概率触发反广告拦截检测。
#
# 现在只删「遮罩层」本身（#google_vignette / #goog_rewarded 这两个外层容器，
# 以及通用的全屏高 z-index 遮罩 div），不碰广告 iframe 节点，把 nuke_ads
# 的作用范围收窄到"只解除页面被完全挡住/锁死滚动"这一件事上。
# ---------------------------------------------------------------------------
_NUKE_ADS_JS = """
(function() {
    var removed = 0;

    // 1. 只删 Google Interstitial / Vignette 的外层遮罩容器本身。
    //    注意：不删 iframe[src*="googlesyndication"] / iframe[id*="aswift"] 等
    //    广告 iframe 本体——这些是广告 SDK 自己的心跳检测节点，删了容易被判定
    //    为开了 adblock，导致后续进入 we-need-your-support 拦截页。
    var overlaySelectors = [
        '#google_vignette',
        '#goog_rewarded',
        '[id*="interstitial"]',
    ];
    overlaySelectors.forEach(function(sel) {
        document.querySelectorAll(sel).forEach(function(el) {
            el.remove(); removed++;
        });
    });

    // 2. 找所有 position:fixed 且 z-index > 9000 的 div（广告遮罩特征）删掉，
    //    但明确跳过任何包含广告 iframe 的容器（避免连带删掉广告本体），
    //    以及页面自己的 header/topbar。
    var allDivs = document.querySelectorAll('div');
    allDivs.forEach(function(el) {
        try {
            var style = window.getComputedStyle(el);
            var z = parseInt(style.zIndex, 10);
            if (style.position === 'fixed' && z > 9000) {
                if (el.closest('.topbar') || el.closest('#main-wrapper > header')) return;
                // 跳过内部含广告 iframe 的遮罩，只删纯遮罩（没有 iframe 子节点的）
                if (el.querySelector('iframe')) return;
                el.remove(); removed++;
            }
        } catch(e) {}
    });

    // 3. 解锁 body scroll（广告弹出时会锁 overflow）
    document.body.style.overflow = '';
    document.body.style.position = '';
    document.documentElement.style.overflow = '';

    // 4. 清除 URL hash（#google_vignette / #goog_rewarded）避免脚本误判 URL
    if (window.location.hash && (
        window.location.hash.includes('google_vignette') ||
        window.location.hash.includes('goog_rewarded')
    )) {
        history.replaceState(null, '', window.location.pathname + window.location.search);
    }

    return removed;
})()
"""


def nuke_ads(page, verbose=True):
    """
    用 JS 只删除广告的「遮罩层」容器（不删广告 iframe 本体），解除页面被
    全屏遮罩挡住/锁死滚动的问题，同时避免触发广告 SDK 的 adblock 心跳检测。
    返回删除的节点数量（>0 表示确实清掉了什么）。
    """
    try:
        removed = page.evaluate(_NUKE_ADS_JS)
        if verbose and removed:
            print(f"  [核弹清广告] JS 删除了 {removed} 个广告节点。")
        elif verbose:
            print("  [核弹清广告] 未找到广告节点（可能已消失或还未加载）。")
        return removed or 0
    except Exception as e:
        if verbose:
            print(f"  [核弹清广告] JS 执行异常: {e}")
        return 0


# ---------------------------------------------------------------------------
def close_google_rewarded_ad(page, verbose=True):
    """
    轮询尝试点击 Google Rewarded/Vignette 广告的 Close 按钮。
    只要找到 dismiss-button 就尝试点击，点击失败通常意味着倒计时还没结束，稍后重试。
    返回 True 表示成功点击。
    """
    try:
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    except Exception:
        frames = [page.main_frame]

    # 按优先级尝试的选择器（从最精确到最宽泛）
    REWARDED_SELECTORS = [
        "#dismiss-button",                      # Google Rewarded 的主关闭容器 div
        "div[aria-label='Close ad']",           # aria-label 匹配
        "div[aria-label='Close ad' i]",
        ".close-button-outer",                  # 外层按钮容器
        "#dismiss-button-element",              # 内层关闭元素
        "div.continue-prompt-text",             # 包含文字 "Close" 的 div
    ]

    for frm in frames:
        try:
            if frm.is_detached():
                continue
        except Exception:
            continue

        frm_url = ""
        try:
            frm_url = frm.url or ""
        except Exception:
            pass

        # 只在广告相关 frame 里找（safeframe 或主页面）
        is_ad_frame = (
            "safeframe" in frm_url
            or "googlesyndication" in frm_url
            or "doubleclick" in frm_url
            or frm == page.main_frame
        )
        if not is_ad_frame:
            continue

        for sel in REWARDED_SELECTORS:
            try:
                loc = frm.locator(sel)
                cnt = loc.count()
                if cnt == 0:
                    continue
            except Exception:
                continue

            for i in range(min(cnt, 3)):
                try:
                    el = loc.nth(i)
                    if not el.is_visible(timeout=300):
                        continue
                    box = el.bounding_box()
                    if not box or box["width"] <= 0 or box["height"] <= 0:
                        continue
                except Exception:
                    continue

                if verbose:
                    print(f"  [关闭插页广告] 找到元素 {sel!r}（frame={frm_url[:50]!r}），尝试点击...")

                # 方式一：Playwright force click
                try:
                    el.click(timeout=2000, force=True)
                    if verbose:
                        print(f"  [关闭插页广告] Playwright click 成功。")
                    return True
                except Exception as e:
                    if verbose:
                        print(f"  [关闭插页广告] Playwright click 失败: {e}")

                # 方式二：JS click()
                try:
                    el.evaluate("(e) => e.click()")
                    if verbose:
                        print(f"  [关闭插页广告] JS click() 成功。")
                    return True
                except Exception as e:
                    if verbose:
                        print(f"  [关闭插页广告] JS click 失败: {e}")

                # 方式三：坐标点击（trusted mouse event）
                try:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    page.mouse.click(cx, cy)
                    if verbose:
                        print(f"  [关闭插页广告] 坐标点击 ({cx:.0f}, {cy:.0f}) 完成。")
                    return True
                except Exception as e:
                    if verbose:
                        print(f"  [关闭插页广告] 坐标点击失败: {e}")

    return False


# ---------------------------------------------------------------------------
# Cloudflare Turnstile（页面内嵌小组件，非整页拦截）处理
# ---------------------------------------------------------------------------
def click_turnstile(page):
    try:
        frames = page.frames
        for i, frame in enumerate(frames):
            url = frame.url or ""
            if "challenges.cloudflare.com" in url or "turnstile" in url:
                try:
                    elem = frame.frame_element()
                    box = elem.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] * 0.12
                        cy = box["y"] + box["height"] * 0.50
                        print(f"  [Turnstile] 找到验证框 frame[{i}]，点击坐标: ({cx:.1f}, {cy:.1f})")
                        page.mouse.click(cx, cy)
                        return True
                except Exception as fe:
                    print(f"  [Turnstile] frame[{i}] 坐标计算异常: {fe}")
    except Exception as e:
        print(f"click_turnstile 异常: {e}")
    return False


def solve_embedded_turnstile(page, timeout_s=45):
    deadline = time.time() + timeout_s
    clicked = False
    while time.time() < deadline:
        try:
            val = page.eval_on_selector(
                "input[name='cf-turnstile-response']", "el => el.value"
            )
        except Exception:
            val = None
        if val:
            print(f"  [Turnstile] 验证通过，token 长度: {len(val)}")
            return True

        try_close_overlays(page)

        if not clicked:
            time.sleep(1.5)
            if click_turnstile(page):
                clicked = True
        time.sleep(1)

    print("  [Turnstile] 等待超时（可能已经通过但检测失败，继续往下走）。")
    return False


# ---------------------------------------------------------------------------
# profile 有效性检测
#
# 不能只看文件大小：Chromium 首次创建 profile 时哪怕一个 cookie 都没有，也会
# 生成带完整表结构的空 SQLite 文件（体积可达 20KB+）。改用 sqlite3 真实查询
# freemcserver 相关 cookie 记录数。
# 新版 Chromium 把 Cookies 挪到了 Default/Network/Cookies，老版本在 Default/Cookies。
# ---------------------------------------------------------------------------
def has_valid_profile():
    import shutil
    import sqlite3
    import tempfile

    candidates = [
        os.path.join(PROFILE_DIR, "Default", "Network", "Cookies"),
        os.path.join(PROFILE_DIR, "Default", "Cookies"),
    ]
    cookies_path = next((p for p in candidates if os.path.exists(p)), None)

    if not cookies_path:
        print("Profile 中未找到 Cookies 数据库文件，跳过 Cookie 登录。")
        return False

    size = os.path.getsize(cookies_path)
    print(f"Profile Cookies 文件大小: {size} 字节 ({cookies_path})")

    # Cookies 文件可能被浏览器锁，先复制到临时文件再查询
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copyfile(cookies_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%freemcserver%'"
            )
            count = cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        print(f"读取 Cookies 数据库失败（视为无效 profile）: {e}")
        count = 0
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    print(f"freemcserver 相关 cookie 记录数: {count}")
    if count <= 0:
        print("没有查到任何 freemcserver 相关 cookie，判定为无效 profile，跳过 Cookie 登录。")
        return False
    return True


# ---------------------------------------------------------------------------
# Cookie 登录（仅 profile 有实质内容时才尝试）
# ---------------------------------------------------------------------------
def try_cookie_login(page):
    print("尝试使用 persistent profile Cookie 直接访问续期页面...")
    try:
        page.goto(RENEW_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"Cookie 登录页面加载异常: {e}")

    time.sleep(3)
    try_close_overlays(page)
    screenshot(page)

    cur = page.url
    print(f"当前 URL: {cur}")

    if "/user/login" in cur:
        print("被重定向到登录页，session 已过期。")
        return False

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        body_low = body_text.strip().lower()
    except Exception as e:
        print(f"页面内容检查异常: {e}")
        return False

    # CF WAF 拦截页特征
    if "why have i been blocked" in body_low or "cloudflare ray id" in body_low:
        print("检测到 Cloudflare WAF 拦截页，当前 IP 被封，需要代理。")
        return False

    if len(body_low) < 50:
        print(f"页面内容过少（{len(body_low)} 字符），视为 Cookie 失效。")
        return False

    print(f"Cookie 登录成功！当前 URL: {cur}")
    return True


# ---------------------------------------------------------------------------
# 用户名密码登录
# ---------------------------------------------------------------------------
def login_with_username_password(page):
    print(f"[登录] 访问登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"登录页加载异常（非致命）: {e}")

    time.sleep(3)
    try_close_overlays(page)
    screenshot(page)

    if "/user/login" not in page.url:
        print(f"[登录] 访问登录页后被重定向到 {page.url}，说明已处于登录状态，跳过表单步骤。")
        return True

    # CF WAF 拦截检测：IP 被封时登录页也会返回拦截页
    try:
        body_low = page.locator("body").inner_text(timeout=3000).lower()
        if "why have i been blocked" in body_low or "cloudflare ray id" in body_low:
            print("❌ 检测到 Cloudflare WAF 拦截页！当前 IP 被封，代理未生效，终止登录。")
            send_notification("❌ <b>FreeMcServer：IP 被 Cloudflare 封锁，代理未生效！</b>", SCREENSHOT_FILE)
            return False
    except Exception:
        pass

    print("等待登录表单...")
    try:
        page.wait_for_selector("input[name='LoginFormModel[username]']", timeout=15000)
    except Exception as e:
        print(f"登录表单未出现: {e}")
        screenshot(page)
        send_notification("❌ <b>FreeMcServer 登录表单未出现</b>", SCREENSHOT_FILE)
        return False

    print("填写登录表单...")
    try:
        page.fill("input[name='LoginFormModel[username]']", USERNAME)
        page.fill("input[name='LoginFormModel[password]']", PASSWORD)
        screenshot(page)
    except Exception as e:
        print(f"填写登录表单失败: {e}")
        screenshot(page)
        send_notification("❌ <b>FreeMcServer 登录表单操作失败</b>", SCREENSHOT_FILE)
        return False

    print("等待登录页 Turnstile 验证通过...")
    solve_embedded_turnstile(page, timeout_s=45)

    try:
        submit_btn = page.locator("form#w0 button[type='submit']").first
        submit_btn.click(timeout=8000)
        print("已提交登录表单，等待跳转...")
    except Exception as e:
        print(f"点击登录按钮失败，尝试回车提交: {e}")
        try:
            page.locator("input[name='LoginFormModel[password]']").press("Enter")
        except Exception as e2:
            print(f"回车提交也失败: {e2}")
            screenshot(page)
            send_notification("❌ <b>FreeMcServer 登录按钮点击失败</b>", SCREENSHOT_FILE)
            return False

    deadline = time.time() + 30
    while time.time() < deadline:
        try_close_overlays(page)
        if "/user/login" not in page.url:
            break
        time.sleep(2)

    if "/user/login" in page.url:
        solve_embedded_turnstile(page, timeout_s=20)
        try:
            page.locator("form#w0 button[type='submit']").first.click(timeout=5000)
            time.sleep(4)
        except Exception:
            pass

    cur = page.url
    if "/user/login" in cur:
        print(f"登录失败，当前 URL: {cur}")
        screenshot(page)
        send_notification("❌ <b>FreeMcServer 登录失败！请检查用户名/密码，或截图确认验证码状态。</b>", SCREENSHOT_FILE)
        return False

    print(f"用户名密码登录成功，当前 URL: {cur}")
    return True


# ---------------------------------------------------------------------------
# 续期主流程
# ---------------------------------------------------------------------------
def find_extended_renewal_button(page):
    candidates = [
        page.locator("a[href*='renew-with-ads']").first,
        page.get_by_text(re.compile(r"choose extended renewal", re.I)).first,
        page.get_by_role("link", name=re.compile(r"extended renewal", re.I)).first,
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def find_normal_renewal_button(page):
    candidates = [
        page.locator("a[href*='renew-basic']").first,
        page.get_by_text(re.compile(r"choose normal renewal", re.I)).first,
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def go_to_renew_page(page, skip_goto=False):
    if skip_goto:
        print(f"已在续期页面，跳过 goto: {page.url}")
    else:
        print(f"访问续期页面: {RENEW_URL}")
        try:
            page.goto(RENEW_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"续期页面加载异常: {e}")
    time.sleep(1)
    # 多轮清理：Welcome/GDPR/广告弹窗可能延迟弹出
    print("清理续期页面弹窗...")
    for _ in range(3):
        closed = try_close_overlays(page, verbose=True)
        time.sleep(0.5)
        if not closed:
            break
    screenshot(page)


def do_extended_renewal(page):
    print("=== 开始「Extended Renewal」看广告续期流程 ===")
    _t0 = time.time()

    go_to_renew_page(page, skip_goto=False)

    print(f"[时间] 续期页面就绪，耗时 {time.time()-_t0:.1f}s")
    print("向下滚动寻找「Choose Extended Renewal」按钮...")
    btn = None
    for _ in range(8):
        try_close_overlays(page)
        btn = find_extended_renewal_button(page)
        if btn is not None:
            try:
                if btn.is_visible(timeout=500):
                    break
            except Exception:
                pass
        page.mouse.wheel(0, 400)
        time.sleep(0.5)

    if btn is None:
        print("未找到「Choose Extended Renewal」按钮。")
        screenshot(page)
        return False

    try:
        btn.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    # 点击前：专门清理所有弹窗（Welcome/GDPR/广告），多轮确保干净
    print("点击前清理弹窗（Welcome/GDPR/广告）...")
    for _ in range(3):
        closed = try_close_overlays(page, verbose=True)
        time.sleep(0.5)
        if not closed:
            break
    screenshot(page)

    print(f"[时间] 找到续期按钮，耗时 {time.time()-_t0:.1f}s")
    print("找到按钮，点击「Choose Extended Renewal」...")
    # Google Rewarded/Vignette 广告出现时有 ~10-15s 倒计时，必须等它结束才能点 Close。
    # 原来 8s 等待不够，改成 30s；同时重试次数从 2 改到 3。
    EXTENDED_RENEWAL_CLICK_RETRIES = 3
    VIGNETTE_WAIT_S = 30  # 每次点击后等 URL 跳转的最长时间（含等倒计时）
    clicked_ok = False
    navigated_ok = False
    for attempt in range(EXTENDED_RENEWAL_CLICK_RETRIES):
        try:
            btn = find_extended_renewal_button(page)
            if btn is None:
                print(f"  第{attempt+1}次：按钮消失了，等待重新出现...")
                time.sleep(2)
                continue
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=8000)
            clicked_ok = True
        except Exception as e:
            print(f"  第{attempt+1}次点击失败: {e}，清理弹窗后重试...")
            try_close_overlays(page, verbose=True)
            time.sleep(1)
            continue

        # 等待 URL 跳转。Google Rewarded/Vignette 广告会加 #google_vignette / #goog_rewarded
        # 锚点，不是真正跳转。广告有倒计时，等倒计时结束后点 Close 才能跳转。
        click_wait_start = time.time()
        deadline_click = click_wait_start + VIGNETTE_WAIT_S
        while time.time() < deadline_click:
            if "renew-with-ads" in page.url:
                break
            elapsed = time.time() - click_wait_start
            if "google_vignette" in page.url or "goog_rewarded" in page.url:
                print(f"  [{elapsed:.0f}s] 检测到 Google 插页广告遮罩，核弹清场...")
                nuke_ads(page)
            try_close_overlays(page)
            time.sleep(0.5)

        if "renew-with-ads" in page.url:
            print(f"  URL 已跳转: {page.url}")
            navigated_ok = True
            break
        else:
            print(f"  第{attempt+1}次：URL 未跳转（仍是 {page.url}），可能还有弹窗遮挡，再清理后重试...")
            try_close_overlays(page, verbose=True)
            time.sleep(1)

    if not clicked_ok:
        print(f"{EXTENDED_RENEWAL_CLICK_RETRIES} 次点击均失败。")
        screenshot(page)
        return False

    if not navigated_ok:
        print(f"点击「Choose Extended Renewal」{EXTENDED_RENEWAL_CLICK_RETRIES} 次后页面始终未跳转到 renew-with-ads"
              f"（当前 URL: {page.url}），大概率被 Google Vignette 广告遮罩卡住，放弃扩展续期，走回退流程。")
        screenshot(page)
        return False

    time.sleep(2)
    screenshot(page)

    print(f"[时间] 跳转到 renew-with-ads，耗时 {time.time()-_t0:.1f}s")
    print(f"当前 URL: {page.url}")

    print("清理「Captcha is required」错误弹窗...")
    keep_closing_ads(page, duration_s=3, interval=0.5)

    print("等待 Extended Renewal 页面 Turnstile 验证通过...")
    solve_embedded_turnstile(page, timeout_s=45)
    try_close_overlays(page)
    screenshot(page)

    print("等待「Watch Ad and Renew!」按钮可点击...")
    RENEW_BTN_TIMEOUT_S = 60  # 之前是约 40s (range(20)*2s) 且没有耗时日志，这里给明确超时并打日志
    watch_btn = page.locator("#renewBtn")
    clicked = False
    wait_start = time.time()
    loop_i = 0
    while time.time() - wait_start < RENEW_BTN_TIMEOUT_S:
        loop_i += 1
        elapsed = time.time() - wait_start
        if loop_i % 5 == 0:  # 每 ~10s 打印一次进度，避免长时间静默
            print(f"  [renewBtn] 仍在等待... 已过 {elapsed:.0f}s / {RENEW_BTN_TIMEOUT_S}s，当前 URL: {page.url}")
        try_close_overlays(page)
        try:
            if watch_btn.count() > 0 and watch_btn.is_visible(timeout=500):
                disabled = watch_btn.get_attribute("disabled")
                if not disabled:
                    watch_btn.scroll_into_view_if_needed(timeout=3000)
                    watch_btn.click(timeout=5000)
                    clicked = True
                    print(f"  [renewBtn] 点击成功，耗时 {elapsed:.0f}s。")
                    break
        except Exception as e:
            if loop_i % 5 == 0:
                print(f"  [renewBtn] 定位/点击异常（继续重试）: {e}")
        time.sleep(1)

    if not clicked:
        print(f"未能点击「Watch Ad and Renew!」按钮（{RENEW_BTN_TIMEOUT_S}s 超时）。")
        screenshot(page)
        return False

    print(f"[时间] Watch Ad 点击完成，耗时 {time.time()-_t0:.1f}s")
    print("已点击「Watch Ad and Renew!」，等待广告播放并持续清理弹窗...")
    time.sleep(0.5)

    success = wait_for_renew_success(page, timeout_s=60)
    print(f"[时间] Extended Renewal 流程总耗时 {time.time()-_t0:.1f}s，结果: {success}")
    screenshot(page)
    return success


def do_normal_renewal(page):
    print("=== 回退到「Normal Renewal」普通续期流程 ===")
    go_to_renew_page(page)

    btn = None
    for _ in range(10):
        try_close_overlays(page)
        btn = find_normal_renewal_button(page)
        if btn is not None:
            try:
                if btn.is_visible(timeout=500):
                    break
            except Exception:
                pass
        page.mouse.wheel(0, 400)
        time.sleep(1)

    if btn is None:
        print("未找到「Choose Normal Renewal」按钮。")
        screenshot(page)
        return False

    try:
        btn.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try_close_overlays(page)

    print("点击「Choose Normal Renewal」...")
    try:
        btn.click(timeout=8000)
    except Exception as e:
        print(f"点击失败: {e}")
        return False

    time.sleep(3)
    try_close_overlays(page)
    screenshot(page)

    solve_embedded_turnstile(page, timeout_s=30)
    try_close_overlays(page)

    submit_candidates = [
        page.locator("button[type='submit']").first,
        page.get_by_role("button", name=re.compile(r"renew", re.I)).first,
    ]
    for loc in submit_candidates:
        try:
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.click(timeout=5000)
                break
        except Exception:
            continue

    return wait_for_renew_success(page, timeout_s=60)



# ---------------------------------------------------------------------------
# 等待并点击 Google Rewarded 广告的 Close 按钮
#
# 从 DevTools 截图确认的真实机制：
#   - 广告容器在 googlesyndication safeframe iframe 里
#   - 倒计时期间：#dismiss-button-element 是 display:none，不可点
#   - 倒计时结束：#dismiss-button-element 变成 display:block（或 flex），
#     div.continue-prompt-text 里的 "Close" 文字出现
#   - 此时点击 div.continue-prompt-text（"Close" 文字那个 div）触发关闭回调
#
# 策略：
#   1. 用 JS 轮询检测 #dismiss-button-element 的 display 是否非 none
#   2. 一旦可见立刻用 frame 的 evaluate 触发 click()（不用 Playwright click，
#      避免跨域 frame 坐标映射问题）
#   3. 坐标点击作为最终兜底
# ---------------------------------------------------------------------------
_WAIT_CLOSE_JS = """
(function() {
    // 在当前 frame 内找 dismiss-button-element，检查是否可见
    var el = document.getElementById('dismiss-button-element');
    if (!el) return 'not_found';
    var style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return 'hidden';
    }
    return 'visible';
})()
"""

_CLICK_CLOSE_JS = """
(function() {
    // 优先点 continue-prompt-text（"Close" 文字本身），
    // 其次点 dismiss-button-element，最后点 dismiss-button
    var targets = [
        document.querySelector('div.continue-prompt-text'),
        document.getElementById('dismiss-button-element'),
        document.getElementById('dismiss-button'),
    ];
    for (var i = 0; i < targets.length; i++) {
        var el = targets[i];
        if (el) {
            el.click();
            return 'clicked:' + (el.id || el.className);
        }
    }
    return 'not_found';
})()
"""


def click_rewarded_close_button(page, verbose=True):
    """
    寻找并用真实鼠标点击 Google Rewarded 广告的 Close 按钮。

    从视频录屏确认：Close 是主页面右上角的纯文字（不在 safeframe 里），
    DOM 路径大致是主页面最外层某个 fixed div 里的文字节点。
    之前的 JS click 打到了 safeframe 内部的 continue-prompt-text，
    那个点击不会触发主页面的关闭事件，所以广告一直没关掉。

    策略（按优先级）：
    1. 主页面找 get_by_text("Close") 且 is_visible → 获取 bounding_box → page.mouse.click
    2. 扫描所有 frame，找 #dismiss-button-element display 非 none → 计算绝对坐标 → page.mouse.click
    3. 固定坐标兜底（视频截图里 Close 约在 1241, 455）——这是「盲点」，
       没有先确认页面上真的出现了 Close 按钮，纯粹是按经验坐标点一下。

    返回值改成三态字符串，方便调用方区分"真的点中了"还是"只是盲点了一下"：
      "real"  → 策略1或2成功定位到元素并点击（可信）
      "blind" → 策略3兜底盲点点击（不代表真的点中了任何东西）
      "none"  → 三种策略都没有触发点击动作（比如坐标点击本身也抛异常了）
    """
    # ── 策略1：主页面文字匹配 ──────────────────────────────────────
    import re as _re
    close_pattern = _re.compile(r"^Close$")
    try:
        candidates = page.get_by_text(close_pattern).all()
        for el in candidates:
            try:
                # 只处理主 frame 里的元素，避免点进广告 safeframe 内部的 "Close" 文字
                try:
                    owner_frame = el.owner_frame()
                    if owner_frame is not None and owner_frame != page.main_frame:
                        if verbose:
                            print(f"  [Rewarded Close] 跳过 iframe 内 'Close' 元素 (frame={owner_frame.url[:60]!r})")
                        continue
                except Exception:
                    pass  # owner_frame() 失败则不跳过，继续尝试
                if not el.is_visible(timeout=300):
                    continue
                box = el.bounding_box()
                if not box or box["width"] <= 0 or box["height"] <= 0:
                    continue
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                if verbose:
                    print(f"  [Rewarded Close] 主页面找到 'Close' 文字，bounding_box={box}，点击坐标 ({cx:.0f}, {cy:.0f})")
                page.mouse.click(cx, cy)
                if verbose:
                    print(f"  [Rewarded Close] 鼠标点击完成（真实命中，策略1）。")
                return "real"
            except Exception as e:
                if verbose:
                    print(f"  [Rewarded Close] 主页面 Close 候选元素点击失败: {e}")
    except Exception as e:
        if verbose:
            print(f"  [Rewarded Close] 主页面文字扫描异常: {e}")

    # ── 策略2：扫描所有 frame 的 #dismiss-button-element ─────────────
    try:
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    except Exception:
        frames = [page.main_frame]

    for frm in frames:
        try:
            if frm.is_detached():
                continue
            frm_url = frm.url or ""
        except Exception:
            continue

        try:
            status = frm.evaluate("""
(function() {
    var el = document.getElementById('dismiss-button-element');
    if (!el) return 'not_found';
    var s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return 'hidden';
    return 'visible';
})()
""")
        except Exception:
            continue

        if status != "visible":
            if verbose and status == "hidden":
                print(f"  [Rewarded Close] 倒计时未结束 (frame={frm_url[:50]!r})")
            continue

        # 找到了，算绝对坐标
        try:
            frame_el = frm.frame_element()
            frame_box = frame_el.bounding_box()
            el = frm.locator("#dismiss-button-element").first
            box = el.bounding_box()
            if frame_box and box and box["width"] > 0:
                cx = frame_box["x"] + box["x"] + box["width"] / 2
                cy = frame_box["y"] + box["y"] + box["height"] / 2
                if verbose:
                    print(f"  [Rewarded Close] frame内 dismiss-button-element box={box}，frame偏移=({frame_box['x']:.0f},{frame_box['y']:.0f})，绝对点击坐标 ({cx:.0f}, {cy:.0f})")
                page.mouse.click(cx, cy)
                if verbose:
                    print(f"  [Rewarded Close] 鼠标点击完成（真实命中，策略2）。")
                return "real"
        except Exception as e:
            if verbose:
                print(f"  [Rewarded Close] frame坐标计算失败: {e}")

    # ── 策略3：固定坐标兜底（视频截图里 Close 约在右上角 1241, 455）─
    # 注意：这一步没有先确认页面上真的有 Close 按钮出现在这个位置，
    # 纯粹是按经验坐标盲点一下，点击成功不代表真的点中了任何东西。
    if verbose:
        print(f"  [Rewarded Close] 前两种策略均未命中，尝试固定坐标兜底盲点 (1241, 455)（未确认命中，仅盲点）...")
    try:
        page.mouse.click(1241, 455)
        if verbose:
            print(f"  [Rewarded Close] 固定坐标兜底盲点完成（未确认是否真的点中）。")
        return "blind"
    except Exception as e:
        if verbose:
            print(f"  [Rewarded Close] 固定坐标兜底盲点也失败: {e}")

    return "none"


def wait_for_renew_success(page, timeout_s=150):
    start = time.time()
    deadline = start + timeout_s
    detected = False
    loop_i = 0
    while time.time() < deadline:
        loop_i += 1
        elapsed = time.time() - start
        if loop_i % 10 == 0:  # 每 ~10s 打印一次进度，避免长时间静默看不出是否卡住
            print(f"  [等待续期结果] 仍在等待... 已过 {elapsed:.0f}s / {timeout_s}s，当前 URL: {page.url}")
        try:
            body_text = page.locator("body").inner_text(timeout=2000)
        except Exception:
            body_text = ""
        low = body_text.lower()
        if "server was renewed" in low or ("success" in low and "renew" in low):
            detected = True
            print(f"[续期] 检测到「Success / Your server was renewed」提示，耗时 {elapsed:.0f}s。")
            screenshot(page)
            break
        # 注意：这里绝对不能调 nuke_ads 删除广告 iframe 本体！广告 DOM 删掉后
        # 播放回调永远不触发（会被判定成 adblock）。只做两件事：
        #   1. try_close_overlays：处理 SweetAlert 续期成功弹窗的 OK 按钮
        #   2. click_rewarded_close_button：检测广告倒计时是否结束，结束后点 Close
        try_close_overlays(page)
        verbose_this = (loop_i % 5 == 0)
        click_result = click_rewarded_close_button(page, verbose=verbose_this)
        # 只有策略1/2真的定位到元素并点击成功（"real"）才算「已点击」，
        # 策略3的兜底盲点（"blind"）单独标注，避免误以为每次都是真的点中了广告。
        if click_result == "real":
            print(f"  [等待续期结果] ✅ 已真实命中并点击广告 Close 按钮（第 {elapsed:.0f}s）。")
        elif click_result == "blind" and verbose_this:
            print(f"  [等待续期结果] 🎯 兜底盲点了一次固定坐标（第 {elapsed:.0f}s，未确认是否真的点中）。")
        time.sleep(1)

    if not detected:
        print(f"[续期] 等待 {timeout_s}s 后仍未检测到成功提示。")

    return detected


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run():
    global LOGIN_SUCCESS, RENEW_SUCCESS

    if not SERVER_ID or not USERNAME or not PASSWORD:
        print("错误: 缺少必要环境变量 (FMC_SERVER_ID / FMC_USERNAME / FMC_PASSWORD)")
        return

    from cloakbrowser import launch_persistent_context

    proxy_arg = PROXY if PROXY else None
    if proxy_arg:
        print(f"使用代理: {proxy_arg}")
    else:
        print("未配置 PROXY，使用直连。")

    print(f"Fingerprint seed: {FINGERPRINT_SEED}")
    print(f"Profile 目录: {PROFILE_DIR}")

    os.makedirs(PROFILE_DIR, exist_ok=True)

    recording_proc = None

    print("[1/5] 正在启动 CloakBrowser...", flush=True)
    t0 = time.time()
    context = launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        proxy=proxy_arg,
        geoip=True,
        humanize=True,
        viewport={"width": XVFB_WIDTH, "height": XVFB_HEIGHT},
        args=[f"--fingerprint={FINGERPRINT_SEED}"],
    )
    print(f"[2/5] CloakBrowser 启动完成（耗时 {time.time()-t0:.1f}s）。", flush=True)

    try:
        print("[3/5] 创建新标签页...", flush=True)
        page = context.new_page()
        print("[4/5] 新标签页已创建。", flush=True)

        if ENABLE_RECORDING:
            recording_proc = start_recording()

        print("[5/5] 初始化完成，进入登录流程。", flush=True)

        if has_valid_profile():
            logged_in = try_cookie_login(page)
        else:
            print("Profile 无有效 session，直接走用户名密码登录。")
            logged_in = False

        if not logged_in:
            logged_in = login_with_username_password(page)
            if not logged_in:
                return

        LOGIN_SUCCESS = True

        t_login_done = time.time()
        print(f"[时间] 登录完成，耗时 {t_login_done - t0:.1f}s")

        # 续期前快照：抓到期时间文本 + 截图，用于和续期后对比
        before_snapshot = capture_renew_snapshot(page, "续期前", BEFORE_SCREENSHOT_FILE)

        # 不 fallback 到 Normal Renewal，失败就重新回到续期页面重试 Extended Renewal
        renew_t0 = time.time()
        success = False
        total_attempts = 1 + EXTENDED_RENEWAL_RETRIES
        for attempt in range(1, total_attempts + 1):
            if attempt > 1:
                print(f"=== Extended Renewal 第 {attempt}/{total_attempts} 次尝试（前一次未确认成功，重新回到续期页面重试）===")
            success = do_extended_renewal(page)
            if success:
                break
            if attempt < total_attempts:
                time.sleep(2)
        print(f"[时间] Extended Renewal 全部尝试耗时 {time.time()-renew_t0:.1f}s，共尝试 {attempt}/{total_attempts} 次，最终结果: {success}")

        RENEW_SUCCESS = success

        # 续期后快照：只有确认续期成功才有意义去对比，跳转回服务器主页拿刷新后的到期时间
        after_snapshot = None
        if success:
            after_snapshot = capture_renew_snapshot(page, "续期后", AFTER_SCREENSHOT_FILE, goto_home_page=True)

        # 组装续期前后对比信息
        compare_lines = []
        duration_s = time.time() - renew_t0
        compare_lines.append(f"⏱ 本次续期耗时: {duration_s:.0f}s（尝试 {attempt}/{total_attempts} 次）")

        before_dt = before_snapshot.get("expiry_dt") if before_snapshot else None
        after_dt = after_snapshot.get("expiry_dt") if after_snapshot else None
        before_remaining = before_snapshot.get("remaining_minutes") if before_snapshot else None
        after_remaining = after_snapshot.get("remaining_minutes") if after_snapshot else None

        if before_snapshot and before_snapshot.get("expiry_text"):
            rem_str = f"（剩余约 {before_remaining:.0f} 分钟）" if before_remaining is not None else ""
            compare_lines.append(f"续期前到期时间: {before_snapshot['expiry_text']}{rem_str}")
        if after_snapshot and after_snapshot.get("expiry_text"):
            rem_str = f"（剩余约 {after_remaining:.0f} 分钟）" if after_remaining is not None else ""
            compare_lines.append(f"续期后到期时间: {after_snapshot['expiry_text']}{rem_str}")

        # server_expires_at 是数据库里的固定绝对时间戳，不会随真实时间流逝自己变化——
        # 没有续期成功，它就是原地不动的同一个值；只有后台真的处理了续期请求，
        # 才会把它改写成"续期那一刻 + 6小时"的新值。所以最直接可靠的判断就是：
        # 续期后的 expires_at 是否比续期前大（哪怕只大一点点），而不是看差值幅度——
        # 续期前可能本来就接近满窗口，这种情况下差值本来就小，用幅度阈值反而会
        # 把真续期误判成失败，或者把失败误判成成功。
        if before_dt and after_dt:
            delta_seconds = (after_dt - before_dt).total_seconds()
            if delta_seconds > 0:
                compare_lines.append(
                    f"✅ 到期时间从 {before_dt} 变成了 {after_dt}（+{delta_seconds/60:.1f} 分钟），"
                    f"expires_at 确实被后台改写了，续期真实生效。"
                )
            elif delta_seconds == 0:
                compare_lines.append(
                    f"⚠️ 续期前后 expires_at 完全没变（都是 {before_dt}），说明后台并没有真的处理续期请求，"
                    f"页面上的「Success」提示可能是误判，请人工核对截图/录屏。"
                )
            else:
                compare_lines.append(f"⚠️ 到期时间反而变早了（{before_dt} → {after_dt}），请人工核对截图确认发生了什么。")
        elif not (before_snapshot and before_snapshot.get("expiry_text")) and not (after_snapshot and after_snapshot.get("expiry_text")):
            compare_lines.append("（未能提取到到期时间，请查看续期前/后截图人工核对）")

        compare_text = "\n".join(compare_lines)
        print("[续期前后对比]\n" + compare_text)

        if success:
            msg = f"✅ <b>FreeMcServer 续期成功！</b>\n服务器过期时间已重置。\n{compare_text}"
        else:
            msg = f"⚠️ <b>FreeMcServer 续期流程已跑完，但未能确认续期成功</b>\n请查看截图/录屏确认实际状态。\n{compare_text}"
        print(msg)
        send_notification(msg, SCREENSHOT_FILE)
        # 续期前后截图各发一次，方便直接肉眼对比。WxPusher 消息接口不支持图片
        # （见 _send_wxpusher 里的说明），所以这两张对比图只通过 Telegram 发送，
        # 避免给 WxPusher 发无意义的纯文字重复消息。
        if before_snapshot:
            _send_telegram(f"📸 续期前截图（{before_snapshot['label']}）", BEFORE_SCREENSHOT_FILE)
        if after_snapshot:
            _send_telegram(f"📸 续期后截图（{after_snapshot['label']}）", AFTER_SCREENSHOT_FILE)

    except Exception as e:
        print(f"主流程异常: {e}")
        screenshot(page if "page" in dir() else None)
        send_notification(f"❌ <b>FreeMcServer 脚本异常</b>\n{e}", SCREENSHOT_FILE)

    finally:
        try:
            context.close()
        except Exception:
            pass
        if recording_proc:
            stop_recording(recording_proc)

        if not LOGIN_SUCCESS:
            flag_path = "state/freemcserver_login_failed"
            os.makedirs("state", exist_ok=True)
            with open(flag_path, "w") as f:
                f.write("login_failed")
            print(f"登录失败，已写入标志文件: {flag_path}，本次 profile 不保存。")


if __name__ == "__main__":
    run()
