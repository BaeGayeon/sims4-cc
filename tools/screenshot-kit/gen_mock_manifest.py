#!/usr/bin/env python3
"""README/위키용 스크린샷을 위한 가짜 라이브러리 manifest.json 생성.
실제 창작자 이름이나 실제 CC 이미지는 전혀 쓰지 않고, 전부 지어낸 샘플 데이터.
읽기 실패/중복 의심/CAS 충돌 배지도 시연되도록 몇 개 예시를 심어둔다.

사용법:
    python3 gen_placeholder_thumbs.py            # 썸네일부터 만들고
    python3 gen_mock_manifest.py [출력폴더]        # 기본: ./mock-data
"""
import json
import os
import sys
import datetime

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "mock-data")

THUMB_NAMES = {
    "헤어": ["h001.png", "h002.png"],
    "상의": ["t001.png"],
    "하의": ["b001.png"],
    "전신": ["t002.png"],
    "스킨": ["s001.png"],
    "액세서리": ["a001.png"],
    "귀걸이": ["a002.png"],
    "신발": ["sh001.png"],
    "문신": ["m001.png"],
    "슬라이더/프리셋": ["m001.png"],
}
ICONS = {
    "헤어": "💇", "상의": "👕", "하의": "👖", "전신": "👗", "스킨": "🧑",
    "액세서리": "🎀", "귀걸이": "💎", "신발": "👟", "문신": "🎨", "슬라이더/프리셋": "🎚️",
}

# (폴더/창작자명, [(파일명, 카테고리, 용량bytes, 날짜), ...])
FOLDERS = [
    ("CC FeaturedCreators/MoonlitStudio", [
        ("[Moonlit] Wavy Long Hair.package", "헤어", 14_200_000, "2026-07-28"),
        ("[Moonlit] Soft Bangs Hair.package", "헤어", 9_800_000, "2026-07-20"),
        ("[Moonlit] Y2K Crop Top.package", "상의", 3_100_000, "2026-06-15"),
    ]),
    ("CC FeaturedCreators/PixelThreads", [
        ("PixelThreads_DenimJacket.package", "상의", 5_400_000, "2026-05-02"),
        ("PixelThreads_WideLegJeans.package", "하의", 4_700_000, "2026-05-02"),
        ("PixelThreads_SummerDress.package", "전신", 6_900_000, "2026-04-11"),
        ("PixelThreads_ChunkySneakers.package", "신발", 2_300_000, "2026-03-30"),
    ]),
    ("CC FeaturedCreators/GlowUp Atelier", [
        ("[GlowUp] Natural Skin N02.package", "스킨", 11_500_000, "2026-02-18"),
        ("[GlowUp] Dewy Skin N03.package", "스킨", 12_100_000, "2026-02-18"),
        ("[GlowUp] Gold Hoop Earrings.package", "귀걸이", 820_000, "2026-01-25"),
        ("[GlowUp] Pearl Drop Earrings.package", "귀걸이", 760_000, "2026-01-25"),
    ]),
    ("CC FeaturedCreators/CocoaCreates", [
        ("Cocoa_MiniTattooSet.package", "문신", 1_450_000, "2025-12-09"),
        ("Cocoa_FacePreset_Round.package", "슬라이더/프리셋", 640_000, "2025-11-30"),
        ("Cocoa_BeadNecklace.package", "액세서리", 980_000, "2025-11-02"),
    ]),
    ("CC FeaturedCreators/StudioNoir", [
        ("StudioNoir_LeatherJacket.package", "상의", 7_200_000, "2025-09-14"),
        ("StudioNoir_PleatedSkirt.package", "하의", 3_900_000, "2025-09-14"),
        ("StudioNoir_AnkleBoots.package", "신발", 2_800_000, "2025-08-21"),
        ("StudioNoir_BrokenPreview_v1.package", "상의", 180_000, "2025-08-01"),
    ]),
]


def mtime_for(date_str):
    return datetime.datetime.strptime(date_str, "%Y-%m-%d").timestamp()


def build():
    folders_out = []
    total_pkgs = 0
    total_thumbs = 0
    all_items_by_path = {}

    for folder_name, files in FOLDERS:
        items = []
        for fname, cat, size, date in files:
            path = f"{folder_name}/{fname}"
            thumbs = THUMB_NAMES.get(cat, ["m001.png"])
            item = {
                "file": fname,
                "path": path,
                "size": size,
                "mtime": mtime_for(date),
                "thumbs": list(thumbs),
                "cats": [cat],
                "primary_cat": cat,
                "cat_icon": ICONS.get(cat, "📝"),
                "casp": True,
                "override": False,
            }
            items.append(item)
            all_items_by_path[path] = item
            total_pkgs += 1
            total_thumbs += len(thumbs)
        items.sort(key=lambda x: x["file"])
        folders_out.append({"name": folder_name, "items": items})

    # 중복 의심 예시: 서로 다른 창작자의 항목이 같은 썸네일을 씀
    a = all_items_by_path["CC FeaturedCreators/GlowUp Atelier/[GlowUp] Gold Hoop Earrings.package"]
    b = all_items_by_path["CC FeaturedCreators/CocoaCreates/Cocoa_BeadNecklace.package"]
    a["dup_paths"] = [b["path"]]; a["dup_count"] = 1
    b["dup_paths"] = [a["path"]]; b["dup_count"] = 1

    # CAS 충돌 예시
    c = all_items_by_path["CC FeaturedCreators/MoonlitStudio/[Moonlit] Wavy Long Hair.package"]
    d = all_items_by_path["CC FeaturedCreators/MoonlitStudio/[Moonlit] Soft Bangs Hair.package"]
    c["conflict_paths"] = [d["path"]]; c["conflict_count"] = 1
    d["conflict_paths"] = [c["path"]]; d["conflict_count"] = 1

    # 읽기 실패 예시
    broken = all_items_by_path["CC FeaturedCreators/StudioNoir/StudioNoir_BrokenPreview_v1.package"]
    broken["unreadable"] = True
    broken["unreadable_reason"] = "헤더가 손상됨 (파일이 너무 작음)"
    broken["thumbs"] = []

    manifest = {
        "folders": folders_out,
        "creators": folders_out,
        "total_pkgs": total_pkgs,
        "total_thumbs": total_thumbs,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(f"{OUT_DIR}/thumbs", exist_ok=True)
    os.makedirs(f"{OUT_DIR}/trash", exist_ok=True)
    with open(f"{OUT_DIR}/manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)
    with open(f"{OUT_DIR}/category_overrides.json", "w") as f:
        json.dump({}, f)
    with open(f"{OUT_DIR}/trash_manifest.json", "w") as f:
        json.dump({}, f)
    print(f"folders={len(folders_out)} items={total_pkgs} -> {OUT_DIR}/manifest.json")
    print("(썸네일 PNG는 gen_placeholder_thumbs.py 로 따로 생성해서 위 thumbs/ 폴더에 넣어야 함)")


if __name__ == "__main__":
    build()
