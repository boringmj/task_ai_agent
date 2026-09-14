from __future__ import annotations

from .registry import tool

import os

import base64

from .. import ctx
from ..core import safe_path


# ---- media 工具专属配置(环境变量名不变,仍可在 .env 覆盖)----
# ---- 图像(多模态)相关 ----
MEDIA_IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif"}
MEDIA_IMG_MAX_BYTES = int(os.environ.get("IMG_MAX_BYTES", str(3 * 1024 * 1024)))  # 单张图片的字节上限,超出拒绝(避免 base64 后撑爆上下文)
# 截屏:最长边会被压缩到这个像素数。视觉模型对大图会内部缩小,原样送 4K 截图
# 只会浪费图像 token 甚至被拒,所以发送前自己压成小数。
MEDIA_SCREEN_MAX_DIM = int(os.environ.get("SCREEN_MAX_DIM", "1280"))


def _img_magic_ok(ext: str, data: bytes) -> bool:
    """按扩展名校验文件头(魔数),确认真的是那种格式的图片。

    只信扩展名不够 —— 一个 .jpg 的文本文件也能通过,交给视觉模型会在 API 层
    报错,不如在这里就拦掉。零依赖,自己读文件头。
    """
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".gif":
        return data.startswith(b"GIF8")
    if ext == ".webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


@tool(
    agents=("main", "sub"),
    description="把工作区里的一张图片加载进来,让视觉模型真正看到它的内容。"
                "当用户提到本地图片、或者需要你查看/分析一张图片(截图、图、图表等)时使用。"
                "支持 jpg/png/webp/gif,单张不超过 3MB。图片会在下一轮以图像形式交给你。"
                "加载前先确认图片在工作区内(可以用 list_files 找)。",
    parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要加载的图片路径(相对工作区),例如 screenshot.png",
                    }
                },
                "required": ["path"],
            },
)
def img(path: str) -> str:
    """把工作区里的图片转成 data URL,登记到待注入队列,供下一轮模型以 image_url 查看。

    工具返回值只能存文本,塞不进 image_url;所以这里不返回 base64 字符串,
    只登记数据,由 run() 在下次调用模型前把它作为 content 里的 image_url 段注入。
    """
    target = safe_path(path, "read")
    if not target.is_file():
        raise FileNotFoundError(f"{target} 不存在或不是文件")
    ext = target.suffix.lower()
    if ext not in MEDIA_IMG_MIME:
        raise ValueError(f"不支持的图片格式 {ext},支持:{'、'.join(sorted(MEDIA_IMG_MIME))}")
    size = target.stat().st_size
    if size > MEDIA_IMG_MAX_BYTES:
        raise ValueError(f"图片过大({size} 字节),上限 {MEDIA_IMG_MAX_BYTES} 字节")

    data = target.read_bytes()
    if not _img_magic_ok(ext, data):
        raise ValueError(f"{target.name} 的文件头与 {ext} 格式不符,可能不是有效的 {ext} 图片。")

    b64 = base64.b64encode(data).decode("ascii")
    ctx.current().pending_images.append(f"data:{MEDIA_IMG_MIME[ext]};base64,{b64}")
    return f"图片 {target.name} 已加载({size} 字节),将在下一轮作为图像信息交给模型。"


def _virtual_screen_origin() -> tuple[int, int]:
    """虚拟桌面(所有显示器拼起来的那块大画布)的原点在屏幕坐标系里的位置。

    多屏时它常常不是 (0,0):副屏若在主屏左边/上边,原点就是负的。截的是整块
    虚拟桌面,所以把图上坐标换算成点击坐标时必须加上这个偏移。
    """
    try:
        import ctypes
        u = ctypes.windll.user32
        return u.GetSystemMetrics(76), u.GetSystemMetrics(77)  # SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN
    except Exception:  # noqa: BLE001
        return 0, 0


def _image_to_data_url(image) -> str:
    """把 PIL Image 压缩到最长边不超过 MEDIA_SCREEN_MAX_DIM,再转成 PNG data URL。

    这是回答"大图"问题的核心:屏幕截图通常远超视力模型能接受的分辨率,
    先在本地压小再发,而不是原样送一个 4K 图给模型内部缩小。
    """
    from PIL import Image  # 延迟导入,只在真用到时加载

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    if max(image.size) > MEDIA_SCREEN_MAX_DIM:
        scale = MEDIA_SCREEN_MAX_DIM / max(image.size)
        image = image.resize(
            (max(1, int(image.size[0] * scale)), max(1, int(image.size[1] * scale))),
            Image.LANCZOS,
        )
    from io import BytesIO
    buf = BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


@tool(
    description="截取用户的整个屏幕,压缩后作为图像交给视觉模型。"
                "只在用户明确要求查看屏幕、或任务确实依赖当前屏幕内容时才调用 —— 这是敏感操作,不要自作主张。"
                "截图会自动压缩到最长边 1280 像素,不会拿原图超大的分辨率去撑模型。",
    parameters={"type": "object", "properties": {}},
)
def screen() -> str:
    """截取整个屏幕,压缩后作为图像交给视觉模型。

    截取的是用户自己的屏幕,属于敏感操作 —— 务必只在用户明确要求查看屏幕、
    或任务确实依赖当前屏幕内容时才调用。
    """
    try:
        from PIL import ImageGrab  # Windows 原生截屏;延迟导入,非截屏场景不背依赖
    except ImportError as exc:
        raise RuntimeError("截屏需要 Pillow,请先执行:pip install Pillow") from exc

    # all_screens=True:抓**整个虚拟桌面**(所有显示器)。默认只抓主屏,
    # 多显示器时副屏上的窗口 agent 既看不到、也算不准点击坐标。
    image = ImageGrab.grab(all_screens=True)
    orig_w, orig_h = image.size
    ox, oy = _virtual_screen_origin()
    url = _image_to_data_url(image)
    ctx.current().pending_images.append(url)
    cw = max(1, int(orig_w * MEDIA_SCREEN_MAX_DIM / max(orig_w, orig_h)))
    ch = max(1, int(orig_h * MEDIA_SCREEN_MAX_DIM / max(orig_w, orig_h)))
    # 给出原分辨率、压缩尺寸和虚拟桌面原点,方便模型算真实点击坐标。
    return (
        f"已截取整个虚拟桌面:原始 {orig_w}×{orig_h},交给模型的压缩图为 {cw}×{ch};"
        f"虚拟桌面原点在屏幕坐标 ({ox}, {oy})。"
        f"click/move 用原始分辨率的屏幕坐标,换算:"
        f"屏幕x = {ox} + 图x × {orig_w}/{cw},屏幕y = {oy} + 图y × {orig_h}/{ch}。"
    )


def inject_pending_images(messages: list[dict]) -> None:
    """把本轮登记的图片作为 image_url 内容段注入对话,让视觉模型能真正看到。

    图像只能出现在消息的 content 列表里(tool 结果只能是字符串),所以单独
    追加一条带图像内容的消息,而不是塞进工具返回值。
    """
    pending = ctx.current().pending_images
    if not pending:
        return
    messages.append(
        {
            "role": "user",
            "content": (
                [{"type": "text", "text": "(以下为 img 工具加载的图片,请据此处理当前任务。)"}]
                + [{"type": "image_url", "image_url": {"url": u}} for u in pending]
            ),
        }
    )
    pending.clear()
