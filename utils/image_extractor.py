"""
图片提取模块 —— 从 PDF 里取出插图，生成缩略图，并与文本卡片关联
================================================================
本模块【不依赖 Streamlit】，可以脱离网页单独运行、单独测试。

主要接口：
    extract_images(pdf_bytes) -> dict       提取全部插图（含缩略图），返回 {images, skipped, elapsed}
    associate_with_cards(images, blocks)    给每张图找同页距离最近的文本卡片
    render_full_image(pdf_bytes, xref, ...) 按需渲染单张图的完整尺寸（点击放大时才调用）

设计取舍（重要）：
    缩略图在解析阶段一次性生成好（每张约几十 KB）；
    【完整尺寸的大图不预先生成】，只有用户点「放大」时才渲染那一张，
    否则一篇带 20 张图的论文会把上百 MB 的图片全塞进内存。
"""

import hashlib
import math
import time
from dataclasses import dataclass

import pymupdf

# ---- 过滤门槛 ----
MIN_PIXELS = 80         # 像素宽或高小于这个值的，视为图标、装饰线，跳过
MIN_DISPLAY = 30.0      # 页面上显示尺寸小于 30pt 的，同样跳过
THUMB_WIDTH = 420       # 缩略图最大宽度（像素）
FULL_MAX_WIDTH = 2200   # 放大图的像素上限，防止超大图撑爆内存
ASSOC_MAX_GAP = 140.0   # 与最近文本块的距离超过这个值（pt），认为「关联不可靠」


@dataclass
class ImageItem:
    """一张插图（按「页面 + 位置」记一条，同一张图在多页出现会记成多条）"""
    key: str            # 唯一标识，形如 "p3-x3045-0"，界面拿它当按钮 key
    page: int           # 所在页码
    xref: int           # PDF 内部的对象编号（渲染大图时要用）
    smask: int          # 透明掩码对象的编号，0 表示没有
    bbox: tuple         # 在页面上的位置 (x0, y0, x1, y1)，单位 pt
    pixel_w: int        # 原始像素宽
    pixel_h: int        # 原始像素高
    disp_w: float       # 在页面上显示的宽度（pt）
    disp_h: float       # 在页面上显示的高度（pt）
    thumb: bytes        # 缩略图 PNG 字节
    thumb_w: int
    thumb_h: int
    digest: str         # 缩略图指纹，用于同页去重
    card_order: int = 0     # 关联到的卡片序号；0 表示没关联上
    gap: float = -1.0       # 与那张卡片的距离（pt）


def _apply_alpha(doc, xref: int, smask: int):
    """
    取出图片的 Pixmap，并把透明掩码（smask）叠加成 alpha 通道。

    为什么要处理 smask：科研图里的示意图常常带透明背景，
    只取图片本体、丢掉掩码的话，透明区域会变成黑色或乱码。
    另外 CMYK 等非 RGB 色彩空间也要转一下，否则显示颜色会不对。
    """
    pix = pymupdf.Pixmap(doc, xref)

    if smask:
        try:
            pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, smask))
        except Exception:
            pass        # 掩码坏了不影响主图，继续用原图

    colorspace = getattr(pix, "colorspace", None)
    if colorspace is None or colorspace.n > 4:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

    return pix


def _shrink_to_width(pixmap, target_width: int):
    """
    把位图缩小到目标宽度附近。

    ⚠️ 关键坑：PyMuPDF 的 Pixmap.shrink(factor) 不是「除以 factor」，
    而是「连续减半 factor 次」，即尺寸除以 2 的 factor 次方；
    而且它是【原地修改】，返回值是 None（写 pix = pix.shrink(...) 会得到 None）。
    所以这里的 factor 要用 log2 求，并且只能取整数——最终宽度会落在
    (target, 2×target] 区间内，对缩略图来说完全够用。
    """
    if target_width and pixmap.width > target_width:
        factor = int(math.log2(pixmap.width / target_width))
        if factor >= 1:
            pixmap.shrink(factor)
    return pixmap


def render_full_image(pdf_bytes: bytes, xref: int, smask: int = 0,
                      max_width: int = FULL_MAX_WIDTH) -> bytes:
    """
    渲染一张图的完整尺寸 PNG（用于点击放大）。

    超过 max_width 会等比缩小——这不是「变形」，而是避免超大图占满内存；
    等比缩放不会让图片失真。返回 PNG 字节。
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        pix = _apply_alpha(doc, xref, smask)
        _shrink_to_width(pix, max_width)
        return pix.tobytes("png")
    finally:
        doc.close()


def extract_images(pdf_bytes: bytes, min_pixels: int = MIN_PIXELS,
                   min_display: float = MIN_DISPLAY,
                   thumb_width: int = THUMB_WIDTH) -> dict:
    """
    提取全部插图并生成缩略图。返回：
        images   成功提取的图片列表（ImageItem）
        skipped  被跳过的图片及原因（小图标、找不到位置、解码失败…）
        elapsed  耗时（秒）
    """
    start = time.time()
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    images, skipped = [], []

    try:
        for page_index, page in enumerate(doc):
            page_no = page_index + 1
            seen_digests = set()        # 同一页里同一张图重复摆放的，只保留一次

            for info in page.get_images(full=True):
                xref, smask, pix_w, pix_h = info[0], info[1], info[2], info[3]

                if pix_w < min_pixels or pix_h < min_pixels:
                    skipped.append({"页码": page_no, "xref": xref,
                                    "尺寸": f"{pix_w}×{pix_h}", "原因": "尺寸过小（图标/装饰线）"})
                    continue

                # 图片在页面上可能出现多次（比如水印），get_image_rects 会全部给出
                rects = page.get_image_rects(xref)
                if not rects:
                    skipped.append({"页码": page_no, "xref": xref,
                                    "尺寸": f"{pix_w}×{pix_h}", "原因": "找不到显示位置"})
                    continue

                try:
                    pixmap = _apply_alpha(doc, xref, smask)
                    full_w, full_h = pixmap.width, pixmap.height
                    _shrink_to_width(pixmap, thumb_width)
                    thumb_png = pixmap.tobytes("png")
                    thumb_w, thumb_h = pixmap.width, pixmap.height
                    del pixmap          # 及时释放全尺寸位图
                except Exception as exc:
                    skipped.append({"页码": page_no, "xref": xref,
                                    "尺寸": f"{pix_w}×{pix_h}",
                                    "原因": f"解码失败：{type(exc).__name__}"})
                    continue

                digest = hashlib.md5(thumb_png).hexdigest()

                for rect_index, rect in enumerate(rects):
                    if rect.width < min_display or rect.height < min_display:
                        skipped.append({"页码": page_no, "xref": xref,
                                        "尺寸": f"{rect.width:.0f}×{rect.height:.0f}pt",
                                        "原因": "页面显示尺寸过小"})
                        continue
                    if digest in seen_digests:
                        continue            # 同一页同一张图重复摆放

                    seen_digests.add(digest)
                    images.append(ImageItem(
                        key=f"p{page_no}-x{xref}-{rect_index}",
                        page=page_no,
                        xref=xref,
                        smask=smask,
                        bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                        pixel_w=full_w,
                        pixel_h=full_h,
                        disp_w=rect.width,
                        disp_h=rect.height,
                        thumb=thumb_png,
                        thumb_w=thumb_w,
                        thumb_h=thumb_h,
                        digest=digest,
                    ))

        return {"images": images, "skipped": skipped, "elapsed": round(time.time() - start, 2)}
    finally:
        doc.close()


def _rect_gap(a: tuple, b: tuple) -> float:
    """
    两个矩形之间的距离（pt）。重叠时为 0，否则取最近的边到边的欧氏距离。
    """
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def render_region_image(pdf_bytes: bytes, page_no: int, rect, dpi: int = 200) -> bytes:
    """
    把页面上的一个矩形区域渲染成 PNG。

    用途：**公式的呈现**。二维公式（分式、积分、矩阵）在 PDF 里只是
    「带坐标的文字」（实测没有 MathML/LaTeX 源码可提取），压成一行必然失真，
    所以直接把原区域截成图片插进卡片——视觉 100% 保真。
    代价是公式图不可选中、不可翻译；线性文本仍保留在「显示全部内容」和译文里。

    dpi 取 200：公式通常只有几十 pt 宽，显示时按原始像素尺寸呈现，
    200dpi 能保证小字号公式也清晰可读。
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[page_no - 1]
        clip = pymupdf.Rect(*rect)
        # 稍微外扩，避免把公式的上下标或分数线边缘切掉
        clip = pymupdf.Rect(clip.x0 - 2, clip.y0 - 3, clip.x1 + 2, clip.y1 + 3)
        return page.get_pixmap(clip=clip, dpi=dpi).tobytes("png")
    finally:
        doc.close()


def associate_with_cards(images, blocks) -> dict:
    """
    给每张图片找「同页、位置最近」的文本卡片，就地写回 card_order / gap。

    典型情况：图片正下方就是 "Figure 3: ..." 图注，或上方是正文里提到它的段落，
    所以「最近距离」是个简单又有效的关联方式。

    返回统计：{关联可靠, 关联存疑, 未关联, 存疑列表}
    """
    blocks_by_page = {}
    for b in blocks:
        blocks_by_page.setdefault(b.page, []).append(b)

    confident, doubtful, orphan = 0, [], []

    for img in images:
        candidates = blocks_by_page.get(img.page, [])
        if not candidates:
            img.card_order, img.gap = 0, -1.0
            orphan.append(img)
            continue

        best, best_gap = None, None
        for b in candidates:
            gap = _rect_gap(img.bbox, (b.x0, b.y0, b.x1, b.y1))
            if best_gap is None or gap < best_gap:
                best, best_gap = b, gap

        img.card_order = best.order
        img.gap = round(best_gap, 1)
        if best_gap <= ASSOC_MAX_GAP:
            confident += 1
        else:
            doubtful.append(img)

    return {
        "关联可靠": confident,
        "关联存疑": len(doubtful),
        "未关联": len(orphan),
        "存疑列表": doubtful,
        "未关联列表": orphan,
    }
