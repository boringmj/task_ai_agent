from __future__ import annotations

from .registry import tool

import os

import queue
import threading
import time
from datetime import datetime

# ---- desktop 工具专属配置(环境变量名不变,仍可在 .env 覆盖)----
# 剪贴板读进上下文的字符上限(读太长会白白占窗口)
DESKTOP_MAX_CLIPBOARD_CHARS = int(os.environ.get("MAX_CLIPBOARD_CHARS", "10000"))


# ---------------- 模拟输入(键盘/鼠标) ----------------
# 这些工具直接作用于宿主机的真实桌面(非容器),全权限、针对当前焦点窗口。
# 走 pyautogui;FAILSAFE 默认开启 —— 鼠标移到屏幕任意一角可立即中止(四个角都算)。
# 注意:这一步是"直接的手",操作宿主 GUI,影响的窗口取决于焦点。


def _pyautogui():
    """按需导入 pyautogui;设置安全的节奏与逃生开关。"""
    try:
        import pyautogui
    except ImportError:
        raise RuntimeError("模拟输入需要 pyautogui,请先执行:pip install pyautogui") from None
    pyautogui.PAUSE = 0.05  # 每一步间隔,避免动作过快
    pyautogui.FAILSAFE = True  # 鼠标甩到屏幕任意一角 = 紧急中止
    return pyautogui


# ---- 点击标记(顶层红点)----
# 在 agent 点击处显示一个顶层、点击穿透的红色十字标记:用户可见 AI 点了哪,
# AI 点完也能截屏核实有没有点歪。用纯 Win32(ctypes)自绘窗口跑在独立线程 ——
# 早先用 Tk,但 Tk 必须在主线程才能渲染,放后台线程不出画面,故改用 Win32。
# 标记通过线程安全的 queue 通信;click/move 更新位置,clear_marker 隐藏。

_marker_thread = None
_marker_queue: "queue.Queue | None" = None
_marker_hwnd = None            # 供测试/调试读取
_window_proc_ref = None        # 保持 Win32 回调存活,防止被 GC

_WM_TIMER = 0x0113
_WM_DESTROY = 0x0002
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_LWA_ALPHA = 0x00000002
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4


def _win32_setup(user32, gdi32, ctypes, wintypes) -> None:
    """给用到的 Win32 函数设 64 位原型 —— 不设会按 32 位签名传参,句柄/参数被截断。"""
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, ctypes.c_byte, wintypes.DWORD]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_void_p
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = wintypes.LPARAM
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = wintypes.LPARAM
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH


def _marker_worker(marker_queue) -> None:
    """专属线程:纯 Win32 顶层红块窗口 + 消息循环。

    之前用 Tk 在后台线程不渲染(Tk 必须跑主线程)。Win32 窗口允许在任何带消息循环
    的线程里运行,所以改用 ctypes 自绘:顶层、半透明红块、点击穿透、不抢焦点、
    不进任务栏。通过线程安全的 queue 接收 move/clear/quit。
    """
    global _marker_hwnd, _window_proc_ref
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    _win32_setup(user32, gdi32, ctypes, wintypes)

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    state = {"hide_deadline": None}

    def _hide(hwnd):
        user32.ShowWindow(hwnd, _SW_HIDE)
        state["hide_deadline"] = None

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == _WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        if msg == _WM_TIMER:
            # 延时隐藏:到点且没被新的动作取消,就隐藏
            if state["hide_deadline"] is not None and datetime.now().timestamp() >= state["hide_deadline"]:
                _hide(hwnd)
                return 0
            try:
                cmd = marker_queue.get_nowait()
            except queue.Empty:
                return 0
            if cmd[0] == "move":
                _, x, y = cmd
                # SWP_NOACTIVATE|SWP_SHOWWINDOW:移动并显示,不抢焦点;HWND_TOPMOST 显式重申置顶
                user32.SetWindowPos(hwnd, wintypes.HWND(-1),
                                    int(x) - 17, int(y) - 17, 34, 34, 0x0010 | 0x0040)
                user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
                state["hide_deadline"] = None  # 新动作取消延时隐藏
            elif cmd[0] == "clear":
                delay_ms = cmd[1]
                if delay_ms > 0:
                    state["hide_deadline"] = datetime.now().timestamp() + delay_ms / 1000.0
                else:
                    _hide(hwnd)
            elif cmd[0] == "quit":
                user32.DestroyWindow(hwnd)
                return 0
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc = WNDPROC(wnd_proc)
    _window_proc_ref = wndproc  # 保活,防 GC

    hinst = kernel32.GetModuleHandleW(None)
    red_brush = gdi32.CreateSolidBrush(0x0000FF)  # COLORREF 红
    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.hbrBackground = red_brush
    wc.lpszClassName = "AgentClickMarkerWin"
    user32.RegisterClassW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(
        # WS_EX_TOPMOST 在创建时就带上:窗口天生置顶,不依赖后面 SetWindowPos 的 hwndInsertAfter
        _WS_EX_TOPMOST | _WS_EX_LAYERED | _WS_EX_TRANSPARENT
        | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
        "AgentClickMarkerWin", "", 0x80000000, 0, 0, 34, 34, None, None, hinst, None)
    _marker_hwnd = hwnd
    user32.SetLayeredWindowAttributes(hwnd, 0, 200, _LWA_ALPHA)  # 半透明
    user32.ShowWindow(hwnd, _SW_HIDE)  # 初始隐藏
    user32.SetTimer(hwnd, 1, 40, None)  # 40ms 轮询队列

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    user32.DestroyWindow(hwnd)


def _ensure_marker():
    global _marker_thread, _marker_queue
    if _marker_thread is None or not _marker_thread.is_alive():
        _marker_queue = queue.Queue()
        _marker_thread = threading.Thread(target=_marker_worker, args=(_marker_queue,), daemon=True)
        _marker_thread.start()
    return _marker_queue


def _show_marker(x, y) -> None:
    """更新标记位置并置顶显示。失败不抛(不影响真实点击)。"""
    try:
        _ensure_marker().put(("move", int(x), int(y)))
    except Exception:  # noqa: BLE001
        pass


def _clear_marker(delay_seconds: float = 0.0) -> None:
    """隐藏标记;delay_seconds>0 表示延时消失。失败不抛。"""
    try:
        _ensure_marker().put(("clear", int(delay_seconds * 1000)))
    except Exception:  # noqa: BLE001
        pass


@tool(
    description="隐藏点击时显示的那个红色标记。给 delay_seconds 则延时后消失,"
                "例如 10 表示最后一次点击的红点 10 秒后再隐藏。"
                "确认 UI 交互结束时调用;点完需要截屏核实时,别急着清,先 screen 再看。",
    parameters={
                "type": "object",
                "properties": {
                    "delay_seconds": {
                        "type": "number",
                        "description": "延时多少秒后隐藏;0 表示立刻隐藏",
                    }
                },
            },
)
def clear_marker(delay_seconds: float = 0.0) -> str:
    """隐藏点击红点。给 delay_seconds 则延时后消失,如 10 表示最后一次点击的标记 10 秒后消失。"""
    if delay_seconds < 0:
        raise ValueError("delay_seconds 不能为负")
    _clear_marker(delay_seconds)
    if delay_seconds > 0:
        return f"将在 {delay_seconds} 秒后隐藏点击标记。"
    return "已隐藏点击标记。"


def _send_unicode_text(text: str) -> None:
    """用 SendInput 逐字符**直接键入**文本(KEYEVENTF_UNICODE),完全不经过剪贴板。

    支持任意 Unicode(中文、emoji —— emoji 走 UTF-16 代理对);换行/制表符用 VK_RETURN/VK_TAB。
    少数老旧或自绘控件(部分游戏/Qt)可能不认这种输入,那时可改走 type_text(via_clipboard=True)。
    """
    import ctypes
    from ctypes import wintypes

    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1
    VK_RETURN, VK_TAB = 0x0D, 0x09
    ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    def ev(scan, flags, vk=0):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.u.ki.wVk = vk
        inp.u.ki.wScan = scan
        inp.u.ki.dwFlags = flags
        inp.u.ki.time = 0
        inp.u.ki.dwExtraInfo = 0
        return inp

    events = []
    for ch in text:
        if ch == "\n":
            events.append(ev(0, 0, VK_RETURN))
            events.append(ev(0, KEYEVENTF_KEYUP, VK_RETURN))
        elif ch == "\t":
            events.append(ev(0, 0, VK_TAB))
            events.append(ev(0, KEYEVENTF_KEYUP, VK_TAB))
        elif ch == "\r":
            continue
        else:
            code = ord(ch)
            codes = [code] if code <= 0xFFFF else None
            if codes is None:  # 非 BMP 字符(如 emoji):拆成 UTF-16 代理对
                c = code - 0x10000
                codes = [0xD800 + (c >> 10), 0xDC00 + (c & 0x3FF)]
            for c in codes:
                events.append(ev(c, KEYEVENTF_UNICODE))
                events.append(ev(c, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    if not events:
        raise ValueError("没有可输入的字符")

    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    BATCH = 128  # 每批 128 个事件(约 64 字符),避免一次塞过大的数组
    for i in range(0, len(events), BATCH):
        chunk = events[i:i + BATCH]
        arr = (INPUT * len(chunk))(*chunk)
        sent = user32.SendInput(len(chunk), arr, ctypes.sizeof(INPUT))
        if sent != len(chunk):
            raise OSError(f"SendInput 只发出 {sent}/{len(chunk)} 个事件(可能被前台窗口或 UIPI 拦截)")
        time.sleep(0.005)  # 略等一下,让目标应用跟得上


@tool(
    description="读取系统剪贴板里的文本内容(只读,不修改剪贴板)。"
                "当用户说「我刚复制了…」「用剪贴板里的内容」时用它把内容拿到手,"
                "之后可直接用这部分文本(如用 type_text 键入),不必去覆盖剪贴板。"
                "只能读文本;若剪贴板是图片/文件则读不到。",
    parameters={"type": "object", "properties": {}},
)
def read_clipboard() -> str:
    """读取系统剪贴板里的**文本**内容(读进来进上下文,方便你说的"拿到原文再直接键入")。

    只读文本;剪贴板里若是图片/文件(非文本),这里取不到。内容过长会截断,避免撑爆上下文。
    """
    import pyperclip
    try:
        text = pyperclip.paste()
    except Exception as exc:  # noqa: BLE001
        return f"错误:读取剪贴板失败:{exc}"
    if not text:
        return "剪贴板里没有文本(可能是空的,或只含图片/文件等非文本内容)。"
    if len(text) > DESKTOP_MAX_CLIPBOARD_CHARS:
        return (f"剪贴板文本(共 {len(text)} 字符,已截断到前 {DESKTOP_MAX_CLIPBOARD_CHARS}):\n"
                + text[:DESKTOP_MAX_CLIPBOARD_CHARS])
    return f"剪贴板文本({len(text)} 字符):\n{text}"


@tool(
    description="在当前有焦点的窗口输入一段文本(支持中文等 unicode)。直接作用于宿主机的真实屏幕。"
                "默认用 SendInput 逐字符直接键入,**不碰剪贴板** —— 用户自己复制的内容不会被覆盖。"
                "只有碰到不认直接键入的老旧/自绘控件时才用 via_clipboard=true 退回粘贴(那样会覆盖剪贴板)。"
                "注意:作用对象取决于当前焦点窗口,输入前确认焦点是对的。",
    parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文本,可含中文"},
                    "via_clipboard": {
                        "type": "boolean",
                        "description": "true 时改用粘贴(兼容性更好但会覆盖剪贴板),默认 false",
                    },
                },
                "required": ["text"],
            },
)
def type_text(text: str, via_clipboard: bool = False) -> str:
    """在当前焦点窗口输入文本(支持中文等 Unicode)。

    默认用 SendInput **逐字符直接键入,不碰剪贴板** —— 所以你剪贴板里复制的东西不会被覆盖。
    via_clipboard=true 才退回"复制 + ctrl+v"的老办法(兼容性最好,但**会覆盖剪贴板**);
    只有遇到不认直接键入的老旧/自绘控件时才用它。
    """
    if not text.strip():
        raise ValueError("要输入的文本不能为空或纯空白")
    if via_clipboard:
        pg = _pyautogui()
        import pyperclip
        pyperclip.copy(text)
        pg.hotkey("ctrl", "v")
        return f"已通过粘贴输入 {len(text)} 个字符(注意:剪贴板已被覆盖)。"
    _send_unicode_text(text)
    return f"已直接键入 {len(text)} 个字符(未使用剪贴板)。"


@tool(
    description="按一个键或组合键,如 enter、tab、ctrl+s、alt+tab、ctrl+shift+esc。"
                "适合模拟快捷键确认、切换窗口、关闭弹窗等。作用于当前焦点窗口。",
    parameters={
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": "按键名或组合,组合用 + 连接,如 'ctrl+s'、'alt+tab'",
                    }
                },
                "required": ["keys"],
            },
)
def press_keys(keys: str) -> str:
    """按一个键或组合键,如 'enter'、'ctrl+s'、'alt+tab'。"""
    pg = _pyautogui()
    keys = keys.strip()
    if not keys:
        raise ValueError("按键不能为空")
    if "+" in keys:
        pg.hotkey(*keys.split("+"))
    else:
        pg.press(keys)
    return f"已按下 {keys}。"


@tool(
    description="在屏幕坐标 (x, y) 处点击。坐标用屏幕原始分辨率。"
                "想定位坐标时先 screen 截屏:真实坐标 = 图上的坐标 × (原宽/压缩后宽)。"
                "作用于真实屏幕,点击前确认坐标准确。",
    parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标(像素,原始分辨率)"},
                    "y": {"type": "integer", "description": "纵坐标(像素,原始分辨率)"},
                    "button": {
                        "type": "string",
                        "description": "鼠标键 left/right/middle,默认 left",
                    },
                },
                "required": ["x", "y"],
            },
)
def click(x: int, y: int, button: str = "left") -> str:
    """在屏幕坐标 (x, y) 处点击。坐标用屏幕原始分辨率,可与 screen 的结果换算。"""
    pg = _pyautogui()
    if button not in ("left", "right", "middle"):
        raise ValueError(f"button 只支持 left/right/middle,收到 {button}")
    _show_marker(x, y)  # 先显示红点(顶层可穿透),再点击,用户/AI 都能看到落点
    pg.click(int(x), int(y), button=button)
    return f"已在 ({x}, {y}) 用 {button} 键点击,红点标记在终点。"


@tool(
    description="把光标移动到屏幕坐标 (x, y)。坐标用屏幕原始分辨率。配合 screen 定位后再点击。",
    parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标(像素,原始分辨率)"},
                    "y": {"type": "integer", "description": "纵坐标(像素,原始分辨率)"},
                },
                "required": ["x", "y"],
            },
)
def move_mouse(x: int, y: int) -> str:
    """把光标移到屏幕坐标 (x, y),用屏幕原始分辨率。"""
    pg = _pyautogui()
    _show_marker(x, y)
    pg.moveTo(int(x), int(y))
    return f"已移动光标到 ({x}, {y})。"


@tool(
    description="按住鼠标从一个坐标拖到另一个坐标 —— 用于拖窗口、拖文件、框选、拖滑块/进度条等。"
                "坐标用屏幕原始分辨率。duration 是拖动耗时(秒),拖拽排序/画布类界面需要平滑移动,给 0.2~0.5 更稳。"
                "红点先标在起点,松开后停在终点。**拖动会真实改变桌面状态(可能移动文件或窗口),执行前务必确认起点和落点。**",
    parameters={
                "type": "object",
                "properties": {
                    "from_x": {"type": "integer", "description": "起点横坐标"},
                    "from_y": {"type": "integer", "description": "起点纵坐标"},
                    "to_x": {"type": "integer", "description": "终点横坐标"},
                    "to_y": {"type": "integer", "description": "终点纵坐标"},
                    "duration": {"type": "number", "description": "拖动耗时秒数,默认 0.3;0 为瞬间到位"},
                    "button": {"type": "string", "description": "鼠标键:left(默认)/right/middle"},
                },
                "required": ["from_x", "from_y", "to_x", "to_y"],
            },
)
def drag(from_x: int, from_y: int, to_x: int, to_y: int,
         duration: float = 0.3, button: str = "left") -> str:
    """按住鼠标从 (from_x, from_y) 拖到 (to_x, to_y) —— 拖窗口/拖文件/框选/拖滑块。

    坐标用屏幕原始分辨率。duration 是拖动耗时(秒),给 0 是瞬间到位;
    某些界面(拖拽排序、画布)需要平滑移动才认,给个 0.2~0.5 更稳。
    红点先标在起点,松开后移到终点。**拖动会真实改变桌面状态(可能移动文件/窗口),执行前务必确认起点与落点。**
    """
    if button not in ("left", "right", "middle"):
        raise ValueError(f"button 只支持 left/right/middle,收到 {button}")
    pg = _pyautogui()
    duration = max(0.0, min(float(duration), 5.0))
    _show_marker(from_x, from_y)           # 起点先亮,让用户/AI 看到从哪开始
    pg.moveTo(int(from_x), int(from_y))
    pg.mouseDown(button=button)
    try:
        pg.moveTo(int(to_x), int(to_y), duration=duration)
    finally:
        pg.mouseUp(button=button)          # 无论中途是否出错(如 failsafe),都要松开,别卡住按键
    _show_marker(to_x, to_y)               # 松手后标记停在终点
    return (f"已从 ({from_x}, {from_y}) 拖到 ({to_x}, {to_y})"
            f"({button} 键,{duration}s),红点标记在终点。")


# Windows 滚轮单位:一个"格"= WHEEL_DELTA = 120。pyautogui 在 Windows 上把 clicks
# 原样当 dwData 传给 mouse_event(不乘 120),所以必须自己换算,否则 scroll(1) 只有 1/120 格。
_WHEEL_DELTA = 120
_MOUSEEVENTF_HWHEEL = 0x01000  # 水平滚轮;pyautogui 的 hscroll 在 Windows 上发的是垂直事件,故自己发


@tool(
    description="滚动鼠标滚轮。clicks 是**格数**:正数向上/向左,负数向下/向右。"
                "1 格 ≈ 滚动 3 行文本(Windows 默认),所以**别给 1、2 这种小值 —— 几乎看不出动静**;"
                "滚一屏通常要 10~30 格,想直接翻到顶/底可以给 ±50 或更大。"
                "给了 x/y 就先把光标移到那里再滚 —— 滚哪个区域通常由光标位置决定;不给则在当前光标处滚。"
                "horizontal=true 走水平滚动(横向表格/看板)。",
    parameters={
                "type": "object",
                "properties": {
                    "clicks": {
                        "type": "integer",
                        "description": "滚动格数:正=向上/左,负=向下/右。滚一屏通常 10~30,别给 1、2 这种小值",
                    },
                    "x": {"type": "integer", "description": "可选:滚动位置横坐标(不填则在当前光标处滚)"},
                    "y": {"type": "integer", "description": "可选:滚动位置纵坐标"},
                    "horizontal": {"type": "boolean", "description": "是否水平滚动,默认 false"},
                },
                "required": ["clicks"],
            },
)
def scroll(clicks: int, x: int | None = None, y: int | None = None,
           horizontal: bool = False) -> str:
    """滚动鼠标滚轮。clicks 是**格数**:正数向上/向左,负数向下/向右;1 格 ≈ 滚动 3 行(Windows 默认)。

    给了 x/y 就先把光标移到那里的再滚(滚哪个区域往往由光标位置决定);
    不给就在光标当前位置滚。horizontal=true 走水平滚轮。
    """
    if int(clicks) == 0:
        raise ValueError("滚动量 clicks 不能为 0(正数向上/左,负数向下/右)")
    pg = _pyautogui()
    if x is not None and y is not None:
        pg.moveTo(int(x), int(y))
    pos = pg.position()
    _show_marker(pos.x, pos.y)              # 标记滚动发生的位置
    delta = int(clicks) * _WHEEL_DELTA      # 换算成 Windows 的滚轮位移量
    if horizontal:
        import ctypes
        u = ctypes.windll.user32
        u.mouse_event.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        u.mouse_event(_MOUSEEVENTF_HWHEEL, 0, 0, delta & 0xFFFFFFFF, 0)
    else:
        pg.scroll(delta)
    axis = "水平" if horizontal else "垂直"
    direction = "正向(上/左)" if clicks > 0 else "反向(下/右)"
    return (f"已在 ({pos.x}, {pos.y}) {axis}滚动 {abs(int(clicks))} 格"
            f"(约 {abs(int(clicks)) * 3} 行文本),方向:{direction}。")
