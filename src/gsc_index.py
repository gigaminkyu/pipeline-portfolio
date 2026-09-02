"""티스토리 미색인 글을 서치콘솔에 매일 색인 요청한다.

2026-07-23: 발행 38개 중 구글 색인 3개뿐이라 수동으로 돌리다 만들었다.
GSC 수동 요청은 속성당 하루 10~12건이 상한이라 오래된 글부터 10건씩 태우고,
할당량 문구가 뜨면 즉시 멈춘다(초과분은 어차피 버려진다).

크롬 합성 입력(Input.dispatchKeyEvent/dispatchMouseEvent)은 탭이 hidden이면
전달되지 않는다. 그래서 입력·엔터·클릭을 전부 페이지 안 JS 이벤트로 처리한다.
"""
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, "~")
import cdp  # noqa: E402

SITE = "https://<TISTORY_ID>.tistory.com/"
RES = urllib.parse.quote(SITE, safe="")
# 크롬에 구글 계정이 여러 개 붙어 있고 기본(u/0)은 속성 소유자가 아니다.
# 2026-08-26~27 색인 요청이 통째로 죽은 원인이 이거였다(전부 TIMEOUT).
AUTHUSER = 0   # 09-03 프로브: u/0(<EMAIL>)만 속성 소유자, u/1은 not-verified
GSC = "https://search.google.com/u/%d/search-console?resource_id=%s" % (AUTHUSER, RES)
DRAFTS = Path("~/pipeline/drafts")
STATE = Path("~/pipeline/.gsc_index.json")  # {"28": "OK", "25": "2026-07-23"}
PER_RUN = 10
RETRY_DAYS = 14  # 한 번 요청한 URL은 이 기간 지나야 다시 태운다(할당량 낭비 방지)

BOX = ("[].slice.call(document.querySelectorAll('input')).filter(function(e){"
       "return /모든 URL 검사/.test(e.getAttribute('aria-label')||'');})[0]")


def targets(state, today):
    """발행된 글 번호 중 색인 안 됐고 최근에 요청도 안 한 것들, 오래된 순."""
    ids = set()
    for f in DRAFTS.glob("*.tpublished"):
        m = re.search(r"/(\d+)\s*$", f.read_text().strip())
        if m:
            ids.add(int(m.group(1)))
    cut = str(today - datetime.timedelta(days=RETRY_DAYS))
    def due(i):
        s = state.get(str(i), "")
        return s not in ("OK", "GONE") and s < cut  # "" (미확인) 또는 오래된 요청일
    # 아직 한 번도 요청 안 한 글을 최신순으로 먼저 태운다. 그동안은 번호 오름차순이라
    # 오래된 글부터 재요청하느라 그날 발행한 글이 며칠씩 밀렸다. 2026-08-08 GSC 실측:
    # 미색인 220건 중 110건이 "발견됨 - 현재 색인이 생성되지 않음"인데 이 버킷은
    # 재요청으로 안 움직인다(7/23부터 매일 10건씩 태운 결과가 그 숫자다).
    # 재요청분은 신규를 다 채우고 남은 할당량으로만 돌린다.
    fresh = sorted((i for i in ids if due(i) and not state.get(str(i))), reverse=True)
    stale = sorted(i for i in ids if due(i) and state.get(str(i)))
    out = []
    for i in fresh + stale:
        # 지워진 글이 큐에 남아 하루 10건 할당량을 먹고 있었다(2026-08-08, 48번이 404).
        if _alive(SITE + str(i)):
            out.append(i)
        else:
            state[str(i)] = "GONE"
            print(i, "404 — 큐에서 제외")
        if len(out) >= PER_RUN:      # 어차피 하루 상한까지만 쓴다. 나머지는 HEAD도 안 친다.
            break
    return out


def _alive(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        return urllib.request.urlopen(req, timeout=15).status == 200
    except urllib.error.HTTPError as e:
        return e.code != 404      # 5xx·429는 살아있다고 보고 넘긴다
    except Exception:
        return True


class Console:
    def __init__(self):
        self.tab = self.p = None

    def _open(self, url):
        """URL 하나당 탭 하나. 한 ws 로 여러 번 navigate 하면 도중에 끊긴다."""
        self.close()
        self.tab = cdp._http("/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
        self.p = cdp.Page(self.tab)
        time.sleep(10)

    def close(self):
        if self.tab:
            try:
                cdp._http("/json/close/" + self.tab["id"])
            except Exception:
                pass
            self.tab = None

    def ev(self, js):
        r = self.p.send("Runtime.evaluate", expression="(function(){%s})()" % js, returnByValue=True)
        return r.get("result", {}).get("value")

    def wait(self, pat, tmo):
        end = time.time() + tmo
        while time.time() < end:
            m = re.search(pat, self.ev("return (document.body.innerText||'');") or "")
            if m:
                return m.group(0)
            time.sleep(3)
        return None

    def click(self, words, tmo):
        end = time.time() + tmo
        while time.time() < end:
            r = self.ev("""var want=%s;
              var els=[].slice.call(document.querySelectorAll('*')).filter(function(e){
                return e.children.length===0 && want.indexOf((e.innerText||'').trim())>=0
                       && e.getBoundingClientRect().width>0;});
              if(!els.length) return '';
              var e=els[0], t=e.innerText.trim(), n=e;
              for(var k=0;k<5 && n;k++){
                if(n.tagName==='BUTTON'||n.getAttribute('role')==='button'){break;}
                n=n.parentElement;}
              (n||e).click(); return t;""" % json.dumps(words, ensure_ascii=False))
            if r:
                return r
            time.sleep(1)
        return None

    def inspect(self, url):
        self._open(GSC)
        if "not-verified" in (self.ev("return location.href;") or ""):
            return "NO_ACCESS"   # 로그인 계정이 속성 소유자가 아님 — AUTHUSER 확인
        ok = self.ev("""var i=%s; if(!i) return 'NO_BOX';
          var set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
          i.focus(); set.call(i,%s);
          ['input','change'].forEach(function(t){i.dispatchEvent(new Event(t,{bubbles:true}));});
          ['keydown','keypress','keyup'].forEach(function(t){
            i.dispatchEvent(new KeyboardEvent(t,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));});
          return 'OK';""" % (BOX, json.dumps(url)))
        if ok != "OK":
            return ok or "NO_EVAL"
        return self.wait(r"URL이 Google에 등록되어 있(지 않음|음)", 120) or "TIMEOUT"

    def request(self):
        if not self.click(["색인 생성 요청", "색인 생성을 요청"], 25):
            return "NO_BUTTON"
        r = self.wait(r"(색인 생성 요청됨|우선순위 크롤링 대기열에 URL이 추가됨|할당량을 초과|하루 할당량)", 240)
        self.click(["확인", "닫기", "OK"], 6)
        return r or "PENDING"


def main():
    today = datetime.date.today()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    todo = targets(state, today)[:PER_RUN]
    print(today, "대상", todo)
    if not todo:
        return
    c = Console()
    try:
        for pid in todo:
            st = c.inspect(SITE + str(pid))
            if "있음" in st:
                state[str(pid)] = "OK"
                print(pid, "이미 색인됨")
                continue
            if st == "NO_ACCESS":
                print("서치콘솔 계정이 속성 소유자가 아님 — AUTHUSER=%d 확인 필요" % AUTHUSER)
                raise SystemExit(2)   # company.py 장부에 FAIL 로 남긴다 — rc 0 이면 "돌았는데 아무것도 안 함"이 14일 가는 패턴
            if "않음" not in st:
                print(pid, "실패:", st)
                continue
            r = c.request()
            if "할당량" in r:
                print(pid, "할당량 소진 — 중단")
                break
            state[str(pid)] = str(today)
            print(pid, "요청:", r)
            time.sleep(4)
    finally:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        c.close()


if __name__ == "__main__":
    main()
