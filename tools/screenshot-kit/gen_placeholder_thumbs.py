#!/usr/bin/env python3
"""README/위키용 스크린샷을 실제 CC 없이 찍기 위한 플레이스홀더 썸네일(PNG) 생성.
외부 라이브러리 없이(zlib+struct만) 단색 배경 + 중앙 도형 형태로 만든다.

사용법:
    python3 gen_placeholder_thumbs.py [출력폴더]   # 기본: ./mock-data/thumbs
"""
import struct
import sys
import zlib
import os


def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_png(path, w, h, bg, fg, shape="circle"):
    raw = bytearray()
    cx, cy = w / 2, h / 2
    r = min(w, h) * 0.30
    for y in range(h):
        raw.append(0)  # filter: none
        for x in range(w):
            dx, dy = x - cx, y - cy
            inside = False
            if shape == "circle":
                inside = (dx * dx + dy * dy) ** 0.5 < r
            elif shape == "square":
                inside = abs(dx) < r and abs(dy) < r
            elif shape == "triangle":
                ny = (y - (cy - r)) / (2 * r) if r else 0
                half_w = r * max(0.0, min(1.0, ny))
                inside = abs(dx) < half_w and (cy - r) <= y <= (cy + r)
            elif shape == "diamond":
                inside = (abs(dx) / r + abs(dy) / r) < 1.0 if r else False
            col = fg if inside else bg
            raw += bytes(col)
    compressed = zlib.compress(bytes(raw), 9)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # RGB, 8bit
    png = sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


# 이름, 배경색, 도형색, 도형 — gen_mock_manifest.py의 THUMB_NAMES와 짝이 맞아야 함
PALETTE = [
    ("h001", (255, 214, 224), (255, 138, 168), "circle"),   # 헤어 - 핑크
    ("h002", (214, 234, 255), (110, 161, 234), "circle"),   # 헤어 - 블루
    ("t001", (255, 236, 210), (240, 170, 90), "square"),    # 상의
    ("t002", (223, 245, 226), (100, 190, 130), "square"),
    ("b001", (232, 223, 255), (150, 120, 230), "square"),   # 하의
    ("s001", (255, 224, 224), (230, 110, 110), "diamond"),  # 스킨
    ("a001", (255, 245, 200), (230, 190, 60), "triangle"),  # 액세서리
    ("a002", (220, 245, 250), (70, 190, 210), "triangle"),
    ("sh001", (240, 230, 220), (150, 110, 80), "square"),   # 신발
    ("m001", (235, 235, 240), (140, 140, 160), "circle"),   # 기타
]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "mock-data", "thumbs")
    os.makedirs(out, exist_ok=True)
    for name, bg, fg, shape in PALETTE:
        make_png(os.path.join(out, f"{name}.png"), 208, 296, bg, fg, shape)
    print(f"{len(PALETTE)}개 썸네일 생성 -> {out}")


if __name__ == "__main__":
    main()
