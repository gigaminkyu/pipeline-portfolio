"""카테고리 정본 — 이름·슬러그·티스토리명 대응을 여기서만 정의한다.

run.py / topic.py / internal_links.py / tistory_publish.py 에 네 벌 흩어져 있던
대응표를 모았다(2026-08-23). 새 카테고리는 ALL 한 줄이면 끝난다.
"""
import re

# (네이버 카테고리명, 드래프트 슬러그, 티스토리 카테고리명)
ALL = [
    ("반도체", "semi", "반도체"),
    ("미국 개별주 분석", "stock", "미국주식"),
    ("내가 보는 거시경제", "macro", "거시경제"),
    ("AI", "ai", "AI·자동화"),
    ("주식 기초 정보!", "term", None),
]

# 매일 자동발행 로테이션. 거시(macro)는 2026-08-18 컷, 코인 카테고리는 2026-09-02 유저 지시로 폐지(네이버·티스토리 둘 다).
DAILY = [(n, s) for n, s, _ in ALL if s in ("semi", "stock", "ai")]

SLUG_CAT = {s: n for n, s, _ in ALL}   # 슬러그 → 네이버 카테고리명
TCAT = {n: t for n, _, t in ALL if t}   # 네이버 카테고리명 → 티스토리 카테고리명


def from_stem(stem):
    """드래프트 stem(파일명 어간)에서 카테고리명. 못 찾으면 "".

    leftover 회수본의 -rMMDD 꼬리는 벗기고 본다 — 안 벗기면 rsplit 이
    'r0822'를 슬러그로 읽어 카테고리를 잃는다(2026-08-23 관련글 목록 실증).
    """
    s = re.sub(r"(-r\d{4})+$", "", stem)
    if "-13f-" in s:
        return "13F"
    toks = s.split("-")[3:]   # 날짜(3토큰) 뒤
    # 마지막 토큰(일일글 {날짜}-{슬러그}) → 첫 토큰({날짜}-ai-tokenomics 같은 스킬 글) 순서로
    for t in (toks[-1], toks[0]) if toks else ():
        if t in SLUG_CAT:
            return SLUG_CAT[t]
    return ""


if __name__ == "__main__":
    assert from_stem("2026-08-22-ai") == "AI"
    assert from_stem("2026-08-22-ai-r0822") == "AI"   # 회수 꼬리
    assert from_stem("2026-08-10-13f-buffett") == "13F"
    assert from_stem("2026-08-23-ai-tokenomics") == "AI"   # 스킬 글(소재 접미사)
    assert from_stem("2026-08-23-perk-housing") == ""             # perk 는 카테고리 없음(정상)
    assert from_stem("2026-08-22-없는거") == ""
    assert [s for _, s in DAILY] == ["semi", "stock", "ai"]
    assert TCAT["미국 개별주 분석"] == "미국주식"
    print("ok")
