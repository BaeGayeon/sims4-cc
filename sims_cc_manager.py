#!/usr/bin/env python3
"""
Sims 4 CC Manager (v0.3)
- 파일명 휴리스틱 + CASP 리소스 파싱으로 카테고리 자동 분류
- 상위 카테고리(옷/헤어/얼굴/스킨/…) → 세부 카테고리 chip 필터
- 폴더별 / 카테고리별 그룹 토글
- 정렬(이름/크기/개수), 검색
- 이전과 동일: 로컬 서버 + 휴지통 시스템
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
TRASH_DIR = MODS / ".cc_trash"

APP_STATE.mkdir(exist_ok=True)
THUMBS_DIR.mkdir(exist_ok=True)
TRASH_DIR.mkdir(exist_ok=True)

CAS_THUMB_TYPE = 0x3C1AF1F2
CASP_TYPE = 0x034AEECB
PORT = 8765


# ─────────── 카테고리 정의 ───────────

# CASP 내부 이름에서 파싱한 파트 타입 → (한글 카테고리, 아이콘)
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

# 파일명 기반 카테고리 (표시명, 아이콘, 소문자 부분매칭 키워드)
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


def categorize_by_filename(filename, size=0):
    lower = filename.lower()
    for name, icon, keywords in CATEGORIES:
        for kw in keywords:
            if kw in lower:
                return (name, icon)
    return ("기타", "❓")


# 심즈4 스튜디오 규칙: "Hezeh_ymHair_..." 처럼 [소문자 1-3][대문자시작 파트타입]
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


# ─────────── DBPF 파서 ───────────

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
    """CASP 리소스에서 내부 이름 문자열만 뽑음. 실패 시 None."""
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
        casp_type = extract_casp_type(name)
        if casp_type and casp_type.lower() in CASP_TYPE_MAP:
            return CASP_TYPE_MAP[casp_type.lower()]
    return None


def extract_thumbs(pkg_path):
    seen = set()
    for entry in parse_dbpf(pkg_path):
        if entry["type"] != CAS_THUMB_TYPE:
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


# ─────────── 스캔 ───────────

IGNORED_DIRS = {".cc_trash", "TMEX-Settings", ".git", "__MACOSX"}


def _iter_packages():
    if not MODS.exists():
        return
    for root, dirs, files in os.walk(MODS):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for f in files:
            if f.lower().endswith(".package"):
                yield Path(root) / f


def scan_cc():
    if not MODS.exists():
        return {"folders": [], "error": f"Mods 폴더 없음: {MODS}"}
    groups = defaultdict(list)
    all_pkgs = list(_iter_packages())
    total_pkgs = len(all_pkgs)
    total_thumbs = 0
    for i, pkg in enumerate(all_pkgs, 1):
        rel_pkg = pkg.relative_to(MODS)
        folder = str(rel_pkg.parent) if rel_pkg.parent != Path(".") else "(최상위)"
        if i % 100 == 0:
            print(f"    ... {i}/{total_pkgs}")
        # 카테고리: CASP → 파일명 폴백
        casp_cat = get_casp_category(pkg)
        if casp_cat:
            cat_name, cat_icon = casp_cat
            is_casp = True
        else:
            cat_name, cat_icon = categorize_by_filename(pkg.name, pkg.stat().st_size)
            is_casp = False
        item = {
            "file": pkg.name,
            "path": str(rel_pkg),
            "size": pkg.stat().st_size,
            "thumbs": [],
            "primary_cat": cat_name,
            "cat_icon": cat_icon,
            "casp": is_casp,
        }
        for jpg, h in extract_thumbs(pkg):
            tn = f"{h[:16]}.jpg"
            tp = THUMBS_DIR / tn
            if not tp.exists():
                tp.write_bytes(jpg)
            item["thumbs"].append(tn)
            total_thumbs += 1
        groups[folder].append(item)

    folders = [{"name": name, "items": sorted(items, key=lambda x: x["file"])}
               for name, items in sorted(groups.items())]
    manifest = {
        "folders": folders,
        "creators": folders,  # 하위호환
        "total_pkgs": total_pkgs,
        "total_thumbs": total_thumbs,
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

def move_to_trash(rel_paths):
    moved, failed = [], []
    for rel in rel_paths:
        src = MODS / rel
        if not src.exists():
            failed.append({"path": rel, "reason": "파일 없음"})
            continue
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
            moved.append(rel)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    return {"moved": moved, "failed": failed}


def restore_from_trash(rel_paths):
    restored, failed = [], []
    for rel in rel_paths:
        src = TRASH_DIR / rel
        dst = MODS / rel
        if not src.exists():
            failed.append({"path": rel, "reason": "휴지통에 없음"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            restored.append(rel)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    return {"restored": restored, "failed": failed}


def list_trash():
    items = []
    total_size = 0
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            sz = f.stat().st_size
            items.append({"path": str(f.relative_to(TRASH_DIR)), "size": sz})
            total_size += sz
    return {"items": items, "total_size": total_size}


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
    return {"count": count, "size_freed": total}


# ─────────── HTTP ───────────

HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Sims 4 CC Manager</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, sans-serif; margin: 0; background: #f5f5f5; color: #222; }
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
  button.primary:hover { background: #c9302c; }
  button.blue { background: #4a90e2; color: white; border-color: #357ab8; }
  .seg { display: inline-flex; border: 1px solid #ccc; border-radius: 4px; overflow: hidden; }
  .seg button { border: none; border-radius: 0; padding: 5px 10px; }
  .seg button + button { border-left: 1px solid #ccc; }
  .seg button.on { background: #333; color: white; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { padding: 4px 10px; border-radius: 14px; background: white; border: 1px solid #ddd; cursor: pointer; font-size: 12px; display: inline-flex; align-items: center; gap: 4px; }
  .chip:hover { background: #f4f4f4; }
  .chip.on { background: #4a90e2; color: white; border-color: #357ab8; }
  .chip .count { opacity: .7; font-size: 11px; }
  .subchips { padding-left: 16px; margin-top: 4px; padding-top: 4px; border-top: 1px dashed #e0e0e0; }
  .subchip { padding: 3px 8px; border-radius: 12px; background: #f4f4f4; border: 1px solid #e0e0e0; cursor: pointer; font-size: 11px; }
  .subchip:hover { background: #e8e8e8; }
  .subchip.on { background: #333; color: white; border-color: #333; }
  main { padding: 16px; padding-bottom: 80px; }
  .creator { background: white; margin-bottom: 12px; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .creator-header { padding: 10px 16px; background: #fafafa; border-bottom: 1px solid #eee; display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; }
  .creator-header h2 { margin: 0; font-size: 15px; flex: 1; }
  .creator-count { color: #666; font-size: 12px; }
  .grid { padding: 10px; display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
  .item { position: relative; background: #fafafa; border: 2px solid transparent; border-radius: 4px; overflow: hidden; cursor: pointer; }
  .item:hover { border-color: #4a90e2; }
  .item.marked { border-color: #d9534f; background: #ffe8e8; }
  .item.marked img, .item.marked .no-thumb { filter: grayscale(0.5) brightness(0.7); }
  .item img, .no-thumb { display: block; width: 100%; height: auto; }
  .no-thumb { padding: 40px 8px; text-align: center; color: #999; font-size: 11px; background: #eee; }
  .item .name { padding: 4px 6px; font-size: 10px; color: #444; word-break: break-all; line-height: 1.3; max-height: 2.6em; overflow: hidden; }
  .item:hover .name { max-height: 20em; background: white; }
  .item .sz { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.6); color: white; padding: 1px 5px; font-size: 10px; border-radius: 3px; }
  .cat-icon { position: absolute; top: 4px; right: 4px; background: rgba(255,255,255,.9); padding: 1px 5px; border-radius: 10px; font-size: 13px; box-shadow: 0 1px 2px rgba(0,0,0,.15); }
  .collapsed .grid { display: none; }
  #footer { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #ddd; padding: 10px 16px; display: flex; align-items: center; gap: 12px; }
  #footer .count { flex: 1; font-size: 14px; }
  #footer .count b { color: #d9534f; }
  .toast { position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 10px 20px; border-radius: 6px; opacity: 0; transition: opacity .3s; z-index: 200; }
  .toast.show { opacity: 1; }
  dialog { border: none; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,.2); padding: 20px; max-width: 600px; }
  dialog::backdrop { background: rgba(0,0,0,.5); }
  .trash-item { padding: 6px 8px; border-bottom: 1px solid #eee; display: flex; gap: 8px; font-size: 12px; align-items: center; }
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
    <span class="label">카테고리</span>
    <div class="chips" id="metaChips" style="flex:1;"></div>
  </div>
  <div class="row" id="subRow" style="display:none;">
    <div class="chips subchips" id="subChips"></div>
  </div>
</header>
<main id="main"><div class="progress">로딩 중...</div></main>
<div id="footer">
  <div class="count">삭제 표시: <b id="marked-count">0</b>개 · 절약 예상: <b id="marked-size">0 B</b></div>
  <button onclick="performDelete()" class="primary">🗑️ 휴지통으로 이동</button>
</div>
<dialog id="trash-dialog">
  <h3>🗑️ 휴지통</h3>
  <div id="trash-summary" style="color:#666; font-size:13px; margin-bottom:8px;"></div>
  <div id="trash-list" style="max-height:50vh; overflow-y:auto; margin:8px 0; border:1px solid #eee; border-radius:4px;"></div>
  <div style="display:flex; gap:8px; justify-content:flex-end;">
    <button onclick="restoreSelected()">↩️ 선택 복원</button>
    <button onclick="emptyTrash()" class="primary">완전 비우기</button>
    <button onclick="document.getElementById('trash-dialog').close()">닫기</button>
  </div>
</dialog>
<div id="toast" class="toast"></div>
<script>
const META = __META__;
let manifest = null;
const marks = new Set();
const state = { groupMode: 'folder', sort: 'name', itemSort: 'name', meta: null, sub: null, q: '' };

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

function allItems() {
  if (!manifest) return [];
  const out = [];
  for (const f of manifest.folders) for (const it of f.items) out.push({...it, folder: f.name});
  return out;
}

function itemMatchesFilter(it) {
  if (state.q && !it.file.toLowerCase().includes(state.q) && !(it.folder||'').toLowerCase().includes(state.q)) return false;
  if (state.sub) return it.primary_cat === state.sub;
  if (state.meta) return (META[state.meta] || []).includes(it.primary_cat);
  return true;
}

function renderChips() {
  const items = allItems();
  const counts = {};
  for (const it of items) counts[it.primary_cat] = (counts[it.primary_cat] || 0) + 1;
  const metaChips = document.getElementById('metaChips');
  const metaHtml = ['<span class="chip ' + (!state.meta ? 'on' : '') + '" data-meta="">전체 <span class="count">' + items.length + '</span></span>'];
  for (const [name, subs] of Object.entries(META)) {
    let c = 0;
    subs.forEach(s => c += counts[s] || 0);
    if (!c) continue;
    metaHtml.push('<span class="chip ' + (state.meta === name ? 'on' : '') + '" data-meta="' + escAttr(name) + '">' + esc(name) + ' <span class="count">' + c + '</span></span>');
  }
  metaChips.innerHTML = metaHtml.join('');
  const subRow = document.getElementById('subRow');
  if (state.meta && META[state.meta]) {
    const subHtml = ['<span class="subchip ' + (!state.sub ? 'on' : '') + '" data-sub="">전체</span>'];
    for (const s of META[state.meta]) {
      const c = counts[s] || 0;
      if (!c) continue;
      subHtml.push('<span class="subchip ' + (state.sub === s ? 'on' : '') + '" data-sub="' + escAttr(s) + '">' + esc(s) + ' (' + c + ')</span>');
    }
    document.getElementById('subChips').innerHTML = subHtml.join('');
    subRow.style.display = '';
  } else {
    subRow.style.display = 'none';
  }
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

function render() {
  if (!manifest || !manifest.folders.length) {
    document.getElementById('main').innerHTML = '<div class="progress">스캔된 항목이 없다. 재스캔을 눌러라.</div>';
    return;
  }
  renderChips();
  const groups = groupItems();
  const parts = [];
  let shown = 0, totalSize = 0;
  for (const g of groups) {
    parts.push('<div class="creator"><div class="creator-header" onclick="this.parentNode.classList.toggle(\\'collapsed\\')"><h2>' + esc(g.name) + '</h2><div class="creator-count">' + g.items.length + '개</div></div><div class="grid">');
    for (const it of g.items) {
      shown++;
      totalSize += it.size;
      const cls = 'item' + (marks.has(it.path) ? ' marked' : '');
      const img = it.thumbs.length
        ? '<img src="/thumb/' + it.thumbs[0] + '" loading="lazy">'
        : '<div class="no-thumb">썸네일 없음</div>';
      parts.push('<div class="' + cls + '" data-path="' + escAttr(it.path) + '">'
        + '<span class="sz">' + fmtSize(it.size) + '</span>'
        + '<span class="cat-icon" title="' + escAttr(it.primary_cat) + '">' + it.cat_icon + '</span>'
        + img
        + '<div class="name" title="' + escAttr(it.file) + '">' + esc(it.file) + '</div>'
        + '</div>');
    }
    parts.push('</div></div>');
  }
  document.getElementById('main').innerHTML = parts.join('');
  const total = allItems().length;
  document.getElementById('stats').textContent = manifest.folders.length + '개 폴더 · ' + total + '개 · 표시 ' + shown + '개 (' + fmtSize(totalSize) + ')';
}

function updateFooter() {
  document.getElementById('marked-count').textContent = marks.size;
  let sz = 0;
  const map = {};
  allItems().forEach(it => map[it.path] = it.size);
  marks.forEach(p => sz += map[p] || 0);
  document.getElementById('marked-size').textContent = fmtSize(sz);
}

document.addEventListener('click', e => {
  const item = e.target.closest('.item');
  if (item) {
    const p = item.dataset.path;
    if (marks.has(p)) { marks.delete(p); item.classList.remove('marked'); }
    else { marks.add(p); item.classList.add('marked'); }
    updateFooter();
    return;
  }
  const chip = e.target.closest('#metaChips .chip');
  if (chip) {
    const v = chip.dataset.meta || null;
    state.meta = (state.meta === v) ? null : v;
    state.sub = null;
    render();
    return;
  }
  const sub = e.target.closest('#subChips .subchip');
  if (sub) {
    state.sub = sub.dataset.sub || null;
    render();
    return;
  }
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
  marks.clear();
  updateFooter();
  render();
  toast('스캔 완료: ' + (manifest.total_pkgs||0) + '개');
}
async function performDelete() {
  if (!marks.size) return toast('선택된 항목이 없다');
  if (!confirm(marks.size + '개 파일을 휴지통으로 이동한다. 계속?')) return;
  const res = await api('/api/delete', { paths: [...marks] });
  toast((res.moved?.length || 0) + '개 이동됨');
  marks.clear();
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
  document.getElementById('trash-dialog').close();
  await rescan();
}
async function emptyTrash() {
  if (!confirm('완전히 비운다. 되돌릴 수 없다. 계속?')) return;
  const res = await api('/api/empty-trash', {});
  toast(res.count + '개 삭제됨 (' + fmtSize(res.size_freed) + ' 확보)');
  document.getElementById('trash-dialog').close();
}
loadManifest();
</script>
</body></html>
"""


def _rendered_page():
    meta = {name: subs for name, (_ic, subs) in META_CATEGORIES.items()}
    return HTML_PAGE.replace("__META__", json.dumps(meta, ensure_ascii=False))


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
            self._json(scan_cc())
            return
        if p == "/api/delete":
            self._json(move_to_trash(body.get("paths", [])))
            return
        if p == "/api/restore":
            self._json(restore_from_trash(body.get("paths", [])))
            return
        if p == "/api/empty-trash":
            self._json(empty_trash())
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
