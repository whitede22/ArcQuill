"""从原始素材生成 ArcArcQuill 的品牌资产。

输入两份素材，产三样东西：

1. `assets/arcquill.png`   应用图标的透明底版本（白底抠掉）
2. `assets/arcquill.ico`   多尺寸图标（16 / 24 / 32 / 48 / 64 / 128 / 256）
3. `assets/arcquill_wordmark_{light,dark}.png`  标题栏左上角的横版字标

为什么自己写 ICO 而不找现成库：环境里没有 Pillow，为一次性的图标转换引入一个
依赖不划算。ICO 的容器格式很简单，且 Vista 之后允许目录项直接指向 PNG 数据，
Windows 10/11 原生支持，PyInstaller 也认。

用法（素材路径可按需替换）：

    .venv/Scripts/python.exe scripts/make_brand_assets.py \
        --icon "D:/path/to/icon.png" --wordmark "D:/path/to/logo_pack"

脚本可重复运行，产物是确定的：同一份素材每次跑出来的 ico 字节一致。
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# 图标尺寸。Windows 会用其中最合适的一档：16 用于列表视图，32/48 用于桌面与
# 任务栏，256 用于大图标视图与文件属性。缺 24 会让某些缩放比例下的任务栏图标
# 由 32 硬缩，边缘发虚。
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

# 字标高度。界面里按 24 逻辑像素显示，素材给 48 像素，正好覆盖 200% 缩放。
WORDMARK_H = 48

# 浅色主题下的字标颜色。取 `theme.py` 里 ink 主题的正文色，字形才不会与旁边的
# 输入框文字显得两套。
INK = (0x1F, 0x1F, 0x1E)


def load(path: Path) -> QImage:
    image = QImage(str(path))
    if image.isNull():
        raise SystemExit(f"读不出这张图：{path}")
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def drop_white_background(src: QImage, threshold: int = 246) -> QImage:
    """把白底抠成透明，保留图形的抗锯齿边缘。

    判据分两种像素：

    - 彩色像素（星星那点蓝）：直接全不透明。只用亮度做 alpha 的话，这颗高亮度的
      蓝星会被判成半透明，缩到 16 像素时几乎消失。
    - 其余像素：按「离白底多远」给 alpha，白底是 0，纯黑是 255。这样羽毛的灰阶
      渐变变成半透明，视觉上与原图压在白底上的观感一致，边缘也不会留一圈白边。

    `threshold` 用来吃掉白底的不均匀：压过再缩放的图，底色常在 250 上下浮动，
    按 255 判定会在边上留下几乎看不见、缩放后却明显的浅灰。
    """

    out = QImage(src.size(), QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    for y in range(src.height()):
        for x in range(src.width()):
            pixel = src.pixelColor(x, y)
            r, g, b = pixel.red(), pixel.green(), pixel.blue()
            if max(r, g, b) >= threshold and max(r, g, b) - min(r, g, b) <= 12:
                continue  # 白底，保持透明
            luma = (r * 299 + g * 587 + b * 114) // 1000
            if b - r > 25 and b > 120:
                alpha = 255  # 蓝星
            else:
                alpha = max(0, min(255, int((255 - luma) * 255 / max(1, threshold))))
                alpha = min(255, int(alpha * 1.15))  # 略微加深，抵消缩放后的通透感
            colour = pixel
            colour.setAlpha(alpha)
            out.setPixelColor(x, y, colour)
    return out


def wordmark(src: QImage, colour: tuple[int, int, int] | None) -> QImage:
    """把白色字标染成指定颜色，保留蓝色星星。

    `colour` 为 None 时原样输出（素材本身就是白色）。染色的做法是保留 alpha 通道
    当形状、只换 RGB，抗锯齿边缘因此不会变硬——若按亮度反推 alpha，细笔画在
    小尺寸下会掉成虚线。
    """

    height = WORDMARK_H
    width = max(1, round(src.width() * height / src.height()))
    scaled = src.scaled(
        width,
        height,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    if colour is None:
        return scaled
    out = QImage(scaled.size(), QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    for y in range(scaled.height()):
        for x in range(scaled.width()):
            pixel = scaled.pixelColor(x, y)
            if pixel.alpha() == 0:
                continue
            r, g, b = pixel.red(), pixel.green(), pixel.blue()
            if b - r > 25 and b > 120:
                out.setPixelColor(x, y, pixel)  # 星星保留原色
                continue
            tinted = pixel
            tinted.setRed(colour[0])
            tinted.setGreen(colour[1])
            tinted.setBlue(colour[2])
            out.setPixelColor(x, y, tinted)
    return out


def png_bytes(image: QImage) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise SystemExit("PNG 编码失败")
    buffer.close()
    return bytes(data)


def write_ico(target: Path, base: QImage) -> None:
    """按 ICO 容器格式写出多尺寸图标，每档直接存 PNG 数据。"""

    frames: list[tuple[int, bytes]] = []
    for size in ICON_SIZES:
        frame = base.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        frames.append((size, png_bytes(frame)))

    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries, blobs = b"", b""
    for size, blob in frames:
        # 宽高各占一字节，256 要写成 0 才符合规范。
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(blob),
            offset,
        )
        blobs += blob
        offset += len(blob)
    target.write_bytes(header + entries + blobs)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 ArcArcQuill 品牌资产")
    parser.add_argument("--icon", required=True, help="方形图标的原始 png（白底）")
    parser.add_argument("--wordmark", required=True, help="横版字标所在目录")
    parser.add_argument("--keep-background", action="store_true", help="保留白底")
    args = parser.parse_args()

    QGuiApplication(sys.argv)  # QImage 的缩放要走 Qt，必须有实例

    ASSETS.mkdir(exist_ok=True)
    source = load(Path(args.icon))
    square = source if args.keep_background else drop_white_background(source)

    # 统一母版尺寸：原始图是 1254 见方，直接缩到 256 会有一点点糊；先规整到
    # 512 再逐档下采，小尺寸的边缘更干净。
    master = square.scaled(
        512, 512, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    master.save(str(ASSETS / "arcquill.png"), "PNG")
    write_ico(ASSETS / "arcquill.ico", master)

    pack = Path(args.wordmark)
    white = load(pack / "arcquill_full_master.png")
    wordmark(white, None).save(str(ASSETS / "arcquill_wordmark_dark.png"), "PNG")
    wordmark(white, INK).save(str(ASSETS / "arcquill_wordmark_light.png"), "PNG")

    for name in ("arcquill.png", "arcquill.ico", "arcquill_wordmark_light.png", "arcquill_wordmark_dark.png"):
        path = ASSETS / name
        print(f"{name:32s} {path.stat().st_size:>8d} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
