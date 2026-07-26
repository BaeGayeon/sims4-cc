#!/usr/bin/env python3
"""
Sims 4 CC Manager
- CC 폴더 스캔해서 썸네일 추출
- 로컬 웹서버로 갤러리 제공
- 브라우저에서 클릭 → 실제 삭제 (안전: 휴지통 폴더로 이동)
- 재스캔, 복원, 통계 지원
"""
import json
import os
import struct
import zlib
import hashlib
import shutil
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote

# ─────────── 경로 설정 ───────────
# 앱 관련 모든 데이터를 Sims 폴더 밖(macOS 표준 위치)에 저장
APP_STATE = Path.home() / "Library" / "Application Support" / "Sims4CCManager"
_CONFIG_PATH = APP_STATE / "config.json"


def find_sims_root():
    """Sims 4 폴더 자동 감지. 캐시 → 환경변수 → 표준 경로 → 심볼릭 링크 순."""
    # 캐시된 경로
    try:
        if _CONFIG_PATH.exists():
            cfg = json.loads(_CONFIG_PATH.read_text())
            p = Path(cfg.get("sims_root", ""))
            if p.exists() and (p / "Mods").exists():
                return p
    except Exception:
        pass
    candidates = []
    env = os.environ.get("SIMS4_PATH")
    if env:
        candidates.append(Path(env))
    home = Path.home()
    candidates.append(home / "Documents" / "Electronic Arts" / "The Sims 4")
    candidates.append(home / "Games" / "Electronic Arts" / "The Sims 4")
    # 심볼릭 링크 따라가기
    ea = home / "Documents" / "Electronic Arts"
    if ea.exists():
        try:
            for child in ea.iterdir():
                if child.name == "The Sims 4" or child.name.startswith("The Sims 4"):
                    try:
                        candidates.append(child.resolve())
                    except Exception:
                        candidates.append(child)
        except Exception:
            pass
    for p in candidates:
        try:
            if p and p.exists() and (p / "Mods").exists():
                # 캐시 저장
                try:
                    APP_STATE.mkdir(parents=True, exist_ok=True)
                    _CONFIG_PATH.write_text(json.dumps({"sims_root": str(p)}, indent=2))
                except Exception:
                    pass
                return p
        except Exception:
            continue
    print("\n" + "=" * 60)
    print("  Sims 4 폴더를 찾을 수 없습니다.")
    print("=" * 60)
    print("  다음 중 하나를 시도해 주세요:")
    print("   1) 환경변수로 지정:  export SIMS4_PATH=\"/경로/The Sims 4\"")
    print("   2) 표준 위치에 설치: ~/Documents/Electronic Arts/The Sims 4")
    print("   3) 설정 파일 편집:   " + str(_CONFIG_PATH))
    print("      예: {\"sims_root\": \"/경로/The Sims 4\"}")
    print("=" * 60 + "\n")
    raise SystemExit(1)


SIMS_ROOT = find_sims_root()
MODS = SIMS_ROOT / "Mods"
CC_ROOT = MODS / "CC FeaturedCreators"
THUMBS_DIR = APP_STATE / "thumbs"
MANIFEST_PATH = APP_STATE / "manifest.json"
TRASH_DIR = APP_STATE / "trash"

# 옛 위치들 → 새 위치 마이그레이션
_OLD_STATE = SIMS_ROOT / ".cc_manager"
if _OLD_STATE.exists() and not APP_STATE.exists():
    APP_STATE.parent.mkdir(parents=True, exist_ok=True)
    import shutil as _shutil
    _shutil.move(str(_OLD_STATE), str(APP_STATE))

APP_STATE.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(exist_ok=True)
TRASH_DIR.mkdir(exist_ok=True)

# 옛 휴지통 (Mods 안) → 새 위치로 옮기기
_OLD_TRASH = MODS / ".cc_trash"
if _OLD_TRASH.exists():
    import shutil as _shutil
    for item in _OLD_TRASH.rglob("*"):
        if item.is_file():
            rel = item.relative_to(_OLD_TRASH)
            dst = TRASH_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                _shutil.move(str(item), str(dst))
    # 빈 폴더 정리
    for d in sorted([d for d in _OLD_TRASH.rglob("*") if d.is_dir()], key=lambda p: -len(p.parts)):
        try: d.rmdir()
        except OSError: pass
    try: _OLD_TRASH.rmdir()
    except OSError: pass

CAS_THUMB_TYPE = 0x3C1AF1F2       # CAS 썸네일 (표준, 작은 사이즈)
CAS_THUMB_MEDIUM = 0x0D338A3A     # CAS 썸네일 (중간)
CAS_THUMB_LARGE = 0x0D338A3B      # CAS 썸네일 (큼)
BUILDBUY_THUMB_TYPE = 0x08D31226  # Build/Buy 썸네일
GENERIC_THUMB = 0x00B2D882        # 일반 썸네일 리소스
PNG_TYPE = 0x2F7D0004             # 임베디드 PNG
CASP_TYPE = 0x034AEECB

# 썸네일로 인식할 모든 리소스 타입 (확장 후)
THUMB_TYPES = {CAS_THUMB_TYPE, CAS_THUMB_MEDIUM, CAS_THUMB_LARGE,
               BUILDBUY_THUMB_TYPE, GENERIC_THUMB, PNG_TYPE}

PORT = 8765

# CASP 내부 이름 → 카테고리 매핑 (심즈4 스튜디오 규칙)
# 예: "Hezeh_ymHair_..." 에서 "Hair" 추출
CASP_TYPE_MAP = {
    "hair":         ("헤어", "💇"),
    "hat":          ("모자", "🎩"),
    "head":         ("헤어", "💇"),  # 대부분 헤어 관련
    "top":          ("상의", "👕"),
    "body":         ("상의", "👕"),
    "shirt":        ("상의", "👕"),
    "bottom":       ("하의", "👖"),
    "pants":        ("하의", "👖"),
    "skirt":        ("하의", "👖"),
    "full":         ("전신", "👗"),
    "fullbody":     ("전신", "👗"),
    "outfit":       ("전신", "👗"),
    "dress":        ("전신", "👗"),
    "shoes":        ("신발", "👟"),
    "boots":        ("신발", "👟"),
    "sock":         ("신발", "👟"),
    "socks":        ("신발", "👟"),
    "skin":         ("스킨", "🧑"),
    "skintone":     ("스킨", "🧑"),
    "skindetail":   ("스킨디테일", "✨"),
    "freckledetail":("스킨디테일", "✨"),
    "eyes":         ("렌즈", "👁️"),
    "eye":          ("렌즈", "👁️"),
    "eyecolor":     ("렌즈", "👁️"),
    "eyebrow":      ("눈썹", "👁️‍🗨️"),
    "eyebrows":     ("눈썹", "👁️‍🗨️"),
    "eyelash":      ("눈속눈썹", "😊"),
    "eyelashes":    ("눈속눈썹", "😊"),
    "lips":         ("입술", "💋"),
    "lip":          ("입술", "💋"),
    "lipstick":     ("입술", "💋"),
    "lipgloss":     ("입술", "💋"),
    "blush":        ("메이크업", "💄"),
    "eyeliner":     ("메이크업", "💄"),
    "eyeshadow":    ("메이크업", "💄"),
    "mascara":      ("메이크업", "💄"),
    "makeup":       ("메이크업", "💄"),
    "facemakeup":   ("메이크업", "💄"),
    "eyemakeup":    ("메이크업", "💄"),
    "facepaint":    ("스킨디테일", "✨"),
    "contour":      ("메이크업", "💄"),
    "beard":        ("수염", "🧔"),
    "facialhair":   ("수염", "🧔"),
    "mustache":     ("수염", "🧔"),
    "necklace":     ("목걸이", "📿"),
    "neck":         ("목걸이", "📿"),
    "earring":      ("귀걸이", "💎"),
    "earrings":     ("귀걸이", "💎"),
    "ears":         ("귀걸이", "💎"),
    "ring":         ("반지", "💍"),
    "rings":        ("반지", "💍"),
    "ringl":        ("반지", "💍"),
    "ringr":        ("반지", "💍"),
    "bracelet":     ("팔찌/시계", "⌚"),
    "wrist":        ("팔찌/시계", "⌚"),
    "wristl":       ("팔찌/시계", "⌚"),
    "wristr":       ("팔찌/시계", "⌚"),
    "watch":        ("팔찌/시계", "⌚"),
    "forearm":      ("팔찌/시계", "⌚"),
    "forearml":     ("팔찌/시계", "⌚"),
    "forearmr":     ("팔찌/시계", "⌚"),
    "glasses":      ("안경", "👓"),
    "eyewear":      ("안경", "👓"),
    "sunglasses":   ("안경", "👓"),
    "bag":          ("가방", "👜"),
    "handbag":      ("가방", "👜"),
    "backpack":     ("가방", "👜"),
    "tattoo":       ("문신", "🎨"),
    "acc":          ("액세서리", "🎀"),
    "accessory":    ("액세서리", "🎀"),
    "tights":       ("신발", "👟"),
    "gloves":       ("액세서리", "🎀"),
    "teeth":        ("스킨디테일", "✨"),
    "nail":         ("액세서리", "🎀"),
    "nails":        ("액세서리", "🎀"),
}

# 내부 이름 패턴: 예) "Hezeh_ymHair_...", "obscurus_yuSkinDetail_..."
#   중간에 [소문자 1-3자][대문자시작+영문자] 패턴이 카테고리
import re
_CASP_NAME_RE = re.compile(r'[_-][a-z]{1,3}([A-Z][a-zA-Z]+?)[_0-9-]')


def extract_casp_type(name):
    """CASP 내부 이름에서 파트 타입 추출. 예: 'Hair', 'Bottom', 'SkinDetail'."""
    if not name:
        return None
    m = _CASP_NAME_RE.search(name)
    if m:
        return m.group(1)
    # 대체: 이름 안 어디에든 알려진 키워드 있는지
    lower = name.lower()
    for key in CASP_TYPE_MAP:
        if key in lower:
            return key
    return None

# ─────────── 카테고리 휴리스틱 ───────────
CATEGORIES = [
    # (표시 이름, 아이콘, 키워드 목록 [소문자 부분 매칭])
    ("헤어",       "💇", ["hair_", "hair no", "hair.", "hairstyle", "머리", "wig", "hair(", " hair", "hairline", "hairs_", "_hair", "untied", "tail_", "tails", "-tail"]),
    ("눈썹",       "👁️‍🗨️", ["eyebrow", "brow_", " brow", "eye brow"]),
    ("눈속눈썹",   "😊", ["eyelash", "lashes"]),
    # 메이크업을 먼저 (eyeliner/eyeshadow 등이 눈 카테고리로 잘못 가지 않게)
    ("입술",       "💋", ["lipstick", "lipgloss", "립스틱", "lip_", "lips_", "_lip", "_lips", " lip ", " lips", "lip("]),
    ("메이크업",   "💄", ["blush", "eyeliner", "eyeshadow", "mascara", "makeup", "liner_", " liner"]),
    # 눈(렌즈)는 아이라이너/섀도우 제외한 진짜 렌즈만
    ("렌즈",   "👁️", ["eyes_", "_eyes", "eye12", "eye ", "eyes.", "eye.", "eyes(", "eye(", "iris", "eyeball", "contact_lens", "contact lens", "sclera", "waterblue", "gpme-gold", "_lens"]),
    ("스킨",       "🧑", ["skintone", "skin_n", "skinoverlay", "skin overlay", "skin(", "_skin", "faceskin", "face skin"]),
    ("스킨디테일", "✨", ["skindetail", "skin detail", "freckle", "mole", "philtrum", "nose_mask", "nosemask", "contour", "dimple", "eyebag", "spot", "facepaint", "facemask", "face mask", "face_mask", "mouthcorner", "mouth details", "mouthdetail"]),
    ("수영복",     "👙", ["swim", "bikini", "swimsuit", "monokini", "swimwear"]),
    ("잠옷",       "🛌", ["pajama", "pyjama", "sleepwear", "nightgown", "nightwear"]),
    ("속옷",       "🩲", ["underwear", "bra_", " bra ", "panties", "thong", "boxer", "brief_"]),
    ("상의",       "👕", ["top(", " top ", "_top", "top.", "shirt", "sweater", "hoodie", "hoody", "sweat_top", "tee_", "tee(", "tee.", "tank", "blouse", "jacket", "coat", "cardigan", "knit", "turtle", "polo", "vest", "상의", "suits", " suit ", "suit_", "_suit"]),
    ("하의",       "👖", ["pants", "jean", "denim", "short(", "shorts", "skirt", "trouser", "legging", "slack", "sweatpants", "sweat pants", "치마", "바지", "culotte", "capri"]),
    ("전신",       "👗", ["dress", "outfit", "jumpsuit", "romper", "fullbody", "full body", "onepiece", "one piece", "hanbok", "-jhsuit"]),
    ("신발",       "👟", ["shoes", "boot", "sneaker", "sandal", "heel", "loafer", "wedge", "slipper", "flat.", "socks", "sock_", "신발", "mule"]),
    ("귀걸이",     "💎", ["earring", "earing", "ear ring", "piercing"]),
    ("목걸이",     "📿", ["necklace", "choker", "pendant", "chain_", " chain "]),
    ("반지",       "💍", ["ring_", " ring ", "ring.", "rings"]),
    ("팔찌/시계",  "⌚", ["bracelet", "watch", "wrist"]),
    ("안경",       "👓", ["glass", "eyewear", "sunglass", "goggle"]),
    ("모자",       "🎩", ["hat_", "hat.", " hat", "cap.", "cap_", " cap", "beanie", "beret"]),
    ("가방",       "👜", ["handbag", "backpack", "purse", "hipbag", "shoulderbag", "chestbag", "waistbag", "totebag", "clutch bag"]),
    ("수염",       "🧔", ["beard", "mustache", "stubble", "goatee"]),
    ("문신",       "🎨", ["tattoo", "tatoo"]),
    ("액세서리",   "🎀", ["accessory", "s4accessory", "brooch"]),
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


def categorize(filename, size=0):
    lower = filename.lower()
    matches = []
    for name, icon, keywords in CATEGORIES:
        for kw in keywords:
            if kw in lower:
                matches.append((name, icon))
                break
    if not matches:
        # 폴백 휴리스틱: 파일 크기·이름 힌트
        if size and size < 100 * 1024 and any(k in lower for k in ["slider", "preset"]):
            return [("슬라이더/프리셋", "🎚️")]
        return [("기타", "❓")]
    return matches

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
    """CASP 리소스에서 이름 문자열만 추출. 실패 시 None."""
    try:
        if len(data) < 14:
            return None
        version = struct.unpack_from("<I", data, 0)[0]
        preset_count = struct.unpack_from("<I", data, 8)[0]
        if preset_count > 0:
            return None  # 프리셋 있는 CASP는 skip
        # uint16 length (bytes) + UTF-16 LE string
        name_len = struct.unpack_from("<H", data, 12)[0]
        if name_len < 4 or name_len > 500 or 14 + name_len > len(data):
            return None
        name = data[14:14+name_len].decode("utf-16-le", errors="replace")
        return name
    except (struct.error, IndexError):
        return None


def get_casp_category(pkg_path):
    """패키지의 첫 CASP를 파싱해서 카테고리 반환. 없으면 None."""
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
        if casp_type:
            lower_type = casp_type.lower()
            if lower_type in CASP_TYPE_MAP:
                return CASP_TYPE_MAP[lower_type]
    return None


def extract_thumbs(pkg_path):
    """.package 안에서 썸네일 이미지(JPEG 또는 PNG) 추출. yield (data, hash, ext)"""
    seen = set()
    for entry in parse_dbpf(pkg_path):
        if entry["type"] not in THUMB_TYPES:
            continue
        try:
            data = read_resource(pkg_path, entry)
        except Exception:
            continue
        if data.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        else:
            continue
        h = hashlib.md5(data).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        yield (data, h, ext)


# ─────────── 스캔 ───────────

IGNORED_DIRS = {".cc_trash", "TMEX-Settings", "Tmex-Settings", ".git", "__MACOSX"}
IGNORED_DIR_PREFIXES = ("GSM ",)  # 스크립트 모드 폴더 (mccc, ww 등 내부는 CC 아님)


def _iter_packages():
    """Mods 아래 모든 .package 파일 순회 (제외 규칙 적용)."""
    if not MODS.exists():
        return
    for root, dirs, files in os.walk(MODS):
        # 제외 폴더 정리
        dirs[:] = [
            d for d in dirs
            if d not in IGNORED_DIRS
            and not any(d.startswith(p) for p in IGNORED_DIR_PREFIXES)
        ]
        for f in files:
            if f.lower().endswith(".package"):
                yield Path(root) / f


SCAN_PROGRESS = {"active": False, "current": 0, "total": 0, "name": ""}


def scan_cc(progress_cb=None):
    """전체 Mods 폴더 스캔 → 폴더 경로별 그룹."""
    if not MODS.exists():
        return {"folders": [], "error": f"Mods 폴더 없음: {MODS}"}
    SCAN_PROGRESS.update({"active": True, "current": 0, "total": 0, "name": "준비 중..."})

    # 폴더별 파일 그룹핑 - key는 Mods 기준 상대 폴더 경로
    from collections import defaultdict
    groups = defaultdict(list)
    all_pkgs = list(_iter_packages())
    total_pkgs = len(all_pkgs)
    total_thumbs = 0
    overrides = _load_overrides()

    for i, pkg in enumerate(all_pkgs, 1):
        rel_pkg = pkg.relative_to(MODS)
        folder = str(rel_pkg.parent) if rel_pkg.parent != Path(".") else "(최상위)"
        SCAN_PROGRESS["current"] = i
        SCAN_PROGRESS["total"] = total_pkgs
        SCAN_PROGRESS["name"] = folder
        if progress_cb and (i % 100 == 0 or i == total_pkgs):
            progress_cb(i, total_pkgs, folder)

        # 카테고리 결정
        # 1) 수동 오버라이드 우선
        rel_pkg_str = str(rel_pkg)
        override_cat = overrides.get(rel_pkg_str)
        if override_cat:
            # 오버라이드 카테고리에 해당하는 아이콘 찾기
            icon = "📝"
            for name, ic, _ in CATEGORIES:
                if name == override_cat:
                    icon = ic; break
            cats = [(override_cat, icon)]
            is_override = True
            is_casp = False
        else:
            is_override = False
            casp_cat = get_casp_category(pkg)
            if casp_cat:
                cats = [casp_cat]
                is_casp = True
            else:
                cats = categorize(pkg.name, pkg.stat().st_size)
                is_casp = False
            # 파일명에서 추가 카테고리도 병합 (필터용, primary는 첫 번째만)
            fn_cats = categorize(pkg.name, pkg.stat().st_size)
            for fc in fn_cats:
                if fc[0] not in [c[0] for c in cats] and fc[0] != "기타":
                    cats.append(fc)

        stat = pkg.stat()
        item = {
            "file": pkg.name,
            "path": rel_pkg_str,
            "size": stat.st_size,
            "mtime": stat.st_mtime,   # 파일 수정 시각 (최근순/오래된순 정렬용)
            "thumbs": [],
            "cats": [c[0] for c in cats],
            "primary_cat": cats[0][0],
            "cat_icon": cats[0][1],
            "casp": is_casp,
            "override": is_override,
        }
        for img_bytes, h, ext in extract_thumbs(pkg):
            thumb_name = f"{h[:16]}{ext}"
            thumb_path = THUMBS_DIR / thumb_name
            if not thumb_path.exists():
                thumb_path.write_bytes(img_bytes)
            item["thumbs"].append(thumb_name)
            total_thumbs += 1
        groups[folder].append(item)

    folders = [{"name": name, "items": sorted(items, key=lambda x: x["file"])}
               for name, items in sorted(groups.items())]

    manifest = {
        "folders": folders,
        "creators": folders,  # 하위호환 alias
        "total_pkgs": total_pkgs,
        "total_thumbs": total_thumbs,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False))
    SCAN_PROGRESS.update({"active": False, "current": total_pkgs, "total": total_pkgs, "name": "완료"})
    return manifest


def load_manifest():
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return None


# ─────────── 파일 조작 ───────────

TRASH_MANIFEST_PATH = APP_STATE / "trash_manifest.json"
OVERRIDES_PATH = APP_STATE / "category_overrides.json"


def _load_overrides():
    if OVERRIDES_PATH.exists():
        try:
            return json.loads(OVERRIDES_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_overrides(m):
    OVERRIDES_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=2))


def _compute_stats():
    m = load_manifest() or {"folders": [], "creators": []}
    folders = m.get("folders") or m.get("creators") or []
    trash_manifest = _load_trash_manifest()
    total_files = 0; total_size = 0
    creators = {}   # name -> {size, count}
    cats = {}       # cat -> {count, size}
    newest = 0; oldest = float("inf")
    for f in folders:
        for it in f.get("items", []):
            if it.get("trashed") or it.get("perma_deleted"): continue
            total_files += 1
            sz = it.get("size", 0); total_size += sz
            cname = f.get("name", "?")
            c = creators.setdefault(cname, {"size": 0, "count": 0})
            c["size"] += sz; c["count"] += 1
            for cat in (it.get("cats") or [it.get("primary_cat") or "기타"]):
                cc = cats.setdefault(cat, {"count": 0, "size": 0})
                cc["count"] += 1; cc["size"] += sz
            mt = it.get("mtime", 0)
            if mt: newest = max(newest, mt); oldest = min(oldest, mt)
    trash_items = list(trash_manifest.values()) if isinstance(trash_manifest, dict) else []
    trash_count = sum(1 for x in trash_items if not x.get("perma_deleted"))
    trash_size = sum(x.get("size", 0) for x in trash_items if not x.get("perma_deleted"))
    top_creators = sorted(
        ({"name": n, "size": v["size"], "count": v["count"]} for n, v in creators.items()),
        key=lambda x: -x["size"]
    )[:10]
    cat_breakdown = sorted(
        ({"name": n, "count": v["count"], "size": v["size"]} for n, v in cats.items()),
        key=lambda x: -x["count"]
    )
    total_thumbs = sum(len(it.get("thumbs", [])) for f in folders for it in f.get("items", []))
    avg_size = (total_size / total_files) if total_files else 0
    return {
        "total_files": total_files,
        "total_size": total_size,
        "avg_size": avg_size,
        "total_thumbs": total_thumbs,
        "creator_count": len(creators),
        "top_creators": top_creators,
        "categories": cat_breakdown,
        "trash_count": trash_count,
        "trash_size": trash_size,
        "overrides_count": len(_load_overrides()),
        "newest_mtime": newest,
        "oldest_mtime": oldest if oldest != float("inf") else 0,
    }


def set_category_override(rel_path, category):
    """수동으로 카테고리 지정 (또는 해제)"""
    overrides = _load_overrides()
    if category is None or category == "":
        overrides.pop(rel_path, None)
    else:
        overrides[rel_path] = category
    _save_overrides(overrides)
    return {"ok": True}


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
    """현재 manifest에서 이 파일의 썸네일 목록 찾기."""
    m = load_manifest()
    if not m:
        return []
    for c in m.get("creators", []):
        for it in c.get("items", []):
            if it["path"] == path:
                return it.get("thumbs", [])
    return []


def _update_manifest_items(paths, updates):
    """매니페스트의 특정 파일들에 필드 업데이트 (재스캔 없이 즉시 반영)."""
    m = load_manifest()
    if not m: return
    path_set = set(paths)
    trash_rel_set = set()  # 휴지통 안 상대 경로도 매칭용
    for folder in m.get("folders", []) + m.get("creators", []):
        for it in folder.get("items", []):
            if it["path"] in path_set or it.get("trash_path") in path_set:
                for k, v in updates.items():
                    it[k] = v
    MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))


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
                candidate = dst.with_stem(f"{dst.stem}_{i}")
                if not candidate.exists():
                    dst = candidate
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
    # 매니페스트에 상태 반영 (재스캔 안 해도 목록에 표시됨)
    if moved:
        _update_manifest_items(moved, {"trashed": True, "perma_deleted": False})
    return {"moved": moved, "failed": failed}


def restore_from_trash(rel_paths):
    restored, failed = [], []
    restored_originals = []
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
            restored_originals.append(original)
        except Exception as e:
            failed.append({"path": rel, "reason": str(e)})
    _save_trash_manifest(trash_m)
    # 복원 → trashed 플래그 제거
    if restored_originals:
        _update_manifest_items(restored_originals, {"trashed": False, "perma_deleted": False})
    return {"restored": restored, "failed": failed}


def list_trash():
    items = []
    total_size = 0
    trash_m = _load_trash_manifest()
    dirty = False
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            rel = str(f.relative_to(TRASH_DIR))
            sz = f.stat().st_size
            info = trash_m.get(rel, {})
            thumbs = info.get("thumbs", [])
            # 트래시 매니페스트에 썸네일 정보 없으면 파일에서 직접 뽑기
            if not thumbs and f.suffix.lower() == ".package":
                extracted = []
                for img_bytes, h, ext in extract_thumbs(f):
                    thumb_name = f"{h[:16]}{ext}"
                    thumb_path = THUMBS_DIR / thumb_name
                    if not thumb_path.exists():
                        thumb_path.write_bytes(img_bytes)
                    extracted.append(thumb_name)
                thumbs = extracted
                # 매니페스트 업데이트
                trash_m[rel] = {
                    "original_path": info.get("original_path", rel),
                    "thumbs": thumbs,
                    "size": sz,
                }
                dirty = True
            items.append({
                "path": rel,
                "size": sz,
                "thumbs": thumbs,
                "original_path": info.get("original_path", rel),
            })
            total_size += sz
    if dirty:
        _save_trash_manifest(trash_m)
    return {"items": items, "total_size": total_size}


def delete_from_trash(rel_paths):
    """휴지통 안의 특정 파일들만 완전 삭제"""
    count = 0
    total = 0
    perma_originals = []
    trash_m = _load_trash_manifest()
    for rel in rel_paths:
        fp = TRASH_DIR / rel
        if fp.exists() and fp.is_file():
            try:
                total += fp.stat().st_size
                fp.unlink()
                count += 1
                info = trash_m.pop(rel, {})
                orig = info.get("original_path", rel)
                perma_originals.append(orig)
            except OSError:
                pass
    _save_trash_manifest(trash_m)
    if perma_originals:
        _update_manifest_items(perma_originals, {"trashed": False, "perma_deleted": True})
    return {"count": count, "size_freed": total}


def empty_trash():
    count = 0
    total = 0
    trash_m = _load_trash_manifest()
    perma_originals = [info.get("original_path", rel) for rel, info in trash_m.items()]
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            f.unlink()
            count += 1
    # 빈 폴더 제거
    for d in sorted([d for d in TRASH_DIR.rglob("*") if d.is_dir()], key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    _save_trash_manifest({})
    # 매니페스트에 perma_deleted 반영
    if perma_originals:
        _update_manifest_items(perma_originals, {"trashed": False, "perma_deleted": True})
    return {"count": count, "size_freed": total}


# ─────────── HTTP 서버 ───────────

HTML_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Sims 4 CC Manager</title>
<link rel="stylesheet" as="style" crossorigin href="https://cdn.jsdelivr.net/gh/sun-typeface/SUITE/fonts/static/woff2/SUITE.css">
<style>
  * { box-sizing: border-box; }
  /* ─── 디자인 토큰 ─── */
  :root {
    --c-blue: #3b82f6;
    --c-blue-hover: #2563eb;
    --c-red: #ef4444;
    --c-red-hover: #dc2626;
    --c-red-bg: #fef2f2;
    --c-blue-bg: #eff6ff;
    --c-bg: #f9fafb;
    --c-surface: #ffffff;
    --c-border: #e5e7eb;
    --c-border-strong: #d1d5db;
    --c-text: #111827;
    --c-text-muted: #6b7280;
    --c-text-subtle: #9ca3af;
    --radius-sm: 6px;
    --radius-md: 8px;
    --radius-lg: 10px;
    --h-input: 30px;
    --h-thumb: 130px;
    --shadow-sm: 0 1px 2px rgba(0,0,0,.04);
    --shadow-md: 0 4px 12px rgba(0,0,0,.08);
    --shadow-lg: 0 8px 24px rgba(0,0,0,.12);
  }
  /* SUITE 폰트 모든 요소 */
  body, button, input, select, textarea, optgroup, option, kbd { font-family: 'SUITE Variable', 'SUITE', -apple-system, BlinkMacSystemFont, sans-serif; }
  input::placeholder { font-family: inherit; color: var(--c-text-subtle); }
  body { margin: 0; background: var(--c-bg); color: var(--c-text); font-size: 13px; }
  header { position: sticky; top: 0; background: var(--c-surface); border-bottom: 1px solid var(--c-border); padding: 6px 16px; z-index: 100; box-shadow: var(--shadow-sm); }
  .title-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 2px 0; }
  .title-row h1 { margin: 0; font-size: 15px; flex-shrink: 0; white-space: nowrap; }
  .stats { color: var(--c-text-muted); font-size: 11px; flex: 1 1 auto; min-width: 0; word-break: keep-all; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* 반응형: 좁을수록 접기 */
  @media (max-width: 1200px) {
    .stats { flex-basis: 100%; order: 10; padding: 2px 0 0; }
  }
  @media (max-width: 900px) {
    #modeSeg button { padding: 0 10px !important; font-size: 11px !important; }
    button { padding: 0 10px; font-size: 11px; }
    /* 라벨은 유지 - 사용자가 뭔지 알아야 함 */
  }
  @media (max-width: 640px) {
    header { padding: 6px 10px; }
    .row { padding: 4px 0; }
    #search { width: 100% !important; }
    #footer { flex-wrap: wrap; height: auto; padding: 6px 10px; }
    #bulkBar { flex-wrap: wrap; padding: 6px 10px; }
    .hide-narrow { display: none; }
    .show-narrow-only { display: flex !important; }
  }
  .show-narrow-only { display: none; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; padding: 5px 0; border-top: 1px solid var(--c-border); }
  .row:first-of-type { border-top: none; padding-top: 3px; }
  .group { display: inline-flex; align-items: center; gap: 6px; background: var(--c-bg); padding: 4px 10px; border-radius: var(--radius-sm); height: var(--h-input); }
  .group .label { color: var(--c-text-muted); font-size: 11px; margin: 0; }
  .divider { width: 1px; height: 20px; background: var(--c-border); margin: 0 4px; }
  .toggle-group { display: inline-flex; align-items: center; gap: 12px; }

  /* ─── 통일된 폼 컨트롤 (모두 32px 높이) ─── */
  input[type=text], input[type=search] {
    height: var(--h-input); padding: 0 12px; border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); font-size: 13px; background: var(--c-surface);
    color: var(--c-text); transition: border-color .12s, box-shadow .12s;
  }
  input[type=text]:focus, input[type=search]:focus {
    outline: none; border-color: var(--c-blue); box-shadow: 0 0 0 3px rgba(59,130,246,.15);
  }
  select {
    height: var(--h-input); padding: 0 28px 0 10px; border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); font-size: 12px; background: var(--c-surface);
    color: var(--c-text); cursor: pointer;
    appearance: none; -webkit-appearance: none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'><path d='M2 4 L5 7 L8 4' stroke='%236b7280' stroke-width='1.4' fill='none'/></svg>");
    background-repeat: no-repeat; background-position: right 10px center;
  }
  select:focus { outline: none; border-color: var(--c-blue); box-shadow: 0 0 0 3px rgba(59,130,246,.15); }

  /* ─── 통일된 버튼 (모두 32px 높이) ─── */
  button {
    height: var(--h-input); padding: 0 14px; font-size: 12px; font-weight: 500;
    background: var(--c-surface); border: 1px solid var(--c-border-strong);
    border-radius: var(--radius-sm); cursor: pointer; color: var(--c-text);
    transition: background-color .12s, border-color .12s, transform .06s;
    display: inline-flex; align-items: center; gap: 5px; white-space: nowrap;
  }
  button:hover { background: var(--c-bg); border-color: var(--c-text-muted); }
  button:active { transform: translateY(1px); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.primary { background: var(--c-red); color: white; border-color: var(--c-red-hover); }
  button.primary:hover { background: var(--c-red-hover); border-color: var(--c-red-hover); }
  button.blue { background: var(--c-blue); color: white; border-color: var(--c-blue-hover); }
  button.blue:hover { background: var(--c-blue-hover); border-color: var(--c-blue-hover); }
  button.icon-only { width: var(--h-input); padding: 0; justify-content: center; font-size: 15px; border-radius: 50%; }
  /* 오버플로우 (⋯) 메뉴 */
  .menu-wrap { position: relative; display: inline-block; }
  .overflow-menu { display: none; position: absolute; top: calc(100% + 4px); right: 0; background: var(--c-surface); border: 1px solid var(--c-border); border-radius: var(--radius-md); box-shadow: var(--shadow-lg); padding: 4px; z-index: 300; min-width: 160px; }
  .overflow-menu.open { display: block; }
  .overflow-menu button { display: flex; width: 100%; justify-content: flex-start; border: none; background: transparent; padding: 8px 12px; border-radius: 4px; text-align: left; height: auto; }
  .overflow-menu button:hover { background: var(--c-bg); }
  .overflow-menu hr { border: 0; border-top: 1px solid var(--c-border); margin: 4px 0; }
  /* 헤더 접기 상태: 첫 title-row + stats 만 보임 */
  body.header-collapsed header > .row { display: none; }

  /* Segmented toggle */
  .seg { display: inline-flex; height: var(--h-input); border: 1px solid var(--c-border-strong); border-radius: var(--radius-sm); overflow: hidden; background: var(--c-surface); }
  .seg button { border: none; border-radius: 0; padding: 0 14px; background: transparent; font-size: 12px; height: 100%; color: var(--c-text-muted); }
  .seg button + button { border-left: 1px solid var(--c-border); }
  .seg button:hover { background: var(--c-bg); }
  .seg button.on { background: var(--c-text); color: white; }
  /* 모드 토글: 삭제 = 빨강, 카테고리 편집 = 파랑 */
  #modeSeg { border: 1.5px solid var(--c-red) !important; background: var(--c-red-bg) !important; box-shadow: 0 0 0 2px rgba(239,68,68,.1); }
  #modeSeg button { padding: 0 14px !important; font-size: 12px !important; font-weight: 600 !important; border-left: none !important; }
  #modeSeg button.on { background: var(--c-red) !important; color: white !important; }
  #modeSeg button:not(.on) { background: transparent !important; color: var(--c-red) !important; }
  body.mode-category #modeSeg { border-color: var(--c-blue) !important; background: var(--c-blue-bg) !important; box-shadow: 0 0 0 2px rgba(59,130,246,.1); }
  body.mode-category #modeSeg button:not(.on) { color: var(--c-blue) !important; }
  body.mode-category #modeSeg button.on { background: var(--c-blue) !important; color: white !important; }
  /* Switch toggle */
  .switch { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #444; cursor: pointer; user-select: none; }
  .switch input { appearance: none; -webkit-appearance: none; width: 32px; height: 18px; background: #ccc; border-radius: 10px; position: relative; cursor: pointer; transition: background .2s; margin: 0; }
  .switch input:checked { background: #4a90e2; }
  .switch input::before { content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; background: white; border-radius: 50%; transition: transform .2s; }
  .switch input:checked::before { transform: translateX(14px); }
  /* Chip filter */
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { padding: 5px 12px; border-radius: 16px; background: var(--c-surface); border: 1px solid var(--c-border-strong); cursor: pointer; font-size: 12px; display: inline-flex; align-items: center; gap: 5px; transition: all .12s; color: var(--c-text); }
  .chip:hover { background: var(--c-bg); border-color: var(--c-text-muted); }
  .chip.on { background: var(--c-blue); color: white; border-color: var(--c-blue-hover); }
  .chip .count { opacity: .7; font-size: 11px; }
  .subchips { padding-left: 16px; margin-top: 4px; padding-top: 4px; border-top: 1px dashed #e0e0e0; }
  .subchip { padding: 3px 8px; border-radius: 12px; background: #f4f4f4; border: 1px solid #e0e0e0; cursor: pointer; font-size: 11px; }
  .subchip:hover { background: #e8e8e8; }
  .subchip.on { background: #333; color: white; border-color: #333; }
  .label { font-size: 11px; color: var(--c-text-muted); margin-right: 4px; }
  main { padding: 16px; }
  .creator { background: var(--c-surface); margin-bottom: 12px; border-radius: var(--radius-md); overflow: hidden; box-shadow: var(--shadow-sm); border: 1px solid var(--c-border); }
  .creator-header { padding: 12px 16px; background: var(--c-bg); border-bottom: 1px solid var(--c-border); display: flex; align-items: center; gap: 12px; cursor: pointer; user-select: none; }
  .creator-header h2 { margin: 0; font-size: 14px; font-weight: 600; flex: 1; }
  .creator-count { color: var(--c-text-muted); font-size: 12px; }
  .creator-actions { display: flex; gap: 6px; }
  .creator-actions button { height: 26px !important; padding: 0 10px !important; font-size: 11px !important; }
  .grid { padding: 10px; display: grid; grid-template-columns: repeat(auto-fill, minmax(var(--h-thumb), 1fr)); gap: 6px; align-items: start; }
  .item { position: relative; border: 2px solid transparent; border-radius: 4px; cursor: pointer; background: #f9f9f9; overflow: visible; content-visibility: auto; contain-intrinsic-size: 200px 180px; }
  .item .thumb-img, .item .no-thumb { border-radius: 2px 2px 0 0; overflow: hidden; }
  .item:hover { border-color: #4a90e2; z-index: 10; }
  .item.marked { border-color: #d9534f; background: #ffe8e8; box-shadow: 0 0 0 1px #d9534f inset; }
  .item.marked .thumb-img, .item.marked .no-thumb { filter: brightness(0.85) saturate(0.7); }
  .item.marked::after {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    background: linear-gradient(rgba(217,83,79,.15), rgba(217,83,79,.25));
    pointer-events: none; border-radius: 2px;
  }
  body.mode-category .item:not(.trashed):hover { border-color: #4a90e2; cursor: cell; }
  body.mode-category .item:not(.trashed).marked { opacity: 0.5; }
  .item.selected { border-color: #4a90e2 !important; box-shadow: 0 0 0 2px #4a90e2, 0 0 8px rgba(74,144,226,.5); }
  .item.selected::before { content: '✓'; position: absolute; top: 4px; left: 4px; background: #4a90e2; color: white; width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: bold; z-index: 3; box-shadow: 0 1px 2px rgba(0,0,0,.2); }
  /* 삭제표시 + 다중선택 둘 다: 파란 테두리 + 빨간 배경 tint 만 (별도 배지 X) */
  .item.marked.selected { background: #ffe8e8 !important; }
  .item.kb-focus { outline: 2px dashed #4a90e2; outline-offset: 2px; }
  .item.marked.selected .thumb-img { filter: brightness(0.9) saturate(0.85); }
  .chip.drop-target { background: #ffe066 !important; color: #333 !important; border-color: #f0a500 !important; transform: scale(1.1); }
  .item.dragging { opacity: 0.4; }
  #bulkBar { position: fixed; bottom: 44px; left: 0; right: 0; background: #333; color: white; padding: 8px 16px; z-index: 99; display: none; align-items: center; gap: 12px; box-shadow: 0 -2px 8px rgba(0,0,0,.2); }
  #footer { height: 44px; padding: 0 16px; }
  body { padding-bottom: 60px; }
  body.has-bulk { padding-bottom: 108px; }
  #bulkBar select { padding: 5px 8px; border-radius: 4px; }
  #bulkBar .info { flex: 1; font-size: 14px; }
  .item.trashed { opacity: 0.6; border-color: #999; background: #f0f0f0; }
  .item.trashed .thumb-img { filter: grayscale(0.7); }
  .item.perma-deleted { opacity: 0.4; border-color: #600; background: #f5eaea; }
  .item.perma-deleted .thumb-img { filter: grayscale(1) brightness(0.6); }
  .item.perma-deleted .name, .item.perma-deleted .name-full { text-decoration: line-through; color: #999; }
  .trash-badge { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.75); color: white; padding: 2px 6px; border-radius: 3px; font-size: 10px; z-index: 3; }
  /* 카테고리 아이콘: 썸네일 이미지 안쪽 우하단 (이름과 안 겹침) */
  .cat-icon { position: absolute; top: 4px; right: 4px; background: rgba(255,255,255,.92); padding: 2px 6px; border-radius: 10px; font-size: 13px; box-shadow: 0 1px 3px rgba(0,0,0,.2); cursor: context-menu; z-index: 2; transition: opacity .15s; }
  .item:hover .cat-icon { opacity: 0.2; }  /* hover 시 흐리게 → thumb nav 안 가림 */
  .item:hover .cat-icon:hover { opacity: 1; }  /* 아이콘 자체 hover 시 다시 진하게 */
  .cat-icon.override { background: #ffd700; box-shadow: 0 0 0 1px #b8860b; }
  #ctxMenu { position: fixed; background: white; border: 1px solid #ccc; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,.15); padding: 4px; z-index: 1000; max-height: 400px; overflow-y: auto; min-width: 180px; display: none; }
  #ctxMenu .ctx-header { padding: 4px 8px; font-size: 11px; color: #666; border-bottom: 1px solid #eee; margin-bottom: 4px; }
  #ctxMenu .ctx-item { padding: 5px 10px; font-size: 13px; cursor: pointer; border-radius: 4px; display: flex; align-items: center; gap: 6px; }
  #ctxMenu .ctx-item:hover { background: #f4f4f4; }
  #ctxMenu .ctx-item.current { background: #e8f4ff; font-weight: 600; }
  #ctxMenu .ctx-item.reset { border-top: 1px solid #eee; margin-top: 4px; padding-top: 8px; color: #d9534f; }
  .creator-sub { padding: 0 6px 2px; font-size: 9px; color: #999; font-style: italic; }
  .item img { display: block; width: 100%; height: auto; }
  .item .no-thumb { padding: 40px 8px; text-align: center; color: #999; font-size: 11px; background: #eee; }
  /* 기본: 2줄까지 잘리지 않고 항상 완전히 보임. 한 줄이면 한 줄 높이 유지 (min-height 없음). */
  .item .name { padding: 3px 6px; font-size: 10px; color: #555; word-break: break-all; line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; text-overflow: ellipsis; }
  /* 3줄 이상 긴 이름만 hover 시 팝오버로 위쪽 확장 - 원본은 그대로 유지, 그 위에 덮음 */
  .item .name-full { display: none; position: absolute; bottom: 0; left: 0; right: 0; padding: 3px 6px; font-size: 10px; color: #222; word-break: break-all; line-height: 1.35; background: white; z-index: 100; box-shadow: 0 -2px 6px rgba(0,0,0,.15); border-radius: 0 0 2px 2px; }
  /* JS로 짧은 이름은 name-full 을 아예 안 그리게 함 */
  .item:hover .name-full { display: block; }
  .item .sz { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.6); color: white; padding: 1px 5px; font-size: 10px; border-radius: 3px; pointer-events: none; z-index: 2; }
  .item.selected::before { left: 4px; }
  .item.selected .sz { display: none; }
  .thumb-img, .no-thumb { cursor: pointer; }
  /* Thumb nav은 썸네일 아래쪽 (이름 바로 위) */
  .thumb-nav { position: absolute; bottom: 32px; left: 50%; transform: translateX(-50%); display: flex; align-items: center; gap: 4px; opacity: 0; transition: opacity .15s; pointer-events: none; background: rgba(0,0,0,.65); border-radius: 14px; padding: 2px 4px; }
  .item:hover .thumb-nav { opacity: 1; pointer-events: auto; }
  .thumb-btn { background: rgba(0,0,0,.6); color: white; border: none; border-radius: 50%; width: 22px; height: 22px; font-size: 10px; padding: 0; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .thumb-btn:hover { background: rgba(0,0,0,.85); }
  .thumb-counter { background: rgba(0,0,0,.6); color: white; padding: 2px 6px; border-radius: 10px; font-size: 10px; font-variant-numeric: tabular-nums; }
  .collapsed .grid, .collapsed .creator-actions { display: none; }
  #footer { position: fixed; bottom: 0; left: 0; right: 0; background: white; border-top: 1px solid #ddd; display: flex; align-items: center; gap: 12px; z-index: 100; box-shadow: 0 -2px 4px rgba(0,0,0,.05); }
  #footer .count { flex: 1; font-size: 14px; }
  #footer .count b { color: #d9534f; }
  .toast { position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 10px 20px; border-radius: 6px; opacity: 0; transition: opacity .3s; z-index: 200; }
  .toast.show { opacity: 1; }
  .badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; margin-left: 4px; }
  .badge.warn { background: #fff3cd; color: #856404; }
  .progress { background: #f0f0f0; padding: 20px; border-radius: 8px; margin: 40px auto; max-width: 500px; text-align: center; }
  .progress-bar { height: 8px; background: #ddd; border-radius: 4px; overflow: hidden; margin-top: 12px; }
  .progress-bar > div { height: 100%; background: #4a90e2; transition: width .3s; }
  dialog { border: none; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,.2); padding: 20px; max-width: 500px; }
  dialog::backdrop { background: rgba(0,0,0,.5); }
  /* 모달 우측 상단 X 닫기 버튼 통일 스타일 */
  .dlg-close { position: absolute; top: 10px; right: 10px; width: 28px; height: 28px; padding: 0; font-size: 14px; border-radius: 50%; background: white; border: 1px solid #ddd; cursor: pointer; z-index: 10; }
  kbd { background: #f0f0f0; border: 1px solid #ccc; border-bottom-width: 2px; border-radius: 3px; padding: 1px 6px; font-size: 11px; font-family: -apple-system, monospace; }
  .dlg-close:hover { background: #f4f4f4; }
  dialog { position: relative; }
  .trash-item { padding: 6px 8px; border-bottom: 1px solid #eee; display: flex; gap: 8px; font-size: 12px; align-items: center; }
  .trash-item input { margin: 0; }
  /* Dark mode 규칙 */
  html[data-theme="light"] { color-scheme: light; }
  html[data-theme="dark"] { color-scheme: dark; }
  /* 라이트 강제 시 auto 다크 media 규칙 무시하도록 (우선순위 이용 - 아래 media 규칙에 :not() 적용은 각 규칙마다 못 넣으니 대신 명시적 다크 블록만 데이터-속성 기반, media 는 auto 만) */
  @media (prefers-color-scheme: dark) {
    html[data-theme="light"] body,
    html[data-theme="light"] header,
    html[data-theme="light"] dialog,
    html[data-theme="light"] .item,
    html[data-theme="light"] .creator { background: revert !important; color: revert !important; border-color: revert !important; }
  }
  @media (prefers-color-scheme: dark) {
    body { background: #1a1a1a; color: #e0e0e0; }
    header { background: #242424; border-bottom-color: #333; box-shadow: 0 2px 4px rgba(0,0,0,.3); }
    .row { border-top-color: #2a2a2a; }
    .stats { color: #999; }
    .group { background: #2a2a2a; }
    .group .label, .label { color: #888; }
    .divider { background: #333; }
    input[type=search], select, button { background: #2a2a2a; color: #e0e0e0; border-color: #444; }
    button:hover { background: #333; }
    .seg { border-color: #444; }
    .seg button { background: #2a2a2a; color: #e0e0e0; }
    .seg button + button { border-left-color: #444; }
    .seg button.on { background: #555; }
    .chip { background: #2a2a2a; color: #e0e0e0; border-color: #444; }
    .chip:hover { background: #333; }
    .subchip { background: #333; color: #e0e0e0; border-color: #444; }
    .subchip:hover { background: #3a3a3a; }
    .creator { background: #242424; box-shadow: 0 1px 3px rgba(0,0,0,.3); }
    .creator-header { background: #2a2a2a; border-bottom-color: #333; }
    .creator-count { color: #999; }
    .item { background: #2a2a2a; }
    .item .name { color: #bbb; }
    .item .name-full { background: #333; color: #e0e0e0; box-shadow: 0 -2px 6px rgba(0,0,0,.4); }
    .item .no-thumb { background: #333; color: #888; }
    .cat-icon { background: rgba(50,50,50,.92); color: #e0e0e0; }
    .item.trashed { background: #2a2a2a; border-color: #666; }
    .item.perma-deleted { background: #3a2a2a; }
    dialog { background: #242424; color: #e0e0e0; }
    .dlg-close { background: #2a2a2a; border-color: #444; color: #e0e0e0; }
    .dlg-close:hover { background: #333; }
    .progress { background: #2a2a2a; color: #e0e0e0; }
    .progress-bar { background: #333; }
    #ctxMenu { background: #2a2a2a; border-color: #444; color: #e0e0e0; }
    #ctxMenu .ctx-header { color: #888; border-bottom-color: #333; }
    #ctxMenu .ctx-item:hover { background: #333; }
    #ctxMenu .ctx-item.current { background: #1e3a5f; }
    #ctxMenu .ctx-item.reset { border-top-color: #333; }
    .creator-sub { color: #777; }
    kbd { background: #333; border-color: #555; color: #e0e0e0; }
    .trash-item { border-bottom-color: #333; }
    /* 유지: 표시된 아이템 색상 유지 */
    .item.marked { border-color: #d9534f; background: #3a1e1e; }
    .item.marked.selected { background: #3a1e1e !important; }
  }
  /* 명시적 다크 모드 (설정에서 선택 시) */

    html[data-theme="dark"] body { background: #1a1a1a; color: #e0e0e0; }
    html[data-theme="dark"] header { background: #242424; border-bottom-color: #333; box-shadow: 0 2px 4px rgba(0,0,0,.3); }
    html[data-theme="dark"] .row { border-top-color: #2a2a2a; }
    html[data-theme="dark"] .stats { color: #999; }
    html[data-theme="dark"] .group { background: #2a2a2a; }
    html[data-theme="dark"] .group .label, html[data-theme="dark"] .label { color: #888; }
    html[data-theme="dark"] .divider { background: #333; }
    html[data-theme="dark"] input[type=search], html[data-theme="dark"] select, html[data-theme="dark"] button { background: #2a2a2a; color: #e0e0e0; border-color: #444; }
    html[data-theme="dark"] button:hover { background: #333; }
    html[data-theme="dark"] .seg { border-color: #444; }
    html[data-theme="dark"] .seg button { background: #2a2a2a; color: #e0e0e0; }
    html[data-theme="dark"] .seg button + button { border-left-color: #444; }
    html[data-theme="dark"] .seg button.on { background: #555; }
    html[data-theme="dark"] .chip { background: #2a2a2a; color: #e0e0e0; border-color: #444; }
    html[data-theme="dark"] .chip:hover { background: #333; }
    html[data-theme="dark"] .subchip { background: #333; color: #e0e0e0; border-color: #444; }
    html[data-theme="dark"] .subchip:hover { background: #3a3a3a; }
    html[data-theme="dark"] .creator { background: #242424; box-shadow: 0 1px 3px rgba(0,0,0,.3); }
    html[data-theme="dark"] .creator-header { background: #2a2a2a; border-bottom-color: #333; }
    html[data-theme="dark"] .creator-count { color: #999; }
    html[data-theme="dark"] .item { background: #2a2a2a; }
    html[data-theme="dark"] .item .name { color: #bbb; }
    html[data-theme="dark"] .item .name-full { background: #333; color: #e0e0e0; box-shadow: 0 -2px 6px rgba(0,0,0,.4); }
    html[data-theme="dark"] .item .no-thumb { background: #333; color: #888; }
    html[data-theme="dark"] .cat-icon { background: rgba(50,50,50,.92); color: #e0e0e0; }
    html[data-theme="dark"] .item.trashed { background: #2a2a2a; border-color: #666; }
    html[data-theme="dark"] .item.perma-deleted { background: #3a2a2a; }
    html[data-theme="dark"] dialog { background: #242424; color: #e0e0e0; }
    html[data-theme="dark"] .dlg-close { background: #2a2a2a; border-color: #444; color: #e0e0e0; }
    html[data-theme="dark"] .dlg-close:hover { background: #333; }
    html[data-theme="dark"] .progress { background: #2a2a2a; color: #e0e0e0; }
    html[data-theme="dark"] .progress-bar { background: #333; }
    html[data-theme="dark"] #ctxMenu { background: #2a2a2a; border-color: #444; color: #e0e0e0; }
    html[data-theme="dark"] #ctxMenu .ctx-header { color: #888; border-bottom-color: #333; }
    html[data-theme="dark"] #ctxMenu .ctx-item:hover { background: #333; }
    html[data-theme="dark"] #ctxMenu .ctx-item.current { background: #1e3a5f; }
    html[data-theme="dark"] #ctxMenu .ctx-item.reset { border-top-color: #333; }
    html[data-theme="dark"] .creator-sub { color: #777; }
    html[data-theme="dark"] kbd { background: #333; border-color: #555; color: #e0e0e0; }
    html[data-theme="dark"] .trash-item { border-bottom-color: #333; }
    /* 유지: 표시된 아이템 색상 유지 */
    html[data-theme="dark"] .item.marked { border-color: #d9534f; background: #3a1e1e; }
    html[data-theme="dark"] .item.marked.selected { background: #3a1e1e !important; }
  
</style>
</head><body>
<header>
  <div class="title-row">
    <h1>🎮 CC Manager</h1>
    <div class="seg" id="modeSeg" title="선택 모드">
      <button data-v="delete" class="on">🗑️ 삭제 선택</button>
      <button data-v="category">🏷️ 카테고리 편집</button>
    </div>
    <div style="flex:1;"></div>
    <button onclick="undo()" id="undoBtn" title="되돌리기 (Cmd+Z)" class="icon-only" disabled>↶</button>
    <button onclick="rescan()" class="blue" title="Mods 폴더 다시 스캔">🔄 재스캔</button>
    <button onclick="openTrash()" title="휴지통">🗑️ 휴지통</button>
    <button onclick="openStats()" title="통계" class="hide-narrow">📊 통계</button>
    <div class="menu-wrap">
      <button onclick="toggleOverflowMenu(event)" class="icon-only" title="더보기">⋯</button>
      <div id="overflowMenu" class="overflow-menu">
        <button onclick="openStats(); closeOverflow();" class="show-narrow-only">📊 통계</button>
        <button onclick="openSettings(); closeOverflow();">⚙️ 설정</button>
        <button onclick="openHelp(); closeOverflow();">❓ 도움말</button>
        <hr>
        <button onclick="toggleHeaderCollapse(); closeOverflow();" id="collapseBtn">▲ 헤더 접기</button>
      </div>
    </div>
    <input type="file" id="importOvFile" accept=".json,application/json" style="display:none;">
  </div>
  <div class="stats" id="stats" style="padding: 2px 0; color: var(--c-text-muted); font-size: 11px;">로딩 중...</div>

  <div class="row">
    <div style="position:relative; display:inline-block;">
      <input type="text" id="search" placeholder="🔍 검색... (여러 단어 가능)" style="width: 260px; padding-right: 32px;" autocomplete="off">
      <button id="searchClear" type="button" title="지우기" style="position:absolute; right:6px; top:50%; transform:translateY(-50%); width:20px; height:20px; padding:0; min-width:0; border-radius:50%; border:none; background:var(--c-border); color:var(--c-text-muted); cursor:pointer; font-size:10px; display:none; line-height:1; align-items:center; justify-content:center;">✕</button>
      <div id="searchHistory" style="display:none; position:absolute; top:calc(100% + 4px); left:0; right:0; background:var(--c-surface); border:1px solid var(--c-border); border-radius:var(--radius-md); z-index:200; box-shadow:var(--shadow-md); overflow:hidden;"></div>
    </div>
    <div class="group">
      <span class="label">그룹</span>
      <div class="seg" id="groupSeg">
        <button data-v="creator" class="on">폴더별</button>
        <button data-v="category">카테고리별</button>
        <button data-v="date">날짜별</button>
      </div>
    </div>
    <div class="group">
      <span class="label">그룹정렬</span>
      <select id="sortBy">
        <option value="name">이름</option>
        <option value="size">크기</option>
        <option value="count">개수</option>
        <option value="recent">최근 파일순</option>
        <option value="oldest">오래된 파일순</option>
      </select>
    </div>
    <div class="group">
      <span class="label">아이템정렬</span>
      <select id="itemSortBy">
        <option value="name">이름</option>
        <option value="size">크기</option>
        <option value="category">카테고리</option>
        <option value="recent">최근순</option>
        <option value="oldest">오래된순</option>
      </select>
    </div>
    <div class="group">
      <span class="label">🖼️ 썸네일</span>
      <input type="range" id="zoomSlider" min="80" max="240" value="130" style="width: 90px; height: 20px;" title="아이템 썸네일 크기">
    </div>
  </div>

  <div class="row">
    <div class="toggle-group">
      <label class="switch"><input type="checkbox" id="tglMarked"> 🗑️ 지울 것만</label>
      <label class="switch"><input type="checkbox" id="tglOverride"> 🖐️ 수동 지정만</label>
      <label class="switch"><input type="checkbox" id="tglHideTrashed"> 휴지통 항목 숨기기</label>
      <label class="switch"><input type="checkbox" id="tglHidePerma" checked> 완전삭제 숨기기</label>
      <label class="switch"><input type="checkbox" id="tglCollapsed"> 모두 접기</label>
    </div>
    <div class="divider"></div>
    <button onclick="selectAllFiltered()" title="지금 화면에 보이는 아이템 모두를 다중선택에 추가">☑️ 전체 선택</button>
    <button onclick="clearBulkSel()" title="다중선택(파란 테두리) 전체 해제">▢ 전체 선택 해제</button>
    <div class="divider"></div>
    <button onclick="clearMarks()" title="삭제 표시(빨간 테두리) 전체 해제">🗑️ 삭제표시 전체 해제</button>
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
  <div class="count" onclick="openMarkedDialog()" style="cursor:pointer; user-select:none;">
    삭제 표시: <b id="marked-count">0</b>개 · 절약 예상: <b id="marked-size">0 B</b>
    <span style="color:#888; font-size:12px; margin-left:8px;">▲ 목록 보기</span>
  </div>
  <button onclick="performDelete()" class="primary">🗑️ 휴지통으로 이동</button>
</div>
<dialog id="settings-dialog" style="max-width: 560px; width: 90vw;">
  <button class="dlg-close" onclick="document.getElementById('settings-dialog').close()">✕</button>
  <h3 style="margin: 0 0 16px;">⚙️ 설정</h3>
  <div style="display: flex; flex-direction: column; gap: 16px;">
    <div>
      <label style="font-weight: 600; font-size: 13px;">🌓 테마</label>
      <div class="seg" id="themeSeg" style="display:inline-flex; margin-left:8px;">
        <button data-v="auto" class="on">시스템</button>
        <button data-v="light">라이트</button>
        <button data-v="dark">다크</button>
      </div>
    </div>
    <div>
      <label style="font-weight: 600; font-size: 13px;">📂 Sims 4 경로</label>
      <div style="font-size: 12px; color: #666; margin-top: 4px;" id="currentPath"></div>
      <div style="display:flex; gap:6px; margin-top:6px; align-items:center;">
        <input type="text" id="pathInput" placeholder="예: ~/Documents/Electronic Arts/The Sims 4" style="flex:1; padding: 5px 8px; font-size: 12px;">
        <button onclick="savePath()" class="blue">저장</button>
      </div>
      <div style="font-size: 11px; color: #888; margin-top: 4px;">경로 변경 후 재스캔 필요</div>
    </div>
    <div>
      <label style="font-weight: 600; font-size: 13px;">🏷️ 카테고리 지정 백업</label>
      <div style="display:flex; gap:6px; margin-top:6px;">
        <button onclick="exportOverrides()">📤 내보내기</button>
        <button onclick="importOverrides()">📥 불러오기</button>
      </div>
      <div style="font-size: 11px; color: #888; margin-top: 4px;">수동으로 지정한 카테고리 목록 백업/공유</div>
    </div>
  </div>
</dialog>
<dialog id="marked-dialog" style="max-width:720px; width:90vw;">
  <button class="dlg-close" onclick="document.getElementById('marked-dialog').close()">✕</button>
  <h3>🗑️ 삭제 표시된 아이템 <span id="marked-summary" style="font-weight:normal; color:#666; font-size:13px;"></span></h3>
  <div style="max-height:60vh; overflow-y:auto; margin:8px 0; border:1px solid #eee; border-radius:6px; padding:8px; background:#fafafa;">
    <div id="marked-list" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(140px, 1fr)); gap:6px;"></div>
  </div>
  <div style="display:flex; gap:8px; justify-content:flex-end;">
    <button onclick="clearMarksFromDialog()">전체 표시 해제</button>
    <button onclick="performDeleteFromDialog()" class="primary">🗑️ 휴지통으로 이동</button>
  </div>
</dialog>
<div id="toast" class="toast"></div>
<div id="ctxMenu"></div>
<dialog id="help-dialog" style="max-width: 640px; width: 90vw; padding: 0;">
  <button class="dlg-close" onclick="document.getElementById('help-dialog').close()">✕</button>
  <div style="padding: 16px 22px; border-bottom: 1px solid #eee;">
    <h2 style="margin: 0; font-size: 17px;">🎮 사용법</h2>
  </div>
  <div style="padding: 16px 22px; max-height: 72vh; overflow-y: auto; font-size: 13px; line-height: 1.55;">

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 18px;">
      <div style="border: 2px solid #d9534f; background: #fbecec; border-radius: 8px; padding: 10px 12px;">
        <div style="font-weight: 700; color: #d9534f; margin-bottom: 4px;">🗑️ 삭제 모드</div>
        <div style="font-size: 12px;">클릭해서 지울 것 <b>표시</b> → 하단 "🗑️ 휴지통으로 이동" 버튼</div>
      </div>
      <div style="border: 2px solid #4a90e2; background: #eaf3fc; border-radius: 8px; padding: 10px 12px;">
        <div style="font-weight: 700; color: #4a90e2; margin-bottom: 4px;">🏷️ 카테고리 모드</div>
        <div style="font-size: 12px;">클릭해서 <b>다중 선택</b> → 하단 드롭다운으로 카테고리 지정</div>
      </div>
    </div>

    <h3 style="margin: 12px 0 6px; font-size: 14px;">🎨 상태 색깔</h3>
    <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 6px; font-size: 12px;">
      <div><span style="display:inline-block; width:14px; height:14px; background:#ffe8e8; border:2px solid #d9534f; border-radius:3px; vertical-align:middle;"></span> 삭제 표시됨</div>
      <div><span style="display:inline-block; width:14px; height:14px; background:white; border:2px solid #4a90e2; border-radius:3px; box-shadow:0 0 4px #4a90e2; vertical-align:middle;"></span> 다중 선택됨</div>
      <div><span style="display:inline-block; width:14px; height:14px; background:#f0f0f0; border:2px solid #999; border-radius:3px; vertical-align:middle;"></span> 🗑️ 휴지통 (복원 가능)</div>
      <div><span style="display:inline-block; width:14px; height:14px; background:#f5eaea; border:2px solid #600; border-radius:3px; vertical-align:middle;"></span> ✖ 완전 삭제됨</div>
    </div>

    <h3 style="margin: 16px 0 6px; font-size: 14px;">⌨️ 단축키</h3>
    <table style="width:100%; border-collapse: collapse; font-size: 12px;">
      <tr><td style="padding:4px 8px; color:#666; width:120px;"><kbd>Cmd+Z</kbd></td><td>되돌리기</td></tr>
      <tr><td style="padding:4px 8px; color:#666;"><kbd>Cmd+클릭</kbd></td><td>모드 상관없이 다중 선택</td></tr>
      <tr><td style="padding:4px 8px; color:#666;">빈 공간 드래그</td><td>박스 다중 선택</td></tr>
      <tr><td style="padding:4px 8px; color:#666;">우클릭</td><td>단일 카테고리 변경 메뉴</td></tr>
      <tr><td style="padding:4px 8px; color:#666;">이름 클릭</td><td>파일명 복사 (검색용)</td></tr>
      <tr><td style="padding:4px 8px; color:#666;">이름 hover</td><td>긴 파일명 전체 표시</td></tr>
    </table>

    <h3 style="margin: 16px 0 6px; font-size: 14px;">💡 팁</h3>
    <ul style="margin: 0; padding-left: 20px; font-size: 12px;">
      <li>대량 카테고리 지정: Cmd+클릭 다중선택 → 하단 드롭다운, 또는 카테고리 chip으로 드래그</li>
      <li>휴지통 이동은 안전. 언제든 복원 가능. <b>완전 삭제만 되돌릴 수 없음</b></li>
      <li>날짜별 그룹 → "3년 이상 전" 오래된 것 대량 정리</li>
      <li>수동 지정한 카테고리는 재스캔 시에도 유지</li>
    </ul>

    <p style="margin: 14px 0 0; padding: 8px; background: #f7f7f7; border-radius: 4px; font-size: 11px; color: #666;">
      📂 앱 데이터: <code>~/Library/Application Support/Sims4CCManager/</code> (Sims 4 폴더 안 건드림)
    </p>

  </div>
</dialog>
<div id="bulkBar">
  <span class="info">📌 <b id="bulkCount">0</b>개 선택됨</span>
  <select id="bulkCat"><option value="">카테고리 선택...</option></select>
  <button onclick="applyBulkCategory()" class="blue">✓ 카테고리 지정</button>
  <button onclick="clearBulkSel()">선택 해제</button>
</div>
<dialog id="failed-dialog" style="max-width: 600px; width: 90vw;">
  <button class="dlg-close" onclick="document.getElementById('failed-dialog').close()">✕</button>
  <h3>⚠️ 일부 파일 이동 실패</h3>
  <div id="failed-summary" style="color: #666; font-size: 13px; margin-bottom: 8px;"></div>
  <div style="max-height: 300px; overflow-y: auto; margin: 8px 0; border: 1px solid #eee; border-radius: 6px; padding: 8px; background: #fafafa;">
    <div id="failed-list"></div>
  </div>
  <div style="display: flex; gap: 8px; justify-content: flex-end;">
    <button onclick="clearFailedMarks()">실패한 것 표시 해제</button>
  </div>
</dialog>
<dialog id="trash-dialog" style="max-width: 720px; width: 90vw;">
  <button class="dlg-close" onclick="closeTrash()">✕</button>
  <h3>휴지통 <span id="trash-summary" style="font-weight: normal; color: #666; font-size: 13px;"></span></h3>
  <div style="display: flex; gap: 8px; margin: 8px 0; align-items: center;">
    <button onclick="trashSelectAll(true)">전체 선택</button>
    <button onclick="trashSelectAll(false)">전체 해제</button>
    <span style="color:#666; font-size:12px;">선택: <b id="trash-selected-count">0</b>개</span>
  </div>
  <div style="max-height: 60vh; overflow-y: auto; margin: 8px 0; border: 1px solid #eee; border-radius: 6px; padding: 8px; background: #fafafa;">
    <div id="trash-list" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 6px;"></div>
  </div>
  <div style="display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap;">
    <button onclick="restoreAll()" class="blue">↩️ 전체 복원</button>
    <button onclick="restoreSelected()" class="blue">↩️ 선택 복원</button>
    <button onclick="deleteSelectedFromTrash()" class="primary">🗑️ 선택 완전 삭제</button>
    <button onclick="emptyTrash()" class="primary">🗑️ 전체 완전 삭제</button>
  </div>
</dialog>
<script>
let MANIFEST = null;
const state = JSON.parse(localStorage.getItem('cc-marks') || '{}');
const bulkSel = new Set();  // 다중선택 (Cmd+클릭)
let currentFilter = '';
let showMarkedOnly = false;
let sortBy = 'name';
let itemSortBy = 'name';
let metaFilter = '';
let subFilter = '';
let groupBy = 'creator';
let collapsedMode = false;
let showOverrideOnly = false;
let hideTrashed = false;
let hidePerma = true;
let editMode = 'delete';  // 'delete' | 'category'

function human(bytes) {
  const u = ['B','KB','MB','GB'];
  let i = 0, s = bytes;
  while (s >= 1024 && i < 3) { s /= 1024; i++; }
  return s.toFixed(1) + ' ' + u[i];
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

async function loadManifest() {
  const res = await fetch('/api/manifest');
  MANIFEST = await res.json();
  // 표시된 것 중 이제 존재하지 않는 파일 제거
  const validPaths = new Set();
  for (const c of MANIFEST.creators)
    for (const it of c.items) validPaths.add(it.path);
  for (const k of Object.keys(state))
    if (!validPaths.has(k)) delete state[k];
  saveMarks();
  // 다중선택 dropdown 강제 재생성
  document.getElementById('bulkCat').innerHTML = '<option value="">카테고리 선택...</option>';
  render();
  updateBulkBar();
}

function saveMarks() {
  localStorage.setItem('cc-marks', JSON.stringify(state));
}

function creatorStats(c) {
  let maxMtime = 0, minMtime = Infinity;
  for (const it of c.items) {
    if (it.mtime) {
      if (it.mtime > maxMtime) maxMtime = it.mtime;
      if (it.mtime < minMtime) minMtime = it.mtime;
    }
  }
  return {
    count: c.items.length,
    size: c.items.reduce((s, it) => s + it.size, 0),
    maxMtime: maxMtime || 0,
    minMtime: minMtime === Infinity ? 0 : minMtime,
  };
}

function populateCatFilter() {
  // 카테고리별 카운트
  const catCounts = {};
  for (const c of MANIFEST.creators)
    for (const it of c.items)
      for (const cat of (it.cats || ['기타']))
        catCounts[cat] = (catCounts[cat] || 0) + 1;

  // 대카테고리별 총 개수
  const meta = MANIFEST.meta_categories || {};
  const metaCounts = {};
  for (const [mname, mdata] of Object.entries(meta)) {
    let total = 0;
    for (const sub of mdata.subs) total += (catCounts[sub] || 0);
    metaCounts[mname] = total;
  }

  // 대카테고리 chip bar
  const metaBar = document.getElementById('metaChips');
  const metaHtml = [`<div class="chip ${metaFilter === '' ? 'on' : ''}" data-meta="">전체 <span class="count">${MANIFEST.total_pkgs}</span></div>`];
  for (const [mname, mdata] of Object.entries(meta)) {
    if (metaCounts[mname] === 0) continue;
    metaHtml.push(`<div class="chip ${metaFilter === mname ? 'on' : ''}" data-meta="${mname}">${mdata.icon} ${mname} <span class="count">${metaCounts[mname]}</span></div>`);
  }
  metaBar.innerHTML = metaHtml.join('');
  metaBar.querySelectorAll('.chip').forEach(el => {
    el.onclick = () => {
      const m = el.dataset.meta;
      if (metaFilter === m) { metaFilter = ''; subFilter = ''; }
      else { metaFilter = m; subFilter = ''; }
      render();
    };
  });

  // 소카테고리 chip bar (선택된 대카테고리에 여러 소카가 있을 때만 표시)
  const subRow = document.getElementById('subRow');
  const subBar = document.getElementById('subChips');
  if (metaFilter && meta[metaFilter] && meta[metaFilter].subs.length > 1) {
    subRow.style.display = 'flex';
    const subHtml = [`<div class="subchip ${subFilter === '' ? 'on' : ''}" data-sub="">전체</div>`];
    for (const sub of meta[metaFilter].subs) {
      const n = catCounts[sub] || 0;
      if (n === 0) continue;
      subHtml.push(`<div class="subchip ${subFilter === sub ? 'on' : ''}" data-sub="${sub}">${sub} <span style="opacity:.7">${n}</span></div>`);
    }
    subBar.innerHTML = subHtml.join('');
    subBar.querySelectorAll('.subchip').forEach(el => {
      el.onclick = () => {
        subFilter = subFilter === el.dataset.sub ? '' : el.dataset.sub;
        render();
      };
      // 드롭 타겟 - 아이템을 여기 놓으면 그 소카테고리로 지정
      const cat = el.dataset.sub;
      if (cat) {
        el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('drop-target'); });
        el.addEventListener('dragleave', () => el.classList.remove('drop-target'));
        el.addEventListener('drop', async (e) => {
          e.preventDefault();
          el.classList.remove('drop-target');
          try {
            const paths = JSON.parse(e.dataTransfer.getData('application/json'));
            await applyToPaths(paths, cat);
          } catch {}
        });
      }
    });
  } else {
    subRow.style.display = 'none';
  }
}

function matchesCatFilter(cats) {
  if (!metaFilter) return true;
  const meta = MANIFEST.meta_categories || {};
  const allowedSubs = meta[metaFilter]?.subs || [];
  if (subFilter) {
    return (cats || []).includes(subFilter);
  }
  return (cats || []).some(c => allowedSubs.includes(c));
}

// 날짜 버킷 정의: [순서, 라벨, cutoff(초 단위 이내면 이 버킷)]
const DATE_BUCKETS = [
  { key: 'today',    label: '오늘',        maxAgeS: 86400 },
  { key: 'week',     label: '이번 주',     maxAgeS: 86400 * 7 },
  { key: 'month',    label: '이번 달',     maxAgeS: 86400 * 30 },
  { key: '3month',   label: '3개월 이내',   maxAgeS: 86400 * 90 },
  { key: '6month',   label: '6개월 이내',   maxAgeS: 86400 * 180 },
  { key: 'year',     label: '1년 이내',     maxAgeS: 86400 * 365 },
  { key: '2year',    label: '1~2년 전',     maxAgeS: 86400 * 365 * 2 },
  { key: '3year',    label: '2~3년 전',     maxAgeS: 86400 * 365 * 3 },
  { key: 'old',      label: '3년 이상 전',   maxAgeS: Infinity },
  { key: 'unknown',  label: '(날짜 정보 없음)', maxAgeS: Infinity },
];

function dateBucket(mtime) {
  if (!mtime) return DATE_BUCKETS[DATE_BUCKETS.length - 1];  // unknown
  const nowS = Date.now() / 1000;
  const ageS = nowS - mtime;
  for (const b of DATE_BUCKETS) {
    if (b.key === 'unknown') continue;
    if (ageS <= b.maxAgeS) return b;
  }
  return DATE_BUCKETS[DATE_BUCKETS.length - 2];  // old
}

function buildGroups() {
  const folders = MANIFEST.folders || MANIFEST.creators || [];
  if (groupBy === 'category') {
    const groups = {};
    for (const c of folders) {
      for (const it of c.items) {
        const cat = it.primary_cat || (it.cats && it.cats[0]) || '기타';
        if (!groups[cat]) groups[cat] = { name: cat, items: [] };
        groups[cat].items.push({...it, _creator: c.name});
      }
    }
    return Object.values(groups);
  }
  if (groupBy === 'date') {
    const groups = {};
    for (const c of folders) {
      for (const it of c.items) {
        const b = dateBucket(it.mtime);
        if (!groups[b.key]) groups[b.key] = { name: b.label, _bucketOrder: DATE_BUCKETS.indexOf(b), items: [] };
        groups[b.key].items.push({...it, _creator: c.name});
      }
    }
    // 최신 → 옛날 순 고정
    return Object.values(groups).sort((a, b) => a._bucketOrder - b._bucketOrder);
  }
  return folders.map(c => ({...c}));
}

function render() {
  const main = document.getElementById('main');
  main.innerHTML = '';
  if (!MANIFEST || !MANIFEST.creators.length) {
    main.innerHTML = `<div class="progress" style="padding:40px;">
      <div style="font-size:48px;margin-bottom:12px;">👋</div>
      <h2 style="margin:0 0 8px 0;">환영합니다!</h2>
      <div style="color:#666;margin-bottom:20px;">Mods 폴더에서 아직 CC를 스캔하지 않았어요.<br>아래 버튼을 눌러 시작해 보세요.</div>
      <button class="blue" style="font-size:14px;padding:10px 20px;" onclick="rescan()">🔄 스캔 시작</button>
    </div>`;
    updateFooter();
    updateStats(0, 0);
    return;
  }
  populateCatFilter();
  let totalItems = 0, visibleItems = 0;
  const creators = buildGroups();
  creators.forEach(c => c._stats = creatorStats(c));
  // 날짜별 그룹은 최신→옛 고정 순서 (buildGroups에서 이미 정렬됨)
  if (groupBy === 'date') {
    // no re-sort
  } else if (sortBy === 'size') creators.sort((a,b) => b._stats.size - a._stats.size);
  else if (sortBy === 'count') creators.sort((a,b) => b._stats.count - a._stats.count);
  else if (sortBy === 'recent') creators.sort((a,b) => b._stats.maxMtime - a._stats.maxMtime);
  else if (sortBy === 'oldest') creators.sort((a,b) => a._stats.minMtime - b._stats.minMtime);
  else creators.sort((a,b) => a.name.localeCompare(b.name));

  for (const c of creators) {
    let items = c.items.filter(it => {
      totalItems++;
      if (hidePerma && it.perma_deleted) return false;
      if (hideTrashed && it.trashed) return false;
      if (showMarkedOnly && !state[it.path]) return false;
      if (showOverrideOnly && !it.override) return false;
      if (!matchesCatFilter(it.cats)) return false;
      if (currentFilter) {
        const hay = (it.file + ' ' + c.name).toLowerCase();
        const terms = currentFilter.split(/\\s+/).filter(Boolean);
        for (const t of terms) { if (!hay.includes(t)) return false; }
      }
      return true;
    });
    if (itemSortBy === 'size') items = items.slice().sort((a,b) => b.size - a.size);
    else if (itemSortBy === 'category') items = items.slice().sort((a,b) => ((a.cats||[])[0]||'기타').localeCompare((b.cats||[])[0]||'기타') || a.file.localeCompare(b.file));
    else if (itemSortBy === 'recent') items = items.slice().sort((a,b) => (b.mtime||0) - (a.mtime||0));
    else if (itemSortBy === 'oldest') items = items.slice().sort((a,b) => (a.mtime||0) - (b.mtime||0));
    else items = items.slice().sort((a,b) => a.file.localeCompare(b.file));
    if (!items.length) continue;
    visibleItems += items.length;
    const section = document.createElement('section');
    section.className = 'creator' + (collapsedMode ? ' collapsed' : '');
    const markedInCreator = items.filter(it => state[it.path]).length;
    section.innerHTML = `
      <div class="creator-header" onclick="if(!event.target.matches('button'))this.parentElement.classList.toggle('collapsed')">
        <h2>${escapeHtml(c.name)}</h2>
        <span class="creator-count">${items.length}개 · ${human(c._stats.size)}${markedInCreator ? ` <span class="badge warn">🗑️ ${markedInCreator}</span>`:''}</span>
        <div class="creator-actions">
          <button onclick="markCreator('${escapeAttr(c.name)}', true)" title="이 폴더 전체를 삭제 대상으로 표시">🗑️ 폴더 전체 삭제표시</button>
          <button onclick="markCreator('${escapeAttr(c.name)}', false)" title="이 폴더의 삭제 표시 전체 해제">✕ 폴더 전체 표시 해제</button>
        </div>
      </div>
      <div class="grid"></div>
    `;
    const grid = section.querySelector('.grid');
    for (const it of items) {
      const div = document.createElement('div');
      const classes = ['item'];
      if (it.perma_deleted) classes.push('perma-deleted');
      else if (it.trashed) classes.push('trashed');
      else if (state[it.path]) classes.push('marked');
      if (bulkSel.has(it.path)) classes.push('selected');
      div.className = classes.join(' ');
      div.draggable = !it.trashed && !it.perma_deleted;
      div.dataset.path = it.path;
      div.dataset.thumbIdx = '0';
      div.title = (it.trashed ? '휴지통에 있음 (클릭 → 복원)\\n' : '') + it.file + '\\n' + human(it.size);
      let thumbHtml;
      if (it.thumbs.length) {
        thumbHtml = `<img class="thumb-img" src="/thumbs/${it.thumbs[0]}" loading="lazy">`;
        if (it.thumbs.length > 1) {
          thumbHtml += `<div class="thumb-nav">
            <button class="thumb-btn thumb-prev" onclick="cycleThumb(event, this, -1)">◀</button>
            <span class="thumb-counter">1/${it.thumbs.length}</span>
            <button class="thumb-btn thumb-next" onclick="cycleThumb(event, this, 1)">▶</button>
          </div>`;
        }
      } else {
        thumbHtml = `<div class="no-thumb">썸네일 없음</div>`;
      }
      const trashBadge = it.perma_deleted ? '<div class="trash-badge" style="background:rgba(60,0,0,.85);">✖ 완전삭제</div>'
                        : it.trashed ? '<div class="trash-badge">🗑️ 휴지통</div>'
                        : '';
      const overrideClass = it.override ? ' override' : '';
      const catTitle = it.override ? '수동 지정' : (it.casp ? 'CASP 파싱' : '파일명 추측') + ' (우클릭으로 변경)';
      const catIcon = it.cat_icon ? `<div class="cat-icon${overrideClass}" title="${escapeHtml(catTitle + ': ' + (it.cats||[]).join(', '))}" data-item-path="${escapeHtml(it.path)}" data-item-cat="${escapeHtml(it.primary_cat||'')}">${it.cat_icon}</div>` : '';
      const creatorSubtitle = it._creator ? `<div class="creator-sub">${escapeHtml(it._creator)}</div>` : '';
      div.dataset.thumbs = JSON.stringify(it.thumbs);
      // 44자 초과 시 hover 팝오버로 전체 이름 보여줌 (짧으면 name-full 안 만듦)
      const needsPopover = it.file.length > 44;
      const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(it.file)}</div>` : '';
      div.innerHTML = thumbHtml + trashBadge + catIcon + `<div class="sz">${human(it.size)}</div>${creatorSubtitle}<div class="name">${escapeHtml(it.file)}</div>${nameFull}`;
      const clickHandler = it.perma_deleted
        ? () => toast('❌ 완전삭제된 파일입니다 (되돌릴 수 없음)')
        : it.trashed
        ? async () => {
            if (!(await customConfirm(`휴지통에서 복원할까요?\\n${it.file}`, {okText:'복원'}))) return;
            const res = await fetch('/api/restore', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({paths: [it.trash_path || it.path]}),
            });
            await res.json();
            toast('↩️ 복원됨');
            await loadManifest();
          }
        : (e) => {
            const isBulk = editMode === 'category' || (e && (e.metaKey || e.ctrlKey));
            if (isBulk) {
              if (bulkSel.has(it.path)) bulkSel.delete(it.path);
              else bulkSel.add(it.path);
              updateBulkBar();
              div.classList.toggle('selected', bulkSel.has(it.path));
            } else {
              toggle(it.path);
            }
          };
      div.querySelector('.thumb-img')?.addEventListener('click', clickHandler);
      div.querySelector('.no-thumb')?.addEventListener('click', clickHandler);
      div.querySelector('.sz').addEventListener('click', clickHandler);
      // 이름 클릭 = 파일명 복사 (확장자 제외) - 검색용
      const nameEl = div.querySelector('.name');
      const nameFullEl = div.querySelector('.name-full');
      const copyName = async (e) => {
        e.stopPropagation();
        const clean = it.file.replace(/\\.package$/i, '');
        try {
          await navigator.clipboard.writeText(clean);
          toast(`📋 복사됨: ${clean.slice(0, 40)}${clean.length > 40 ? '…' : ''}`);
        } catch { toast('복사 실패'); }
      };
      nameEl.style.cursor = 'copy';
      nameEl.title = '클릭=파일명 복사 · 우클릭=다른 복사 옵션';
      nameEl.addEventListener('click', copyName);
      const nameRightClick = (e) => {
        e.preventDefault(); e.stopPropagation();
        showNameCopyMenu(e, it, c.name);
      };
      nameEl.addEventListener('contextmenu', nameRightClick);
      if (nameFullEl) {
        nameFullEl.style.cursor = 'copy';
        nameFullEl.title = '클릭=파일명 복사 · 우클릭=다른 복사 옵션';
        nameFullEl.style.pointerEvents = 'auto';
        nameFullEl.addEventListener('click', copyName);
        nameFullEl.addEventListener('contextmenu', nameRightClick);
      }
      // 우클릭 or cat-icon 클릭 → 카테고리 변경 메뉴
      const catEl = div.querySelector('.cat-icon');
      if (catEl) {
        catEl.addEventListener('click', (e) => {
          e.stopPropagation();
          // 우클릭/카테고리 아이콘 클릭은 항상 그 아이템 하나만
          // (다중 적용은 하단 bulk 바나 드래그 앤 드롭 사용)
          showCategoryMenu(e, [it.path], it.primary_cat);
        });
      }
      div.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        showCategoryMenu(e, [it.path], it.primary_cat);
      });
      // Drag: 카테고리 chip으로 드래그해서 카테고리 지정
      div.addEventListener('dragstart', (e) => {
        div.classList.add('dragging');
        // 선택된 것이 있으면 그 세트 전체 드래그, 아니면 이 아이템만
        const draggedPaths = bulkSel.has(it.path) ? [...bulkSel] : [it.path];
        e.dataTransfer.setData('application/json', JSON.stringify(draggedPaths));
        e.dataTransfer.effectAllowed = 'copy';
      });
      div.addEventListener('dragend', () => div.classList.remove('dragging'));
      grid.appendChild(div);
    }
    main.appendChild(section);
  }
  if (totalItems > 0 && visibleItems === 0) {
    const empty = document.createElement('div');
    empty.className = 'progress';
    empty.style.cssText = 'padding:40px;';
    empty.innerHTML = `<div style="font-size:36px;margin-bottom:8px;">🔍</div>
      <h3 style="margin:0 0 6px 0;">일치하는 아이템 없음</h3>
      <div style="color:#666;margin-bottom:16px;">현재 필터 조건에 맞는 항목이 없어요.</div>
      <button class="blue" onclick="resetFilters()">필터 초기화</button>`;
    main.appendChild(empty);
  }
  updateStats(totalItems, visibleItems);
  updateFooter();
}

function resetFilters() {
  currentFilter = '';
  showMarkedOnly = false;
  showOverrideOnly = false;
  hideTrashed = false;
  hidePerma = false;
  metaFilter = '';
  subFilter = '';
  const s = document.getElementById('search'); if (s) s.value = '';
  const t1 = document.getElementById('tglMarked'); if (t1) t1.checked = false;
  const t2 = document.getElementById('tglOverride'); if (t2) t2.checked = false;
  const t3 = document.getElementById('tglHideTrashed'); if (t3) t3.checked = false;
  const t4 = document.getElementById('tglHidePerma'); if (t4) t4.checked = false;
  render();
}

function escapeHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escapeAttr(s) { return s.replace(/'/g, "\\\\'"); }

function updateStats(total, visible) {
  const folderCount = (MANIFEST.folders || MANIFEST.creators || []).length;
  document.getElementById('stats').textContent =
    `폴더 ${folderCount}개 · 파일 ${total.toLocaleString()}개 · 표시 중 ${visible.toLocaleString()}개 · 썸네일 ${MANIFEST.total_thumbs.toLocaleString()}개`;
}

function toggle(path) {
  if (state[path]) delete state[path]; else state[path] = true;
  saveMarks();
  // 최소 렌더링: 해당 카드만 marked 클래스 토글
  document.querySelectorAll(`.item[data-path="${CSS.escape(path)}"]`).forEach(el => {
    el.classList.toggle('marked', !!state[path]);
  });
  updateFooter();
}

function updateBulkBar() {
  const bar = document.getElementById('bulkBar');
  const count = bulkSel.size;
  document.getElementById('bulkCount').textContent = count;
  bar.style.display = count > 0 ? 'flex' : 'none';
  document.body.classList.toggle('has-bulk', count > 0);
  // 드롭다운 채우기
  const sel = document.getElementById('bulkCat');
  if (sel.options.length <= 1 && MANIFEST) {
    const cats = MANIFEST.all_categories || [];
    sel.innerHTML = '<option value="">카테고리 선택...</option>' +
      cats.map(c => `<option value="${escapeHtml(c.name)}">${c.icon} ${c.name}</option>`).join('') +
      '<option value="__reset__">↺ 수동 지정 해제 (자동 재분류)</option>';
  }
}

async function applyBulkCategory() {
  const cat = document.getElementById('bulkCat').value;
  if (!cat) { toast('카테고리를 선택해주세요'); return; }
  if (bulkSel.size === 0) return;
  const paths = [...bulkSel];
  const catToSend = cat === '__reset__' ? '' : cat;
  const res = await fetch('/api/set-category-batch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths, category: catToSend}),
  });
  await res.json();
  toast(catToSend
    ? `✅ ${paths.length}개 → ${catToSend}`
    : `↺ ${paths.length}개 수동 지정 해제`);
  bulkSel.clear();
  await loadManifest();
}

function clearBulkSel() {
  bulkSel.clear();
  document.querySelectorAll('.item.selected').forEach(el => el.classList.remove('selected'));
  updateBulkBar();
}

async function applyToPaths(paths, category) {
  if (!paths.length) return;
  const catToSend = category === '__reset__' ? '' : category;
  await fetch('/api/set-category-batch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths, category: catToSend}),
  });
  toast(catToSend
    ? `✅ ${paths.length}개 → ${catToSend}`
    : `↺ ${paths.length}개 수동 지정 해제`);
  bulkSel.clear();
  await loadManifest();
}

function openHelp() {
  document.getElementById('help-dialog').showModal();
}

// ─────── 오버플로우 메뉴 ───────
function toggleOverflowMenu(e) {
  e.stopPropagation();
  document.getElementById('overflowMenu').classList.toggle('open');
}
function closeOverflow() {
  document.getElementById('overflowMenu').classList.remove('open');
}
document.addEventListener('click', (e) => {
  const m = document.getElementById('overflowMenu');
  if (m && !m.contains(e.target) && !e.target.closest('.menu-wrap button')) closeOverflow();
});

// ─────── 헤더 접기 ───────
function toggleHeaderCollapse() {
  const collapsed = document.body.classList.toggle('header-collapsed');
  localStorage.setItem('ccm_header_collapsed', collapsed ? '1' : '0');
  const btn = document.getElementById('collapseBtn');
  if (btn) btn.textContent = collapsed ? '▼ 헤더 펼치기' : '▲ 헤더 접기';
}
(function initHeaderCollapse() {
  if (localStorage.getItem('ccm_header_collapsed') === '1') {
    document.body.classList.add('header-collapsed');
    document.addEventListener('DOMContentLoaded', () => {
      const btn = document.getElementById('collapseBtn');
      if (btn) btn.textContent = '▼ 헤더 펼치기';
    });
  }
})();

// ─────── 설정 다이얼로그 ───────
function openSettings() {
  // 현재 경로 표시
  fetch('/api/config').then(r => r.json()).then(cfg => {
    document.getElementById('currentPath').textContent = '현재: ' + (cfg.sims_root || '(미지정)');
    document.getElementById('pathInput').value = cfg.sims_root || '';
  }).catch(() => {
    document.getElementById('currentPath').textContent = '(설정 조회 실패)';
  });
  // 현재 테마 하이라이트
  const theme = localStorage.getItem('theme') || 'auto';
  document.querySelectorAll('#themeSeg button').forEach(b => {
    b.classList.toggle('on', b.dataset.v === theme);
  });
  document.getElementById('settings-dialog').showModal();
}

async function savePath() {
  const path = document.getElementById('pathInput').value.trim();
  if (!path) { toast('경로를 입력해주세요'); return; }
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sims_root: path}),
  });
  const data = await res.json();
  if (data.ok) {
    toast('✅ 저장됨 · 재스캔 실행 중...');
    await rescan();
    document.getElementById('settings-dialog').close();
  } else {
    toast('❌ ' + (data.error || '저장 실패'));
  }
}

// 테마 적용
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('theme', theme);
}
(function initTheme() {
  const saved = localStorage.getItem('theme') || 'auto';
  applyTheme(saved);
})();
document.addEventListener('DOMContentLoaded', () => {
  const seg = document.getElementById('themeSeg');
  if (seg) {
    seg.querySelectorAll('button').forEach(b => {
      b.onclick = () => {
        seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
        applyTheme(b.dataset.v);
      };
    });
  }
});

async function _copyText(text, label) {
  try { await navigator.clipboard.writeText(text); toast(`📋 복사: ${label}`); }
  catch { toast('복사 실패'); }
}

function showNameCopyMenu(event, item, creatorName) {
  const menu = document.getElementById('ctxMenu');
  const clean = item.file.replace(/\\.package$/i, '');
  const opts = [
    {label: '파일명 복사 (확장자 제외)', val: clean, hint: '기본'},
    {label: '전체 파일명 복사 (.package 포함)', val: item.file},
    {label: '창작자 복사', val: creatorName || '(폴더 없음)'},
    {label: '카테고리 복사', val: (item.cats || [item.primary_cat || '기타']).join(', ')},
    {label: '경로 복사', val: item.path},
  ];
  let html = `<div class="ctx-header">📋 복사 옵션</div>`;
  html += opts.map((o, i) => `<div class="ctx-item" data-idx="${i}">${escapeHtml(o.label)}${o.hint?` <span style="color:#999;font-size:10px;margin-left:auto;">${o.hint}</span>`:''}</div>`).join('');
  menu.innerHTML = html;
  const x = Math.min(event.clientX, window.innerWidth - 250);
  const y = Math.min(event.clientY, window.innerHeight - 250);
  menu.style.left = x + 'px'; menu.style.top = y + 'px'; menu.style.display = 'block';
  menu.querySelectorAll('.ctx-item').forEach(el => {
    el.onclick = () => {
      menu.style.display = 'none';
      const o = opts[+el.dataset.idx];
      const preview = String(o.val).slice(0, 40) + (String(o.val).length > 40 ? '…' : '');
      _copyText(o.val, preview);
    };
  });
}

function showCategoryMenu(event, pathOrPaths, currentCat) {
  // 배열 or 단일 경로 둘 다 받음
  const paths = Array.isArray(pathOrPaths) ? pathOrPaths : [pathOrPaths];
  const isBulk = paths.length > 1;
  const menu = document.getElementById('ctxMenu');
  const cats = MANIFEST.all_categories || [];
  const label = isBulk
    ? `<b style="color:#4a90e2;">📌 다중 ${paths.length}개</b> 카테고리 변경`
    : `카테고리 변경 <span style="color:#999">(${paths[0].split('/').pop()})</span>`;
  let html = `<div class="ctx-header">${label}</div>`;
  for (const c of cats) {
    const cls = !isBulk && c.name === currentCat ? ' current' : '';
    html += `<div class="ctx-item${cls}" data-cat="${escapeHtml(c.name)}">${c.icon} ${c.name}</div>`;
  }
  html += `<div class="ctx-item reset" data-cat="">↺ 수동 지정 해제 (자동 재분류)</div>`;
  menu.innerHTML = html;
  const x = Math.min(event.clientX, window.innerWidth - 200);
  const y = Math.min(event.clientY, window.innerHeight - 300);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.style.display = 'block';
  menu.querySelectorAll('.ctx-item').forEach(el => {
    el.onclick = async () => {
      menu.style.display = 'none';
      const cat = el.dataset.cat;
      await applyToPaths(paths, cat || '__reset__');
    };
  });
}
document.addEventListener('click', (e) => {
  const menu = document.getElementById('ctxMenu');
  if (!menu.contains(e.target)) menu.style.display = 'none';
});

function cycleThumb(event, btn, dir) {
  event.stopPropagation();
  const item = btn.closest('.item');
  const thumbs = JSON.parse(item.dataset.thumbs);
  let idx = parseInt(item.dataset.thumbIdx || '0', 10);
  idx = (idx + dir + thumbs.length) % thumbs.length;
  item.dataset.thumbIdx = idx;
  item.querySelector('.thumb-img').src = `/thumbs/${thumbs[idx]}`;
  item.querySelector('.thumb-counter').textContent = `${idx+1}/${thumbs.length}`;
}

function markCreator(name, mark) {
  const c = MANIFEST.creators.find(x => x.name === name);
  if (!c) return;
  for (const it of c.items) {
    if (mark) state[it.path] = true;
    else delete state[it.path];
  }
  saveMarks();
  render();
}

function updateFooter() {
  const marked = Object.keys(state);
  document.getElementById('marked-count').textContent = marked.length;
  let sz = 0;
  if (MANIFEST) for (const c of MANIFEST.creators)
    for (const it of c.items) if (state[it.path]) sz += it.size;
  document.getElementById('marked-size').textContent = human(sz);
}

function showOnlyMarked() { showMarkedOnly = !showMarkedOnly; render(); }
async function clearMarks() {
  if (!(await customConfirm('모든 표시를 초기화할까요?', {okText:'초기화', danger:true}))) return;
  for (const k of Object.keys(state)) delete state[k];
  saveMarks();
  render();
}

function openMarkedDialog() {
  const paths = Object.keys(state);
  if (!paths.length) { toast('삭제 표시된 항목이 없어요.'); return; }
  const list = document.getElementById('marked-list');
  let sz = 0;
  const items = [];
  if (MANIFEST) {
    for (const f of (MANIFEST.folders || MANIFEST.creators || [])) {
      for (const it of f.items) {
        if (state[it.path]) { items.push(it); sz += it.size; }
      }
    }
  }
  document.getElementById('marked-summary').textContent = `(${items.length}개 · ${human(sz)})`;
  list.innerHTML = items.map(it => {
    const thumbHtml = it.thumbs && it.thumbs.length
      ? `<img class="thumb-img" src="/thumbs/${it.thumbs[0]}" style="width:100%; display:block;">`
      : `<div class="no-thumb" style="padding:40px 8px; text-align:center; color:#999; font-size:11px; background:#eee;">썸네일 없음</div>`;
    const needsPopover = it.file.length > 44;
    const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(it.file)}</div>` : '';
    return `<div class="item marked-tile" data-path="${escapeHtml(it.path)}" style="border:2px solid #d9534f; background:white;">
      ${thumbHtml}
      <button style="position:absolute; top:4px; right:4px; background:rgba(255,255,255,.9); border:1px solid #ccc; border-radius:50%; width:22px; height:22px; padding:0; cursor:pointer; font-size:12px; z-index:3;" title="표시 해제" onclick="unmarkFromDialog('${escapeAttr(it.path)}')">✕</button>
      <div class="sz">${human(it.size)}</div>
      <div class="name">${escapeHtml(it.file)}</div>
      ${nameFull}
    </div>`;
  }).join('');
  document.getElementById('marked-dialog').showModal();
}

function unmarkFromDialog(path) {
  delete state[path];
  saveMarks();
  // 다이얼로그 아이템만 즉시 제거
  const tile = document.querySelector(`.marked-tile[data-path="${CSS.escape(path)}"]`);
  if (tile) tile.remove();
  // 갤러리도 반영
  document.querySelectorAll(`.item[data-path="${CSS.escape(path)}"]`).forEach(el => el.classList.remove('marked'));
  updateFooter();
  const remaining = Object.keys(state).length;
  if (remaining === 0) document.getElementById('marked-dialog').close();
}

async function clearMarksFromDialog() {
  if (!(await customConfirm('삭제 표시 전체를 해제할까요?', {okText:'해제', danger:true}))) return;
  for (const k of Object.keys(state)) delete state[k];
  saveMarks();
  document.getElementById('marked-dialog').close();
  render();
}

async function performDeleteFromDialog() {
  document.getElementById('marked-dialog').close();
  await performDelete();
}

async function performDelete() {
  const paths = Object.keys(state);
  if (!paths.length) { toast('삭제 표시된 항목이 없어요.'); return; }
  if (!(await customConfirm(`${paths.length}개 파일을 휴지통으로 이동할까요?\\n(휴지통에서 복원 가능해요)`, {okText:'이동', danger:true}))) return;
  const res = await fetch('/api/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths}),
  });
  const data = await res.json();
  // 성공한 것 표시 제거
  for (const p of data.moved) delete state[p];
  // 실패한 것 처리
  const missing = data.failed.filter(f => f.reason === '파일 없음');
  const realErrors = data.failed.filter(f => f.reason !== '파일 없음');
  // 이미 없는 파일은 표시에서 자동 제거
  for (const f of missing) delete state[f.path];
  saveMarks();
  if (realErrors.length) {
    showFailedDialog(data.moved.length, missing.length, realErrors);
  } else if (missing.length) {
    toast(`✅ ${data.moved.length}개 이동됨 · 이미 없던 ${missing.length}개 표시 정리됨`);
  } else {
    toast(`✅ ${data.moved.length}개 이동됨`);
  }
  await loadManifest();
}

async function rescan() {
  showScanOverlay(true);
  await fetch('/api/scan', {method: 'POST'});
  await pollScanProgress();
  showScanOverlay(false);
  await loadManifest();
  toast('✅ 재스캔 완료');
}

function showScanOverlay(show) {
  let ov = document.getElementById('scanOverlay');
  if (!ov && show) {
    ov = document.createElement('div');
    ov.id = 'scanOverlay';
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;display:flex;align-items:center;justify-content:center;';
    ov.innerHTML = `<div style="background:white;padding:32px;border-radius:10px;min-width:360px;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.3);">
      <div style="font-size:32px;margin-bottom:8px;">🔄</div>
      <h3 style="margin:0 0 12px 0;">CC 스캔 중...</h3>
      <div id="scanOvName" style="color:#666;font-size:12px;margin-bottom:10px;word-break:break-all;min-height:16px;">준비 중...</div>
      <div class="progress-bar" style="margin-top:6px;"><div id="scanOvBar" style="width:0%"></div></div>
      <div id="scanOvCount" style="margin-top:8px;font-size:12px;color:#888;">0 / 0</div>
    </div>`;
    document.body.appendChild(ov);
  }
  if (ov && !show) ov.remove();
}

async function pollScanProgress() {
  return new Promise((resolve) => {
    const tick = async () => {
      try {
        const r = await fetch('/api/scan-progress');
        const p = await r.json();
        const bar = document.getElementById('scanOvBar');
        const nameEl = document.getElementById('scanOvName');
        const cntEl = document.getElementById('scanOvCount');
        if (bar) {
          const pct = p.total ? Math.round((p.current / p.total) * 100) : 0;
          bar.style.width = pct + '%';
          if (nameEl) nameEl.textContent = p.name || '';
          if (cntEl) cntEl.textContent = `${p.current} / ${p.total}`;
        }
        if (!p.active) { resolve(); return; }
      } catch {}
      setTimeout(tick, 300);
    };
    tick();
  });
}

async function openTrash() {
  const res = await fetch('/api/trash');
  const data = await res.json();
  const list = document.getElementById('trash-list');
  document.getElementById('trash-summary').textContent = `(${data.items.length}개 파일, ${human(data.total_size)})`;
  if (!data.items.length) {
    list.innerHTML = '<div style="padding: 40px; text-align: center; color: #999; grid-column: 1/-1;">휴지통이 비어있어요</div>';
  } else {
    list.innerHTML = data.items.map((it, i) => {
      const fileName = it.original_path.split('/').pop();
      const thumbs = it.thumbs || [];
      let thumbHtml;
      if (thumbs.length) {
        thumbHtml = `<img class="thumb-img" src="/thumbs/${thumbs[0]}" style="width:100%;display:block;">`;
        if (thumbs.length > 1) {
          thumbHtml += `<div class="thumb-nav">
            <button class="thumb-btn thumb-prev" onclick="cycleThumb(event, this, -1)">◀</button>
            <span class="thumb-counter">1/${thumbs.length}</span>
            <button class="thumb-btn thumb-next" onclick="cycleThumb(event, this, 1)">▶</button>
          </div>`;
        }
      } else {
        thumbHtml = `<div class="no-thumb" style="padding:40px 8px; text-align:center; color:#999; font-size:11px; background:#eee;">썸네일 없음</div>`;
      }
      const needsPopover = fileName.length > 44;
      const nameFull = needsPopover ? `<div class="name-full">${escapeHtml(fileName)}</div>` : '';
      return `
        <label class="trash-tile item" data-thumbs='${JSON.stringify(thumbs).replace(/'/g, "&#39;")}' data-thumb-idx="0" style="background:white; display:block;">
          <input type="checkbox" value="${escapeHtml(it.path)}" onchange="updateTrashSelectedCount()" style="position:absolute; top:6px; left:6px; z-index:3; transform:scale(1.3);">
          ${thumbHtml}
          <div class="sz">${human(it.size)}</div>
          <div class="name">${escapeHtml(fileName)}</div>
          ${nameFull}
        </label>
      `;
    }).join('');
  }
  updateTrashSelectedCount();
  document.getElementById('trash-dialog').showModal();
}

let lastFailedPaths = [];
function showFailedDialog(movedCount, missingCount, errors) {
  lastFailedPaths = errors.map(e => e.path);
  const summary = `이동 성공: ${movedCount}개 · 이미 삭제된 파일 정리: ${missingCount}개 · 실패: ${errors.length}개`;
  document.getElementById('failed-summary').textContent = summary;
  document.getElementById('failed-list').innerHTML = errors.map(e => `
    <div style="padding: 6px; border-bottom: 1px solid #eee; font-size: 12px;">
      <div style="color: #d9534f; font-weight: 600;">${escapeHtml(e.reason)}</div>
      <div style="color: #666; margin-top: 2px; word-break: break-all;">${escapeHtml(e.path)}</div>
    </div>
  `).join('');
  document.getElementById('failed-dialog').showModal();
}
function clearFailedMarks() {
  for (const p of lastFailedPaths) delete state[p];
  saveMarks();
  document.getElementById('failed-dialog').close();
  render();
  toast(`✅ 실패한 ${lastFailedPaths.length}개 표시 해제됨`);
  lastFailedPaths = [];
}

function trashSelectAll(state) {
  document.querySelectorAll('#trash-list input[type=checkbox]').forEach(cb => cb.checked = state);
  updateTrashSelectedCount();
}

function updateTrashSelectedCount() {
  const n = document.querySelectorAll('#trash-list input:checked').length;
  document.getElementById('trash-selected-count').textContent = n;
}

function closeTrash() { document.getElementById('trash-dialog').close(); }

async function restoreSelected() {
  const paths = [...document.querySelectorAll('#trash-list input:checked')].map(el => el.value);
  if (!paths.length) { toast('선택된 파일이 없어요.'); return; }
  const res = await fetch('/api/restore', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths}),
  });
  const data = await res.json();
  toast(`✅ ${data.restored.length}개 복원됨`);
  await loadManifest();
  openTrash();
}

async function emptyTrash() {
  if (!(await customConfirm('휴지통을 완전히 비울까요? 되돌릴 수 없어요.', {okText:'비우기', danger:true}))) return;
  const res = await fetch('/api/empty-trash', {method: 'POST'});
  const data = await res.json();
  toast(`✅ ${data.count}개 완전 삭제 (${human(data.size_freed)} 확보)`);
  await postBulkAction();
}

async function restoreAll() {
  const paths = [...document.querySelectorAll('#trash-list input')].map(el => el.value);
  if (!paths.length) { toast('휴지통이 비어있어요.'); return; }
  if (!(await customConfirm(`${paths.length}개 파일 전체를 원위치로 복원할까요?`, {okText:'복원'}))) return;
  const res = await fetch('/api/restore', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths}),
  });
  const data = await res.json();
  toast(`✅ ${data.restored.length}개 복원됨${data.failed.length ? ` (${data.failed.length}개 실패)`:''}`);
  await postBulkAction();
}

async function deleteSelectedFromTrash() {
  const paths = [...document.querySelectorAll('#trash-list input:checked')].map(el => el.value);
  if (!paths.length) { toast('선택된 파일이 없어요.'); return; }
  if (!(await customConfirm(`선택한 ${paths.length}개를 완전히 삭제할까요? 되돌릴 수 없어요.`, {okText:'완전 삭제', danger:true}))) return;
  const res = await fetch('/api/delete-from-trash', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paths}),
  });
  const data = await res.json();
  toast(`✅ ${data.count}개 완전 삭제 (${human(data.size_freed)} 확보)`);
  await postBulkAction();
}

async function postBulkAction() {
  closeTrash();
  await loadManifest();
  // 매니페스트가 자동 업데이트되므로 재스캔 불필요.
  // 필요 시 사용자가 상단 '🔄 재스캔' 버튼 수동 클릭.
}

(function initSearch(){
  const s = document.getElementById('search');
  const clearBtn = document.getElementById('searchClear');
  const hist = document.getElementById('searchHistory');
  const HIST_KEY = 'ccm_search_history';
  const loadHist = () => { try { return JSON.parse(localStorage.getItem(HIST_KEY) || '[]'); } catch { return []; } };
  const saveHist = (q) => {
    if (!q) return;
    const arr = loadHist().filter(x => x !== q);
    arr.unshift(q);
    localStorage.setItem(HIST_KEY, JSON.stringify(arr.slice(0, 5)));
  };
  const removeHistItem = (q) => {
    const arr = loadHist().filter(x => x !== q);
    localStorage.setItem(HIST_KEY, JSON.stringify(arr));
    showHist();
  };
  const clearAllHist = () => {
    localStorage.setItem(HIST_KEY, '[]');
    hist.style.display = 'none';
  };
  window._ccmRemoveHist = removeHistItem;
  window._ccmClearHist = clearAllHist;
  const showHist = () => {
    const arr = loadHist();
    if (!arr.length || s.value) { hist.style.display = 'none'; return; }
    let html = '<div style="padding:6px 10px; font-size:11px; color:#888; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #eee;"><span>🕒 최근 검색</span><span onmousedown="event.preventDefault(); window._ccmClearHist()" style="cursor:pointer; color:#999; font-size:11px;">전체 지우기</span></div>';
    html += arr.map(q => `<div class="hist-item" data-q="${escapeHtml(q)}" style="padding:6px 10px; cursor:pointer; font-size:12px; display:flex; justify-content:space-between; align-items:center;"><span>${escapeHtml(q)}</span><span onmousedown="event.preventDefault(); event.stopPropagation(); window._ccmRemoveHist('${escapeAttr(q)}')" style="opacity:.5; cursor:pointer; padding:0 4px;">✕</span></div>`).join('');
    hist.innerHTML = html;
    hist.style.display = 'block';
    hist.querySelectorAll('.hist-item').forEach(el => {
      el.onmousedown = (ev) => {
        if (ev.target.tagName === 'SPAN' && ev.target.textContent === '✕') return;
        ev.preventDefault();
        s.value = el.dataset.q;
        currentFilter = s.value.toLowerCase();
        clearBtn.style.display = 'block';
        hist.style.display = 'none';
        render();
      };
    });
  };
  s.addEventListener('input', e => {
    currentFilter = e.target.value.toLowerCase().trim();
    clearBtn.style.display = e.target.value ? 'block' : 'none';
    hist.style.display = 'none';
    render();
  });
  s.addEventListener('focus', showHist);
  s.addEventListener('blur', () => { setTimeout(() => { hist.style.display = 'none'; }, 150); });
  s.addEventListener('change', () => saveHist(s.value.trim()));
  s.addEventListener('keydown', (e) => { if (e.key === 'Enter') saveHist(s.value.trim()); });
  clearBtn.addEventListener('click', () => {
    s.value = ''; currentFilter = ''; clearBtn.style.display = 'none'; render(); s.focus();
  });
})();
document.getElementById('sortBy').addEventListener('change', e => {
  sortBy = e.target.value;
  render();
});
document.getElementById('itemSortBy').addEventListener('change', e => {
  itemSortBy = e.target.value;
  render();
});
// 아이템 크기 슬라이더
(function initZoom() {
  const slider = document.getElementById('zoomSlider');
  const saved = localStorage.getItem('ccm_zoom');
  if (saved) { slider.value = saved; document.documentElement.style.setProperty('--h-thumb', saved + 'px'); }
  slider.addEventListener('input', e => {
    const v = e.target.value;
    document.documentElement.style.setProperty('--h-thumb', v + 'px');
    localStorage.setItem('ccm_zoom', v);
  });
})();
document.getElementById('groupSeg').querySelectorAll('button').forEach(b => {
  b.onclick = () => {
    groupBy = b.dataset.v;
    document.querySelectorAll('#groupSeg button').forEach(x => x.classList.toggle('on', x === b));
    render();
  };
});
document.getElementById('modeSeg').querySelectorAll('button').forEach(b => {
  b.onclick = () => {
    editMode = b.dataset.v;
    document.querySelectorAll('#modeSeg button').forEach(x => x.classList.toggle('on', x === b));
    // 모드 표시 힌트
    document.body.classList.toggle('mode-category', editMode === 'category');
    toast(editMode === 'category' ? '🏷️ 카테고리 편집 모드' : '🗑️ 삭제 선택 모드');
  };
});
document.getElementById('tglMarked').addEventListener('change', e => {
  showMarkedOnly = e.target.checked;
  render();
});
document.getElementById('tglCollapsed').addEventListener('change', e => {
  collapsedMode = e.target.checked;
  render();
});
document.getElementById('tglOverride').addEventListener('change', e => {
  showOverrideOnly = e.target.checked;
  render();
});
document.getElementById('tglHideTrashed').addEventListener('change', e => {
  hideTrashed = e.target.checked;
  render();
});
document.getElementById('tglHidePerma').addEventListener('change', e => {
  hidePerma = e.target.checked;
  render();
});

function selectAllFiltered() {
  // 현재 렌더된 모든 아이템 (필터/그룹 상관없이 화면에 보이는 것)을 다중선택에 추가
  const visible = document.querySelectorAll('.item');
  let added = 0;
  visible.forEach(el => {
    const p = el.dataset.path;
    if (p && !el.classList.contains('trashed') && !bulkSel.has(p)) {
      bulkSel.add(p);
      el.classList.add('selected');
      added++;
    }
  });
  if (!added) { toast('추가된 아이템이 없어요 (이미 다 선택되어 있거나 없음)'); return; }
  toast(`✅ ${added}개 추가로 선택됨 (총 ${bulkSel.size}개)`);
  updateBulkBar();
}

// ─────── 드래그 박스 선택 ───────
(function initDragBox() {
  const main = document.getElementById('main');
  let box = null;
  let startX = 0, startY = 0;
  let baseline = null;
  main.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (e.target.closest('.item, .creator-header, button, a, input, select, label')) return;
    e.preventDefault();
    startX = e.clientX; startY = e.clientY;
    baseline = new Set(bulkSel);  // 기존 선택 유지 (Shift/Cmd 눌렀을 때) 아니면 replace
    if (!(e.shiftKey || e.metaKey || e.ctrlKey)) {
      bulkSel.clear();
      document.querySelectorAll('.item.selected').forEach(el => el.classList.remove('selected'));
      baseline = new Set();
    }
    box = document.createElement('div');
    box.style.cssText = 'position:fixed; border:1.5px dashed #4a90e2; background:rgba(74,144,226,.15); pointer-events:none; z-index:500;';
    document.body.appendChild(box);
  });
  window.addEventListener('mousemove', (e) => {
    if (!box) return;
    const x = Math.min(e.clientX, startX), y = Math.min(e.clientY, startY);
    const w = Math.abs(e.clientX - startX), h = Math.abs(e.clientY - startY);
    box.style.left = x + 'px'; box.style.top = y + 'px';
    box.style.width = w + 'px'; box.style.height = h + 'px';
    // hit test
    const rect = {left:x, top:y, right:x+w, bottom:y+h};
    document.querySelectorAll('.item').forEach(el => {
      if (el.classList.contains('trashed')) return;
      const r = el.getBoundingClientRect();
      const intersects = !(r.right < rect.left || r.left > rect.right || r.bottom < rect.top || r.top > rect.bottom);
      const path = el.dataset.path;
      if (intersects) {
        if (!baseline.has(path)) { bulkSel.add(path); el.classList.add('selected'); }
      } else {
        if (!baseline.has(path) && bulkSel.has(path)) { bulkSel.delete(path); el.classList.remove('selected'); }
      }
    });
    updateBulkBar();
  });
  window.addEventListener('mouseup', () => {
    if (box) { box.remove(); box = null; baseline = null; }
  });
})();

// ─────── 되돌리기 (Cmd+Z) ───────
const undoStack = [];  // {type, data} 각 액션
const MAX_UNDO = 30;

function pushUndo(action) {
  undoStack.push(action);
  if (undoStack.length > MAX_UNDO) undoStack.shift();
  document.getElementById('undoBtn').disabled = false;
}

async function undo() {
  const a = undoStack.pop();
  document.getElementById('undoBtn').disabled = undoStack.length === 0;
  if (!a) { toast('되돌릴 것 없음'); return; }
  if (a.type === 'mark') {
    if (a.wasMarked) state[a.path] = true; else delete state[a.path];
    saveMarks();
    document.querySelectorAll(`.item[data-path="${CSS.escape(a.path)}"]`).forEach(el => el.classList.toggle('marked', !!state[a.path]));
    updateFooter();
    toast('↶ 삭제 표시 되돌림');
  } else if (a.type === 'category') {
    // 각 파일의 이전 카테고리 복원
    for (const [p, prev] of Object.entries(a.prev)) {
      await fetch('/api/set-category', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: p, category: prev || ''}),
      });
    }
    toast(`↶ ${Object.keys(a.prev).length}개 카테고리 되돌림`);
    await loadManifest();
  } else if (a.type === 'delete') {
    // 휴지통에서 원래대로 복원
    const trashPaths = a.movedTrashPaths;
    if (trashPaths.length) {
      await fetch('/api/restore', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({paths: trashPaths}),
      });
      // 삭제 표시도 되살리기
      for (const p of a.originalPaths) state[p] = true;
      saveMarks();
      toast(`↶ ${trashPaths.length}개 파일 복원`);
      await loadManifest();
    }
  }
}

// Cmd+Z / Ctrl+Z 리스너
window.addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'z' && !e.shiftKey) {
    if (e.target.matches('input, textarea')) return;
    e.preventDefault();
    undo();
  }
});

// 기존 toggle을 감싸서 undo 추적
const _origToggle = toggle;
toggle = function(path) {
  const wasMarked = !!state[path];
  _origToggle(path);
  pushUndo({type: 'mark', path, wasMarked});
};

// 배치 카테고리 지정 시 undo 추적
const _origApply = applyBulkCategory;
applyBulkCategory = async function() {
  const paths = [...bulkSel];
  const prev = {};
  if (MANIFEST) {
    for (const f of (MANIFEST.folders || MANIFEST.creators || [])) {
      for (const it of f.items) {
        if (paths.includes(it.path)) {
          prev[it.path] = it.override ? it.primary_cat : null;
        }
      }
    }
  }
  await _origApply();
  if (Object.keys(prev).length) pushUndo({type: 'category', prev});
};

// 개별 카테고리 지정 시 undo 추적
const _origApplyToPaths = applyToPaths;
applyToPaths = async function(paths, category) {
  const prev = {};
  if (MANIFEST) {
    for (const f of (MANIFEST.folders || MANIFEST.creators || [])) {
      for (const it of f.items) {
        if (paths.includes(it.path)) {
          prev[it.path] = it.override ? it.primary_cat : null;
        }
      }
    }
  }
  await _origApplyToPaths(paths, category);
  if (Object.keys(prev).length) pushUndo({type: 'category', prev});
};

// 삭제도 undo 추적
const _origPerformDelete = performDelete;
performDelete = async function() {
  const originalPaths = Object.keys(state);
  await _origPerformDelete();
  // 잘 이동된 것들만 undo 스택에 저장
  // (이미 loadManifest 후라 정보 얻으려면 별도 처리 필요)
  // 간단하게: 다시 스캔해서 trashPaths를 얻거나 API 응답에서 얻는 방식
  // 여기서는 originalPaths를 저장하고 restore 시 그대로 요청
  if (originalPaths.length) {
    pushUndo({type: 'delete', originalPaths, movedTrashPaths: originalPaths});
  }
};

// ─────── 통계 & 오버라이드 import/export ───────
function exportOverrides() {
  window.location = '/api/overrides/export';
}
function importOverrides() {
  const inp = document.getElementById('importOvFile');
  inp.onchange = async () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    try {
      const text = await f.text();
      const parsed = JSON.parse(text);
      const payload = parsed.overrides ? parsed : {overrides: parsed};
      const res = await fetch('/api/overrides/import', {
        method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload),
      });
      const data = await res.json();
      toast(`📥 ${data.imported}개 추가/변경됨 (총 ${data.total}개)`);
      await loadManifest();
    } catch (e) {
      toast('❌ 파일 형식 오류');
    }
    inp.value = '';
  };
  inp.click();
}

async function openStats() {
  const res = await fetch('/api/stats');
  const s = await res.json();
  let dlg = document.getElementById('stats-dialog');
  if (!dlg) {
    dlg = document.createElement('dialog');
    dlg.id = 'stats-dialog';
    dlg.style.cssText = 'max-width:640px;width:90vw;';
    document.body.appendChild(dlg);
  }
  const maxSz = Math.max(1, ...(s.top_creators || []).map(c => c.size));
  const fmtDate = t => t ? new Date(t * 1000).toLocaleDateString('ko-KR') : '-';
  dlg.innerHTML = `
    <button class="dlg-close" onclick="this.parentElement.close()">✕</button>
    <h2 style="margin:0 0 12px 0;">📊 통계</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px;">
      <div style="padding:10px;background:#f7f7f7;border-radius:6px;"><div style="font-size:11px;color:#666;">전체 파일</div><div style="font-size:20px;font-weight:600;">${s.total_files.toLocaleString()}</div></div>
      <div style="padding:10px;background:#f7f7f7;border-radius:6px;"><div style="font-size:11px;color:#666;">전체 용량</div><div style="font-size:20px;font-weight:600;">${human(s.total_size)}</div></div>
      <div style="padding:10px;background:#f7f7f7;border-radius:6px;"><div style="font-size:11px;color:#666;">폴더/창작자</div><div style="font-size:20px;font-weight:600;">${s.creator_count}</div></div>
      <div style="padding:10px;background:#f7f7f7;border-radius:6px;"><div style="font-size:11px;color:#666;">수동 카테고리</div><div style="font-size:20px;font-weight:600;">${s.overrides_count}</div></div>
      <div style="padding:10px;background:#f7f7f7;border-radius:6px;"><div style="font-size:11px;color:#666;">휴지통</div><div style="font-size:14px;">${s.trash_count}개 · ${human(s.trash_size)}</div></div>
      <div style="padding:10px;background:#f7f7f7;border-radius:6px;"><div style="font-size:11px;color:#666;">최신 / 최고령</div><div style="font-size:12px;">${fmtDate(s.newest_mtime)}<br>${fmtDate(s.oldest_mtime)}</div></div>
      <div style="padding:10px;background:#f7f7f7;border-radius:6px;"><div style="font-size:11px;color:#666;">평균 크기 / 썸네일</div><div style="font-size:12px;">${human(s.avg_size||0)}<br>${(s.total_thumbs||0).toLocaleString()}개</div></div>
    </div>
    <h3 style="margin:8px 0;font-size:14px;">🏆 창작자 Top 10 (용량 기준)</h3>
    <div style="margin-bottom:16px;">
      ${(s.top_creators || []).map(c => `
        <div style="margin:4px 0;">
          <div style="display:flex;justify-content:space-between;font-size:12px;"><span>${escapeHtml(c.name)}</span><span style="color:#666;">${c.count}개 · ${human(c.size)}</span></div>
          <div style="height:6px;background:#eee;border-radius:3px;overflow:hidden;"><div style="height:100%;background:#4a90e2;width:${(c.size/maxSz*100).toFixed(1)}%;"></div></div>
        </div>`).join('')}
    </div>
    <h3 style="margin:8px 0;font-size:14px;">🏷️ 카테고리 분포</h3>
    <div style="display:flex;flex-wrap:wrap;gap:6px;">
      ${(s.categories || []).map(c => `<span style="padding:4px 8px;background:#f0f0f0;border-radius:12px;font-size:12px;">${escapeHtml(c.name)} <b>${c.count}</b> <span style="color:#888;">(${human(c.size)})</span></span>`).join('')}
    </div>`;
  dlg.showModal();
}

// ─────── 키보드 네비게이션 ───────
(function initKbNav(){
  let focusIdx = -1;
  const getItems = () => Array.from(document.querySelectorAll('#main .item')).filter(el => !el.classList.contains('trashed') && !el.classList.contains('perma-deleted'));
  const setFocus = (idx) => {
    const items = getItems();
    if (!items.length) return;
    idx = Math.max(0, Math.min(items.length - 1, idx));
    document.querySelectorAll('.item.kb-focus').forEach(el => el.classList.remove('kb-focus'));
    focusIdx = idx;
    items[idx].classList.add('kb-focus');
    items[idx].scrollIntoView({block: 'nearest', behavior: 'smooth'});
  };
  const columns = () => {
    const items = getItems();
    if (items.length < 2) return 1;
    const y0 = items[0].getBoundingClientRect().top;
    let n = 1;
    for (let i = 1; i < items.length; i++) {
      if (Math.abs(items[i].getBoundingClientRect().top - y0) < 5) n++;
      else break;
    }
    return n;
  };
  window.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea, select')) return;
    if (document.querySelector('dialog[open]')) return;
    const items = getItems();
    if (!items.length) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); setFocus(focusIdx < 0 ? 0 : focusIdx + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); setFocus(focusIdx < 0 ? 0 : focusIdx - 1); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setFocus(focusIdx < 0 ? 0 : focusIdx + columns()); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setFocus(focusIdx < 0 ? 0 : focusIdx - columns()); }
    else if (e.key === 'Enter' || e.key === ' ') {
      if (focusIdx < 0) return;
      e.preventDefault();
      const el = items[focusIdx];
      const p = el.dataset.path;
      if (p) toggle(p);
    } else if (e.key === 'Escape') {
      if (bulkSel.size > 0) { e.preventDefault(); clearBulkSel(); }
      document.querySelectorAll('.item.kb-focus').forEach(el => el.classList.remove('kb-focus'));
      focusIdx = -1;
    }
  });
})();

// ─────── customConfirm ───────
function customConfirm(message, opts) {
  opts = opts || {};
  const okText = opts.okText || '확인';
  const cancelText = opts.cancelText || '취소';
  const danger = !!opts.danger;
  return new Promise((resolve) => {
    let dlg = document.getElementById('customConfirmDlg');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'customConfirmDlg';
      dlg.style.cssText = 'min-width:300px;max-width:480px;';
      document.body.appendChild(dlg);
    }
    dlg.innerHTML = `
      <div id="ccMsg" style="white-space:pre-wrap;margin-bottom:16px;font-size:14px;line-height:1.5;"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button id="ccCancel" type="button">${escapeHtml(cancelText)}</button>
        <button id="ccOk" type="button" class="${danger ? 'primary' : 'blue'}">${escapeHtml(okText)}</button>
      </div>`;
    dlg.querySelector('#ccMsg').textContent = message;
    const close = (v) => {
      dlg.removeEventListener('keydown', onKey);
      try { dlg.close(); } catch {}
      resolve(v);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
      else if (e.key === 'Enter') { e.preventDefault(); close(true); }
    };
    dlg.addEventListener('keydown', onKey);
    dlg.querySelector('#ccOk').onclick = () => close(true);
    dlg.querySelector('#ccCancel').onclick = () => close(false);
    dlg.showModal();
    setTimeout(() => dlg.querySelector('#ccOk').focus(), 20);
  });
}

loadManifest();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 조용히

    def _send(self, code, content, ctype="text/html; charset=utf-8"):
        if isinstance(content, str):
            content = content.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        # 캐시 방지: HTML/API는 항상 최신, 썸네일은 캐시 가능
        if ctype.startswith("text/html") or "json" in ctype:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        if path == "/" or path == "/index.html":
            self._send(200, HTML_PAGE)
            return
        if path == "/api/manifest":
            m = load_manifest()
            if not m:
                m = scan_cc()
            m["meta_categories"] = {name: {"icon": icon, "subs": subs} for name, (icon, subs) in META_CATEGORIES.items()}
            m["all_categories"] = [{"name": name, "icon": icon} for name, icon, _ in CATEGORIES]
            self._json(m)
            return
        if path == "/api/trash":
            self._json(list_trash())
            return
        if path == "/api/scan-progress":
            self._json(dict(SCAN_PROGRESS))
            return
        if path == "/api/config":
            self._json({"sims_root": str(SIMS_ROOT)})
            return
        if path == "/api/overrides/export":
            from datetime import datetime
            data = _load_overrides()
            body = json.dumps({"exported_at": datetime.now().isoformat(), "count": len(data), "overrides": data}, ensure_ascii=False, indent=2).encode()
            fn = f"overrides-{datetime.now().strftime('%Y%m%d')}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fn}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/stats":
            self._json(_compute_stats())
            return
        if path.startswith("/thumbs/"):
            name = path[len("/thumbs/"):]
            fp = THUMBS_DIR / name
            if fp.exists() and fp.is_file():
                mime = "image/png" if name.lower().endswith(".png") else "image/jpeg"
                self._send(200, fp.read_bytes(), mime)
            else:
                self._send(404, "not found")
            return
        self._send(404, "not found")

    def do_POST(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if path == "/api/delete":
            self._json(move_to_trash(data.get("paths", [])))
        elif path == "/api/restore":
            self._json(restore_from_trash(data.get("paths", [])))
        elif path == "/api/config":
            new_path = data.get("sims_root", "").strip()
            if not new_path:
                self._json({"ok": False, "error": "경로가 비어있음"})
                return
            expanded = Path(new_path).expanduser().resolve()
            if not expanded.exists():
                self._json({"ok": False, "error": f"경로가 존재하지 않음: {expanded}"})
                return
            if not (expanded / "Mods").exists():
                self._json({"ok": False, "error": f"Mods 폴더 없음: {expanded}/Mods"})
                return
            _CONFIG_PATH.write_text(json.dumps({"sims_root": str(expanded)}, indent=2))
            # 전역 경로 업데이트
            global SIMS_ROOT, MODS, CC_ROOT, TRASH_DIR
            SIMS_ROOT = expanded
            MODS = SIMS_ROOT / "Mods"
            CC_ROOT = MODS / "CC FeaturedCreators"
            self._json({"ok": True, "sims_root": str(expanded)})
        elif path == "/api/scan":
            # 백그라운드 스레드에서 스캔, 프론트는 /api/scan-progress로 폴링
            if not SCAN_PROGRESS["active"]:
                SCAN_PROGRESS.update({"active": True, "current": 0, "total": 0, "name": "시작 중..."})
                t = threading.Thread(target=scan_cc, daemon=True)
                t.start()
            self._json({"started": True})
        elif path == "/api/empty-trash":
            self._json(empty_trash())
        elif path == "/api/delete-from-trash":
            self._json(delete_from_trash(data.get("paths", [])))
        elif path == "/api/overrides/import":
            merged = _load_overrides()
            incoming = data.get("overrides") if isinstance(data.get("overrides"), dict) else (data if all(isinstance(v, str) for v in data.values()) else {})
            added = 0
            for k, v in (incoming or {}).items():
                if isinstance(k, str) and isinstance(v, str) and v:
                    if merged.get(k) != v:
                        merged[k] = v; added += 1
            _save_overrides(merged)
            self._json({"imported": added, "total": len(merged)})
        elif path == "/api/set-category-batch":
            # {paths: [...], category: "..."}
            paths = data.get("paths", [])
            cat = data.get("category")
            for p in paths:
                set_category_override(p, cat)
            # 매니페스트 즉시 반영
            m = load_manifest()
            if m:
                path_set = set(paths)
                icon = "📝"
                for name, ic, _ in CATEGORIES:
                    if name == cat: icon = ic; break
                for folder in m.get("folders", []) + m.get("creators", []):
                    for it in folder.get("items", []):
                        if it["path"] in path_set:
                            if cat:
                                it["cats"] = [cat]
                                it["primary_cat"] = cat
                                it["cat_icon"] = icon
                                it["override"] = True
                                it["casp"] = False
                            else:
                                it["override"] = False
                MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))
            self._json({"count": len(paths), "category": cat})
        elif path == "/api/set-category":
            # {path, category (or "" to remove)}
            result = set_category_override(data.get("path"), data.get("category"))
            # 매니페스트에도 즉시 반영 (재스캔 없이)
            m = load_manifest()
            if m:
                target = data.get("path")
                cat = data.get("category")
                for folder in m.get("folders", []) + m.get("creators", []):
                    for it in folder.get("items", []):
                        if it["path"] == target:
                            if cat:
                                # 아이콘 찾기
                                icon = "📝"
                                for name, ic, _ in CATEGORIES:
                                    if name == cat: icon = ic; break
                                it["cats"] = [cat]
                                it["primary_cat"] = cat
                                it["cat_icon"] = icon
                                it["override"] = True
                                it["casp"] = False
                            else:
                                # 재계산 필요 - 이 파일만 다시 스캔
                                it["override"] = False
                            break
                MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False))
            self._json(result)
        else:
            self._send(404, "not found")


def run():
    print("Sims 4 CC Manager")
    print("=" * 50)
    print(f"CC 폴더:  {CC_ROOT}")
    print(f"휴지통:   {TRASH_DIR}")
    print(f"캐시:     {APP_STATE}")
    print()

    if not load_manifest():
        print("첫 실행 - CC 스캔 중... (몇 초 걸림)")
        def cb(i, n, name):
            if i % 20 == 0 or i == n:
                print(f"  [{i}/{n}] {name}")
        scan_cc(progress_cb=cb)
        print("스캔 완료")
        print()

    url = f"http://localhost:{PORT}/"
    print(f"서버 시작: {url}")
    print("종료하려면 Ctrl+C")
    print()

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        HTTPServer(("localhost", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    run()
