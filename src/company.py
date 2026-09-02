#!/opt/homebrew/bin/python3
"""ventures/company.py — 회사 스케줄러. launchd 가 하루 몇 번 부르고, 한 번 돌고, 끝난다.

2026-09-02 재건. 전 데몬(daemon.py, _archive/)은 10초 상주 루프 + 30분마다 49팀 전부 재실행이었고
'성공 산출물이 없으면 재시도' 구조라 실패한 팀은 영원히 돌았다(09-01 6,810회 / 09-02 4,324회 호출).
이 파일의 규칙:
  - 상주 루프 없음. 호출당 라인별 최대 1회 시도.
  - 라인 = 돈이 들어오는 경로 하나. 하루 상한(max_per_day)·연속실패 백오프(2h·4h·8h…, 3연속이면 안건 등록).
  - 시도 자체를 기록한다(state/company_attempts.json) — 산출물 유무로 판단하지 않는다.
  - 원장 state/company_ledger.jsonl 에 실행마다 1행(호출 수 증분 포함). 보고·토론은 이 원장을 읽는다.
  - 저장은 전부 로컬. 드라이브는 --archive 때 rclone 1방향 복사만(NFS 마운트 경로 절대 안 만짐).
  - 자기수정 금지. 바꿀 건 state/agenda.md 에 안건으로 올리고 /토론 에서 결정.
    예외(2026-09-02 유저 "장애 나면 자동 복구, 어렵다면 안정 버전 롤백"): 마지막으로 OK 를 낸 코드로
    되돌리는 것은 자기수정이 아니다. 새 코드를 쓰지 않는다 — 있던 것을 복원할 뿐.
  - 자가치유 순서(LLM 0회): 라인 selftest → 실행 → 발행 URL 생사 확인 → OK 면 안정판 스냅샷
    (state/stable/NAME.tgz, 코드 해시 기준). selftest 실패 또는 3연속 실패면:
      코드가 안정판과 다르다 → 안정판 복원(롤백), fails 0, 원장 event=rollback. 복원 뒤엔 해시가 같으니 두 번 안 돈다.
      코드가 안정판과 같다 → 원인이 바깥(네이버 DOM·브라우저·상한) → 안건 + 🚨 즉시 DM.
    '안정판'의 한계: rc==0 만 본다. 품질이 나빠진 버전도 OK 면 안정판이 된다.
  - 사람 호출은 두 단계뿐: 🚨 즉시 DM = 롤백 후에도 죽은 라인·스케줄러 자체 크래시. 나머지는 23:30 보고.
    company.py                 # 라인 전부 1회 시도
    company.py --line NAME     # 한 라인만
    company.py --report [--dry]# 오늘 원장 → 평문 보고(디코 DM)
    company.py --archive       # output → 드라이브 복사
    company.py --selftest
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PY = "/opt/homebrew/bin/python3"          # 3.9 시스템 파이썬은 websockets 가 없어 cdp 가 죽는다
STATE = ROOT / "state"
ATTEMPTS = STATE / "company_attempts.json"
LEDGER = STATE / "company_ledger.jsonl"
AGENDA = STATE / "agenda.md"
STABLE = STATE / "stable"
USAGE = STATE / "claude_usage.json"
CAPPED = STATE / "llm_capped.log"
CONFIG = STATE / "company_config.json"    # /토론 결정이 여기 반영된다 (auto_publish 등)
NIMONEM = ROOT / "ventures/team_blog_nimonem"

LINES = {
    "nimonem_review": {
        "cmd": [PY, str(NIMONEM / "run.py"), "--next"],
        "selftest": [PY, str(NIMONEM / "run.py"), "--selftest"],   # LLM 0회, 실행 전 검문
        "dir": NIMONEM,                                            # 안정판 스냅샷·롤백 대상(코드만)
        "max_per_day": 2, "timeout": 900,
        "desc": "네이버 리뷰 블로그(<NIMONEM_ID>) — 제품 1개당 글 1편, 쇼핑커넥트 수익",
    },
    # 2026-09-02 유저 "티스토리 니모넴 <NAVER_BLOG_ID> 잘나오게": 14:06 재공사 때 daily.py 플리스트가 꺼지며
    # run.py(본블로그+티스토리 체인)가 무스케줄이 됐다. nimonem 다음에 돈다(1~3 + ≤12 = 15).
    # dir 없음 — 파이프라인 루트는 스냅샷/롤백 대상이 아니다 → 3연속 실패면 안건+🚨.
    "naver_tistory": {
        "cmd": [PY, str(ROOT / "run.py")],
        "selftest": [PY, str(ROOT / "run.py"), "--selftest"],      # LLM 0회
        "calls": 6, "max_per_day": 2, "timeout": 3600,   # 09-03 안건 15①: enrich 2 + 초안 1 + 수정 ≤1 + 티스토리(격일) ≤1 + VET/제목 여유 = 6
        "desc": "본블로그(<NAVER_BLOG_ID>) 하루 1편 + 티스토리(<TISTORY_ID>) 변형글 — run.py 체인",
    },
    # 2026-09-03 안건 17 ② 수용("오케 ㄱㄱ"): 13F 스타투자자 드립(하루 1명, 네이버+티스토리) — LLM 정확히 1회.
    # 큐 비면 note → FAIL 로 세어 3회째 🚨(run_daily 의 매일 DM 대신). 채우기: skills/13f_build_queue.py
    "thirteenf": {
        "cmd": [PY, "-m", "skills.thirteenf", "--company"],
        "calls": 1, "max_per_day": 1, "timeout": 1200,
        "desc": "13F 스타투자자 개별편 듀얼 발행 — LLM 1회, 큐 state/13f_queue.json",
    },
    # 2026-09-02 밤 안건 17 ① 수용("복귀"): 14:06 재공사 때 daily.py 와 함께 꺼진 것 중 예산 안 먹는 것만.
    # history_drip 은 _publish_one 으로 글 한 편(≈8회)을 쓰므로 제외 — run.py 예산과 충돌.
    "x_perk": {
        "cmd": [PY, "-m", "skills.x_perk"],
        "calls": 0, "max_per_day": 1, "timeout": 600,
        "desc": "X 혜택·지원금 글감 수집 → 글감/latest.md + DM (LLM 0회)",
    },
    "gongmo": {
        "cmd": [PY, "-m", "skills.gongmo", "--weekly"],
        "calls": 1, "max_per_day": 1, "timeout": 600, "weekday": (0,),
        "desc": "공모전 추천 주 1회(월) — 큐레이션 LLM 1회, 맨 뒤라 예산 남을 때만",
    },
    # 2026-09-03 안건 17 ② 감시용 3개(LLM 0회). 플리스트 부활 아님 — 데몬 패턴 금지, 라인으로만.
    "tistory_check": {
        "cmd": [PY, str(ROOT / "tistory_check.py")],
        "calls": 0, "max_per_day": 1, "timeout": 300,
        "desc": "티스토리 카카오 세션 살아있나 확인(CDP) — 죽으면 DM",
    },
    "gsc_index": {
        "cmd": [PY, str(ROOT / "gsc_index.py")],
        "calls": 0, "max_per_day": 1, "timeout": 1800,   # URL당 최대 ~6분(inspect 120s+request 240s), 10건이면 300s 로는 중간에 죽는다(09-03)
        "desc": "티스토리 미색인 글 서치콘솔 색인 요청(하루 ≤10) — NO_ACCESS 면 rc 2",
    },
    "traffic": {
        "cmd": [PY, str(ROOT / "traffic.py")],
        "calls": 0, "max_per_day": 1, "timeout": 120, "after_hour": 22,
        "desc": "방문자 집계 — 당일 수치라 밤(23:30) 회차에서만 (after_hour)",
    },
    # 2026-09-03 안건 18③: 앱·전자책·크몽 신규 수익화 라인 동결(해커톤·공모전 예외) — 여기 새 라인 추가 금지, 안건으로만.
    # ebook_learn 라인은 2026-09-02 종료(안건 3) — 스냅샷이 페이지 머리글 1,200자뿐이라 지표를 못 배웠다. 신간은 유저 지시 때 1회씩.
}
BACKOFF_H = (2, 4, 8, 16, 24)
SKIP_DIRS = {"__pycache__", "output", "inbox"}    # 데이터는 안정판에 안 넣는다 — 코드·규격만
CAUSES = (("timeout", "timeout"), ("selftest", "selftest"), ("url_dead", "url_dead"),
          ("검문", "check"), ("캡처", "capture"), ("발행", "publish"), ("Traceback", "exception"))


def _load(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def config():
    return _load(CONFIG, {"auto_publish": False})


def _calls_today():
    """자동 호출만(llm._capped 와 같은 셈) — 유저 대화(claude_bridge)는 뺀다."""
    d = _load(USAGE, {}).get(date.today().isoformat(), {})
    return d.get("calls", 0) - sum(v for k, v in d.get("callers", {}).items() if "claude_bridge" in k)


def _ledger(row):
    STATE.mkdir(exist_ok=True)
    row = {"ts": datetime.now().isoformat(timespec="seconds"), **row}
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _agenda(text):
    STATE.mkdir(exist_ok=True)
    cur = AGENDA.read_text(encoding="utf-8") if AGENDA.exists() else "# 토론 안건\n"
    if text in cur:
        return
    AGENDA.write_text(cur.rstrip() + f"\n- [{date.today()}] {text}\n", encoding="utf-8")


def _alert(text):
    """🚨 즉시 DM — 롤백으로도 못 살린 라인, 스케줄러 크래시. 그 외는 23:30 보고에 싣는다."""
    try:
        import llm
        return llm.send(("🚨 " + text)[:1900])
    except Exception as e:
        print(f"[alert 실패] {e}")
        return False


# ── 안정판(스냅샷·롤백) ─────────────────────────────────────────
def _code_files(d):
    return sorted(p for p in Path(d).rglob("*") if p.is_file()
                  and not p.name.startswith(".") and not SKIP_DIRS & set(p.relative_to(d).parts))


def _dir_hash(d):
    h = hashlib.sha256()
    for p in _code_files(d):
        h.update(str(p.relative_to(d)).encode()); h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _snapshot(name, d):
    """OK 를 낸 코드가 안정판과 다르면 새 안정판으로 저장한다. 반환: 새로 저장했나."""
    cur = _dir_hash(d)
    meta = _load(STABLE / f"{name}.json", {})
    if meta.get("hash") == cur:
        return False
    STABLE.mkdir(parents=True, exist_ok=True)
    with tarfile.open(STABLE / f"{name}.tgz", "w:gz") as tf:
        for p in _code_files(d):
            tf.add(p, arcname=str(p.relative_to(d)))
    (STABLE / f"{name}.json").write_text(json.dumps({"hash": cur, "ts": datetime.now().isoformat(timespec="seconds")}))
    return True


def _rollback(name, d):
    """코드가 안정판과 다를 때만 복원. 반환: 복원했나(같으면 False → 원인은 바깥)."""
    meta = _load(STABLE / f"{name}.json", {})
    tgz = STABLE / f"{name}.tgz"
    if not meta.get("hash") or not tgz.exists() or meta["hash"] == _dir_hash(d):
        return False
    with tarfile.open(tgz) as tf:
        tf.extractall(d)   # 안정판 파일로 덮어씀. 안정판에 없는 새 파일은 남지만 해시엔 들어간다 → 아래서 확인
    for p in _code_files(d):   # 안정판에 없던 파일은 지운다(해시 불일치 → 롤백 반복 방지)
        with tarfile.open(tgz) as tf:
            names = set(tf.getnames())
        if str(p.relative_to(d)) not in names:
            p.unlink()
    return _dir_hash(d) == meta["hash"]


def _url_alive(url):
    """네이버 글 생사. 실측(09-02): 정상 글 PostView 200 + 'se-main-container', 없는 글 404 + '삭제되었거나'.
    None = 판단 불가(네트워크 등) → 실패로 안 센다."""
    m = re.search(r"blog\.naver\.com/([^/]+)/(\d+)", url or "")
    if not m:
        return None
    try:
        req = urllib.request.Request(f"https://blog.naver.com/PostView.naver?blogId={m.group(1)}&logNo={m.group(2)}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return "se-main-container" in r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:
        return None


def _cause(note, tail):
    blob = (note or "") + "\n" + (tail or "")
    return next((c for k, c in CAUSES if k in blob), "unknown")


def _skip_reason(st, spec):
    today = date.today().isoformat()
    if "weekday" in spec and date.today().weekday() not in spec["weekday"]:
        return "요일 아님"
    if datetime.now().hour < spec.get("after_hour", 0):
        return f"{spec['after_hour']}시 이후 회차용"
    # LLM 상한에 걸린 날은 실행 자체를 안 한다 — 돌려 봐야 ask()가 ""를 주고 '실패'로
    # 세어져 백오프·안건이 거짓으로 쌓인다(2026-09-02 실측). attempts 도 안 건드린다.
    cap = int(os.environ.get("CLAUDE_DAILY_CAP", "15"))
    # 한 사이클 최대 3회 — 남은 예산이 그보다 적으면 중간에 끊겨 거짓 실패가 되니 아예 안 돈다.
    if spec.get("calls", 3) and _calls_today() + spec.get("calls", 3) > cap:   # 0회 감시 라인은 폭주한 날도 돈다
        return f"LLM 일일 상한 부족({_calls_today()}/{cap}, 사이클 {spec.get('calls', 3)}회 필요)"
    if st.get("day") == today and st.get("runs", 0) >= spec["max_per_day"]:
        return f"하루 상한 {spec['max_per_day']}회 도달"
    fails = st.get("fails", 0)
    if fails and st.get("last"):
        wait = BACKOFF_H[min(fails, len(BACKOFF_H)) - 1]
        until = datetime.fromisoformat(st["last"]) + timedelta(hours=wait)
        if datetime.now() < until:
            return f"연속실패 {fails}회 → {until:%H:%M} 까지 대기"
    return ""


def _run(cmd, timeout):
    """(rc, tail, note). 예외·타임아웃은 세고 계속."""
    try:
        r = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout,
                           env={"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "HOME": str(Path.home()),
                                "LANG": "ko_KR.UTF-8", "CLAUDE_DAILY_CAP": os.environ.get("CLAUDE_DAILY_CAP", "15")})
        return r.returncode, r.stdout[-3000:], ""
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout {timeout}s"
    except Exception as e:
        return 1, "", f"exc {type(e).__name__}: {e}"[:200]


def run_line(name, spec):
    att = _load(ATTEMPTS, {})
    st = att.get(name, {})
    if st.get("day") != date.today().isoformat():
        st = {"day": date.today().isoformat(), "runs": 0, "fails": st.get("fails", 0), "last": st.get("last")}
    why = _skip_reason(st, spec)
    if why:
        print(f"[{name}] 건너뜀: {why}")
        return None
    st["runs"] += 1
    st["last"] = datetime.now().isoformat(timespec="seconds")
    att[name] = st
    ATTEMPTS.write_text(json.dumps(att, ensure_ascii=False, indent=1))   # 시도를 먼저 적는다

    c0, t0 = _calls_today(), time.time()
    rc, tail, note, result, alive = 1, "", "", {}, None
    if spec.get("selftest"):                       # 1) 검문(LLM 0회) — 깨진 코드로 예산을 안 태운다
        src, stail, snote = _run(spec["selftest"], 120)
        if src != 0:
            note, tail = "selftest 실패 — 본실행 안 함", stail
    if not note:                                   # 2) 본실행
        rc, tail, note = _run(spec["cmd"], spec["timeout"])
        for line in reversed(tail.strip().splitlines()):   # 라인 스크립트는 마지막 줄에 JSON 한 줄
            if line.startswith("{"):
                result = _load_str(line)
                break
        if rc == 0 and result.get("url"):          # 3) 발행 QC — URL 이 실제로 살아 있나
            alive = _url_alive(result["url"])
            if alive is False:
                rc, note = 1, f"url_dead {result['url']}"
    ok = rc == 0 and not note
    st["fails"] = 0 if ok else st.get("fails", 0) + 1
    att[name] = st
    ATTEMPTS.write_text(json.dumps(att, ensure_ascii=False, indent=1))
    row = _ledger({"line": name, "ok": ok, "rc": rc, "sec": round(time.time() - t0),
                   "calls": _calls_today() - c0, "item": result.get("item", ""),
                   "output": result.get("output", ""), "url": result.get("url", ""), "url_alive": alive,
                   "cause": "" if ok else _cause(note, tail),
                   "note": note or result.get("note", "") or (tail.strip().splitlines() or [""])[-1][:200]})
    print(f"[{name}] {'OK' if ok else 'FAIL'} rc={rc} calls+{row['calls']} {row['cause'] or ''} {row['note'][:120]}")
    if ok and spec.get("dir") and _snapshot(name, spec["dir"]):     # 4) 안정판 갱신
        _ledger({"line": name, "event": "stable", "hash": _dir_hash(spec["dir"])})
    # == 3: 딱 한 번 안건+🚨. >= 면 바깥 원인(계정 권한 등)이 안 고쳐지는 동안 매일 DM·안건이 쌓인다(09-03).
    if not ok and (row["cause"] == "selftest" or st["fails"] == 3):  # 5) 치유
        _heal(name, spec, st, att, row)
    return row


def _heal(name, spec, st, att, row):
    """코드가 안정판과 다르면 롤백(새 코드 안 씀). 같으면 원인이 바깥 → 안건 + 🚨."""
    if spec.get("dir") and _rollback(name, spec["dir"]):
        st["fails"] = 0
        att[name] = st
        ATTEMPTS.write_text(json.dumps(att, ensure_ascii=False, indent=1))
        _ledger({"line": name, "event": "rollback", "cause": row["cause"], "note": row["note"][:150]})
        print(f"[{name}] 롤백: 안정판 복원, fails 0")
        return
    _agenda(f"라인 {name} 연속 {st['fails']}회 실패({row['cause']}) — 코드는 안정판과 같음, 원인은 바깥. 마지막: {row['note'][:120]}. 고칠지 접을지.")
    _alert(f"{name} 라인 다운. 원인 {row['cause']}, 코드는 안정판 그대로라 롤백으로 못 살림. {row['note'][:200]}. 안건 올림.")


def _load_str(s):
    try:
        return json.loads(s)
    except Exception:
        return {}


def rows_on(day):
    if not LEDGER.exists():
        return []
    return [r for r in (_load_str(l) for l in LEDGER.read_text(encoding="utf-8").splitlines())
            if r.get("ts", "").startswith(day)]


def rows_today():
    return rows_on(date.today().isoformat())


def report(dry=False):
    """사람에게 올리는 보고 — 마크다운 없이 평문, 1,900자 이내, 팩트만. 고친 것·바뀐 것 위주."""
    rows = rows_today()
    runs = [r for r in rows if "line" in r and "event" not in r]
    ok = [r for r in runs if r.get("ok")]
    bad = [r for r in runs if not r.get("ok")]
    y = [r for r in rows_on((date.today() - timedelta(days=1)).isoformat()) if "event" not in r]
    cap = int(os.environ.get("CLAUDE_DAILY_CAP", "15"))
    capped = sum(1 for l in (CAPPED.read_text(encoding="utf-8").splitlines() if CAPPED.exists() else [])
                 if l.startswith(date.today().isoformat()))
    parts = [f"{date.today():%m/%d} 회사 보고. 실행 {len(runs)}건, 성공 {len(ok)}, 실패 {len(bad)}"
             f"(어제 {len(y)}건, 실패 {sum(1 for r in y if not r.get('ok'))}). "
             f"LLM 호출 오늘 {_calls_today()}회(상한 {cap}, 상한 차단 {capped}회)."]
    # 2026-09-03 안건 18④: 취업 마감·시험일 리마인드 1줄(LLM 0회). state/deadlines.tsv = 날짜<TAB>항목, 14일 내만, 지난 건 표시 안 함.
    dl = AGENDA.parent / "deadlines.tsv"
    soon = []
    for l in (dl.read_text(encoding="utf-8").splitlines() if dl.exists() else []):
        if l.startswith("#") or "\t" not in l:
            continue
        try:
            d = date.fromisoformat(l.split("\t", 1)[0].strip())
        except ValueError:
            continue
        left = (d - date.today()).days
        if 0 <= left <= 14:
            soon.append(f"{l.split(chr(9), 1)[1].strip()} D-{left}")
    if soon:
        parts.append("리마인드: " + ", ".join(soon) + ".")
    for r in ok:
        what = r.get("item") or LINES.get(r["line"], {}).get("desc", r["line"])
        parts.append(f"{r['line']}: {what}. 산출 {Path(r['output']).name if r.get('output') else '없음'}"
                     + (f", 발행 {r['url']}" + ("(생존 확인)" if r.get("url_alive") else "") if r.get("url") else "") + ".")
    for r in bad:
        parts.append(f"{r['line']} 실패({r.get('cause', '')}): {r.get('note', '')[:150]}.")
    for r in rows:
        if r.get("event") == "rollback":
            parts.append(f"{r['line']} 자동 롤백: 안정판 복원({r.get('cause')}).")
        elif r.get("event") == "stable":
            parts.append(f"{r['line']} 안정판 갱신 {r.get('hash')}.")
    att = _load(ATTEMPTS, {})
    for n, st in att.items():
        why = _skip_reason(st, LINES.get(n, {"max_per_day": 99}))
        if "대기" in why:
            parts.append(f"{n} 백오프: {why}.")
    pending = [l for l in (AGENDA.read_text(encoding="utf-8").splitlines() if AGENDA.exists() else [])
               if re.match(r"\s*(\d+\.|-)\s", l) and "~~" not in l]
    parts.append(f"토론 대기 안건 {len(pending)}개. /토론 으로 연다.")
    text = " ".join(parts)[:1900]
    print(text)
    if not dry:
        import llm
        llm.send(text)
    return text


def archive():
    from ventures.gdrive_sync import sync_to_gdrive
    return sync_to_gdrive()


def selftest():
    import tempfile
    global ATTEMPTS, LEDGER, AGENDA, STABLE, USAGE, _alert
    tmp = Path(tempfile.mkdtemp())
    ATTEMPTS, LEDGER, AGENDA, STABLE = tmp / "a.json", tmp / "l.jsonl", tmp / "g.md", tmp / "stable"
    USAGE = tmp / "u.json"   # 실제 사용량과 분리 — 상한 찬 날 selftest 가 거짓 실패하던 것(09-02)
    alerts = []
    _alert = lambda t: alerts.append(t)
    assert _skip_reason({}, {"calls": 0, "max_per_day": 1, "weekday": ((date.today().weekday() + 1) % 7,)}) == "요일 아님"
    assert not _skip_reason({}, {"calls": 0, "max_per_day": 1, "weekday": (date.today().weekday(),)}), "요일 게이트가 오늘을 막음"
    assert _skip_reason({}, {"calls": 0, "max_per_day": 1, "after_hour": 24}) == "24시 이후 회차용"
    assert not _skip_reason({}, {"calls": 0, "max_per_day": 1, "after_hour": 0})
    os.environ["CLAUDE_DAILY_CAP"] = "0"
    assert not _skip_reason({}, {"calls": 0, "max_per_day": 1}) and _skip_reason({}, {"calls": 1, "max_per_day": 1}).startswith("LLM 일일 상한")
    os.environ["CLAUDE_DAILY_CAP"] = "99999"
    spec = {"cmd": [PY, "-c", "print('{\"item\":\"x\"}')"], "max_per_day": 1, "timeout": 30}
    assert run_line("t", spec)["ok"]
    assert run_line("t", spec) is None, "하루 상한이 안 걸림"
    assert len(LEDGER.read_text().splitlines()) == 1

    def age(n):
        a = json.loads(ATTEMPTS.read_text()); a[n]["last"] = (datetime.now() - timedelta(days=2)).isoformat()
        ATTEMPTS.write_text(json.dumps(a))
    bad = {"cmd": [PY, "-c", "import sys; sys.exit(3)"], "max_per_day": 9, "timeout": 30}
    for _ in range(3):
        row = run_line("b", bad); age("b")
    assert row and not row["ok"] and json.loads(ATTEMPTS.read_text())["b"]["fails"] == 3
    assert "연속 3회 실패" in AGENDA.read_text() and len(alerts) == 1, "dir 없는 라인: 안건+🚨 한 번"
    a = json.loads(ATTEMPTS.read_text()); a["b"]["last"] = datetime.now().isoformat(); ATTEMPTS.write_text(json.dumps(a))
    assert run_line("b", bad) is None, "백오프가 안 걸림"
    age("b"); run_line("b", bad)
    assert json.loads(ATTEMPTS.read_text())["b"]["fails"] == 4 and len(alerts) == 1, "4회째는 조용히(백오프만)"

    # 안정판 → 코드 변경 → 3연속 실패 → 롤백(fails 0) → 다시 3연속 실패(코드 같음) → 🚨
    d = tmp / "line"; d.mkdir(); (d / "output").mkdir()
    (d / "code.py").write_text("v1"); (d / "output" / "post.md").write_text("data")
    good = {"cmd": [PY, "-c", "print('{}')"], "dir": d, "max_per_day": 99, "timeout": 30}
    assert run_line("s", good)["ok"] and (STABLE / "s.tgz").exists(), "OK 뒤 안정판 없음"
    assert not _snapshot("s", d), "코드 안 바뀌었는데 재스냅샷"
    (d / "code.py").write_text("v2 broken"); (d / "new.py").write_text("x"); (d / "output" / "post.md").write_text("changed")
    broken = {**good, "cmd": bad["cmd"]}
    for _ in range(3):
        run_line("s", broken); age("s")
    st = json.loads(ATTEMPTS.read_text())["s"]
    assert (d / "code.py").read_text() == "v1" and not (d / "new.py").exists(), "롤백이 코드를 안 되돌림"
    assert (d / "output" / "post.md").read_text() == "changed", "롤백이 데이터를 건드림"
    assert st["fails"] == 0 and len(alerts) == 1, "롤백 뒤 fails 0·DM 없음이어야"
    assert any(r.get("event") == "rollback" for r in map(_load_str, LEDGER.read_text().splitlines()))
    for _ in range(3):
        run_line("s", broken); age("s")
    assert json.loads(ATTEMPTS.read_text())["s"]["fails"] == 3 and len(alerts) == 2, "코드 같은데 롤백 반복/🚨 누락"
    assert "원인은 바깥" in AGENDA.read_text()

    # selftest 게이트: 검문 실패면 본실행 없이 즉시 치유 경로
    (d / "code.py").write_text("v3")
    st_bad = {**good, "selftest": bad["cmd"], "cmd": [PY, "-c", "open('%s','w').write('ran')" % (tmp / "ran")]}
    age("s"); a = json.loads(ATTEMPTS.read_text()); a["s"]["fails"] = 0; ATTEMPTS.write_text(json.dumps(a))
    row = run_line("s", st_bad)
    assert row["cause"] == "selftest" and not (tmp / "ran").exists(), "검문 실패인데 본실행함"
    assert (d / "code.py").read_text() == "v1", "검문 실패 → 즉시 롤백이어야"
    assert _cause("timeout 900s", "") == "timeout" and _cause("", "Traceback") == "exception"
    assert _url_alive("nope") is None
    print("selftest OK")


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        if "--selftest" in a:
            selftest()
        elif "--report" in a:
            report(dry="--dry" in a)
        elif "--archive" in a:
            archive()
        else:
            names = [a[a.index("--line") + 1]] if "--line" in a else list(LINES)
            for n in names:
                try:
                    run_line(n, LINES[n])
                except Exception as e:
                    _ledger({"line": n, "ok": False, "rc": -1, "cause": "scheduler", "note": f"scheduler exc {e}"[:200]})
                    print(f"[{n}] 스케줄러 예외: {e}")
    except Exception as e:                      # run_line 바깥(설정·원장·임포트) 크래시 → 🚨
        if "--selftest" in a:
            raise
        _alert(f"company.py 크래시 ({' '.join(a) or 'run'}): {type(e).__name__}: {e}"[:400])
        raise
