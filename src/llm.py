"""Claude 단일 스택 헬퍼 — LLM 호출 + 디스코드 전송. (2026-07-13 헤르메스 적출)

ask()  : claude CLI 헤드리스(-p) 단발 호출. JSON 출력에서 결과 텍스트를 꺼내고
         사용량(비용)은 state/claude_usage.json 에 날짜별로 쌓는다.
send() : 디스코드 봇 REST API 직접 호출(photo_inbox.py 와 같은 방식).
         2000자 제한은 1900자 단위로 쪼개 보내고, media 경로를 주면 파일 첨부.

launchd 는 PATH 에 nvm 이 없어서 claude 를 못 찾는다 → _claude_bin() 이 직접 찾는다.
"""
import glob
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "claude-sonnet-5"           # 기본 모델(2026-08-05 Fable→Sonnet, 토큰 최적화). 싼 작업은 model="haiku" 로.
DM_EFFORT = "high"                  # 디코 DM 배선(agy/claude)엔 effort 플래그가 없었다 — 기본값으로 돌고 있었음 (2026-08-27)

# 한도 소진 시 인계 순서(2026-07-20). Fable 한도가 늘 먼저 차는데, 그때마다 게이트·초안
# 생성이 통째로 멈춰서 하루치 발행이 날아갔다. 손으로 모델을 갈아끼우면 복구 후
# 되돌리는 걸 잊는다 — 그래서 ask()가 한도 에러를 보고 스스로 다음 모델로 넘긴다.
# Fable이 살아 있으면 언제나 Fable이 1순위다(되돌릴 것 없음).
# 2026-07-28 유저 지시("Fable 떨어지면 opus로 해") — 1순위 대타를 Opus 5로 올렸다.
# 2026-08-05 되돌림: Fable 한도가 매일 조기에 차는 바람에 인계가 예외가 아니라 상시 경로가
# 됐다(8/1 83건, 8/3 105건이 Opus 5로 나감 — model_fallback.json). Opus는 같은 일을
# 훨씬 비싸게 먹어서 계정 한도가 더 빨리 닳고, 그래서 더 많이 인계되는 악순환이 됐다.
# 대타를 Sonnet 5로 내린다 — Fable과 같은 급이라 초안·게이트 품질 손실은 사실상 없다.
# 품질이 중요한 경로(verify.REVISE, 브리지 DM)는 각자 model= 을 명시하고 있어 영향 없다.
FALLBACK = {
    "claude-fable-5": ["claude-sonnet-5", "claude-opus-4-8", "claude-opus-5"],
    "claude-opus-5": ["claude-opus-4-8", "claude-sonnet-5"],
    "claude-opus-4-8": ["claude-sonnet-5"],
}
_LIMIT_PAT = re.compile(
    r"reached your .*limit|hit your .*limit|usage limit|session limit|rate.?limit|quota|weekly limit", re.I)
_ACCOUNT_LIMIT_PAT = re.compile(r"weekly limit", re.I)
# "You've hit your session limit · resets 1:10pm (Asia/Seoul)" 에서 리셋 시각을 뽑는다.
_RESET_PAT = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", re.I)
MAX_WAIT = 6 * 3600  # 이보다 먼 리셋은 기다리지 않는다(그날 작업 자체가 무의미해짐).


def _is_limit(msg):
    """한도 소진 에러인가. 다른 오류(프롬프트 문제 등)는 인계하면 안 된다 — 같은 에러만 반복된다."""
    return bool(_LIMIT_PAT.search(msg or ""))


def _reset_wait(msg):
    """에러에 적힌 리셋 시각까지 남은 초. 시각을 못 읽으면 None."""
    m = _RESET_PAT.search(msg or "")
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "pm" and hh != 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    if hh > 23:
        return None
    now = datetime.now()
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return (t - now).total_seconds() + 60  # 리셋 직후 1분 여유
DM_CHANNEL = "<DISCORD_DM_CHANNEL_ID>"
API = "https://discord.com/api/v10"
USAGE = ROOT / "state" / "claude_usage.json"
FALLBACK_LOG = ROOT / "state" / "model_fallback.json"
_ENV_PATHS = (ROOT / ".env", Path.home() / "<BROWSER_PROFILE_DIR>" / ".env")


def env(key):
    """pipeline/.env 우선, ~/<BROWSER_PROFILE_DIR>/.env 폴백. 값은 절대 로그에 찍지 말 것."""
    for p in _ENV_PATHS:
        try:
            for ln in p.read_text().splitlines():
                ln = ln.strip()
                if ln.startswith(key + "="):
                    v = ln.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        except OSError:
            continue
    return None


def _claude_bin():
    p = shutil.which("claude")
    if p:
        return p
    hits = glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/claude"))
    if hits:
        return sorted(hits)[-1]
    raise FileNotFoundError("claude CLI를 찾을 수 없음")


def _log_usage(res, model):
    """비용 집계는 부가 기능 — 실패해도 본 호출을 막지 않는다.

    2026-08-05: 어느 스크립트가 태우는지도 같이 센다(callers). 하루 200회를 넘겨도
    범인을 못 찍어서 폴백 로그로 역추적해야 했다 — 각 스크립트가 별도 프로세스라
    argv[0] 이 곧 호출자다."""
    try:
        day = date.today().isoformat()
        data = json.loads(USAGE.read_text()) if USAGE.exists() else {}
        d = data.setdefault(day, {"calls": 0, "cost_usd": 0.0, "models": {}})
        d["calls"] += 1
        c = res.get("total_cost_usd") or 0.0
        d["cost_usd"] = round(d["cost_usd"] + c, 4)
        d["models"][model] = d["models"].get(model, 0) + 1
        who = Path(sys.argv[0]).name or "?"
        d.setdefault("callers", {})[who] = d.setdefault("callers", {}).get(who, 0) + 1
        # 2026-08-27: 총액만 쌓아서 "어느 모델이 태우나"를 회귀분석으로 역추정해야 했고
        # 실패했다(음수 단가). 건수 말고 금액을 모델·호출자별로 같이 센다.
        mc = d.setdefault("model_cost", {}); mc[model] = round(mc.get(model, 0.0) + c, 4)
        cc = d.setdefault("caller_cost", {}); cc[who] = round(cc.get(who, 0.0) + c, 4)
        USAGE.parent.mkdir(exist_ok=True)
        USAGE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    except Exception:
        pass


# 2026-09-02 유저 지시(무한루프 방지·토큰 방어). 하루 자동 호출 상한 — 넘으면 호출 안 하고
# 세서 적는다(state/llm_capped.log). 대화 브리지는 예외(유저와의 대화는 막지 않는다).
# CLAUDE.md "게이트로 막지 말라"보다 이 지시가 나중이고 명시적이다. 상한은 env CLAUDE_DAILY_CAP. 09-02 유저: 3 → 15 (글 한 편 8~12회라 3이면 0편).
CAP_LOG = ROOT / "state" / "llm_capped.log"
DAILY_CAP = int(os.environ.get("CLAUDE_DAILY_CAP", "15"))


def _capped(kind):
    who = Path(sys.argv[0]).name or "?"
    if "claude_bridge" in who:
        return False
    try:
        data = json.loads(USAGE.read_text()) if USAGE.exists() else {}
        d = data.get(date.today().isoformat(), {})
        # 유저 대화(bridge)는 예외 호출자인데 분모에는 들어가 있었다 — 아침에 몇 마디 나누면
        # 파이프라인 예산이 그만큼 줄던 것(2026-09-02). 자동 호출만 센다.
        n = d.get("calls", 0) - sum(v for k, v in d.get("callers", {}).items() if "claude_bridge" in k)
    except Exception:
        return False
    if n < DAILY_CAP:
        return False
    try:
        CAP_LOG.parent.mkdir(exist_ok=True)
        with CAP_LOG.open("a") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')}\t{who}\t{kind}\tcalls={n}\n")
    except Exception:
        pass
    print(f"[llm] 일일 상한 {DAILY_CAP}회 초과(calls={n}) — {who} {kind} 호출 생략", file=sys.stderr)
    return True


def _run_once(prompt, model, timeout, max_turns=None):
    """CLI 한 번 호출 → (본문, 오류문구). 오류면 본문이 "", 오류문구가 채워진다."""
    # PIPELINE_HEADLESS: UserPromptSubmit 훅(디코 DM 주워오기)이 이 서브프로세스에서도
    # 터져서 DM 내용이 프롬프트에 섞여 들어갔다. 훅이 이 값을 보고 스스로 빠진다. (2026-07-26)
    cmd = [_claude_bin(), "-p", prompt, "--model", model, "--output-format", "json"]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    r = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        env={**os.environ, "PIPELINE_HEADLESS": "1"},
    )
    try:
        res = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return "", f"JSON 파싱 실패 (exit {r.returncode}): {r.stderr[:200]}"
    _log_usage(res, model)
    if res.get("is_error"):
        return "", str(res.get("result"))[:300]
    return (res.get("result") or "").strip(), ""


def _log_fallback(asked, used):
    """어느 날 어떤 작업이 대체 모델로 나갔는지 남긴다.
    Fable 한도가 풀린 뒤 '그동안 오푸스로 나간 것들'을 다시 훑기 위한 목록이다."""
    try:
        day = date.today().isoformat()
        data = json.loads(FALLBACK_LOG.read_text()) if FALLBACK_LOG.exists() else {}
        d = data.setdefault(day, {})
        k = f"{asked}->{used}"
        d[k] = d.get(k, 0) + 1
        FALLBACK_LOG.parent.mkdir(exist_ok=True)
        FALLBACK_LOG.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    except Exception:
        pass


def _grok_bin():
    p = shutil.which("grok")
    if p:
        return p
    for candidate in (
        str(Path.home() / ".grok/bin/grok"),
        "/usr/local/bin/grok",
        "/opt/homebrew/bin/grok",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _agy_bin():
    p = shutil.which("agy")
    if p:
        return p
    for candidate in ("/opt/homebrew/bin/agy", "/usr/local/bin/agy"):
        if os.path.exists(candidate):
            return candidate
    return None


# 2026-09-02 유저: "agy는 한도를 다 써버렸다, 제외하라. 클로드 단독으로 진행".
# 모든 agy 호출이 이 함수를 지나므로 여기서 한 번만 끊는다(호출부는 그대로).
AGY_ENABLED = False


def _run_agy(prompt, timeout, session_id=None, model="Gemini 3.7 Flash (High)"):
    if not AGY_ENABLED:
        return "", session_id, "agy 제외(2026-09-02 유저 지시, 한도 소진)"
    bin_path = _agy_bin()
    if not bin_path:
        return "", None, "agy CLI를 찾을 수 없음"
    cmd = [
        bin_path, "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--dangerously-skip-permissions"
    ]
    if session_id:
        cmd += ["--conversation", session_id]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout, cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        return "", session_id, "agy 타임아웃"
    except Exception as e:
        return "", session_id, f"agy 실행 실패: {e}"
    _log_usage({"total_cost_usd": 0.0}, "agy")
    try:
        res = json.loads(r.stdout)
        cid = res.get("conversation_id") or session_id
        ans = (res.get("response") or "").strip()
        if not ans and res.get("status") != "SUCCESS":
            return "", cid, f"agy 에러: {res.get('status')}"
        return ans, cid, ""
    except Exception as e:
        return r.stdout.strip(), session_id, ""


# 2026-09-02 유저: "클로드가 아니라 코덱스가 와도, 안티그래비티가 와도 퀄리티가 똑같게 —
# 공장의 시스템화". 글쓰기 호출은 백엔드 하나로 갈아끼운다. 품질은 모델이 아니라 호출부의
# 명세(SPEC.md)와 코드 검문(run.py check)이 지킨다. 선택: env LLM_WRITE_BACKEND 또는
# state/company_config.json "write_backend". 기본 claude. 전부 일일 상한(_capped)을 같이 받는다.
# agy 는 2026-09-02 한도 소진으로 제외(AGY_ENABLED=False) — 목록에 안 넣는다.
WRITE_BACKENDS = {
    "gemini": lambda p: ["/opt/homebrew/bin/gemini", "-p", p],
    "codex": lambda p: ["codex", "exec", "--skip-git-repo-check", p],
}


def _write_backend():
    b = os.environ.get("LLM_WRITE_BACKEND")
    if not b:
        try:
            b = json.loads((ROOT / "state" / "company_config.json").read_text()).get("write_backend")
        except Exception:
            b = None
    return (b or "claude").lower()


def ask_write(prompt, timeout=600, model=MODEL):
    """글쓰기·잡일 — 설정된 백엔드 한 번, 실패하면 클로드 한 번. 스스로 재시도하지 않는다.

    image= 나 max_turns= 가 필요한 호출은 ask() 를 직접 써라(외부 CLI는 텍스트 한 방).
    남의 웹페이지 본문을 그대로 먹이는 호출도 ask(agy_fallback=False) — 인젝션 방지.
    """
    if _capped("ask_write"):
        return ""
    b = _write_backend()
    if b in WRITE_BACKENDS:
        try:
            r = subprocess.run(WRITE_BACKENDS[b](prompt), capture_output=True, text=True, timeout=timeout)
            _log_usage({"total_cost_usd": 0.0}, b)
            if r.returncode == 0 and len(r.stdout.strip()) > 20:
                return r.stdout.strip()
            err = (r.stderr or r.stdout)[-200:]
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:200]
        print(f"[llm.ask_write] {b} 실패 → 클로드로 인계: {err}", file=sys.stderr)
    return ask(prompt, model=model, timeout=timeout)


def _is_account_limit(msg):
    """주간 한도. 같은 계정의 다른 클로드 모델로 넘겨도 막힌다."""
    return bool(_ACCOUNT_LIMIT_PAT.search(msg or ""))


def _ask_chain(prompt, model, timeout, max_turns=None):
    """FALLBACK 순서대로 시도 → (본문, 마지막 오류). 한도 외 오류는 즉시 반환.
    주간 한도면 나머지 클로드 모델은 건너뛴다(같은 계정)."""
    chain = [model] + [m for m in FALLBACK.get(model, []) if m != model]
    err = ""
    for i, m in enumerate(chain):
        out, err = _run_once(prompt, m, timeout, max_turns)
        if not err:
            if i:
                print(f"[llm.ask] {model} 한도 → {m} 로 인계", file=sys.stderr)
                _log_fallback(model, m)
            return out, ""
        if not _is_limit(err):
            return "", err
        if _is_account_limit(err):
            return "", err
    return "", err


def ask(prompt, model=MODEL, image=None, timeout=600, max_turns=None, agy_fallback=True):
    """단발 질문 → 답 텍스트. CLI 실패 시 "" (호출부가 빈 답을 실패로 처리).
    timeout 초과는 기존 hermes 시절과 동일하게 예외로 올라간다.

    한도 소진(usage limit)이면 FALLBACK 순서대로 다음 모델에 자동 인계한다.
    Fable 한도가 늘 먼저 차는데 그때마다 파이프라인 전체가 멈추던 걸 막는다(2026-07-20).

    세션 한도는 계정 단위라 어느 모델로 넘겨도 똑같이 막힌다 — 에러에 적힌 리셋 시각까지
    기다렸다가 한 번 더 돌린다. (2026-07-27: "hit your session limit"이 _LIMIT_PAT에
    안 걸려 인계조차 안 됐고 기다렸다 잇는 경로도 없어서, 12시에 발행 2건·wiki·feedback이
    통째로 날아갔다. 리셋은 1:10pm이었다.)"""
    if _capped("ask"):
        return ""
    if image:
        imgs = [image] if isinstance(image, str) else list(image)
        lst = "\n".join(f"- {p}" for p in imgs)
        prompt = (f"먼저 Read 도구로 아래 이미지 파일을 전부 열어서 봐라(순서대로).\n"
                  f"{lst}\n\n{prompt}")
    out, err = _ask_chain(prompt, model, timeout, max_turns)
    if not err:
        return out

    # 2026-09-02 agy 배제 프로토콜: 한도면 agy 로 넘기지 않는다(agy 호출이 하루 5천 회 폭주의 원인).
    # agy_fallback 인자는 호출부 호환용으로만 남긴다. 한도면 빈 답 → 호출부가 실패로 세고 넘어간다.
    print(f"[llm.ask] 오류 응답({model}): {err[:200]}", file=sys.stderr)
    return ""


def _post_message(token, channel, payload, file_path=None):
    url = f"{API}/channels/{channel}/messages"
    headers = {"Authorization": f"Bot {token}", "User-Agent": "pipeline-claude/1.0"}
    try:
        if file_path and Path(file_path).exists():
            boundary = uuid.uuid4().hex
            fname = Path(file_path).name
            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            body = (
                (f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                 f"Content-Type: application/json\r\n\r\n"
                 f"{json.dumps(payload, ensure_ascii=False)}\r\n"
                 f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; '
                 f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
                + Path(file_path).read_bytes()
                + f"\r\n--{boundary}--\r\n".encode()
            )
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        else:
            body = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers)
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        print(f"[llm.send] 전송 실패: {e}", file=sys.stderr)
        return False


def send(text, media=None, channel=DM_CHANNEL):
    """디스코드 DM 전송. 실패해도 예외 대신 False (알림은 파이프라인을 죽이면 안 됨)."""
    token = env("DISCORD_BOT_TOKEN")
    if not token:
        print("[llm.send] DISCORD_BOT_TOKEN 없음 — 전송 스킵", file=sys.stderr)
        return False
    text = text or ""
    chunks = []
    rest = text
    while len(rest) > 1900:
        cut = rest.rfind("\n", 0, 1900)
        if cut < 1000:
            cut = rest.rfind(" ", 0, 1900)
        if cut < 1000:
            cut = 1900
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n ")
    chunks.append(rest)
    chunks = chunks or [""]
    ok = True
    for i, chunk in enumerate(chunks):
        attach = media if i == len(chunks) - 1 else None  # 첨부는 마지막 조각에
        ok = _post_message(token, channel, {"content": chunk}, attach) and ok
    if not ok:
        # 알림 실패는 삼키되 흔적은 남긴다 — 전부 무음이면 알람이 죽은 걸 아무도 모른다
        try:
            p = Path(__file__).parent / "state" / "notify_fail.log"
            p.parent.mkdir(exist_ok=True)
            with p.open("a") as fh:
                fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + repr(text[:120]) + "\n")
        except OSError:
            pass
    return ok


if __name__ == "__main__":
    # 사용법: python3 llm.py ask "질문" | python3 llm.py send "메시지"
    if len(sys.argv) >= 3 and sys.argv[1] == "ask":
        print(ask(sys.argv[2], model=sys.argv[3] if len(sys.argv) > 3 else MODEL))
    elif len(sys.argv) >= 3 and sys.argv[1] == "send":
        print("전송됨" if send(sys.argv[2]) else "전송 실패")


def _check():
    """모델·호출자별 '금액'이 실제로 쌓이는지. 총액만 세던 회귀를 막는다."""
    import tempfile, llm as _m
    from datetime import date as _d
    orig = _m.USAGE
    try:
        _m.USAGE = Path(tempfile.mkdtemp()) / "u.json"
        _m._log_usage({"total_cost_usd": 0.25}, "claude-opus-5")
        _m._log_usage({"total_cost_usd": 0.05}, "claude-sonnet-5")
        d = json.loads(_m.USAGE.read_text())[_d.today().isoformat()]
        assert d["model_cost"]["claude-opus-5"] == 0.25, d
        assert d["model_cost"]["claude-sonnet-5"] == 0.05, d
        assert round(sum(d["model_cost"].values()), 4) == d["cost_usd"], d
        assert round(sum(d["caller_cost"].values()), 4) == d["cost_usd"], d
    finally:
        _m.USAGE = orig
    print("check ok")
