"""발행 전 self-verify 루프 — 초안을 체크리스트로 검문하고 안전한 것만 스스로 고친다.

영상(Anthropic) "루프가 자기 실수를 고친다"의 구현. 흐름:
    WRITE(draft.py) → [CRITIQUE(Fable) → REVISE(Opus)]×N → GATE(review.py veto) → 발행

이 모듈은 CRITIQUE→REVISE 루프만 담당한다. **절대 발행하지 않는다.**
최종 발행 결정은 기존 review.py veto + 사람이 쥔다.

규칙은 코드가 아니라 wiki/블로그 발행 체크리스트.md 에 있다(거기서 편집·축적).

하드캡(토큰폭주·무한루프 차단):
- MAX_LOOPS = 2  (critique→revise 사이클)
- LLM 호출 ≤ 5회  (초기 critique 1 + (critique+revise)×2)
- REVISE 결과가 원문의 60% 미만으로 쪼그라들면 거부(퇴행 방지)
- REVISE는 사실(URL·수치)을 지어내지 않는다 — 그건 사람에게 넘김
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CHECKLIST = ROOT / "wiki" / "블로그 발행 체크리스트.md"

_FRONTMATTER = re.compile(r"^---\n.*?\n---\n", re.S)

MAX_LOOPS = 1   # 2026-09-03 안건 15①: CRITIQUE 폐지, REVISE 최대 1회(코드검문에 걸린 게 있을 때만)
MIN_KEEP_RATIO = 0.6  # revise 후 본문이 이 비율 미만이면 퇴행으로 보고 거부

OPUS = "claude-opus-5"   # 2026-07-28 유저 승인으로 4.8 → 5 상향. REVISE는 상시 경로다.
# 2026-08-27 토큰 최적화: 채점(CRITIQUE)까지 Opus로 돌려서 초안 1건당 Opus 5회였다.
# 글을 '쓰는' REVISE 만 Opus 로 남기고 체크리스트 채점은 Sonnet 으로 내린다.
# 회귀 감시: 로그의 `[verify] ok= ... 남은지적=N` 이 눈에 띄게 줄면(=놓치는 것) 되돌려라.
CRITIC = "claude-sonnet-5"
                         # 2026-08-19 유저 지시로 CRITIQUE도 Fable→Opus 5 (Fable 과사용 정리).


def _checklist_text():
    return CHECKLIST.read_text() if CHECKLIST.exists() else ""


def _body_only(md):
    """메타 꼬리(### 태그/테마/메모)는 검문 본문에서 뺀다."""
    from metatail import split_meta
    return split_meta(md)[0].rstrip()


CRITIQUE_PROMPT = """너는 블로그 발행 직전 초안을 검문하는 편집장이다.
아래 체크리스트의 각 항목을 초안에 대해 PASS/FAIL로 판정해라.

**주의**: 네 학습 시점 이후의 기업·수치·발표는 네가 몰라도 정상이다. 모르는 고유명사를
"존재하지 않는다"고 FAIL 처리하지 마라. 판정 대상은 '초안이 체크리스트를 지켰는가'뿐이다.
플랫폼: {platform}

--- 체크리스트 ---
{checklist}

--- 초안 ---
{body}
--- 초안 끝 ---

반드시 아래 형식으로만 답해라(다른 말 금지). 항목명은 체크리스트의 굵은 이름(출처링크/한계표시/수치근거/제목/AI티/훅/구조/마무리/태그/이미지 등)을 쓴다:
[결과]
- 항목명 | PASS 또는 FAIL | FAIL이면 무엇이 문제인지 한 줄
- ...
[종합] 발행가능 또는 수정필요
"""

REVISE_PROMPT = """너는 블로그 초안을 고치는 에디터다. 아래 지적된 항목들만 고쳐라.

**철칙**:
- 지적된 항목만 손댄다. 나머지 문장·사실·톤은 그대로 둔다.
- **사실을 지어내지 마라.** 없는 출처 URL을 창작하거나, 없는 수치를 만들지 마라.
  출처 URL이 초안에 없으면 링크를 창작하지 마라. 대신 **인용 자체를 지우고 논지만 남겨라** —
  X 계정 인용, 링크 없는 헤드라인 인용이 여기 해당한다("시장에서 나온 해석은 ~"처럼 일반화하면 된다).
  지우면 논지가 무너지는 대목이면 맨 아래 `### 메모` 절에 "무엇을 확인 못 했는지"로 옮겨라.
  **본문이나 출처 절에 `한계 표시 — ...` 줄을 새로 쓰는 건 금지다** — 메모는 발행 때 잘리지만
  본문에 쓴 고백은 독자에게 그대로 나가고 봇 티가 난다(2026-08-04 유저 지적).
  **본문에 `[링크확인]` 같은 편집용 표식이나 HTML 주석을 남기지 마라** — 독자에게 그대로 노출되고
  발행 게이트에서 통째로 반려된다(2026-07-29 stock·macro 두 편이 이걸로 막혔다).
  안전하게 고칠 수 있는 것만 고친다: 상투구 제거, 제목 길이·키워드,
  소제목/구조, 마무리 문단, 톤. 사실 확인이 필요한 지적은 손대지 말고 그대로 남겨라.
- **수치는 절대 지우지 마라. 근거를 붙여라.** '수치근거' 지적이 왔을 때 %·배수·금액을
  통째로 빼거나 "8분의 1 수준" 같은 뭉뚱그린 말로 바꾸면 안 된다 — 그건 고친 게 아니라
  글의 근거를 없앤 거다. 고치는 방법은 하나뿐이다: **그 수치를 낸 곳의 이름을 문장에 붙인다**
  단, **출처명은 문장 앞머리가 아니라 뒤에 붙인다** — "포브스 보도를 보면 애플은 …"처럼
  문단마다 출처로 시작하면 읽기 불편하다(2026-08-01 유저 지적). "애플은 … 올렸다고 포브스가
  전했어요", "메모리는 … 30%를 먹어요, Tom's Hardware 집계예요"처럼 주어를 앞에 두고
  출처를 뒤로 뺀다. 한 문단에 출처명은 1회면 충분하다. 아래 출처 섹션에 URL이 이미 있으면
  그것으로 충분하니 본문에 링크를 또 박지 마라(출처는 맨 아래 몰아쓰는 게 이 블로그 규칙이다).
  출처를 도무지 못 찾는 수치만, 그때만 문장째 뺀다.
  (2026-08-01: REVISE가 45%·서버 원가·capex 합계를 이 지적 하나로 전부 지워 글이 빈 논지만 남았다.)
- **분량이 짧다는 지적은 무시해라.** 살을 붙이거나 같은 말을 다시 쓰지 마라 — 그건 재료가
  모자란다는 신호지 문장을 늘리라는 뜻이 아니다. 분량은 사람이 판단한다.
- 플랫폼 톤 유지 — {platform}.

--- 쓸 수 있는 출처 (이 글의 조사 단계에서 실제로 수집된 URL) ---
{materials}
**이 목록에 있는 URL만 쓸 수 있다. 목록에 없는 URL은 절대 창작하지 마라.**
'출처링크'·'수치근거' 지적은 이 목록에서 맞는 걸 골라 출처 섹션에 URL로 넣어 해결해라.
목록에 맞는 게 없으면 그때만 인용을 지운다.

--- 이 글의 기준일: {day} ---
'수치+날짜 동반' 지적은 이 날짜를 쓰면 된다 — 지어내는 게 아니라 발행일이다.
"8월 26일 발표 기준", "{day} 종가 기준"처럼 수치 옆에 붙인다. 모르는 시점은 붙이지 마라.

--- 고쳐야 할 항목 ---
{fails}

--- 원본 초안 ---
{body}
--- 원본 끝 ---

고친 초안 전체를 마크다운으로만 출력해라(설명·인사말·코드펜스 금지). 제목 `# ` 부터 시작한다.
"""



def _materials(md_path):
    """그 글의 조사 로그에서 URL 목록과 기준일을 꺼낸다.

    지적 61건 중 26건(43%)이 '출처 URL 없음'인데 REVISE 는 URL 창작이 금지라
    고칠 수단이 아예 없었다. 재료는 이미 state/research-{날짜}-{카테고리}.md 에
    13~20개씩 쌓여 있다 — 안 물려줬을 뿐이다(2026-08-27).
    """
    import cats
    stem = Path(md_path).stem
    m = re.match(r"(\d{4}-\d{2}-\d{2})-", stem)
    if not m:
        return "", ""
    day = m.group(1)
    cat = cats.from_stem(stem)
    f = ROOT / "state" / f"research-{day}-{cat}.md"
    if not (cat and f.exists()):
        return "", day
    urls = []
    for line in f.read_text().splitlines():
        u = re.search(r"https?://[^\s)\]\"'<>]+", line)
        if u and u.group(0) not in urls:
            # URL 이 있는 줄 통째로 준다 — 매체명·제목이 같이 있어야 문장에 붙일 수 있다
            urls.append(u.group(0))
            if len(urls) >= 25:
                break
    return "\n".join(f"- {u}" for u in urls), day

def _parse_critique(raw):
    """[결과] 블록에서 FAIL 항목만 뽑는다. 반환: (fails:list[(name,reason)], verdict)."""
    fails = []
    for line in raw.splitlines():
        m = re.match(r"\s*[-*]\s*(.+?)\s*\|\s*(PASS|FAIL)\s*\|?\s*(.*)", line, re.I)
        if m and m.group(2).upper() == "FAIL":
            fails.append((m.group(1).strip(), m.group(3).strip()))
    verdict = "수정필요"
    if re.search(r"\[종합\]\s*발행가능", raw):
        verdict = "발행가능"
    return fails, verdict


def _strip_fence(txt):
    return re.sub(r"^```(?:markdown)?\n(.*)\n```$", r"\1", txt.strip(), flags=re.S).strip()


def _finalize(body, tail, changed):
    """루프를 빠져나온 본문에 draft.normalize 를 한 번 더 먹인다.

    REVISE 가 '한계 표시 — ...' 줄을 새로 써 붙이는 사고가 반복돼서(2026-08-04 13F편 5문단),
    프롬프트 금지만으로는 못 막는다. normalize 가 그 줄을 '### 메모'로 결정론적으로 옮긴다.
    """
    from draft import normalize
    final = normalize(body + tail)
    return final, changed or final != body + tail



# ── LLM 0회 코드 검문 (2026-09-02 /토론 11-A·12) ────────────────────────
# 반환: (fixable, log_only). fixable 은 revise 프롬프트에 넣어 LLM 이 고칠 수 있는 것,
# log_only 는 코드로 못 고치는 것(자체 차트) — 세서 로그에만, 게이트 아님.
_PRIMARY = re.compile(
    r"\]\(https?://[^)\s]*(sec\.gov|dart\.fss\.or\.kr|arxiv\.org|doi\.org|openreview\.net|ssrn\.com|"
    r"fred\.stlouisfed\.org|bls\.gov|federalreserve\.gov|ecb\.europa\.eu|imf\.org|oecd\.org|"
    r"bok\.or\.kr|kostat\.go\.kr|kind\.krx\.co\.kr|\.go\.kr|korea\.kr|nature\.com|science\.org|"
    r"github\.com|huggingface\.co|jedec\.org|/ir\.|/investors?[./]|/investor-relations|\.pdf\b)", re.I)


def code_checks(body):
    head = body[:600]
    fixable = []
    if not re.search(r"[가-힣A-Za-z0-9)]+(은|는|이란|란)\s.{2,80}(이다|다\.|입니다)", head):
        fixable.append(("정의문", "첫 600자 안에 'X는 ~이다' 꼴 정의 문장이 없다 — AI 검색 인용용 한 줄 정의를 넣어라"))
    if not any(h.rstrip().endswith("?") for h in re.findall(r"^##\s+(.+)$", body, re.M)):
        fixable.append(("질문형 소제목", "'?'로 끝나는 ## 소제목이 하나도 없다 — 검색 질문 그대로인 소제목을 1개 이상"))
    if not re.search(r"\d{4}-\d{2}-\d{2} 기준|\d{4}년 \d{1,2}월 \d{1,2}일 기준", body):
        fixable.append(("날짜 기준", "'YYYY-MM-DD 기준' 시점 표기가 없다 — 수치 첫 등장 근처에 넣어라"))
    if not re.search(r"반대로|반대 신호|반론|하방", body):
        fixable.append(("반대로 문단", "반대 시나리오(반대로/반론/하방) 문단이 없다 — 1문단 추가"))
    if len(re.findall(r"\]\(https?://", body)) < 2:
        fixable.append(("원문 링크", "출처 하이퍼링크가 2개 미만이다 — 1차 자료 링크를 이름에 걸어라(맨 URL 금지)"))
    # 2026-09-03 안건 18①: 자기 해석 프레임 1개 — 편집 문제라 REVISE 가 고칠 수 있다(fixable).
    if not re.search(r"통념|약한 고리|내 판|내 해석|내 생각|내 결론|결론부터|내가 보기엔|이렇게 읽는다", body):
        fixable.append(("해석 프레임", "글쓴이의 해석 프레임(통념→약한 고리→내 판, 또는 '결론부터'/'내 해석') 문장이 없다 — 사실 나열 뒤에 '내 판'을 1문단 세워라"))
    log_only = []
    if not re.search(r"!\[", body):
        log_only.append(("자체 차트", "본문에 이미지(자체 차트)가 0개"))
    # 2026-09-03 안건 18①: 1차 자료(논문·공시·원문 데이터) 하이퍼링크 1개 — REVISE 가 없는 논문·공시를
    # 지어내면 안 되므로 로그만(materials 에 없으면 못 채운다). 허용 도메인은 최근 초안에 실제 나온 것 기준.
    if not _PRIMARY.search(body):
        log_only.append(("1차 자료", "논문·공시·원문 데이터(sec.gov·dart·arxiv·통계청·중앙은행·IR·PDF 등) 하이퍼링크가 0개"))
    return fixable, log_only

def verify_and_fix(md_path, platform="네이버", write_back=False):
    """초안을 검문하고 안전한 지적만 고친 뒤 결과를 돌려준다. 발행은 하지 않는다.

    반환: {
        "ok": bool,            # 전 항목 PASS로 수렴했는가
        "loops": int,          # 돈 사이클 수
        "calls": int,          # LLM 호출 수
        "remaining": [(항목, 이유)],  # 끝까지 남은 FAIL (사람이 봐야 할 것)
        "changed": bool,       # 본문이 바뀌었나
        "final": str,          # 최종 본문(메타 꼬리 포함 원본 구조 유지)
    }
    """
    from llm import ask

    orig = Path(md_path).read_text()
    fm = _FRONTMATTER.match(orig)
    frontmatter = fm.group(0) if fm else ""  # 프런트매터도 검문/수정 대상에서 뺀다
    rest = orig[len(frontmatter):]
    body = _body_only(rest)
    tail = rest[len(body):]  # 메타 꼬리는 검문/수정에서 제외하고 그대로 붙인다
    checklist = _checklist_text()
    materials, day = _materials(md_path)

    calls = 0
    loops = 0
    changed = False
    remaining = []

    # 2026-09-03 안건 15①: LLM 채점(CRITIQUE)은 폐지 — 검문은 코드(code_checks, LLM 0회)만,
    # 고칠 게 있을 때만 REVISE 1회. 편당 LLM = 초안 1 + 검문 0 + 수정 1 = 2회.
    cfix, clog = code_checks(body)
    for n, r in clog:
        with (ROOT / "state" / "검수-남은지적.log").open("a") as f:
            f.write(f"{Path(md_path).stem}\t{n}(코드·로그만)\t{r}\n")
    remaining = cfix
    if cfix:
        fails_txt = "\n".join(f"- {n}: {r}" for n, r in cfix)
        revised = ask(REVISE_PROMPT.format(platform=platform, fails=fails_txt, body=body,
                                           materials=materials or "(수집된 URL 없음 — 창작 금지, 인용을 지워라)",
                                           day=day or "(불명)"),
                      model=OPUS, timeout=600, agy_fallback=False)
        calls += 1
        revised = _strip_fence(revised or "")
        # 퇴행 가드: 비었거나 원문의 60% 미만이면 수정 거부
        if revised and len(revised) >= len(body) * MIN_KEEP_RATIO:
            body = revised
            changed = True
            loops = 1
            remaining, _ = code_checks(body)   # 고친 뒤 남은 것만 사람에게(로그만 항목은 재기록 안 함)

    final, changed = _finalize(body, tail, changed)
    if write_back and changed:
        Path(md_path).write_text(frontmatter + final)
    return {"ok": not remaining, "loops": loops, "calls": calls,
            "remaining": remaining, "changed": changed, "final": final}


def _check():
    good = ("반도체는 전자 산업의 기초 부품이다. 2026-09-02 기준 수치. [로이터](https://a) [10-K](https://www.sec.gov/x)\n\n"
            "## 왜 올랐나?\n본문\n\n통념은 A다. 반대로 보면 하방도 있다. 내 판은 B.\n\n![차트](x.png)")
    assert code_checks(good) == ([], []), code_checks(good)
    bad = "## 정리\n그냥 본문 https://a"
    f, l = code_checks(bad)
    assert {n for n, _ in f} == {"정의문", "질문형 소제목", "날짜 기준", "반대로 문단", "원문 링크", "해석 프레임"}, f
    assert {n for n, _ in l} == {"자체 차트", "1차 자료"}, l
    assert l and l[0][0] == "자체 차트"
    """재료 주입이 살아있는지 — 조사 로그에서 URL을 꺼내고 프롬프트에 박히는가."""
    mats, day = _materials("drafts/2026-08-27-stock-nvda.md")
    assert day == "2026-08-27", day
    assert mats.count("http") >= 3, f"URL을 못 꺼냈다: {mats!r}"
    out = REVISE_PROMPT.format(platform="네이버", fails="- 출처링크: x",
                               body="본문", materials=mats, day=day)
    assert "창작하지 마라" in out and "http" in out
    # 조사 로그가 없는 글은 막지 말고 '창작 금지'로 넘어가야 한다
    m2, d2 = _materials("drafts/1999-01-01-없는글.md")
    assert m2 == "" and d2 == "1999-01-01"
    print("check ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check(); sys.exit(0)
    args = sys.argv[1:]
    md = next((a for a in args if a.endswith(".md")), None)
    if not md:
        print("usage: verify.py <draft.md> [--platform 네이버|티스토리] [--write]")
        sys.exit(1)
    platform = "티스토리" if "티스토리" in args else "네이버"
    write_back = "--write" in args
    out = verify_and_fix(md, platform=platform, write_back=write_back)
    print(f"[verify] ok={out['ok']} loops={out['loops']} calls={out['calls']} "
          f"changed={out['changed']} platform={platform}")
    if out["remaining"]:
        print("남은 지적(사람 확인):")
        for n, r in out["remaining"]:
            print(f"  - {n}: {r}")
    if write_back and out["changed"]:
        print(f"[verify] 수정본 저장됨 → {md}")
