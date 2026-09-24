"""Draw the application icons: canexpert/resources/canexpert, dummy_ecu and test_expert (.png, .ico).

    python tools/make_icons.py        (needs PyQt5 to draw and Pillow to write the .ico files)

The picture is a CAN bus at work: CAN-H and CAN-L drawn apart for dominant bits and together for recessive
ones, on a rounded square - blue for CAN Expert, amber for the Dummy ECU. Small sizes get fewer bits and
thicker lines, so the icon still reads at 16 pixels.
"""
import io
import sys
from pathlib import Path

from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen

RESOURCES = Path(__file__).resolve().parents[1] / "canexpert" / "resources"
SIZES = (16, 24, 32, 48, 64, 128, 256)
SCHEMES = {
    "canexpert": ("#2563eb", "#1e3a8a", "#ffffff", "#93c5fd"),     # top, bottom, CAN-H, CAN-L
    "dummy_ecu": ("#f59e0b", "#b45309", "#ffffff", "#fde68a"),
    "test_expert": ("#16a34a", "#14532d", "#ffffff", "#bbf7d0"),
}


def draw(size: int, scheme) -> QImage:
    top, bottom, high, low = scheme
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(size / 256, size / 256)
    gradient = QLinearGradient(0, 0, 0, 256)
    gradient.setColorAt(0, QColor(top))
    gradient.setColorAt(1, QColor(bottom))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(QRectF(8, 8, 240, 240), 52, 52)
    bits = (0, 1, 0, 1) if size <= 32 else (0, 1, 0, 1, 1, 0)
    width = max(12.0, 2.0 * 256 / size)
    left, right = 40.0, 216.0
    step = (right - left) / len(bits)
    for colour, dominant, recessive in ((high, 72.0, 116.0), (low, 184.0, 140.0)):
        points, x = [], left
        for bit in bits:
            y = dominant if bit else recessive
            points += [QPointF(x, y), QPointF(x + step, y)]
            x += step
        pen = QPen(QColor(colour), width, Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin)
        painter.setPen(pen)
        painter.drawPolyline(*points)
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def main() -> int:
    from PIL import Image
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    RESOURCES.mkdir(parents=True, exist_ok=True)
    for name, scheme in SCHEMES.items():
        images = {size: Image.open(io.BytesIO(png_bytes(draw(size, scheme)))) for size in SIZES}
        images[256].save(RESOURCES / f"{name}.png")
        images[256].save(RESOURCES / f"{name}.ico", sizes=[(size, size) for size in SIZES],
                         append_images=[images[size] for size in SIZES if size != 256])
        print(f"{name}: {RESOURCES / (name + '.ico')}")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
