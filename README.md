# Sims 4 CC Manager

심즈4 CC(커스텀 콘텐츠) 정리 도구.

`Mods/` 폴더의 `.package` 파일에서 썸네일을 뽑아 브라우저 갤러리로 보여주고,
필요 없는 항목을 골라 안전하게 정리할 수 있게 해준다.

## 요구사항

- macOS
- Python 3.8+
- 심즈4 설치 경로: `~/Games/Electronic Arts/The Sims 4/`

## 실행

```bash
python3 sims_cc_manager.py
```

또는 Finder에서 `CC Manager.command` 를 더블클릭.

## 주요 기능

- `.package` 파일 DBPF v2 파싱 → CAS 썸네일(JPEG) 추출
- CASP 리소스 파싱 + 파일명 휴리스틱으로 카테고리 자동 분류
- 브라우저 UI: 창작자/카테고리별 그룹, 검색, 필터, 정렬
- 안전한 삭제: `Mods/.cc_trash/` 로 이동 (복원 가능)
- 수동 카테고리 오버라이드
- 다중 선택, 일괄 카테고리 지정
- Cmd+Z 되돌리기

## 폴더 구조

```
~/Games/Electronic Arts/The Sims 4/
├── Mods/
│   ├── CC FeaturedCreators/   ← 스캔 대상
│   └── .cc_trash/             ← 이동된 파일 (복원 가능)
└── .cc_manager/
    ├── manifest.json          ← 스캔 결과 캐시
    ├── thumbs/                ← 추출된 썸네일
    ├── category_overrides.json
    └── trash_manifest.json
```
