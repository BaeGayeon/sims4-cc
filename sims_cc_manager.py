#!/usr/bin/env python3
"""
Sims 4 CC Manager (v0.4)
- 다중 썸네일 순환 (◀/▶ 버튼)
- 다중 선택 (Cmd+클릭), 일괄 카테고리 지정 (플로팅 바)
- 우클릭 → 카테고리 컨텍스트 메뉴
- 수동 카테고리 오버라이드 (.cc_manager/category_overrides.json)
- 갤러리에 휴지통 아이템 포함 (grayed out)
- '만 보기' 필터 (삭제표시만/수동지정만)
- 실패 다이얼로그, 사라진 파일 자동 정리
"""
import json
import os
import re
import struct
import zlib
import hashlib
import shutil
import threading
import webbrowser
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote

# ─────────── 경로 설정 ───────────
SIMS_ROOT = Path.home() / "Games" / "Electronic Arts" / "The Sims 4"
MODS = SIMS_ROOT / "Mods"
CC_ROOT = MODS / "CC FeaturedCreators"
APP_STATE = SIMS_ROOT / ".cc_manager"
THUMBS_DIR = APP_STATE / "thumbs"
MANIFEST_PATH = APP_STATE / "manifest.json"
TRASH_MANIFEST_PATH = APP_STATE / "trash_manifest.json"
OVERRIDES_PATH = APP_STATE / "category_overrides.json"
TRASH_DIR = MODS / ".cc_trash"

APP_STATE.mkdir(exist_ok=True)
THUMBS_DIR.mkdir(exist_ok=True)
TRASH_DIR.mkdir(exist_ok=True)

CAS_THUMB_TYPE = 0x3C1AF1F2
BUILDBUY_THUMB_TYPE = 0x0D338A3A
CASP_TYPE = 0x034AEECB
PORT = 8765


# ─────────── 카테고리 정의 ───────────

CASP_TYPE_MAP = {
    "hair":         ("헤어", "💇"),
    "hat":          ("모자", "🎩"),
    "top":          ("상의", "👕"),
    "shirt":        ("상의", "👕"),
    "bottom":       ("하의", "👖"),
    "pants":        ("하의", "👖"),
    "skirt":        ("하의", "👖"),
    "fullbody":     ("전신", "👗"),
    "dress":        ("전신", "👗"),
    "shoes":        ("신발", "👟"),
    "boots":        ("신발", "👟"),
    "skin":         ("스킨", "🧑"),
    "skintone":     ("스킨", "🧑"),
    "skindetail":   ("스킨디테일", "✨"),
    "eyes":         ("렌즈", "👁️"),
    "eyecolor":     ("렌즈", "👁️"),
    "eyebrow":      ("눈썹", "👁️‍🗨️"),
    "eyebrows":     ("눈썹", "👁️‍🗨️"),
    "eyelash":      ("눈속눈썹", "😊"),
    "lips":         ("입술", "💋"),
    "lipstick":     ("입술", "💋"),
    "blush":        ("메이크업", "💄"),
    "eyeliner":     ("메이크업", "💄"),
    "eyeshadow":    ("메이크업", "💄"),
    "makeup":       ("메이크업", "💄"),
    "beard":        ("수염", "🧔"),
    "necklace":     ("목걸이", "📿"),
    "earring":      ("귀걸이", "💎"),
    "earrings":     ("귀걸이", "💎"),
    "ring":         ("반지", "💍"),
    "bracelet":     ("팔찌/시계", "⌚"),
    "glasses":      ("안경", "👓"),
    "bag":          ("가방", "👜"),
    "tattoo":       ("문신", "🎨"),
    "accessory":    ("액세서리", "🎀"),
}

CATEGORIES = [
    ("헤어",       "💇", ["hair", "wig", "hairstyle"]),
    ("눈썹",       "👁️‍🗨️", ["eyebrow", "brow_"]),
    ("눈속눈썹",   "😊", ["eyelash", "lashes"]),
    ("입술",       "💋", ["lipstick", "lipgloss", "lip_", "lips_"]),
    ("메이크업",   "💄", ["blush", "eyeliner", "eyeshadow", "mascara", "makeup"]),
    ("렌즈",       "👁️", ["eyes_", "_eyes", "iris", "eye_lens"]),
    ("스킨",       "🧑", ["skintone", "skin_n", "skinoverlay"]),
    ("스킨디테일", "✨", ["skindetail", "freckle", "mole", "contour"]),
    ("수영복",     "👙", ["swim", "bikini", "swimsuit", "swimwear"]),
    ("잠옷",       "🛌", ["pajama", "sleepwear", "nightgown"]),
    ("속옷",       "🩲", ["underwear", "panties", "boxer"]),
    ("상의",       "👕", ["top(", "_top", "shirt", "sweater", "hoodie", "tee_", "tank", "blouse", "jacket", "coat", "cardigan"]),
    ("하의",       "👖", ["pants", "jean", "denim", "shorts", "skirt", "trouser", "legging"]),
    ("전신",       "👗", ["dress", "outfit", "jumpsuit", "romper", "fullbody"]),
    ("신발",       "👟", ["shoes", "boot", "sneaker", "sandal", "heel", "loafer"]),
    ("귀걸이",     "💎", ["earring", "piercing"]),
    ("목걸이",     "📿", ["necklace", "choker", "pendant"]),
    ("반지",       "💍", ["ring_", " ring ", "rings"]),
    ("팔찌/시계",  "⌚", ["bracelet", "watch", "wrist"]),
    ("안경",       "👓", ["glass", "eyewear", "sunglass"]),
    ("모자",       "🎩", ["hat_", "cap_", "beanie", "beret"]),
    ("가방",       "👜", ["handbag", "backpack", "purse"]),
    ("수염",       "🧔", ["beard", "mustache", "stubble"]),
    ("문신",       "🎨", ["tattoo"]),
    ("액세서리",   "🎀", ["accessory", "brooch"]),
    ("슬라이더/프리셋", "🎚️", ["slider", "preset"]),
]

META_CATEGORIES = {
    "옷":       ("👔", ["상의", "하의", "전신", "잠옷", "속옷", "수영복"]),
    "헤어":     ("💇", ["헤어"]),
    "얼굴":     ("😊", ["눈썹", "눈속눈썹", "렌즈", "입술", "메이크업", "수염"]),
    "스킨":     ("🧑", ["스킨", "스킨디테일"]),
    "액세서리": ("💎", ["귀걸이", "목걸이", "반지", "팔찌/시계", "안경", "모자", "가방", "액세서리", "문신"]),
    "신발":     ("👟", ["신발"]),
    "유틸":     ("🎚️", ["슬라이더/프리셋"]),
    "기타":     ("❓", ["기타"]),
}


def cat_icon_of(name):
    for n, ic, _ in CATEGORIES:
        if n == name:
            return ic
    return "📝"


def categorize_by_filename(filename, size=0):
    lower = filename.lower()
    for name, icon, keywords in CATEGORIES:
        for kw in keywords:
            if kw in lower:
                return (name, icon)
    return ("기타", "❓")


_CASP_NAME_RE = re.compile(r'[_-][a-z]{1,3}([A-Z][a-zA-Z]+?)[_0-9-]')


def extract_casp_type(name):
    if not name:
        return None
    m = _CASP_NAME_RE.search(name)
    if m:
        return m.group(1)
    lower = name.lower()
    for key in CASP_TYPE_MAP:
        if key in lower:
            return key
    return None


# ─────────── DBPF ───────────

def parse_dbpf(path):
    try:
        with open(path, "rb") as f:
            hdr = f.read(96)
            if hdr[:4] != b"DBPF" or struct.unpack("<I", hdr[4:8])[0] != 2:
                return
            index_count = struct.unpack("<I", hdr[36:40])[0]
            index_offset = struct.unpack("<I", hdr[64:68])[0]
            if index_offset == 0 or index_count == 0:
                return
            f.seek(index_offset)
            flags = struct.unpack("<I", f.read(4))[0]
            ct = struct.unpack("<I", f.read(4))[0] if flags & 1 else 0
            cg = struct.unpack("<I", f.read(4))[0] if flags & 2 else 0
            ch = struct.unpack("<I", f.read(4))[0] if flags & 4 else 0
            for _ in range(index_count):
                try:
                    t = ct if flags & 1 else struct.unpack("<I", f.read(4))[0]
                    g = cg if flags & 2 else struct.unpack("<I", f.read(4))[0]
                    ihi = ch if flags & 4 else struct.unpack("<I", f.read(4))[0]
                    ilo = struct.unpack("<I", f.read(4))[0]
                    offset = struct.unpack("<I", f.read(4))[0]
                    size = struct.unpack("<I", f.read(4))[0] & 0x7FFFFFFF
                    _ = f.read(4)
                    compression = struct.unpack("<H", f.read(2))[0]
                    _ = f.read(2)
                    yield {"type": t, "instance": (ihi << 32) | ilo,
                           "offset": offset, "size": size, "compression": compression}
                except struct.error:
                    return
    except OSError:
        return


def read_resource(path, entry):
    with open(path, "rb") as f:
        f.seek(entry["offset"])
        data = f.read(entry["size"])
    if entry["compression"] == 0x5A42:
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
    return data


def parse_casp_name(data):
    try:
        if len(data) < 14:
            return None
        preset_count = struct.unpack_from("<I", data, 8)[0]
        if preset_count > 0:
            return None
        name_len = struct.unpack_from("<H", data, 12)[0]
        if name_len < 4 or name_len > 500 or 14 + name_len > len(data):
            return None
        return data[14:14+name_len].decode("utf-16-le", errors="replace")
    except (struct.error, IndexError):
        return None


def get_casp_category(pkg_path):
    for entry in parse_dbpf(pkg_path):
        if entry["type"] != CASP_TYPE:
            continue
        try:
            data = read_resource(pkg_path, entry)
        except Exception:
            continue
        name = parse_casp_name(data)
        if not name:
            continue
        t = extract_casp_type(name)
        if t and t.lower() in CASP_TYPE_MAP:
            return CASP_TYPE_MAP[t.lower()]
    return None


def extract_thumbs(pkg_path):
    seen = set()
    for entry in parse_dbpf(pkg_path):
        if entry["type"] not in (CAS_THUMB_TYPE, BUILDBUY_THUMB_TYPE):
            continue
        try:
            data = read_resource(pkg_path, entry)
        except Exception:
            continue
        if not data.startswith(b"\xff\xd8\xff"):
            continue
        h = hashlib.md5(data).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        yield (data, h)


# ─────────── 오버라이드 ───────────

def _load_overrides():
    if OVERRIDES_PATH.exists():
        try:
            return json.loads(OVERRIDES_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_overrides(m):
    OVERRIDES_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=2))


def set_category_override(rel_path, category):
    overrides = _load_overrides()
    if not category:
        overrides.pop(rel_path, None)
    else:
        overrides[rel_path] = category
    _save_overrides(overrides)
    return {"ok": True}


def set_category_batch(rel_paths, category):
    overrides = _load_overrides()
    for rel in rel_paths:
        if not category:
            overrides.pop(rel, None)
        else:
            overrides[rel] = category
    _save_overrides(overrides)
    return {"ok": True, "count": len(rel_paths)}


def cleanup_missing_overrides():
    """존재하지 않는 파일에 대한 오버라이드 제거."""
    overrides = _load_overrides()
    removed = []
    for rel in list(overrides):
        if not (MODS / rel).exists() and not (TRASH_DIR / rel).exists():
            overrides.pop(rel)
            removed.append(rel)
    if removed:
        _save_overrides(overrides)
    return removed


# ─────────── 스캔 ───────────

IGNORED_DIRS = {".cc_trash", "TMEX-Settings", ".git", "__MACOSX"}


def _iter_packages(root):
    if not root.exists():
        return
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for f in files:
            if f.lower().endswith(".package"):
                yield Path(r) / f


def _build_item(pkg, rel_base, overrides, in_trash=False):
    rel_pkg = pkg.relative_to(rel_base)
    rel_pkg_str = str(rel_pkg)
    override_cat = overrides.get(rel_pkg_str)
    is_override = False
    is_casp = False
    if override_cat:
        cat_name, cat_icon = override_cat, cat_icon_of(override_cat)
        is_override = True
    else:
        cc = get_casp_category(pkg)
        if cc:
            cat_name, cat_icon = cc
            is_casp = True
        else:
            cat_name, cat_icon = categorize_by_filename(pkg.name, pkg.stat().st_size)
    thumbs = []
    for jpg, h in extract_thumbs(pkg):
        tn = f"{h[:16]}.jpg"
        tp = THUMBS_DIR / tn
        if not tp.exists():
            tp.write_bytes(jpg)
        thumbs.append(tn)
    return {
        "file": pkg.name,
        "path": rel_pkg_str,
        "size": pkg.stat().st_size,
        "thumbs": thumbs,
        "primary_cat": cat_name,
        "cat_icon": cat_icon,
        "casp": is_casp,
        "override": is_override,
        "trashed": in_trash,
    }


def scan_cc():
    if not MODS.exists():
        return {"folders": [], "error": f"Mods 폴더 없음: {MODS}"}
    cleanup_missing_overrides()
    overrides = _load_overrides()
    groups = defaultdict(list)
    all_pkgs = list(_iter_packages(MODS))
    total_pkgs = len(all_pkgs)
    total_thumbs = 0
    for i, pkg in enumerate(all_pkgs, 1):
        if i % 100 == 0:
            print(f"    ... {i}/{total_pkgs}")
        it = _build_item(pkg, MODS, overrides, in_trash=False)
        total_thumbs += len(it["thumbs"])
        rel_pkg = pkg.relative_to(MODS)
        folder = str(rel_pkg.parent) if rel_pkg.parent != Path(".") else "(최상위)"
        groups[folder].append(it)

    # 휴지통 아이템도 갤러리에 grayed out으로 표시
    trash_m = _load_trash_manifest()
    for f in TRASH_DIR.rglob("*.package"):
        if not f.is_file():
            continue
        rel = f.relative_to(TRASH_DIR)
        info = trash_m.get(str(rel), {})
        original = info.get("original_path", str(rel))
        # 원본 경로 기준 폴더에 넣음
        original_p = Path(original)
        folder = str(original_p.parent) if original_p.parent != Path(".") else "(최상위)"
        cat_name, cat_icon = ("기타", "❓")
        override = overrides.get(original)
        if override:
            cat_name, cat_icon = override, cat_icon_of(override)
        else:
            cc = get_casp_category(f)
            if cc:
                cat_name, cat_icon = cc
            else:
                cat_name, cat_icon = categorize_by_filename(f.name, f.stat().st_size)
        thumbs = info.get("thumbs", [])
        if not thumbs:
            for jpg, h in extract_thumbs(f):
                tn = f"{h[:16]}.jpg"
                tp = THUMBS_DIR / tn
                if not tp.exists():
                    tp.write_bytes(jpg)
                thumbs.append(tn)
        groups[folder].append({
            "file": f.name,
            "path": original,           # 원본 경로 기준 (필터 일관성)
            "trash_rel": str(rel),      # 실제 휴지통 안 상대 경로
            "size": f.stat().st_size,
            "thumbs": thumbs,
            "primary_cat": cat_name,
            "cat_icon": cat_icon,
            "casp": False,
            "override": bool(override),
            "trashed": True,
        })

    folders = [{"name": name, "items": sorted(items, key=lambda x: x["file"])}
               for name, items in sorted(groups.items())]
    manifest = {
        "folders": folders,
        "creators": folders,
        "total_pkgs": total_pkgs,
        "total_thumbs": total_thumbs,
        "trash_count": sum(1 for _ in TRASH_DIR.rglob("*.package")),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False))
    return manifest


def load_manifest():
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return None


# ─────────── 휴지통 ───────────

def _load_trash_manifest():
    if TRASH_MANIFEST_PATH.exists():
        try:
            return json.loads(TRASH_MANIFEST_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_trash_manifest(m):
    TRASH_MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))


def _find_thumbs_for(path):
    m = load_manifest()
    if not m:
        return []
    for c in m.get("folders", []):
        for it in c.get("items", []):
            if it["path"] == path:
                return it.get("thumbs", [])
    return []


def move_to_trash(rel_paths):
    moved, failed = [], []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        src = MODS / rel
        if not src.exists():
            failed.append({"path": rel, "reason": "파일 없음"})
            continue
        thumbs = _find_thumbs_for(rel)
        dst = TRASH_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            i = 1
            while True:
                cand = dst.with_stem(f"{dst.stem}_{i}")
                if not cand.exists():
                    dst = cand
                    break
                i += 1
        try:
            shutil.move(str(src), str(dst))
            trash_rel = str(dst.relative_to(TRASH_DIR))
            trash_m[trash_rel] = {"original_path": rel, "thumbs": thumbs, "size": dst.stat().st_size}
            moved.append(rel)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    _save_trash_manifest(trash_m)
    return {"moved": moved, "failed": failed}


def restore_from_trash(rel_paths):
    restored, failed = [], []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        src = TRASH_DIR / rel
        info = trash_m.get(rel, {})
        original = info.get("original_path", rel)
        dst = MODS / original
        if not src.exists():
            failed.append({"path": rel, "reason": "휴지통에 없음"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            trash_m.pop(rel, None)
            restored.append(rel)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    _save_trash_manifest(trash_m)
    return {"restored": restored, "failed": failed}


def list_trash():
    items = []
    total_size = 0
    trash_m = _load_trash_manifest()
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            rel = str(f.relative_to(TRASH_DIR))
            sz = f.stat().st_size
            info = trash_m.get(rel, {})
            items.append({
                "path": rel,
                "size": sz,
                "thumbs": info.get("thumbs", []),
                "original_path": info.get("original_path", rel),
            })
            total_size += sz
    return {"items": items, "total_size": total_size}


def delete_from_trash(rel_paths):
    count = 0
    total = 0
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        fp = TRASH_DIR / rel
        if fp.exists() and fp.is_file():
            try:
                total += fp.stat().st_size
                fp.unlink()
                count += 1
                trash_m.pop(rel, None)
            except OSError:
                pass
    _save_trash_manifest(trash_m)
    return {"count": count, "size_freed": total}


def empty_trash():
    count = 0
    total = 0
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
    for d in sorted([d for d in TRASH_DIR.rglob("*") if d.is_dir()],
                    key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    _save_trash_manifest({})
    return {"count": count, "size_freed": total}


# ─────────── HTTP ───────────

HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Sims 4 CC Manager</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, sans-serif; margin: 0; background: #f5f5f5; color: #222; padding-bottom: 70px; }
  body.has-bulk { padding-bottom: 118px; }
  header { position: sticky; top: 0; background: white; padding: 8px 16px; border-bottom: 1px solid #ddd; z-index: 10; box-shadow: 0 1px 3px rgba(0,0,0,.05); }
  .title-row { display: flex; align-items: center; gap: 12px; }
  .title-row h1 { margin: 0; font-size: 16px; }
  .stats { color: #666; font-size: 12px; flex: 1; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 6px 0; border-top: 1px solid #f0f0f0; }
  .row:first-of-type { border-top: none; }
  .group { display: inline-flex; align-items: center; gap: 4px; background: #f7f7f7; padding: 3px 8px; border-radius: 6px; }
  .group .label { color: #666; font-size: 11px; }
  input[type=search] { padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px; min-width: 220px; }
  select { padding: 4px 8px; border: 1px solid #ccc; border-radius: 4px; font-size: 12px; }
  button { padding: 5px 10px; font-size: 12px; background: white; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; }
  button:hover { background: #f0f0f0; }
  button.primary { background: #d9534f; color: white; border-color: #d43f3a; }
  button.blue { background: #4a90e2; color: white; border-color: #357ab8; }
  .seg { display: inline-flex; border: 1px solid #ccc; border-radius: 4px; overflow: hidden; }
  .seg button { border: none; border-radius: 0; padding: 5px 10px; }
  .seg button + button { border-left: 1px solid #ccc; }
  .seg button.on { background: #333; color: white; }
  .switch { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #444; cursor: pointer; user-select: none; }
  .switch input { appearance: none; -webkit-appearance: none; width: 32px; height: 18px; background: #ccc; border-radius: 10px; position: relative; margin: 0; }
  .switch input:checked { background: #4a90e2; }
  .switch input::before { content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; background: white; border-radius: 50%; transition: transform .2s; }
  .switch input:checked::before { transform: translateX(14px); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { padding: 4px 10px; border-radius: 14px; background: white; border: 1px solid #ddd; cursor: pointer; font-size: 12px; }
  .chip:hover { background: #f4f4f4; }
  .chip.on { background: #4a90e2; color: white; border-color: #357ab8; }
  .chip .count { opacity: .7; font-size: 11px; margin-left: 2px; }
  .subchips { padding-left: 16px; margin-top: 4px; padding-top: 4px; border-top: 1px dashed #e0e0e0; }
  .subchip { padding: 3px 8px; border-radius: 12px; background: #f4f4f4; border: 1px solid #e0e0e0; cursor: pointer; font-size: 11px; }
  .subchip:hover { background: #e8e8e8; }
  .subchip.on { background: #333; color: white; border-color: #333; }
  main { padding: 16px; }
  .creator { background: white; margin-bottom: 12px; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .creator-header { padding: 10px 16px; background: #fafafa; border-bottom: 1px solid #eee; display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
  .creator-header h2 { margin: 0; font-size: 15px; flex: 1; }
  .creator-count { color: #666; font-size: 12px; }
  .grid { padding: 10px; display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
  .collapsed .grid { display: none; }
  .item { position: relative; background: #fafafa; border: 2px solid transparent; border-radius: 4px; overflow: hidden; cursor: pointer; }
  .item:hover { border-color: #4a90e2; }
  .item.marked { border-color: #d9534f; background: #ffe8e8; }
  .item.marked img, .item.marked .no-thumb { filter: grayscale(0.5) brightness(0.7); }
  .item.selected { border-color: #4a90e2 !important; box-shadow: 0 0 0 2px #4a90e2; }
  .item.selected::before { content: '✓'; position: absolute; top: 4px; left: 4px; background: #4a90e2; color: white; width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: bold; z-index: 3; }
  .item.trashed { opacity: 0.55; background: #f0f0f0; }
  .item.trashed img { filter: grayscale(0.6); }
  .trash-badge { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.75); color: white; padding: 2px 6px; border-radius: 3px; font-size: 10px; z-index: 2; }
  .item img, .no-thumb { display: block; width: 100%; height: auto; }
  .no-thumb { padding: 40px 8px; text-align: center; color: #999; font-size: 11px; background: #eee; }
  .item .name { padding: 4px 6px; font-size: 10px; color: #444; word-break: break-all; line-height: 1.3; max-height: 2.6em; overflow: hidden; }
  .item:hover .name { max-height: 20em; background: white; }
  .item .sz { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.6); color: white; padding: 1px 5px; font-size: 10px; border-radius: 3px; }
  .item.selected .sz { display: none; }
  .cat-icon { position: absolute; top: 4px; right: 4px; background: rgba(255,255,255,.9); padding: 1px 5px; border-radius: 10px; font-size: 13px; cursor: context-menu; }
  .cat-icon.override { background: #ffd700; box-shadow: 0 0 0 1px #b8860b; }
  .thumb-nav { position: absolute; top: 4px; left: 4px; right: 4px; display: flex; align-items: center; justify-content: space-between; opacity: 0; transition: opacity .15s; pointer-events: none; }
  .item:hover .thumb-nav { opacity: 1; pointer-events: auto; }
  .thumb-btn { background: rgba(0,0,0,.6); color: white; border: none; border-radius: 50%; width: 22px; height: 22px; font-size: 10px; padding: 0; cursor: pointer; }
  .thumb-btn:hover { background: rgba(0,0,0,.85); }
  .thumb-counter { background: rgba(0,0,0,.6); color: white; padding: 2px 6px; border-radius: 10px; font-size: 10px; }
  #footer { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #ddd; padding: 10px 16px; display: flex; align-items: center; gap: 12px; z-index: 100; }
  #footer .count { flex: 1; font-size: 14px; }
  #footer .count b { color: #d9534f; }
  #bulkBar { position: fixed; bottom: 44px; left: 0; right: 0; background: #333; color: white; padding: 8px 16px; z-index: 99; display: none; align-items: center; gap: 12px; }
  #bulkBar .info { flex: 1; font-size: 14px; }
  #bulkBar select { padding: 5px 8px; border-radius: 4px; }
  #ctxMenu { position: fixed; background: white; border: 1px solid #ccc; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,.15); padding: 4px; z-index: 1000; max-height: 400px; overflow-y: auto; min-width: 180px; display: none; }
  #ctxMenu .ctx-header { padding: 4px 8px; font-size: 11px; color: #666; border-bottom: 1px solid #eee; margin-bottom: 4px; }
  #ctxMenu .ctx-item { padding: 5px 10px; font-size: 13px; cursor: pointer; border-radius: 4px; display: flex; align-items: center; gap: 6px; }
  #ctxMenu .ctx-item:hover { background: #f4f4f4; }
  #ctxMenu .ctx-item.current { background: #e8f4ff; font-weight: 600; }
  #ctxMenu .ctx-item.reset { border-top: 1px solid #eee; margin-top: 4px; padding-top: 8px; color: #d9534f; }
  .toast { position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 10px 20px; border-radius: 6px; opacity: 0; transition: opacity .3s; z-index: 200; }
  .toast.show { opacity: 1; }
  dialog { border: none; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,.2); padding: 20px; max-width: 600px; }
  dialog::backdrop { background: rgba(0,0,0,.5); }
  .trash-item { padding: 6px 8px; border-bottom: 1px solid #eee; display: flex; gap: 8px; font-size: 12px; align-items: center; }
  .fail-item { padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 12px; }
  .fail-item .why { color: #d9534f; font-size: 11px; }
  .progress { background: #f0f0f0; padding: 20px; border-radius: 8px; margin: 40px auto; max-width: 500px; text-align: center; }
</style>
</head><body>
<header>
  <div class="title-row">
    <h1>🎮 CC Manager</h1>
    <div class="stats" id="stats">로딩 중...</div>
    <button onclick="rescan()" class="blue">🔄 재스캔</button>
    <button onclick="openTrash()">🗑️ 휴지통</button>
  </div>
  <div class="row">
    <input type="search" id="search" placeholder="🔍 검색..." style="flex:1;">
    <div class="group">
      <span class="label">그룹</span>
      <div class="seg" id="groupSeg">
        <button data-v="folder" class="on">폴더별</button>
        <button data-v="category">카테고리별</button>
      </div>
    </div>
    <div class="group">
      <span class="label">그룹정렬</span>
      <select id="sortBy">
        <option value="name">이름</option>
        <option value="size">크기</option>
        <option value="count">개수</option>
      </select>
    </div>
    <div class="group">
      <span class="label">아이템정렬</span>
      <select id="itemSortBy">
        <option value="name">이름</option>
        <option value="size">크기</option>
        <option value="category">카테고리</option>
      </select>
    </div>
  </div>
  <div class="row">
    <label class="switch"><input type="checkbox" id="tglMarked"> 🗑️ 지울 것만</label>
    <label class="switch"><input type="checkbox" id="tglOverride"> 🖐️ 수동 지정만</label>
    <label class="switch"><input type="checkbox" id="tglCollapsed"> 모두 접기</label>
    <button onclick="clearBulkSel()" style="margin-left:auto;">▢ 다중선택 해제</button>
    <button onclick="clearMarks()">🗑️❌ 삭제표시 초기화</button>
  </div>
  <div class="row">
    <span class="label">카테고리</span>
    <div class="chips" id="metaChips" style="flex:1;"></div>
  </div>
  <div class="row" id="subRow" style="display:none;">
    <div class="chips subchips" id="subChips"></div>
  </div>
</header>
<main id="main"><div class="progress">로딩 중...</div></main>
<div id="bulkBar">
  <div class="info"><b id="bulkCount">0</b>개 선택됨</div>
  <select id="bulkCat"></select>
  <button onclick="bulkAssign()" class="blue">🏷️ 카테고리 지정</button>
  <button onclick="bulkAssign(true)">↺ 오버라이드 해제</button>
</div>
<div id="footer">
  <div class="count">삭제 표시: <b id="marked-count">0</b>개 · 절약 예상: <b id="marked-size">0 B</b></div>
  <button onclick="performDelete()" class="primary">🗑️ 휴지통으로 이동</button>
</div>
<div id="ctxMenu"></div>
<dialog id="trash-dialog">
  <h3>🗑️ 휴지통</h3>
  <div id="trash-summary" style="color:#666; font-size:13px; margin-bottom:8px;"></div>
  <div id="trash-list" style="max-height:50vh; overflow-y:auto; margin:8px 0; border:1px solid #eee; border-radius:4px;"></div>
  <div style="display:flex; gap:8px; justify-content:flex-end;">
    <button onclick="restoreSelected()">↩️ 선택 복원</button>
    <button onclick="deleteSelectedTrash()" class="primary">선택 완전삭제</button>
    <button onclick="emptyTrash()" class="primary">완전 비우기</button>
    <button onclick="document.getElementById('trash-dialog').close()">닫기</button>
  </div>
</dialog>
<dialog id="fail-dialog">
  <h3>⚠️ 일부 작업 실패</h3>
  <div id="fail-list" style="max-height:50vh; overflow-y:auto; margin:8px 0;"></div>
  <div style="text-align:right;"><button onclick="document.getElementById('fail-dialog').close()">닫기</button></div>
</dialog>
<div id="toast" class="toast"></div>
<script>
const META = __META__;
const CAT_LIST = __CATS__;
let manifest = null;
const marks = new Set();
const selection = new Set();
const thumbIdx = {};
const state = { groupMode: 'folder', sort: 'name', itemSort: 'name', meta: null, sub: null, q: '', onlyMarked: false, onlyOverride: false, collapseAll: false };

function fmtSize(n) {
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + u[i];
}
function esc(s) { return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function escAttr(s) { return esc(s).replace(/"/g, '&quot;'); }

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

function initBulkCatOptions() {
  const sel = document.getElementById('bulkCat');
  sel.innerHTML = '<option value="">-- 카테고리 선택 --</option>' +
    CAT_LIST.map(c => '<option value="' + escAttr(c[0]) + '">' + c[1] + ' ' + esc(c[0]) + '</option>').join('');
}

function allItems() {
  if (!manifest) return [];
  const out = [];
  for (const f of manifest.folders) for (const it of f.items) out.push({...it, folder: f.name});
  return out;
}

function itemMatchesFilter(it) {
  if (state.q && !it.file.toLowerCase().includes(state.q) && !(it.folder||'').toLowerCase().includes(state.q)) return false;
  if (state.onlyMarked && !marks.has(it.path)) return false;
  if (state.onlyOverride && !it.override) return false;
  if (state.sub) return it.primary_cat === state.sub;
  if (state.meta) return (META[state.meta] || []).includes(it.primary_cat);
  return true;
}

function renderChips() {
  const items = allItems();
  const counts = {};
  for (const it of items) counts[it.primary_cat] = (counts[it.primary_cat] || 0) + 1;
  const metaHtml = ['<span class="chip ' + (!state.meta ? 'on' : '') + '" data-meta="">전체 <span class="count">' + items.length + '</span></span>'];
  for (const [name, subs] of Object.entries(META)) {
    let c = 0; subs.forEach(s => c += counts[s] || 0);
    if (!c) continue;
    metaHtml.push('<span class="chip ' + (state.meta === name ? 'on' : '') + '" data-meta="' + escAttr(name) + '">' + esc(name) + ' <span class="count">' + c + '</span></span>');
  }
  document.getElementById('metaChips').innerHTML = metaHtml.join('');
  const subRow = document.getElementById('subRow');
  if (state.meta && META[state.meta]) {
    const subHtml = ['<span class="subchip ' + (!state.sub ? 'on' : '') + '" data-sub="">전체</span>'];
    for (const s of META[state.meta]) {
      const c = counts[s] || 0; if (!c) continue;
      subHtml.push('<span class="subchip ' + (state.sub === s ? 'on' : '') + '" data-sub="' + escAttr(s) + '">' + esc(s) + ' (' + c + ')</span>');
    }
    document.getElementById('subChips').innerHTML = subHtml.join('');
    subRow.style.display = '';
  } else subRow.style.display = 'none';
}

function groupItems() {
  const items = allItems().filter(itemMatchesFilter);
  const groups = {};
  const keyFn = state.groupMode === 'folder' ? (it => it.folder) : (it => it.primary_cat);
  for (const it of items) {
    const k = keyFn(it) || '(없음)';
    (groups[k] = groups[k] || []).push(it);
  }
  let arr = Object.entries(groups).map(([name, items]) => ({name, items}));
  if (state.sort === 'size') arr.sort((a,b) => b.items.reduce((s,i)=>s+i.size,0) - a.items.reduce((s,i)=>s+i.size,0));
  else if (state.sort === 'count') arr.sort((a,b) => b.items.length - a.items.length);
  else arr.sort((a,b) => a.name.localeCompare(b.name));
  for (const g of arr) {
    if (state.itemSort === 'size') g.items.sort((a,b) => b.size - a.size);
    else if (state.itemSort === 'category') g.items.sort((a,b) => a.primary_cat.localeCompare(b.primary_cat) || a.file.localeCompare(b.file));
    else g.items.sort((a,b) => a.file.localeCompare(b.file));
  }
  return arr;
}

function renderItem(it) {
  const cls = ['item'];
  if (marks.has(it.path)) cls.push('marked');
  if (selection.has(it.path)) cls.push('selected');
  if (it.trashed) cls.push('trashed');
  const ti = thumbIdx[it.path] || 0;
  const thumb = it.thumbs.length
    ? '<img src="/thumb/' + it.thumbs[ti % it.thumbs.length] + '" loading="lazy">'
    : '<div class="no-thumb">썸네일 없음</div>';
  const navHtml = it.thumbs.length > 1
    ? '<div class="thumb-nav">'
      + '<button class="thumb-btn" data-act="prev">◀</button>'
      + '<span class="thumb-counter">' + ((ti % it.thumbs.length) + 1) + '/' + it.thumbs.length + '</span>'
      + '<button class="thumb-btn" data-act="next">▶</button>'
    + '</div>'
    : '';
  const catIcon = '<span class="cat-icon' + (it.override ? ' override' : '') + '" title="' + escAttr(it.primary_cat) + ' (우클릭)">' + it.cat_icon + '</span>';
  const trashBadge = it.trashed ? '<span class="trash-badge">휴지통</span>' : '';
  return '<div class="' + cls.join(' ') + '" data-path="' + escAttr(it.path) + '"' + (it.trashed ? ' data-trashed="1"' : '') + '>'
    + trashBadge
    + '<span class="sz">' + fmtSize(it.size) + '</span>'
    + catIcon
    + thumb
    + navHtml
    + '<div class="name" title="' + escAttr(it.file) + '">' + esc(it.file) + '</div>'
    + '</div>';
}

function render() {
  if (!manifest || !manifest.folders.length) {
    document.getElementById('main').innerHTML = '<div class="progress">스캔된 항목이 없다.</div>';
    return;
  }
  renderChips();
  const groups = groupItems();
  const parts = [];
  let shown = 0, totalSize = 0;
  for (const g of groups) {
    const cls = 'creator' + (state.collapseAll ? ' collapsed' : '');
    parts.push('<div class="' + cls + '"><div class="creator-header" onclick="this.parentNode.classList.toggle(\\'collapsed\\')"><h2>' + esc(g.name) + '</h2><div class="creator-count">' + g.items.length + '개</div></div><div class="grid">');
    for (const it of g.items) { shown++; totalSize += it.size; parts.push(renderItem(it)); }
    parts.push('</div></div>');
  }
  document.getElementById('main').innerHTML = parts.join('');
  document.getElementById('stats').textContent =
    manifest.folders.length + '개 폴더 · ' + allItems().length + '개 · 표시 ' + shown + '개 (' + fmtSize(totalSize) + ')';
  updateBulkBar();
}

function updateFooter() {
  document.getElementById('marked-count').textContent = marks.size;
  let sz = 0;
  const map = {};
  allItems().forEach(it => map[it.path] = it.size);
  marks.forEach(p => sz += map[p] || 0);
  document.getElementById('marked-size').textContent = fmtSize(sz);
}

function updateBulkBar() {
  const bar = document.getElementById('bulkBar');
  document.getElementById('bulkCount').textContent = selection.size;
  if (selection.size > 0) { bar.style.display = 'flex'; document.body.classList.add('has-bulk'); }
  else { bar.style.display = 'none'; document.body.classList.remove('has-bulk'); }
}

function clearBulkSel() {
  selection.clear();
  document.querySelectorAll('.item.selected').forEach(el => el.classList.remove('selected'));
  updateBulkBar();
}
function clearMarks() {
  marks.clear();
  document.querySelectorAll('.item.marked').forEach(el => el.classList.remove('marked'));
  updateFooter();
}

document.addEventListener('click', e => {
  // 썸네일 순환
  const tb = e.target.closest('.thumb-btn');
  if (tb) {
    const it = tb.closest('.item');
    const path = it.dataset.path;
    const item = allItems().find(x => x.path === path);
    if (item && item.thumbs.length > 1) {
      const cur = thumbIdx[path] || 0;
      const next = tb.dataset.act === 'next' ? cur + 1 : cur - 1 + item.thumbs.length;
      thumbIdx[path] = next % item.thumbs.length;
      render();
    }
    e.stopPropagation(); return;
  }
  // 다중 선택 (Cmd/Ctrl+클릭)
  const item = e.target.closest('.item');
  if (!item) return;
  if (item.dataset.trashed) return;  // 휴지통 아이템은 마킹/선택 불가
  const path = item.dataset.path;
  if (e.metaKey || e.ctrlKey) {
    if (selection.has(path)) { selection.delete(path); item.classList.remove('selected'); }
    else { selection.add(path); item.classList.add('selected'); }
    updateBulkBar();
    return;
  }
  // 삭제 마킹
  if (marks.has(path)) { marks.delete(path); item.classList.remove('marked'); }
  else { marks.add(path); item.classList.add('marked'); }
  updateFooter();
});

// 우클릭 카테고리 메뉴
document.addEventListener('contextmenu', e => {
  const item = e.target.closest('.item');
  if (!item || item.dataset.trashed) return;
  e.preventDefault();
  const path = item.dataset.path;
  const it = allItems().find(x => x.path === path);
  if (!it) return;
  const menu = document.getElementById('ctxMenu');
  const header = '<div class="ctx-header">' + esc(it.file) + '</div>';
  const items = CAT_LIST.map(c => {
    const cur = it.primary_cat === c[0] ? ' current' : '';
    return '<div class="ctx-item' + cur + '" data-cat="' + escAttr(c[0]) + '">' + c[1] + ' ' + esc(c[0]) + '</div>';
  }).join('');
  const reset = it.override ? '<div class="ctx-item reset" data-cat="">↺ 자동 감지로 되돌리기</div>' : '';
  menu.innerHTML = header + items + reset;
  menu.dataset.path = path;
  menu.style.display = 'block';
  menu.style.left = Math.min(e.clientX, window.innerWidth - 200) + 'px';
  menu.style.top = Math.min(e.clientY, window.innerHeight - 320) + 'px';
});
document.addEventListener('click', e => {
  const menu = document.getElementById('ctxMenu');
  const ci = e.target.closest('#ctxMenu .ctx-item');
  if (ci) {
    const cat = ci.dataset.cat;
    const path = menu.dataset.path;
    api('/api/set-category', { path, category: cat }).then(async () => {
      toast(cat ? ('카테고리: ' + cat) : '오버라이드 해제됨');
      menu.style.display = 'none';
      await rescan();
    });
    return;
  }
  if (!e.target.closest('#ctxMenu')) document.getElementById('ctxMenu').style.display = 'none';
});

document.getElementById('groupSeg').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  [...b.parentNode.children].forEach(x => x.classList.remove('on'));
  b.classList.add('on');
  state.groupMode = b.dataset.v;
  render();
});
document.getElementById('sortBy').addEventListener('change', e => { state.sort = e.target.value; render(); });
document.getElementById('itemSortBy').addEventListener('change', e => { state.itemSort = e.target.value; render(); });
document.getElementById('search').addEventListener('input', e => { state.q = e.target.value.toLowerCase(); render(); });
document.getElementById('tglMarked').addEventListener('change', e => { state.onlyMarked = e.target.checked; render(); });
document.getElementById('tglOverride').addEventListener('change', e => { state.onlyOverride = e.target.checked; render(); });
document.getElementById('tglCollapsed').addEventListener('change', e => { state.collapseAll = e.target.checked; render(); });

document.addEventListener('click', e => {
  const chip = e.target.closest('#metaChips .chip');
  if (chip) { const v = chip.dataset.meta || null; state.meta = (state.meta === v) ? null : v; state.sub = null; render(); return; }
  const sub = e.target.closest('#subChips .subchip');
  if (sub) { state.sub = sub.dataset.sub || null; render(); return; }
});

async function api(url, body) {
  const opt = { method: body ? 'POST' : 'GET' };
  if (body) { opt.headers = {'Content-Type':'application/json'}; opt.body = JSON.stringify(body); }
  return (await fetch(url, opt)).json();
}

async function loadManifest() {
  manifest = await api('/api/manifest');
  if (manifest && manifest.folders && manifest.folders.length) render();
  else await rescan();
}
async function rescan() {
  document.getElementById('main').innerHTML = '<div class="progress">스캔 중...</div>';
  manifest = await api('/api/scan', {});
  updateFooter();
  render();
  toast('스캔 완료: ' + (manifest.total_pkgs||0) + '개');
}

function showFailDialog(failed, action) {
  document.getElementById('fail-list').innerHTML =
    '<div style="margin-bottom:8px;">' + action + ' 실패 ' + failed.length + '건:</div>' +
    failed.map(f => '<div class="fail-item">' + esc(f.path) + '<div class="why">' + esc(f.reason) + '</div></div>').join('');
  document.getElementById('fail-dialog').showModal();
}

async function performDelete() {
  if (!marks.size) return toast('선택된 항목이 없다');
  if (!confirm(marks.size + '개 파일을 휴지통으로 이동한다. 계속?')) return;
  const res = await api('/api/delete', { paths: [...marks] });
  toast((res.moved?.length || 0) + '개 이동됨');
  if (res.failed?.length) showFailDialog(res.failed, '삭제');
  marks.clear();
  await rescan();
}

async function bulkAssign(reset) {
  if (!selection.size) return toast('선택된 항목이 없다');
  const cat = reset ? '' : document.getElementById('bulkCat').value;
  if (!reset && !cat) return toast('카테고리를 골라라');
  const res = await api('/api/set-category-batch', { paths: [...selection], category: cat });
  toast(res.count + '개 ' + (reset ? '오버라이드 해제' : '카테고리 지정: ' + cat));
  clearBulkSel();
  await rescan();
}

async function openTrash() {
  const dlg = document.getElementById('trash-dialog');
  const data = await api('/api/trash');
  document.getElementById('trash-summary').textContent = data.items.length + '개 · ' + fmtSize(data.total_size);
  document.getElementById('trash-list').innerHTML = data.items.map(it =>
    '<label class="trash-item"><input type="checkbox" value="' + escAttr(it.path) + '">'
    + '<span style="flex:1;">' + esc(it.path) + '</span>'
    + '<span style="color:#888;">' + fmtSize(it.size) + '</span></label>'
  ).join('') || '<div style="padding:20px; text-align:center; color:#999;">비어있음</div>';
  dlg.showModal();
}
async function restoreSelected() {
  const boxes = document.querySelectorAll('#trash-list input:checked');
  if (!boxes.length) return toast('선택된 항목이 없다');
  const paths = [...boxes].map(b => b.value);
  const res = await api('/api/restore', { paths });
  toast((res.restored?.length || 0) + '개 복원됨');
  if (res.failed?.length) showFailDialog(res.failed, '복원');
  document.getElementById('trash-dialog').close();
  await rescan();
}
async function deleteSelectedTrash() {
  const boxes = document.querySelectorAll('#trash-list input:checked');
  if (!boxes.length) return toast('선택된 항목이 없다');
  if (!confirm(boxes.length + '개를 완전 삭제한다. 되돌릴 수 없다. 계속?')) return;
  const paths = [...boxes].map(b => b.value);
  const res = await api('/api/delete-from-trash', { paths });
  toast(res.count + '개 삭제됨');
  document.getElementById('trash-dialog').close();
  await rescan();
}
async function emptyTrash() {
  if (!confirm('완전히 비운다. 되돌릴 수 없다. 계속?')) return;
  const res = await api('/api/empty-trash', {});
  toast(res.count + '개 삭제됨 (' + fmtSize(res.size_freed) + ')');
  document.getElementById('trash-dialog').close();
  await rescan();
}

initBulkCatOptions();
loadManifest();
</script>
</body></html>
"""


def _rendered_page():
    meta = {name: subs for name, (_ic, subs) in META_CATEGORIES.items()}
    cats = [[name, icon] for name, icon, _ in CATEGORIES]
    return (HTML_PAGE
            .replace("__META__", json.dumps(meta, ensure_ascii=False))
            .replace("__CATS__", json.dumps(cats, ensure_ascii=False)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): return

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._bytes(_rendered_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if p.startswith("/thumb/"):
            fp = THUMBS_DIR / unquote(p[len("/thumb/"):])
            if fp.exists() and fp.is_file():
                self._bytes(fp.read_bytes(), "image/jpeg")
            else:
                self.send_error(404)
            return
        if p == "/api/manifest":
            self._json(load_manifest() or {"folders": [], "creators": [], "total_pkgs": 0, "total_thumbs": 0})
            return
        if p == "/api/trash":
            self._json(list_trash())
            return
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._read_body()
        if p == "/api/scan":
            self._json(scan_cc()); return
        if p == "/api/delete":
            self._json(move_to_trash(body.get("paths", []))); return
        if p == "/api/restore":
            self._json(restore_from_trash(body.get("paths", []))); return
        if p == "/api/empty-trash":
            self._json(empty_trash()); return
        if p == "/api/delete-from-trash":
            self._json(delete_from_trash(body.get("paths", []))); return
        if p == "/api/set-category":
            self._json(set_category_override(body.get("path", ""), body.get("category", "")))
            return
        if p == "/api/set-category-batch":
            self._json(set_category_batch(body.get("paths", []), body.get("category", "")))
            return
        self.send_error(404)


def ensure_launcher():
    launcher = Path(__file__).parent / "CC Manager.command"
    if launcher.exists():
        return
    launcher.write_text(
        '#!/bin/bash\n'
        '# Sims 4 CC Manager 실행기\n'
        'cd "$(dirname "$0")"\n'
        'python3 sims_cc_manager.py\n'
    )
    os.chmod(launcher, 0o755)


def main():
    ensure_launcher()
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"[+] CC Manager: {url}")
    print(f"[i] Mods: {MODS}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] 종료")
        server.server_close()


if __name__ == "__main__":
    main()
