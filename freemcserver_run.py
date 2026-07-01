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

如果扩展续期（看广告）流程失败（比如广告不可用/按钮找不到），会自动回退尝试
"Choose Normal Renewal"（普通续期，无需看广告）。

Profile 持久化: GitHub Actions Cache（不写入 git 历史，公开仓库安全）
代理: Xray SOCKS5 本地代理，透传给 CloakBrowser
通知: WxPusher + Telegram 双通道，任一未配置则自动跳过，互不影响
"""

import os
import re
import signal
import subprocess
import time

import requests

# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------
BASE_URL   = os.getenv("FMC_BASE_URL", "https://panel.freemcserver.net").rstrip("/")
SERVER_ID  = os.getenv("FMC_SERVER_ID", "").strip()
RENEW_URL       = f"{BASE_URL}/server/{SERVER_ID}/renew" if SERVER_ID else None
RENEW_ADS_URL   = f"{BASE_URL}/server/{SERVER_ID}/renew-with-ads" if SERVER_ID else None
RENEW_BASIC_URL = f"{BASE_URL}/server/{SERVER_ID}/renew-basic" if SERVER_ID else None

USERNAME  = os.getenv("FMC_USERNAME")
PASSWORD  = os.getenv("FMC_PASSWORD")
LOGIN_URL = os.getenv("FMC_LOGIN_URL", f"{BASE_URL}/user/login")

# persistent_context profile 目录（由 workflow 的 actions/cache 在运行间持久化）
PROFILE_DIR = os.getenv("FMC_PROFILE_DIR", "state/freemcserver_profile")

# SOCKS5 代理，空字符串 = 直连
PROXY = os.getenv("PROXY", "socks5://127.0.0.1:10808").strip()

# 是否在扩展续期失败时回退到普通续期
FALLBACK_TO_NORMAL = os.getenv("FMC_FALLBACK_NORMAL", "true").strip().lower() == "true"

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
# 弹窗广告 / SweetAlert 实时清理
# ---------------------------------------------------------------------------
CLOSE_BUTTON_SELECTORS = [
    ".swal2-confirm",                      # SweetAlert2 确认/OK 按钮（验证码提示、续期成功提示都用它）
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

CLOSE_TEXT_PATTERN = re.compile(r"^(close|skip ad|skip|×|✕)$", re.I)


def try_close_overlays(page, verbose=False):
    closed_any = False

    try:
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    except Exception:
        frames = [page.main_frame]

    for frm in frames:
        for sel in CLOSE_BUTTON_SELECTORS:
            try:
                loc = frm.locator(sel)
                cnt = loc.count()
                if cnt == 0:
                    continue
                cnt = min(cnt, 4)
                for i in range(cnt):
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
            time.sleep(3)
            if click_turnstile(page):
                clicked = True
        time.sleep(2)

    print("  [Turnstile] 等待超时（可能已经通过但检测失败，继续往下走）。")
    return False


# ---------------------------------------------------------------------------
# profile 有效性检测
# ---------------------------------------------------------------------------
def has_valid_profile():
    cookies_path = os.path.join(PROFILE_DIR, "Default", "Cookies")
    if not os.path.exists(cookies_path):
        print(f"Profile Cookies 文件不存在: {cookies_path}，跳过 Cookie 登录。")
        return False
    size = os.path.getsize(cookies_path)
    print(f"Profile Cookies 文件大小: {size} 字节")
    if size <= 8192:
        print("Cookies 文件过小（可能只有空库或 CF 临时 cookie），跳过 Cookie 登录。")
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
        if len(body_text.strip()) < 50:
            print(f"页面内容过少（{len(body_text.strip())} 字符），视为 Cookie 失效。")
            return False
    except Exception as e:
        print(f"页面内容检查异常: {e}")
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


def go_to_renew_page(page):
    print(f"访问续期页面: {RENEW_URL}")
    try:
        page.goto(RENEW_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"续期页面加载异常: {e}")
    time.sleep(3)
    try_close_overlays(page, verbose=True)
    screenshot(page)


def do_extended_renewal(page):
    print("=== 开始「Extended Renewal」看广告续期流程 ===")

    go_to_renew_page(page)

    print("向下滚动寻找「Choose Extended Renewal」按钮...")
    btn = None
    for _ in range(15):
        try_close_overlays(page)
        btn = find_extended_renewal_button(page)
        if btn is not None:
            try:
                if btn.is_visible(timeout=500):
                    break
            except Exception:
                pass
        page.mouse.wheel(0, 400)
        time.sleep(1)

    if btn is None:
        print("未找到「Choose Extended Renewal」按钮。")
        screenshot(page)
        return False

    try:
        btn.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try_close_overlays(page)
    time.sleep(0.5)

    print("找到按钮，点击「Choose Extended Renewal」...")
    try:
        btn.click(timeout=8000)
    except Exception as e:
        print(f"点击「Choose Extended Renewal」失败: {e}")
        try_close_overlays(page)
        try:
            btn.click(timeout=8000, force=True)
        except Exception as e2:
            print(f"强制点击仍然失败: {e2}")
            screenshot(page)
            return False

    time.sleep(3)
    screenshot(page)

    print(f"当前 URL: {page.url}")

    print("清理「Captcha is required」错误弹窗...")
    keep_closing_ads(page, duration_s=6, interval=1)

    print("等待 Extended Renewal 页面 Turnstile 验证通过...")
    solve_embedded_turnstile(page, timeout_s=45)
    try_close_overlays(page)
    screenshot(page)

    print("等待「Watch Ad and Renew!」按钮可点击...")
    watch_btn = page.locator("#renewBtn")
    clicked = False
    for _ in range(20):
        try_close_overlays(page)
        try:
            if watch_btn.count() > 0 and watch_btn.is_visible(timeout=500):
                disabled = watch_btn.get_attribute("disabled")
                if not disabled:
                    watch_btn.scroll_into_view_if_needed(timeout=3000)
                    watch_btn.click(timeout=5000)
                    clicked = True
                    break
        except Exception:
            pass
        time.sleep(2)

    if not clicked:
        print("未能点击「Watch Ad and Renew!」按钮。")
        screenshot(page)
        return False

    print("已点击「Watch Ad and Renew!」，等待广告播放并持续清理弹窗...")
    time.sleep(2)

    success = wait_for_renew_success(page, timeout_s=150)
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


def wait_for_renew_success(page, timeout_s=150):
    deadline = time.time() + timeout_s
    detected = False
    while time.time() < deadline:
        try:
            body_text = page.locator("body").inner_text(timeout=2000)
        except Exception:
            body_text = ""
        low = body_text.lower()
        if "server was renewed" in low or ("success" in low and "renew" in low):
            detected = True
            print("[续期] 检测到「Success / Your server was renewed」提示。")
            screenshot(page)
            break
        try_close_overlays(page)
        time.sleep(2)

    keep_closing_ads(page, duration_s=4, interval=1)
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

    context = launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        proxy=proxy_arg,
        geoip=True,
        humanize=True,
        viewport={"width": XVFB_WIDTH, "height": XVFB_HEIGHT},
        args=[f"--fingerprint={FINGERPRINT_SEED}"],
    )

    try:
        page = context.new_page()

        if ENABLE_RECORDING:
            recording_proc = start_recording()

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

        success = do_extended_renewal(page)

        if not success and FALLBACK_TO_NORMAL:
            print("扩展续期未确认成功，尝试回退到普通续期...")
            success = do_normal_renewal(page)

        RENEW_SUCCESS = success

        if success:
            msg = "✅ <b>FreeMcServer 续期成功！</b>\n服务器过期时间已重置。"
        else:
            msg = "⚠️ <b>FreeMcServer 续期流程已跑完，但未能确认续期成功</b>\n请查看截图/录屏确认实际状态。"
        print(msg)
        send_notification(msg, SCREENSHOT_FILE)

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
