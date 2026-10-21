#!/usr/bin/env python3
"""
나이스 출결 자동 입력기 v4.0
구글 캘린더 출결 캘린더 → 나이스 일일출결관리(담임용) 자동 입력
"""

import asyncio
import re
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from playwright.async_api import async_playwright, Page

import keyring
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import teacher_config

# ============================================================
# 설정 — 학교 공통(하드코딩) 값
# ============================================================
YEAR       = date.today().year
TOKEN_FILE = teacher_config.TOKEN_FILE

# 아래 값들은 load_teacher_settings()가 설정 파일에서 채운다.
NEIS_URL      = None   # 시도교육청별 나이스 주소
SHEET_ID      = ""     # 휴업일 시트 (비어 있으면 휴업일 확인을 건너뛴다)
HOLIDAY_RANGE = ""
LAST_PERIOD   = 7      # 하루 최대 교시 수

# ============================================================
# 설정 — 교사별 값 (load_teacher_settings()에서 채움)
# ============================================================
GRADE = CLASS = CERT_NAME = CERT_PASSWORD = None
ATTENDANCE_CALENDARS: dict[str, str] = {}


def _origin_of(url: str) -> str:
    """https://xxx.neis.go.kr/jsp/main.jsp  ->  https://xxx.neis.go.kr"""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def load_teacher_settings() -> None:
    """%LOCALAPPDATA%의 teacher_config.json + keyring에서 교사별 설정을 읽어
    모듈 전역(GRADE/CLASS/CERT_NAME/CERT_PASSWORD/ATTENDANCE_CALENDARS)에 채운다."""
    global GRADE, CLASS, CERT_NAME, CERT_PASSWORD, ATTENDANCE_CALENDARS
    global NEIS_URL, SHEET_ID, HOLIDAY_RANGE, LAST_PERIOD
    cfg = teacher_config.load_config()
    NEIS_URL      = cfg["neis_url"]
    SHEET_ID      = cfg.get("holiday_sheet_id", "")
    HOLIDAY_RANGE = cfg.get("holiday_sheet_range", "")
    LAST_PERIOD   = int(cfg.get("last_period", 7))
    GRADE = cfg["grade"]
    CLASS = cfg["class"]
    CERT_NAME = cfg["cert_name"]
    ATTENDANCE_CALENDARS = cfg["calendars"]
    CERT_PASSWORD = keyring.get_password(teacher_config.KEYRING_SERVICE, CERT_NAME)
    if not CERT_PASSWORD:
        raise teacher_config.ConfigMissingError(
            f"'{CERT_NAME}' 인증서 비밀번호를 찾을 수 없습니다. 설정 마법사를 다시 실행하세요."
        )

KOREAN_WEEKDAYS = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

# ATTENDANCE_CALENDARS는 load_teacher_settings()가 teacher_config.json에서 채운다
# (교사별 8개 캘린더 ID — 설정 마법사가 자동 생성)

ATTENDANCE_MAP = {
    "질병결석":     ("질병",    "결석"),
    "미인정결석":   ("미인정",  "결석"),
    "기타결석":     ("기타",    "결석"),
    "출석인정결석": ("출석인정","결석"),
    "질병지각":     ("질병",    "지각"),
    "미인정지각":   ("미인정",  "지각"),
    "기타지각":     ("기타",    "지각"),
    "출석인정지각": ("출석인정","지각"),
    "질병조퇴":     ("질병",    "조퇴"),
    "미인정조퇴":   ("미인정",  "조퇴"),
    "기타조퇴":     ("기타",    "조퇴"),
    "출석인정조퇴": ("출석인정","조퇴"),
}

# ============================================================
# 구글 시트 휴업일 읽기
# ============================================================
def load_holidays() -> set:
    if not SHEET_ID:
        return set()   # 휴업일 시트를 설정하지 않았으면 이 기능만 건너뛴다
    try:
        svc = build("sheets", "v4", credentials=Credentials.from_authorized_user_file(str(TOKEN_FILE)))
        res = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=HOLIDAY_RANGE).execute()
        holidays = set()
        for row in res.get("values", []):
            if not row or not row[0].strip():
                continue
            try:
                m, d = row[0].strip().split("/")
                holidays.add(date(YEAR, int(m), int(d)))
            except Exception:
                pass
        return holidays
    except Exception as e:
        print(f"  ⚠️  휴업일 로드 실패: {e}")
        return set()


# ============================================================
# 구글 캘린더 읽기
# ============================================================
def get_calendar_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def load_from_calendar(start_date: date, end_date: date) -> list[dict]:
    service = get_calendar_service()
    time_min = datetime.combine(start_date, datetime.min.time()).isoformat() + "Z"
    time_max = datetime.combine(end_date + timedelta(days=1), datetime.min.time()).isoformat() + "Z"

    records = []
    for att_type, cal_id in ATTENDANCE_CALENDARS.items():
        result = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        for event in result.get("items", []):
            title = event.get("summary", "")
            parts = title.split()
            if len(parts) < 2:
                continue
            try:
                number = int(parts[0])
            except ValueError:
                continue

            name = parts[1]
            # 교시 정보는 보통 제목 끝 괄호 안에 있음 (예: "31 이영희 (4교시~)")
            paren = re.search(r"\(([^)]*)\)\s*$", title)
            note = paren.group(1) if paren else event.get("description", "")
            ev_start = event["start"]
            ev_end   = event["end"]

            if "date" in ev_start:
                s = date.fromisoformat(ev_start["date"])
                e = date.fromisoformat(ev_end["date"]) - timedelta(days=1)
            else:
                s = datetime.fromisoformat(ev_start["dateTime"]).date()
                e = s

            records.append({
                "number":     str(number),
                "name":       name,
                "att_type":   att_type,
                "start_date": s,
                "end_date":   e,
                "note":       note,
            })

    return records


def _missed_periods(task: dict) -> float:
    """하루 중 빠진 교시 수 추정 — 같은 날 여러 건일 때 많이 빠진 건을 마지막에 입력하기 위한 정렬 키."""
    jongryu = task["jongryu"]
    if jongryu == "결석":
        return 99  # 하루 전체
    g = parse_gyosi_num(task["note"], jongryu)
    if g is None:
        return 50  # 교시 미상이면 중간 순위
    if jongryu == "지각":
        return g + 1  # 조회(0)~g교시까지 빠짐
    if jongryu == "조퇴":
        return LAST_PERIOD - g + 1  # g교시부터 하루 끝까지 빠짐
    return 50


def _pick_duplicate(kept: dict, other: dict, day: date) -> dict:
    """같은 날·같은 학생·같은 분류 이벤트가 둘일 때 어느 쪽을 쓸지 고른다.

    ⚠️ 예전에는 먼저 읽힌 쪽을 무조건 남겼다. 캘린더에 빈 중복 이벤트가 있으면
       교시가 적힌 쪽이 버려지고 교시 없는 지각이 입력됐다
       (2026-09-08 실측: note '' 건과 '~2교시' 건 중 '' 가 남음).
    → 교시를 읽을 수 있는 쪽을 쓴다. 둘 다 읽히는데 교시가 서로 다르면 어느 쪽이 맞는지
      모르므로 «conflict» 를 달아 둔다 — enter_student 가 그 학생을 넣지 않고 ❌ 로 올린다.
    """
    k = parse_gyosi_num(kept["note"], kept["jongryu"])
    o = parse_gyosi_num(other["note"], other["jongryu"])
    pick, drop = (other, kept) if (k is None and o is not None) else (kept, other)
    print(f"  🔁 중복: {day} {pick['number']}번 {pick['name']} [{pick['label']}] "
          f"— note '{pick['note']}' 를 쓰고 '{drop['note']}' 는 버립니다")
    if k is not None and o is not None and k != o:
        return {**pick, "conflict": f"캘린더에 같은 {pick['label']} 이벤트가 둘인데 교시가 다름 "
                                    f"('{kept['note']}' / '{other['note']}')"}
    return pick


def build_task_map(records: list[dict]) -> dict[date, list[dict]]:
    task_map: dict[date, list[dict]] = {}
    for rec in records:
        att = ATTENDANCE_MAP.get(rec["att_type"])
        if not att:
            print(f"  ⚠️  알 수 없는 분류: '{rec['att_type']}' — 건너뜀")
            continue
        cur = rec["start_date"]
        while cur <= rec["end_date"]:
            if cur.weekday() >= 5:  # 토(5)·일(6)요일은 등교일이 아니므로 건너뜀
                cur += timedelta(days=1)
                continue
            tasks = task_map.setdefault(cur, [])
            new_task = {
                "number":  rec["number"],
                "name":    rec["name"],
                "gubun":   att[0],
                "jongryu": att[1],
                "note":    rec["note"],
                "label":   rec["att_type"],
                "memo":    rec.get("memo", ""),   # 나이스 「사유」 칸 (출결 앱 파일에만 있다)
            }
            # 같은 학생이라도 분류가 다르면 둘 다 입력 (예: 같은 날 지각+조퇴)
            # — 완전히 동일한 (학생, 분류) 건만 하나로 합친다
            dup = next((i for i, t in enumerate(tasks)
                        if t["number"] == rec["number"] and t["label"] == rec["att_type"]), None)
            if dup is None:
                tasks.append(new_task)
            else:
                tasks[dup] = _pick_duplicate(tasks[dup], new_task, cur)
            cur += timedelta(days=1)
    # 같은 학생이 하루에 여러 건이면 빠진 시간이 짧은 것부터 입력하고
    # 제일 많이 빠진(제일 안 좋은) 건을 맨 마지막에 입력한다.
    # 예: 지각(조회만 늦음) + 조퇴(2교시~) → 지각 먼저, 조퇴 마지막
    #     지각(~5교시) + 조퇴(7교시~)     → 조퇴 먼저, 지각 마지막
    for tasks in task_map.values():
        tasks.sort(key=_missed_periods)
    return dict(sorted(task_map.items()))


# ============================================================
# NEIS 자동화
# ============================================================
async def auto_login(page: Page, wait_ms: int = 8_000):
    """인증서 자동 로그인. 회선이 느리면 wait_ms를 늘려 부른다."""
    print("🔐  로그인 페이지 확인 중...")

    try:
        await page.wait_for_selector("text=학급담임", timeout=3000)
        print("✅  이미 로그인 상태")
        return
    except:
        pass

    try:
        # 인증서 로그인 버튼 클릭
        btn = page.get_by_role("button", name="인증서 로그인")
        await btn.wait_for(timeout=wait_ms)
        await btn.click()
        print("🔐  인증서 로그인 버튼 클릭")

        # 인증서 선택 (다이얼로그가 뜨는 경우)
        try:
            cert = page.locator(f"xpath=//div[contains(@title,'{CERT_NAME}')]").first
            await cert.wait_for(timeout=5000)
            await cert.click()
            print(f"🔐  인증서 '{CERT_NAME}' 선택")
        except:
            pass  # 자동 선택된 경우 무시

        # 비밀번호 입력 후 Enter (버튼 클릭 대신)
        pwd = page.get_by_role("textbox", name="인증서 암호 입력필드")
        await pwd.wait_for(timeout=8000)
        await pwd.fill(CERT_PASSWORD)
        await pwd.press("Enter")
        print("🔐  비밀번호 입력 완료")
        print("✅  인증서 입력 완료. 로그인 대기 중...")

    except Exception as e:
        print(f"⚠️  자동 로그인 실패: {e}")
        try:    # 인증서 창이 안 뜬 가장 흔한 이유 — 집 PC 에 인증서 프로그램이 없다
            import neis_cert
            h = neis_cert.hint()
            if h:
                print(h)
        except Exception:
            pass
        try:    # 무슨 화면에 서 있는지 남긴다 (오류 페이지 구분용)
            print(f"   현재 화면: {page.url}")
            print(f"   제목: {await page.title()}")
        except Exception:
            pass
        print("   브라우저에서 수동으로 로그인해주세요.")


async def wait_for_attendance_page(page: Page):
    print("📌  로그인 대기 중...")
    await page.wait_for_selector("text=학급담임", timeout=120_000)
    print("✅  로그인 완료!")

    # 팝업은 «이미 열려 있는 경우» 에도 걸리적거린다. 조회·저장 클릭까지 가로채므로
    # 아래 «이미 열려있음» 판정보다 먼저 치운다 (2026-09-11).
    await dismiss_alert_popup(page, timeout=1000)
    await dismiss_notice_popup(page, timeout=1500)

    # 이미 출결관리 페이지에 있으면 바로 진행 (다른 탭에 숨어있는 버튼은 무시하고 실제로 보이는 것만 확인)
    if await page.locator(".cl-dateinput-button:visible").count() > 0:
        print("✅  출결관리 페이지 이미 열려있음!\n")
        return

    print("📌  출결관리로 이동 중...")

    # 학급담임 → 출결관리(2단계 카테고리) → 출결관리(3단계=일일출결관리)
    # 로그인 직후 팝업이 메뉴를 가리거나, 메뉴가 이미 열려 있어 토글로 닫히는
    # 경우가 있어 팝업 닫기 + 재시도로 감싼다.
    # 팝업은 두 종류이고 닫는 버튼이 서로 다르다 — 둘 다 시도해야 한다.
    #   알림('확인')  ← dismiss_alert_popup
    #   공지('닫기')  ← dismiss_notice_popup   (이걸 안 해서 메뉴가 막혔었다)
    last_error = None
    for attempt in range(1, 4):
        await dismiss_alert_popup(page, timeout=1500)
        await dismiss_notice_popup(page, timeout=1500)
        try:
            sub2 = page.get_by_role("link", name="출결관리 2단계").first
            if not await sub2.is_visible():
                await page.get_by_role("link", name="학급담임 0단계 메뉴항목").click()
                await page.wait_for_timeout(800)

            await sub2.click(timeout=8000)
            await page.wait_for_timeout(500)

            await page.locator("a").filter(has_text="출결관리 3단계").click(timeout=8000)

            # 날짜 달력 버튼이 나타날 때까지 대기 (숨은 탭의 버튼은 무시)
            await page.locator(".cl-dateinput-button:visible").first.wait_for(timeout=15_000)
            print("✅  출결관리 페이지 진입!\n")
            return
        except Exception as e:
            last_error = e
            print(f"  ⚠️  메뉴 이동 실패, 재시도 {attempt}/3")
            await page.wait_for_timeout(1000)
    raise last_error


# ────────────────────────────────────────────────────────────────
# 나이스 대화상자(알림창·확인창·저장 결과창) 찾기
#
# ⚠️ `wait_for_selector("text=알림")` 으로 찾으면 안 된다. 로그인 뒤 화면에는 «숨은»
#    「민원현황 알림」 메뉴 글자가 맨 앞에 있고, wait_for_selector 는 첫 번째로 걸린
#    요소 하나만 보고 «안 보인다» 며 끝까지 기다린다 — 알림창이 떠 있어도 시간 초과다.
#    (2026-09-11 실측: 출결 화면에서 'text=알림' 매치 2개 · #0 숨은 「민원현황 알림」 ·
#     #1 은 보이는데도 시간 초과. 그래서 저장이 된 날도 「결과창을 못 봤습니다」 가 났고,
#     알림창 닫기는 로그에 한 번도 찍히지 않았다.)
# → 글자 대신 «보이는 대화상자» 를 기다리고, 창 글자로 무슨 창인지 가린다.
#   나이스 대화상자 = .cl-dialog-wrapper > .cl-dialog[role=dialog] (공지 팝업 DOM 실측).
#   인증서용 [role=dialog] 18개가 늘 «숨은 채» 붙어 있어서 :visible 을 빼면 안 된다
#   (창이 없을 때 :visible 로는 0개 — 실측).
DIALOG_SEL = ".cl-dialog-wrapper:visible, [role=dialog]:visible"


def dialog_title(raw: str) -> str:
    """창 글자의 첫 줄 = 창 제목 (실측: 「알림 / 저장했습니다. / 확인」)."""
    return next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")


async def wait_dialog(page: Page, accept, timeout_ms: int):
    """보이는 대화상자 중 accept(창 글자) 가 참인 것을 기다린다.

    돌려주는 값: (창 ElementHandle, 공백 한 칸으로 합친 글자). 못 보면 (None, "").
    accept 에는 줄바꿈이 살아 있는 글자를 넘긴다 — 첫 줄이 창 제목이다.
    ⚠️ locator.nth(i) 를 돌려주면 안 된다. locator 는 쓸 때마다 다시 찾기 때문에, 글자를
       읽은 뒤 창 목록이 바뀌면 «검사한 창» 과 «누르는 창» 이 달라진다 (코덱스 검토 2026-09-11).
       ElementHandle 은 읽은 그 요소에 고정되고, 닫혔으면 클릭이 실패할 뿐 딴 창을 누르지 않는다.
    """
    loops = max(1, timeout_ms // 250)
    for n in range(loops):
        try:
            handles = await page.locator(DIALOG_SEL).element_handles()
        except Exception:
            handles = []
        for h in reversed(handles):
            try:
                raw = await h.inner_text()
            except Exception:
                continue   # 읽는 사이에 닫혔다
            if raw.strip() and accept(raw):
                return h, " ".join(raw.split())
        if n < loops - 1:
            await page.wait_for_timeout(250)
    return None, ""


async def click_dialog_confirm(dialog) -> bool:
    """그 창 «안의» [확인] 을 누른다 (dialog = wait_dialog 가 준 ElementHandle).
    나이스 버튼은 a 일 때도 div[role=button] 일 때도 있다. 창이 이미 닫혔으면 False."""
    try:
        for btn in reversed(await dialog.query_selector_all("a, button, [role=button]")):
            if (await btn.inner_text()).strip() == "확인":
                await btn.click(force=True, timeout=3000)
                return True
    except Exception:
        pass
    return False


async def dismiss_alert_popup(page: Page, timeout=3000) -> bool:
    """제목이 「알림」 인 창이 떠 있으면 [확인] 을 누르고, 무슨 알림이었는지 남긴다.

    「저장하시겠습니까?」 같은 확인창(제목 「확인」)이나 공지 팝업(제목 「공지사항」,
    dismiss_notice_popup 담당)은 건드리지 않는다.
    """
    # 제목이 「알림」 이어도 묻는 창(하시겠습니까)은 누르지 않는다 — [확인] 이 무슨 일을 할지 모른다.
    dialog, text = await wait_dialog(
        page, lambda raw: dialog_title(raw) == "알림" and "하시겠습니까" not in raw, timeout)
    if dialog is None:
        return False
    ok = await click_dialog_confirm(dialog)
    if ok:
        print(f"  ℹ️  알림 팝업 닫기: {text[:70]}")
        await page.wait_for_timeout(400)
    return ok


# ────────────────────────────────────────────────────────────────
# 공지 팝업 — 「알림」 팝업과 다른 물건이다
#
#   알림 팝업 : 본문에 '알림' 글자        → '확인' 버튼   (dismiss_alert_popup)
#   공지 팝업 : '공지사항/전달사항내용조회' → '닫기' 버튼   (여기)
#
# 예전에는 dismiss_alert_popup() 하나가 둘 다 닫는 줄 알았다. 실제로는
# `text=알림` 을 못 찾고 1.5초 뒤 그냥 흘러가서 공지 팝업이 그대로 남았고,
# 그 팝업이 메뉴 클릭을 가로채 '메뉴 이동 실패'가 났다 (2026-09-11 실측).
#
# 아래 선택자는 추측이 아니라 로그인 직후 화면의 DOM 을 그대로 뜬 것이다:
#   <div class="cl-aside">  … 공지사항 / 전달사항내용조회 …
#     <div class="cl-dialog-close" role="button" aria-label="닫기"></div>
#     <div class="cl-checkbox-icon" role="checkbox" aria-checked="false"
#          aria-label="오늘 하루 창 열지 않음"></div>
#     <div class="cl-checkbox-icon" role="checkbox" aria-checked="false"
#          aria-label="일주일 창 열지 않음"></div>
#     <div role="button" class="btn-outline-secondary cl-control cl-button">닫기</div>
#   </div>
#
# 주의 ① data-ndid 는 실행할 때마다 바뀐다(n1·n2 …). 선택자로 쓰지 않는다.
# 주의 ② .cl-aside 자체는 높이가 0이라 playwright 기준 '보이지 않는' 요소다.
#        컨테이너에 is_visible() 을 걸면 안 되고, 안쪽 컨트롤로 판정해야 한다.
NOTICE_MARK       = "전달사항내용조회"   # 공지 팝업을 알아보는 글자
NOTICE_SKIP_LABEL = "일주일 창 열지 않음"  # 자동화 전용 프로필이라 여기서 공지를 볼 일이 없다
_NOTICE_STILL_OPEN = f'[role="checkbox"][aria-label="{NOTICE_SKIP_LABEL}"]:visible'


async def dismiss_notice_popup(page: Page, timeout: int = 3000, rounds: int = 3) -> bool:
    """로그인 직후 뜨는 공지 팝업을 닫는다. 닫았으면 True.

    「일주일 창 열지 않음」을 먼저 체크하고 닫으므로, 이 크롬 프로필에서는
    한 주 동안 다시 뜨지 않는다. 공지는 평소 쓰는 브라우저에서 따로 본다.
    """
    closed_any = False
    for i in range(rounds):
        popup = page.locator(".cl-aside").filter(has_text=NOTICE_MARK).last
        try:
            await popup.wait_for(state="attached", timeout=timeout if i == 0 else 800)
        except Exception:
            break

        # ① 「일주일 창 열지 않음」 체크
        box = popup.locator(f'[role="checkbox"][aria-label="{NOTICE_SKIP_LABEL}"]')
        try:
            if await box.count() > 0:
                if await box.first.get_attribute("aria-checked") != "true":
                    await box.first.click(timeout=3000)
                    await page.wait_for_timeout(300)
                state = await box.first.get_attribute("aria-checked")
                print(f"  ℹ️  공지 팝업: 「{NOTICE_SKIP_LABEL}」 체크 (aria-checked={state})")
                if state != "true":
                    print("  ⚠️  공지 팝업: 체크가 안 먹었습니다 — 다음 실행에 또 뜰 수 있습니다.")
            else:
                print(f"  ⚠️  공지 팝업: 「{NOTICE_SKIP_LABEL}」 체크박스가 없습니다 (그냥 닫습니다).")
        except Exception as e:
            print(f"  ⚠️  공지 팝업: 체크박스를 누르지 못했습니다 — {str(e)[:70]}")

        # ② 닫기 — '확인'이 아니라 '닫기'. 본문 버튼이 안 되면 머리말의 X 아이콘.
        for what, loc in (
            ("닫기 버튼", popup.locator('[role="button"]:visible').filter(has_text=re.compile(r"^닫기$"))),
            ("X 아이콘",  popup.locator(".cl-dialog-close:visible")),
        ):
            try:
                if await loc.count() == 0:
                    continue
                await loc.last.click(timeout=3000)
                await page.wait_for_timeout(500)
                if await page.locator(_NOTICE_STILL_OPEN).count() == 0:
                    print(f"  ℹ️  공지 팝업 닫기 ({what})")
                    closed_any = True
                    break
            except Exception:
                continue
        else:
            print("  ⚠️  공지 팝업을 닫지 못했습니다 — 메뉴 클릭이 막힐 수 있습니다.")
            break

        if await page.locator(_NOTICE_STILL_OPEN).count() > 0:
            print("  ⚠️  공지 팝업이 아직 떠 있습니다 — 다시 시도합니다.")
            continue
        break

    return closed_any


async def set_date_and_search(page: Page, d: date) -> bool:
    date_str = d.strftime("%Y.%m.%d.")
    target_name = f"{d.year}년 {d.month}월 {d.day}일 {KOREAN_WEEKDAYS[d.weekday()]}"

    # overlay 닫기 (사이드바가 열려있으면 Escape로 닫기)
    overlay = page.locator(".cl-overlay")
    if await overlay.count() > 0:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
    if await overlay.count() > 0:
        await overlay.first.click(force=True)
        await page.wait_for_timeout(500)

    # 달력 버튼 클릭 (날짜 선택 후 aria-label이 "선택됨:..."으로 바뀌므로 클래스로 찾기)
    # 페이지에 여러 개 있을 수 있으므로 is_visible()로 실제 보이는 버튼 선택
    all_cal = page.locator(".cl-dateinput-button")
    n = await all_cal.count()
    cal_btn = None
    for i in range(n):
        btn = all_cal.nth(i)
        if await btn.is_visible():
            cal_btn = btn
            break
    if cal_btn is None:
        cal_btn = all_cal.last
    try:
        await cal_btn.click(timeout=5000)
    except:
        await cal_btn.click(force=True)
    await page.wait_for_timeout(400)

    # 날짜 버튼 클릭 (달 이동 필요시 최대 6번 시도)
    date_btn = page.get_by_role("button", name=target_name)
    found = False
    for _ in range(6):
        if await date_btn.count() > 0:
            await date_btn.click()
            found = True
            break
        prev = page.locator("button").filter(has_text=re.compile(r"이전|prev", re.IGNORECASE))
        if await prev.count() > 0:
            await prev.first.click()
            await page.wait_for_timeout(300)
        else:
            break

    if not found:
        print(f"  ⚠️  달력 날짜 버튼 없음, 타이핑 시도: {date_str}")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        inputs = page.locator("input")
        cnt = await inputs.count()
        for i in range(cnt):
            val = await inputs.nth(i).input_value()
            if val and re.match(r"\d{4}\.\d{2}\.\d{2}", val):
                await inputs.nth(i).click(force=True)
                await inputs.nth(i).press("Control+a")
                await inputs.nth(i).type(date_str, delay=30)
                await inputs.nth(i).press("Tab")
                break

    print(f"  📅  날짜 설정: {date_str}")

    # 화면의 날짜 칸을 «읽어서» 그날이 맞는지 본다. 엉뚱한 날짜 화면에 출결을
    # 적는 것이 제일 나쁘다. 날짜 칸 자체를 못 찾으면 예전처럼 그냥 진행한다
    # (여기서 막으면 되던 것까지 멈춘다).
    # 날짜 칸이 화면에 여러 개일 수 있다(조회 기간 등). 그래서 «하나라도 그날이면»
    # 통과로 본다. 「맨 앞 칸이 다르다」로 막으면 되던 날까지 통째로 건너뛴다.
    seen_dates = []
    _inputs = page.locator("input:visible")
    for _i in range(await _inputs.count()):
        try:
            _v = (await _inputs.nth(_i).input_value()).strip()
        except Exception:
            continue
        if re.match(r"^\d{4}\.\d{2}\.\d{2}\.?$", _v):
            seen_dates.append(_v if _v.endswith(".") else _v + ".")
    if not seen_dates:
        print("  ⚠️  화면에서 날짜 칸을 못 찾아 확인하지 못했습니다 (그대로 진행)")
    elif date_str not in seen_dates:
        print(f"  ⛔  날짜가 안 바뀌었습니다 — 화면: {', '.join(seen_dates)} / 넣으려던 날: {date_str}")
        return False

    # 날짜를 바꾸면 나이스가 "학적변동 시간표 확인하세요" 같은 알림 팝업을 바로 띄우는
    # 경우가 있음 — 안 닫으면 팝업 오버레이가 조회 버튼 클릭을 막아 타임아웃 남
    await dismiss_alert_popup(page, timeout=1500)

    # 조회 클릭 (tabpanel 안의 <a> 태그)
    # 알림 팝업이 위 dismiss_alert_popup 체크 이후 뒤늦게 뜨는 경우가 있어
    # 오버레이가 클릭을 막으면(30초 타임아웃 대신) 팝업을 다시 닫고 재시도한다.
    조회_btn = page.get_by_role("tabpanel").locator("a").filter(has_text=re.compile(r"^조회$"))
    for attempt in range(3):
        try:
            await 조회_btn.click(timeout=5000)
            break
        except Exception:
            print("  ⚠️  조회 클릭 막힘 (알림 팝업 추정) — 확인 클릭 후 재시도")
            await dismiss_alert_popup(page, timeout=3000)
            await page.wait_for_timeout(300)
    else:
        await 조회_btn.click(force=True)
    print("  ✅  조회 클릭")

    # 알림 팝업 처리
    await dismiss_alert_popup(page, timeout=2000)

    # 그리드 로드 대기
    try:
        await page.wait_for_selector("text=Total", timeout=5000)
    except:
        await page.wait_for_timeout(2000)

    return True


def parse_gyosi_num(note: str, jongryu: str = "") -> int | None:
    """note에서 교시 번호 추출.
    조퇴: 첫 번째 빠진 교시 (e.g. '4교시~'→4, '5,6교시'→5)
    지각: 마지막 빠진 교시 (e.g. '조회'→0, '1교시'→1, '1,2교시'→2)
    """
    if not note:
        return None
    if re.search(r"조회", note):
        return 0
    nums = re.findall(r"\d+", note)
    if not nums:
        return None
    return int(nums[-1]) if jongryu == "지각" else int(nums[0])



GRID_HEADER_JS = r"""
() => {
  const NL = String.fromCharCode(10);
  // 헤더 셀에는 정렬 표시 등이 덧붙는다. 첫 줄의 첫 낱말만 취한다
  // (컬럼 이름에는 공백이 없다: 번호·성명·마감·조회·N교시·종례·사유)
  const clean = t => ((t || '').trim().split(NL)[0].trim().split(/\s+/)[0] || '');
  for (const r of document.querySelectorAll('.cl-grid-row')) {
    const kids = Array.from(r.children).map(d => d.innerText);
    if (kids.length && clean(kids[0]) === '번호') return kids.map(clean);
  }
  return [];
}
"""


async def read_day_columns(page: Page) -> list:
    """그날 그리드의 실제 컬럼 이름 목록.

    ['번호','성명','마감','조회','1교시',...,'종례','사유'] 형태.
    교시 수가 날마다 다르므로(단축수업·학교급별 차이) 위치를 고정하면 안 된다.
    읽지 못하면 빈 리스트 — 그때는 예전처럼 고정 위치로 계산한다.
    """
    try:
        cols = await page.evaluate(GRID_HEADER_JS)
    except Exception:
        return []
    if not isinstance(cols, list):
        return []
    # 제대로 읽었는지 확인한다. 헤더 모양이 바뀌어 엉뚱하게 읽혔는데 그대로 믿으면
    # 멀쩡한 학생을 "없는 교시"라며 통째로 건너뛰게 된다 — 그게 더 나쁘다.
    if "조회" not in cols or not any(c.endswith("교시") for c in cols):
        print("  ⚠️  그리드 헤더를 읽지 못했습니다 — 교시 위치를 예전 방식으로 계산합니다.")
        return []
    return cols


def day_last_period(day_cols) -> int:
    """그날 마지막 교시 번호. 못 읽으면 0."""
    nums = [int(c[:-2]) for c in (day_cols or []) if c.endswith("교시") and c[:-2].isdigit()]
    return max(nums) if nums else 0


def gyosi_col_index(gyosi_num: int, day_cols=None):
    """교시 번호 → td 0-based 인덱스. 그날 없는 교시면 None.

    day_cols(그날 실제 헤더)가 있으면 **이름으로** 찾는다 — 이게 정확한 방법이다.
    헤더 목록의 인덱스가 곧 이 함수가 돌려주는 값이다
    (번호=0, 성명=1, 마감=2, 조회=3, 1교시=4 ...).

    day_cols가 없으면 예전처럼 6~7교시 시간표를 가정하고 계산한다.
    """
    want = "조회" if gyosi_num == 0 else f"{gyosi_num}교시"
    if day_cols:
        return day_cols.index(want) if want in day_cols else None
    if gyosi_num == 0:
        return 3  # 조회
    return 3 + gyosi_num  # 1교시→4, 4교시→7


async def pick_student_row(page: Page, name: str, number: str):
    """이름 «과 번호» 가 맞는 행을 고른다.

    ⚠️ 예전에는 이름이 들어간 «첫 행» 을 그냥 썼다. 동명이인이 있으면 다른 학생에게
       출결이 들어간다 — 되돌리기도 어렵고 알아채기도 어렵다 (2026-09-11 수정).
       번호로 특정하지 못하는데 후보가 여럿이면, 넣지 않고 그 학생만 실패로 넘긴다.
    """
    rows = page.locator(".cl-grid-row").filter(has_text=name)
    n = await rows.count()
    if n == 0:
        raise RuntimeError("'" + name + "' 이름이 있는 행을 찾지 못했습니다")
    if n == 1:
        return rows.first

    # 후보가 둘 이상이면 «고르지 않는다».
    # 행 글자에서 번호를 찾아 맞히는 방법도 생각했지만, 행에는 교시·날짜 같은
    # 다른 숫자도 같이 들어 있어서 «14번» 을 찾다가 «14교시» 에 걸릴 수 있다.
    # 잘못 넣는 것보다 안 넣고 알리는 편이 낫다 — 동명이인은 드물고, 그때 생기는
    # 실수는 되돌리기가 제일 어렵다 (2026-09-11).
    raise RuntimeError(
        f"'{name}' 이름이 붙은 행이 {n}개입니다 (동명이인으로 보입니다) — "
        f"{number}번 학생을 확실히 고를 수 없어 넣지 않았습니다. 나이스에서 직접 넣어 주세요")


async def click_magam_cell(page: Page, name: str, number: str, col: int = 2):
    """셀 클릭. col은 td 0-based 인덱스.
    CSS nth-child 오프셋 = col + 2 (그리드 구조상 앞에 숨은 열이 1개 있음)
    마감=2→nth-child(4), 조회=3→nth-child(5), 1교시=4→nth-child(6), 4교시=7→nth-child(9) ...
    """
    row = await pick_student_row(page, name, number)
    await row.scroll_into_view_if_needed()
    await page.wait_for_timeout(150)

    nth = col + 2
    cell = row.locator(f"> div:nth-child({nth})").locator(".cl-text")
    if await cell.count() == 0:
        cell = row.locator(f"> div:nth-child({nth})")
    await cell.click(timeout=5000)


async def handle_magam_popup(page: Page, gubun: str, jongryu: str, memo: str = ""):
    """팝업 열리면 구분/종류 선택 (+ memo 가 있으면 사유) 후 적용."""
    await page.wait_for_selector("text=출결마감구분", timeout=10_000)
    await page.wait_for_timeout(200)

    # 구분 선택
    await page.get_by_text(gubun, exact=True).first.click()
    await page.wait_for_timeout(200)

    # 종류 선택 (radio)
    await page.get_by_role("radio", name=jongryu).click()
    await page.wait_for_timeout(200)

    # 사유 — 캘린더 방식은 memo 가 없어 예전처럼 비워 둔다. 출결 앱 파일은 상세 사유를 넘긴다.
    # ⚠️ 화면에는 aria-label="사유" 칸이 둘 보인다 (2026-10-14 실측) — 이 팝업 안으로 좁힌다.
    if memo:
        dialog = page.locator(".cl-dialog-wrapper:visible").filter(has_text="출결마감구분").last
        sayu = dialog.locator('input[aria-label="사유"]')
        await sayu.click(timeout=5000)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await sayu.press_sequentially(memo, delay=30)
        await page.wait_for_timeout(200)

    # 적용
    await page.locator("a").filter(has_text=re.compile(r"^적용$")).click()
    await page.wait_for_timeout(400)


# 학생 한 명의 결과 갈래
FAILED  = "failed"   # ❌ 아예 안 들어감 — 나이스에서 직접 넣어야 한다
PARTIAL = "partial"  # 구분·종류는 넣었는데 교시 칸에서 실패 — enter_day 가 그날을 다시 불러 처리한다
UNSURE  = "unsure"   # ❓ 들어갔는지 «확인할 수 없다» — 나이스에서 눈으로 봐야 한다

# 저장 결과 (save_page 가 돌려준다)
SAVE_OK     = "saved"    # 나이스가 결과창으로 답한 것을 확인했다
SAVE_UNSURE = "unsure"   # 그렇게 답하지 않았다 — 안 들어갔을 수도 있다


async def enter_student(page: Page, task: dict, day_cols=None) -> tuple[str, str] | None:
    """한 학생의 출결을 입력한다.

    돌려주는 값: 다 들어갔으면 None, 아니면 (갈래, 이유).
      (FAILED,  이유) — 한 칸도 안 들어갔다
      (PARTIAL, 이유) — 구분/종류는 화면에 넣었는데 그 뒤(교시 칸)에서 실패했다
    예전에는 print 만 하고 삼켜서, 한 명이 빠져도 루프는 그대로 돌고
    마지막에 '모든 출결 입력 완료'가 찍혔다 — 누가 빠졌는지 알 길이 없었다.
    """
    name    = task["name"]
    number  = task["number"]
    gubun   = task["gubun"]
    jongryu = task["jongryu"]
    note    = task["note"]

    if task.get("conflict"):
        print(f"  ❌  {number}번 {name}: {task['conflict']} — 입력하지 않았습니다.")
        return (FAILED, task["conflict"])

    if jongryu in ("조퇴", "지각"):
        _g = parse_gyosi_num(note, jongryu)
        # 교시를 못 읽으면 아예 손대지 않는다.
        # ⚠️ 예전에는 구분·종류만 넣고 ⚠️(교시 없음) 로 넘어갔다. 그런데 나이스는 교시 없는
        #    조퇴·지각이 한 줄이라도 있으면 그날 저장을 «통째로» 받지 않는다 — 같은 날 다른
        #    학생까지 빠진다 (2026-09-08 실측: 두 명 넣고 저장 → 두 줄 다 빈칸 /
        #    교시 없는 지각을 빼고 한 명만 저장 → 「저장했습니다.」).
        if _g is None:
            print(f"  ❌  {number}번 {name}: note '{note}' 에서 교시를 못 읽어 입력하지 않았습니다 "
                  f"— 넣으면 그날 저장이 통째로 막힙니다.")
            print(f"      캘린더 제목 끝 괄호에 교시를 적은 뒤 다시 돌려주세요 (예: (4교시~)).")
            return (FAILED, f"{jongryu}인데 note '{note}' 에서 교시를 못 읽음 "
                            f"(넣으면 그날 저장이 통째로 막혀서 넣지 않음)")
    # 없는 교시를 요구하면 아예 손대지 않는다.
    # 그날 3교시까지인데 '5교시~'를 넣으라고 하면, 예전 코드는 위치를 고정 계산해
    # '사유' 칸이나 '종례' 칸을 눌러 엉뚱한 값을 만들었다 (오류도 안 났다).
    if jongryu in ("조퇴", "지각") and day_cols:
        if gyosi_col_index(_g, day_cols) is None:
            last = day_last_period(day_cols)
            print(f"  ⛔  {number}번 {name}: 이 날은 {last}교시까지입니다 "
                  f"— note '{note}' 의 {_g}교시는 없는 교시라 입력하지 않았습니다.")
            print(f"      캘린더 제목을 그날 시간표에 맞게 고친 뒤 다시 돌려주세요.")
            return (FAILED, f"이 날은 {last}교시까지인데 note '{note}' 는 {_g}교시를 가리킴")

    applied = False   # 구분·종류가 «들어간» 뒤부터 True — 여기서 터지면 ❌ 가 아니라 ⚠️ 다

    try:
        # 1단계: 항상 마감(col=2) 셀 클릭 → 구분/종류 팝업 → 적용
        await click_magam_cell(page, name, number, col=2)
        await handle_magam_popup(page, gubun, jongryu, task.get("memo", ""))
        applied = True   # 여기까지 왔으면 구분·종류는 화면에 들어갔다

        if jongryu in ("조퇴", "지각"):
            # 2단계: 교시 셀 클릭 (마감 팝업 적용 후 수정된 행에서 해당 교시 셀 클릭)
            # 교시를 못 읽는 경우는 위에서 이미 걸러냈다 — 여기서 gyosi_num 은 None 이 아니다.
            gyosi_num = parse_gyosi_num(note, jongryu)
            col = gyosi_col_index(gyosi_num, day_cols)
            row = await pick_student_row(page, name, number)
            nth = col + 2
            cell = row.locator(f"> div:nth-child({nth})").locator(".cl-text")
            if await cell.count() == 0:
                cell = row.locator(f"> div:nth-child({nth})")
            await cell.click(timeout=5000)
            await page.wait_for_timeout(300)
            # 교시 셀 클릭 후 팝업이 다시 열리면 그대로 적용
            apply_btn = page.locator("a").filter(has_text=re.compile(r"^적용$"))
            if await apply_btn.count() > 0:
                await apply_btn.click()
                await page.wait_for_timeout(300)
            gyosi_label = " 조회" if gyosi_num == 0 else f" {gyosi_num}교시"
            print(f"  ✅  {number}번 {name}: {gubun}/{jongryu}{gyosi_label}")
        else:
            print(f"  ✅  {number}번 {name}: {gubun}/{jongryu}")
    except Exception as e:
        if applied:
            # 구분·종류까지는 들어갔다. «한 칸도 안 들어갔다» 고 말하면 그게 거짓말이다.
            print(f"  ⚠️  {number}번 {name}: 구분·종류는 넣었는데 그 뒤에서 실패 — {e}")
            return (PARTIAL, f"구분·종류는 넣었으나 그 뒤에서 실패 — {type(e).__name__}: {e}")
        print(f"  ❌  {number}번 {name} 실패: {e}")
        return (FAILED, f"{type(e).__name__}: {e}")

    return None


MAX_PARTIAL_TRIES = 2   # 교시 칸에서 실패한 학생을 몇 번까지 다시 넣어 볼지


def _failure(d: date, kind: str, task: dict, reason: str) -> dict:
    return {"date": d, "kind": kind, "number": task["number"], "name": task["name"],
            "label": task.get("label", ""), "reason": reason}


async def _row_text(page: Page, task: dict):
    """그 학생 줄에 지금 보이는 글자. 줄을 확실히 못 고르면 None."""
    try:
        row = await pick_student_row(page, task["name"], task["number"])
        await row.scroll_into_view_if_needed()
        return " | ".join(ln.strip() for ln in (await row.inner_text()).split("\n"))
    except Exception:
        return None


async def _row_restored(page: Page, task: dict, before, timeout_ms: int = 4000) -> bool:
    """다시 조회한 뒤 그 학생 줄이 넣기 전 글자(before)로 돌아왔는지."""
    if before is None:
        return False
    for _ in range(max(1, timeout_ms // 250)):
        if await _row_text(page, task) == before:
            return True
        await page.wait_for_timeout(250)
    return False


async def enter_day(page: Page, d: date, tasks: list[dict], day_cols,
                    prepare=None) -> tuple[list[dict], bool]:
    """그날 학생들을 넣는다. 돌려주는 값: (실패 목록, 저장해도 되는지).

    교시 칸에서 실패한 학생(PARTIAL)이 생기면 화면에 «교시 없는 줄» 이 남는다. 나이스는
    그런 줄이 하나라도 있으면 그날 저장을 통째로 받지 않는다 (2026-09-08 실측).
    → 같은 날을 다시 조회해 화면 입력을 버리고 처음부터 다시 넣는다. 저장 안 한 입력은
      조회 한 번에 사라지고 묻는 창도 없다 (2026-09-11 실측: 첫 학생 줄에 질병결석을
      화면에만 넣고 조회 → 그 줄이 넣기 전과 똑같이 돌아옴).
      같은 학생이 MAX_PARTIAL_TRIES 번 실패하면 그 학생만 빼고(❌) 나머지를 넣는다.
    ⚠️ 조회가 «됐다고 믿지» 않는다. 실패한 학생 줄이 넣기 전 글자로 돌아온 것을 읽어서
       확인한 뒤에만 다시 넣는다. 확인 못 하면 그날은 저장하지 않는다 (코덱스 검토 2026-09-11 —
       set_date_and_search 는 조회가 거절돼도 True 를 돌려줄 수 있다).

    prepare: 넣기 전에 매번(다시 조회한 뒤에도) 부르는 준비 단계 — 출결 앱 맞추기에서 틀린 줄을
    지운다(clear_rows). 실패 목록을 돌려주고, 하나라도 있으면 그날은 저장하지 않는다.
    """
    tries: dict[tuple, int] = {}
    last_reason: dict[tuple, str] = {}
    before: dict[tuple, str | None] = {}   # 넣기 전 그 학생 줄 글자 — 지워졌는지 대조용
    while True:
        failures, dirty = [], None
        # 넣기 전 글자는 «지우기 전» 에 읽어 둔다 — 다시 조회하면 지운 것까지 원래대로 돌아오기 때문
        for task in tasks:
            key = (task["number"], task["name"])
            if key not in before:
                before[key] = await _row_text(page, task)
        if prepare is not None:
            problems = await prepare()
            if problems:
                return problems + [_failure(d, FAILED, t, "그날 틀린 줄을 지우지 못해 손대지 않았습니다")
                                   for t in tasks], False
        for task in tasks:
            key = (task["number"], task["name"])
            if tries.get(key, 0) >= MAX_PARTIAL_TRIES:
                failures.append(_failure(d, FAILED, task,
                    f"교시 칸에서 {MAX_PARTIAL_TRIES}번 실패해 이 학생만 빼고 저장했습니다 — {last_reason[key]}"))
                continue
            result = await enter_student(page, task, day_cols)
            if result is None:
                continue
            kind, reason = result
            if kind == PARTIAL:
                tries[key] = tries.get(key, 0) + 1
                last_reason[key] = reason
                dirty = task
                break   # 어차피 처음부터 다시 넣는다
            failures.append(_failure(d, kind, task, reason))
        if dirty is None:
            return failures, True
        print(f"  🔄  교시 없는 줄이 남았습니다 — {d.strftime('%m/%d')} 화면 입력을 버리고 다시 넣습니다")
        dirty_before = before.get((dirty["number"], dirty["name"]))
        if not (await set_date_and_search(page, d) and await _row_restored(page, dirty, dirty_before)):
            print(f"  ⛔  {d.strftime('%m/%d')} 교시 없는 줄이 지워졌는지 확인하지 못해 그날은 저장하지 않습니다")
            return [_failure(d, FAILED, t, "교시 없는 줄을 지우려고 다시 조회했는데, 그 줄이 넣기 전으로 "
                                           "돌아온 것을 확인하지 못해 그날은 저장하지 않았습니다")
                    for t in tasks], False
        day_cols = await read_day_columns(page)


def report_failures(problems: list[dict]) -> int:
    """못 들어간 학생을 갈래별로 찍고 종료코드를 돌려준다 (하나라도 있으면 1)."""
    if not problems:
        print("\n🎉  모든 출결 입력 완료!")
        return 0

    failed  = [p for p in problems if p["kind"] == FAILED]
    unsure  = [p for p in problems if p["kind"] == UNSURE]

    def people(items):   # 같은 학생이 그날 두 건(지각+조퇴)이면 한 명으로 센다
        return len({(p["number"], p["name"]) for p in items})

    def block(items, mark, title, tail):
        print("\n" + "!" * 60)
        print(f"{mark}  {people(items)}명 — {title}")
        print("!" * 60)
        for p in items:
            label = f" [{p['label']}]" if p.get("label") else ""
            print(f"  {mark}  {p['date'].strftime('%m/%d')}  {p['number']}번 {p['name']}{label}")
            print(f"        이유: {p['reason']}")
        print("!" * 60)
        print(f"  ※  {tail}")
        print("!" * 60)

    if failed:
        block(failed, "❌", "안 들어감 (나이스에서 직접 넣어야 함)",
              "이 학생들은 저장된 내용에 없습니다. 나이스에서 직접 입력해 주세요.")
    if unsure:
        block(unsure, "❓", "들어갔는지 확인하지 못함 (나이스에서 눈으로 확인)",
              "나이스가 「저장했습니다」라고 답하지 않았습니다. "
              "이미 같은 내용이 있어서일 수도, 입력이 안 된 것일 수도 있습니다.")

    print(f"\n   정리: ❌ 안 들어감 {people(failed)}명 / ❓ 확인 필요 {people(unsure)}명")
    return 1


# 아직 결과가 아닌 창 — 이 글자가 있으면 결과창으로 치지 않고 더 기다린다.
NOT_RESULT_YET = ("하시겠습니까", "중입니다")

# 성공 문구.
#   「저장했습니다」   — 일일출결관리 실측 (2026-09-11 저장 직후 창 글자 「알림 / 저장했습니다. / 확인」)
#   「저장되었습니다」 — 다른 화면(자유학기활동관리) 스크립트가 성공으로 보던 문구.
#                        이 화면에서 본 적은 없지만 뜻이 같아 같이 받는다
SUCCESS_TEXTS = ("저장했습니다", "저장되었습니다")

# 성공 문구에 섞일 리 없는 말들. 하나라도 있으면 성공으로 세지 않는다.
# ⚠️ «취소» 한 글자는 넣으면 안 된다 — 창 본문에 버튼 글자(확인·취소)가 같이
#    딸려 와서 정상 저장까지 실패로 뒤집는다. 그래서 «취소되» 처럼 문장 꼴로만 잡는다.
SAVE_BAD_WORDS = ("없습니다", "실패", "오류", "않았", "불가", "취소되", "하시겠습니까", "중입니다")


async def _wait_result_dialog(page: Page, timeout_ms: int = 8000):
    """«저장하시겠습니까?» 다음에 뜨는 결과창을 기다린다.
    돌려주는 값: (창 locator, 창 글자). 못 보면 (None, "").

    ⚠️ 예전에는 `wait_for_selector("text=알림")` 으로 기다렸다. 출결관리 화면에는
       «숨은» 「민원현황 알림」 메뉴 글자가 맨 앞에 있고, wait_for_selector 는 첫 번째로
       걸린 요소 하나만 보고 «안 보인다» 며 끝까지 기다린다. 결과창이 떠 있어도 6초 뒤
       시간 초과였고, 실제로 저장된 날도 「저장 결과창을 못 봤습니다」가 났다
       (2026-09-11 실측: 창 없는 화면에서 'text=알림' 매치 2개 · #0 숨은 「민원현황 알림」 ·
        #1 이 보이는데도 wait_for_selector 시간 초과).
    → 글자 대신 «보이는 대화상자» 를 기다리고, 아직 묻는 창·처리 중 창은 건너뛴다.
    """
    return await wait_dialog(page, lambda raw: not any(w in raw for w in NOT_RESULT_YET), timeout_ms)


def _judge_save(body: str, told: bool) -> tuple[str, str]:
    """결과창 글자로 저장됐는지 판정한다."""
    if not body:
        return (SAVE_UNSURE, "저장 결과창을 못 봤습니다 — 저장됐는지 확인할 수 없습니다")
    if "변경된 내용이 없습니다" in body:
        print("  ⚠️  나이스: 「변경된 내용이 없습니다」")
        return (SAVE_UNSURE,
                "나이스가 「변경된 내용이 없습니다」라고 답했습니다 — "
                "이미 같은 내용이 들어 있거나, 입력이 반영되지 않았습니다")
    # 아는 성공 문구일 때만 성공이다.
    # ⚠️ 예전에는 「저장」 이 들어 있고 금지어만 없으면 성공으로 봤다. 그러면 「저장하지
    #    못했습니다」 가 성공이 된다 (코덱스 검토 2026-09-11). 모르는 문구는 ❓ 로 올리고
    #    글자를 남긴다 — 나이스가 문구를 바꿨다면 그 글자로 SUCCESS_TEXTS 를 고치면 된다.
    if any(w in body for w in SAVE_BAD_WORDS) or not any(s in body for s in SUCCESS_TEXTS):
        print(f"  ⚠️  저장 결과창이 예상과 다릅니다: {body[:80]}")
        return (SAVE_UNSURE, f"저장 결과창: 「{body[:120]}」")
    print("  💾  저장 완료!")
    if not told:
        print("  ⚠️  결과창의 [확인] 을 못 눌렀습니다 — 창이 남아 있으면 다음 날짜 입력이 막힐 수 있습니다")
    return (SAVE_OK, "")


async def save_page(page: Page) -> tuple[str, str]:
    """그날 입력분을 저장한다. 돌려주는 값: (결과, 이유).

    (SAVE_OK,     "")    나이스가 «결과창» 으로 답한 것을 확인했다
    (SAVE_UNSURE, 이유)  그렇게 답하지 않았다 — 사람이 나이스를 봐야 한다

    ⚠️ 예전에는 확인 팝업 두 개를 각각 `except: pass` 로 삼키고 아무것도 돌려주지
       않았다. 그래서 «저장이 안 됐는데 🎉 가 뜨고 종료코드 0» 이 될 수 있었다.
    ⚠️ 그 뒤에도 «저장하시겠습니까?» 를 «누른 것» 만으로 성공으로 셌다. 누른 것과
       저장된 것은 다르다 — 결과창을 못 봤으면 모르는 것이다 (2026-09-11 수정).
    """
    # 직전 단계에서 알림 팝업·사이드바 오버레이가 남아 있으면 저장 클릭을 막으므로 먼저 정리
    await dismiss_alert_popup(page, timeout=1000)
    overlay = page.locator(".cl-overlay")
    if await overlay.count() > 0:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

    # 저장 버튼 클릭 (<button> 역할, <a>가 아님 — codegen에서 확인)
    await page.get_by_role("button", name="저장").click()
    await page.wait_for_timeout(600)

    # 1차 팝업: "저장하시겠습니까?" → 확인
    # 결과창과 같은 이유로 글자(text=…)가 아니라 «보이는 창» 을 기다리고, 그 창 안의 [확인] 을 누른다.
    asked_box, _ = await wait_dialog(page, lambda raw: "저장하시겠습니까" in raw, 5000)
    if asked_box is not None and await click_dialog_confirm(asked_box):
        print("  ✅  저장 확인")
    await page.wait_for_timeout(600)

    # 2차 팝업: 나이스가 «결과» 를 알려주는 창.
    # ⚠️ 창이 떴다는 것만으로 성공으로 세면 «오류 알림» 까지 성공이 된다.
    #    판정 근거는 창이 뜬 사실이 아니라 창의 «본문 글자» 다 (2026-09-11).
    dialog, body = await _wait_result_dialog(page)
    told = False
    if dialog is not None:
        told = await click_dialog_confirm(dialog)
    await page.wait_for_timeout(600)
    return _judge_save(body, told)


async def _day_matches(page: Page, tasks: list[dict]) -> bool:
    """그날 화면에서 각 학생 행의 «마감» 칸(데이터 행 index 3 — verify_attendance.IDX 와 같다)이
    넣으려던 구분·종류를 담고 있는지. 한 명이라도 못 찾거나 다르면 False."""
    cells = await page.evaluate(r"""() => {
      const o = {};
      for (const r of document.querySelectorAll('.cl-grid-row')) {
        const k = r.children;
        if (k.length > 3) o[(k[0].innerText || '').trim()] = (k[3].innerText || '').trim();
      }
      return o;
    }""")
    for t in tasks:
        got = cells.get(str(t["number"]), "")
        if t["gubun"] not in got or t["jongryu"] not in got:
            return False
    return True


# 출결마감 결과창 성공 문구.
#   묻는 창 「확인 / 일출결마감을 하시겠습니까?」 → 결과창 「알림 / 마감했습니다.」 (2026-10-14 실측)
CLOSE_SUCCESS = ("마감했습니다", "마감되었습니다")


async def close_day(page: Page) -> tuple[bool, str]:
    """저장이 끝난 그날을 [출결마감] 한다 (--마감 일 때만). 돌려주는 값: (마감 확인됨?, 이유).

    ⚠️ 마감은 그날 반 전체를 확정한다 — 저장이 확실하고 실패자가 없는 날에만 부른다.
    묻는 창이 «마감 … 하시겠습니까» 가 아니면 [확인] 을 누르지 않고 멈춘다.
    """
    await dismiss_alert_popup(page, timeout=1000)
    btn = page.get_by_role("button", name="출결마감").filter(visible=True)
    if await btn.count() == 0:
        return (False, "[출결마감] 버튼이 안 보입니다")
    await btn.first.click(timeout=5000)
    await page.wait_for_timeout(600)

    asked, ask_text = await wait_dialog(page, lambda raw: True, 5000)
    if asked is None:
        return (False, "[출결마감] 을 눌렀는데 아무 창도 뜨지 않았습니다")
    if not ("마감" in ask_text and "하시겠습니까" in ask_text):
        # 묻는 창이 아니라 바로 결과·경고가 뜬 경우 — 글자만 남기고 판단은 사람에게
        return (False, f"예상과 다른 창이 떠서 누르지 않았습니다: 「{ask_text[:150]}」")
    print(f"  ❔  나이스: 「{ask_text[:80]}」 → 확인")
    if not await click_dialog_confirm(asked):
        return (False, "마감 확인창의 [확인] 을 못 눌렀습니다")
    await page.wait_for_timeout(800)

    dialog, body = await wait_dialog(page, lambda raw: not any(w in raw for w in NOT_RESULT_YET), 8000)
    if dialog is None:
        return (False, "마감 결과창을 못 봤습니다 — 마감됐는지 나이스에서 확인해 주세요")
    print(f"  ❔  나이스: 「{body[:80]}」")
    if any(w in body for w in SAVE_BAD_WORDS) or not any(s in body for s in CLOSE_SUCCESS):
        return (False, f"마감 결과창이 예상과 다릅니다 (누르지 않음): 「{body[:150]}」")
    await click_dialog_confirm(dialog)
    await page.wait_for_timeout(600)
    print("  🔒  출결마감 완료!")
    return (True, "")


# ============================================================
# 출결 앱 파일에 맞추기 — 파일이 기준, 나이스에서 다른 줄은 지우고 다시 넣는다
# ============================================================
# app_to_neis.py 가 «파일이 책임지는 날짜» 를 넣어 준다. None 이면 예전처럼 넣기만 한다
# (구글 캘린더 방식은 맞추지 않는다).
SYNC_DAYS: set[date] | None = None


def _same_as_file(task: dict, cells: dict) -> bool:
    """나이스 그 학생 줄(cells)이 파일의 출결(task) 한 건과 같은지."""
    if cells.get("마감", "") != f"{task['gubun']}{task['jongryu']}":
        return False
    if task["jongryu"] not in ("조퇴", "지각"):
        return True   # 결석은 분류만 본다
    import verify_attendance as V
    marks = {k: v for k, v in cells.items() if k not in ("번호", "성명") and v}
    return V._check_periods(task["jongryu"], task["note"], marks) is None


def plan_sync(tasks: list[dict], grid: dict) -> tuple[list[dict], list[dict]]:
    """파일(tasks)과 나이스 화면(grid: 번호 → {컬럼: 값})을 견줘 (지울 줄, 넣을 출결) 을 돌려준다.

    - 나이스에만 있는 출결            → 지운다
    - 분류·교시가 다른 학생          → 지우고 파일대로 다시 넣는다
    - 나이스에 없는 학생             → 넣는다
    - 같은 학생                       → 건드리지 않는다
    같은 학생이 그날 두 건(지각+조퇴)이면 화면 한 칸으로 견줄 수 없어 늘 지우고 다시 넣는다.
    사유 칸은 견주지 않는다 (화면 표시가 파일 글자와 같은지 실측하지 않았다).
    """
    by_num: dict[str, list[dict]] = {}
    for t in tasks:
        by_num.setdefault(str(t["number"]), []).append(t)
    to_clear, to_enter = [], []
    for num, cells in grid.items():
        got = cells.get("마감", "")
        if not got:
            if num in by_num:
                to_enter += by_num[num]
            continue
        if num in by_num:
            ts = by_num[num]
            if len(ts) == 1 and _same_as_file(ts[0], cells):
                continue
            to_enter += ts
        to_clear.append({"number": num, "name": cells.get("성명", ""), "label": "지우기", "was": got})
    # 화면에서 못 읽은 학생(가상 스크롤 등)은 넣는다 — 예전 방식과 같다
    to_enter += [t for num, ts in by_num.items() if num not in grid for t in ts]
    return to_clear, to_enter


async def day_is_closed(page: Page) -> bool:
    """그날이 이미 [출결마감] 됐는지. [마감취소] 는 마감된 날에만 눌린다 (마감 전엔 aria-disabled=true, 2026-10-15 실측)."""
    btn = page.get_by_role("button", name="마감취소").filter(visible=True)
    if await btn.count() == 0:
        return False
    return (await btn.first.get_attribute("aria-disabled")) != "true"


async def reopen_day(page: Page, d: date) -> tuple[bool, str]:
    """마감된 그날을 [마감취소] 한다 (파일과 달라 고쳐야 할 때만). 돌려주는 값: (풀림 확인됨?, 이유).

    ⚠️ [마감취소] 는 묻지 않고 바로 취소한다 — 뜨는 창은 「알림 / 마감취소 되었습니다. / 확인」 뿐 (2026-10-21 실측).
       그래도 결과창 «글자» 만 믿지 않고
       [마감취소] 버튼이 다시 잠겼는지(aria-disabled=true) — 즉 «마감 안 된 날» 로 바뀐 것을 읽어서 판단한다.
       묻는 창이 «취소 … 하시겠습니까» 가 아니면 [확인] 을 누르지 않고 멈춘다 (close_day 와 같은 원칙).
    월마감이 돼 있으면 일마감취소가 막힐 수 있다 — 그때는 결과창 글자를 그대로 올리고 멈춘다.
    """
    await dismiss_alert_popup(page, timeout=1000)
    btn = page.get_by_role("button", name="마감취소").filter(visible=True)
    if await btn.count() == 0:
        return (False, "[마감취소] 버튼이 안 보입니다")
    await btn.first.click(timeout=5000)
    await page.wait_for_timeout(600)

    asked, ask_text = await wait_dialog(page, lambda raw: True, 5000)
    if asked is None:
        return (False, "[마감취소] 를 눌렀는데 아무 창도 뜨지 않았습니다")
    if "마감취소" in ask_text and ("되었습니다" in ask_text or "했습니다" in ask_text):
        # 묻지 않고 바로 취소된다 — 「알림 / 마감취소 되었습니다. / 확인」 (2026-10-21 실측)
        dialog, body = asked, ask_text
    elif "취소" in ask_text and "하시겠습니까" in ask_text:
        print(f"  ❔  나이스: 「{ask_text[:80]}」 → 확인")
        if not await click_dialog_confirm(asked):
            return (False, "마감취소 확인창의 [확인] 을 못 눌렀습니다")
        await page.wait_for_timeout(800)
        dialog, body = await wait_dialog(page, lambda raw: not any(w in raw for w in NOT_RESULT_YET), 8000)
    else:
        return (False, f"예상과 다른 창이 떠서 누르지 않았습니다: 「{ask_text[:150]}」")

    if dialog is not None:
        print(f"  ❔  나이스: 「{body[:80]}」")
        if any(w in body for w in ("없습니다", "실패", "오류", "불가", "할 수 없")):
            return (False, f"마감취소가 안 됐습니다 (월마감이 돼 있으면 월마감부터 풀어야 합니다): 「{body[:150]}」")
        await click_dialog_confirm(dialog)
        await page.wait_for_timeout(600)

    # 화면을 다시 불러 «마감 안 된 날» 로 바뀌었는지 읽는다
    if not await set_date_and_search(page, d):
        return (False, "마감취소 뒤 그날을 다시 불러오지 못했습니다 — 풀렸는지 나이스에서 확인해 주세요")
    if await day_is_closed(page):
        return (False, "마감취소를 눌렀는데 아직 마감된 날로 보입니다 — 나이스에서 확인해 주세요")
    print("  🔓  마감취소 완료 — 고친 뒤 다시 마감합니다")
    return (True, "")


CHECKED_JS = r"""e => {
  const c = e.closest('[role=checkbox]') || e.querySelector('[role=checkbox]') || e;
  const a = c.getAttribute('aria-checked');
  if (a !== null) return a === 'true';
  const i = c.tagName === 'INPUT' ? c : c.querySelector('input[type=checkbox]');
  return i ? i.checked : null;
}"""


async def _clean_box(page: Page):
    """그리드 왼쪽 위 「깨끗이」 체크박스. 켜고 학생 마감 칸을 누르면 그 줄이 지워진다 (나이스출결 스킬 실측)."""
    for loc in (page.get_by_role("checkbox", name=re.compile("깨끗이")),
                page.get_by_text("깨끗이", exact=True)):
        loc = loc.filter(visible=True)
        if await loc.count() > 0:
            return loc.first
    return None


async def _set_clean_box(page: Page, box, on: bool) -> bool:
    """「깨끗이」 를 켜거나 끈다. 바뀐 것을 읽어서 확인했을 때만 True."""
    if await box.evaluate(CHECKED_JS) == on:
        return True
    await box.click(timeout=5000)
    await page.wait_for_timeout(300)
    return await box.evaluate(CHECKED_JS) == on


async def _magam_cell_text(page: Page, number: str) -> str | None:
    """그 번호 줄의 마감 칸 글자 (데이터 행 index 3). 줄이 화면에 없으면 None."""
    cells = await page.evaluate(r"""() => {
      const o = {};
      for (const r of document.querySelectorAll('.cl-grid-row')) {
        const k = r.children;
        if (k.length > 3) o[(k[0].innerText || '').trim()] = (k[3].innerText || '').trim();
      }
      return o;
    }""")
    return cells.get(str(number))


async def clear_rows(page: Page, d: date, targets: list[dict]) -> list[dict]:
    """targets 학생 줄을 화면에서 지운다 (저장은 하지 않는다). 돌려주는 값: 실패 목록 (비면 다 지움).

    ⚠️ 「깨끗이」 가 켜진 채로 두면 그 뒤 «넣기» 클릭이 전부 «지우기» 가 된다. 끝에서 반드시 끄고,
       끈 것을 확인하지 못하면 전체 작업을 멈춘다 (RuntimeError).
    """
    box = await _clean_box(page)
    if box is None:
        return [_failure(d, FAILED, t, "「깨끗이」 체크박스를 찾지 못해 지우지 못했습니다") for t in targets]
    if await box.evaluate(CHECKED_JS) is None:
        return [_failure(d, FAILED, t, "「깨끗이」 가 켜졌는지 읽을 수 없어 지우지 않았습니다") for t in targets]
    problems = []
    try:
        if not await _set_clean_box(page, box, True):
            return [_failure(d, FAILED, t, "「깨끗이」 를 켜지 못했습니다") for t in targets]
        for t in targets:
            try:
                await click_magam_cell(page, t["name"], t["number"], col=2)
                await page.wait_for_timeout(400)
                if await page.locator(".cl-dialog-wrapper:visible").filter(has_text="출결마감구분").count():
                    problems.append(_failure(d, FAILED, t, "지우려고 눌렀는데 입력 창이 떴습니다 — 지우지 못했습니다"))
                    break   # 창이 뜬 채로 더 누르지 않는다. 그날은 저장하지 않으니 다시 조회하면 사라진다
                left = await _magam_cell_text(page, t["number"])
                if left:
                    problems.append(_failure(d, FAILED, t, f"지웠는데 마감 칸에 아직 '{left}' 가 남아 있습니다"))
                else:
                    print(f"  🧹  {t['number']}번 {t['name']}: '{t['was']}' 지움")
            except Exception as e:
                problems.append(_failure(d, FAILED, t, f"지우다 실패 — {type(e).__name__}: {e}"))
    finally:
        if not await _set_clean_box(page, box, False):
            raise RuntimeError("「깨끗이」 체크박스를 끄지 못했습니다 — 켜진 채로 두면 다음 입력이 전부 지우기가 "
                               "되므로 여기서 멈춥니다. 나이스 화면에서 「깨끗이」 를 직접 꺼 주세요")
    return problems


# ============================================================
# 메인
# ============================================================
async def main():
    args = sys.argv[1:]

    print("=" * 52)
    print("🏫  나이스 출결 자동 입력기 v4.0")
    print("=" * 52)

    try:
        load_teacher_settings()
    except teacher_config.ConfigMissingError as e:
        print(f"\n❌  {e}")
        sys.exit(1)
    # 학년·반은 선택값이다 — 담임이 아니면 비어 둔다.
    who = f"{GRADE}학년 {CLASS}반 담임 " if GRADE and CLASS else ""
    print(f"👤  {who}({CERT_NAME})")

    # --future: 오늘 이후 날짜도 입력 (예정된 결석을 미리 넣을 때)
    allow_future = "--future" in args
    # --dry-run: 무엇을 넣을지 목록만 보여주고 나이스는 열지 않는다
    dry_run = "--dry-run" in args
    # --마감: 저장이 확실하고 실패자가 없는 날만 [출결마감] 까지 누른다 (선생님이 말할 때만)
    do_close = "--마감" in args
    # --마감풀어둠: 고치려고 [마감취소] 한 날을 다시 마감하지 않는다 (나중에 다시 넣고 마감할 때)
    keep_open = "--마감풀어둠" in args
    date_arg = next((a for a in args if re.match(r"\d{4}-\d{2}-\d{2}", a)), None)
    if date_arg:
        try:
            start_date = date.fromisoformat(date_arg)
        except ValueError:
            print("❌  날짜 형식 오류. YYYY-MM-DD로 입력하세요.")
            sys.exit(1)
    else:
        today = date.today()
        start_date = date(today.year, today.month, 1)

    end_date = max(date.today(), start_date)
    print(f"\n📅  {start_date} ~ {end_date} 캘린더 읽는 중...")

    records  = load_from_calendar(start_date, end_date)
    task_map = build_task_map(records)
    sync = SYNC_DAYS is not None
    if sync:
        # 파일에 출결이 한 건도 없는 날도 연다 — 나이스에만 남은 출결을 지워야 하므로
        for d in SYNC_DAYS:
            if d.weekday() < 5:
                task_map.setdefault(d, [])
        task_map = dict(sorted(task_map.items()))
    total    = sum(len(v) for v in task_map.values())

    if not task_map:
        print("✅  해당 기간에 입력할 출결이 없습니다.")
        sys.exit(0)

    print(f"\n📊  {len(task_map)}개 날짜, 총 {total}건\n")
    for d, tasks in task_map.items():
        print(f"  {d.strftime('%m/%d')} — "
              + (" / ".join(f"{t['number']}번 {t['name']}({t['label']})" for t in tasks)
                 or "(출결 없음)"))
    print()
    if sync:
        print("🔁  파일 기준으로 맞춥니다: 나이스에 이미 들어간 것이 파일과 다르면 지우고 파일대로 다시 넣고,")
        print("    파일에 없는 출결은 지웁니다. 마감된 날이 다르면 [마감취소] → 고치기 → 다시 [출결마감] 합니다.\n")

    if dry_run:
        print("🔍  --dry-run 이라 여기서 멈춥니다. 나이스는 열지 않았습니다.")
        print("    목록이 맞으면 --dry-run 을 빼고 다시 실행하세요.")
        return

    PROFILE_DIR = str(teacher_config.CONFIG_DIR / "chrome_profile")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            channel="chrome",
            headless=False,
            slow_mo=50,
            args=[
                "--profile-directory=Default",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=PrivateNetworkAccessForNavigations,PrivateNetworkAccessPermissionPrompt,PermissionChip",
                "--disable-web-security",
                "--allow-running-insecure-content",
                "--test-type",
                "--start-maximized",
                "--noerrdialogs",
            ],
            ignore_default_args=["--enable-automation", "--no-sandbox"],
            viewport=None,
        )
        await context.grant_permissions(["notifications"], origin=_origin_of(NEIS_URL))
        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        failures: list[dict] = []   # 못 들어간 학생 (날짜·갈래·번호·이름·이유)
        exit_code = 0
        closed: list[date] = []               # --마감 으로 마감한 날
        not_closed: list[tuple[date, str]] = []  # --마감 인데 마감하지 않은 날과 이유
        close_stopped = False                 # 예상과 다른 창이 뜨면 그 뒤로는 아무것도 하지 않는다

        try:
            await page.goto(NEIS_URL)
            await auto_login(page)
            await wait_for_attendance_page(page)

            holidays = load_holidays()
            if holidays:
                print(f"  📅  휴업일 {len(holidays)}개 로드: {sorted(d.strftime('%m/%d') for d in holidays)}")

            for d, tasks in task_map.items():
                if d < start_date:
                    print(f"  ⏭  {d.strftime('%m/%d')} 건너뜀 (시작일 이전)")
                    continue
                if d > date.today() and not allow_future:
                    print(f"  ⏭  {d.strftime('%m/%d')} 건너뜀 (미래 날짜 — --future 옵션으로 미리 입력 가능)")
                    continue
                if d in holidays:
                    print(f"  ⏭  {d.strftime('%m/%d')} 건너뜀 (휴업일)")
                    continue
                if d.weekday() >= 5:
                    print(f"  ⏭  {d.strftime('%m/%d')} 건너뜀 (주말)")
                    continue
                print(f"\n📅  {d.strftime('%Y년 %m월 %d일')} ({len(tasks)}명)...")
                await dismiss_alert_popup(page, timeout=1000)
                if not await set_date_and_search(page, d):
                    print(f"  ⛔  {d.strftime('%m/%d')} 건너뜀 — 날짜를 못 맞췄습니다")
                    for task in tasks:
                        failures.append(_failure(d, FAILED, task,
                                                 "화면 날짜가 그날로 바뀌지 않아 입력하지 않았습니다"))
                    continue
                day_cols = await read_day_columns(page)
                last_p = day_last_period(day_cols)
                if last_p:
                    print(f"    이 날 시간표: {last_p}교시까지")
                prepare, to_enter = None, tasks
                reopened = False   # 이 실행에서 [마감취소] 한 날 — 고친 뒤 다시 마감한다
                if sync:
                    import verify_attendance
                    grid = await verify_attendance.read_grid(page)
                    if not grid and not tasks:
                        # 10/05·10/09 실측: 「시간표를 확인 하세요」 알림 뒤 표가 0행 — 수업 없는 날로 보인다
                        print("  ⏭  나이스 표가 비어 있고 파일에도 출결이 없습니다 — 건너뜀 (시간표 없는 날?)")
                        continue
                    if not grid:
                        print("  ⚠️  나이스 표를 읽지 못했습니다 — 지우기 없이 파일 것만 넣습니다")
                    else:
                        to_clear, to_enter = plan_sync(tasks, grid)
                        if not to_clear and not to_enter:
                            print("  ✅  나이스가 이미 파일과 같습니다 — 고칠 것 없음")
                            if do_close and not await day_is_closed(page):
                                ok, why = await close_day(page)
                                if ok:
                                    closed.append(d)
                                else:
                                    not_closed.append((d, why))
                                    print(f"  ⛔  {d.strftime('%m/%d')} 마감 못 함 — {why}")
                                    close_stopped = True
                                    break
                            continue
                        changes = [*to_clear, *[t for t in to_enter
                                                if t["number"] not in {c["number"] for c in to_clear}]]
                        if await day_is_closed(page):
                            # 마감된 날은 칸이 안 눌린다 — [마감취소] 로 풀고 고친 뒤 다시 마감한다
                            print(f"  🔒  {d.strftime('%m/%d')} 은 마감된 날입니다 ({len(changes)}건 다름) — [마감취소] 하고 고칩니다")
                            ok, why = await reopen_day(page, d)
                            if not ok:
                                print(f"  ⛔  {d.strftime('%m/%d')} 마감취소 못 함 — {why}")
                                for t in changes:
                                    failures.append(_failure(d, FAILED, t, f"마감된 날인데 마감취소를 못 해 고치지 않았습니다 — {why}"))
                                if "예상과 다른 창" in why:
                                    print("      나머지 날짜는 손대지 않고 멈춥니다.")
                                    close_stopped = True
                                    break
                                continue
                            reopened = True
                            # 풀린 화면을 새로 읽는다
                            day_cols = await read_day_columns(page)
                            grid = await verify_attendance.read_grid(page)
                            if grid:
                                to_clear, to_enter = plan_sync(tasks, grid)
                        for c in to_clear:
                            again = " → 파일대로 다시 넣음" if any(t["number"] == c["number"] for t in to_enter) else ""
                            print(f"  ✏️   {c['number']}번 {c['name']}: 나이스 '{c['was']}' 지움{again}")
                        if to_clear:
                            prepare = lambda d=d, to_clear=to_clear: clear_rows(page, d, to_clear)
                day_failures, can_save = await enter_day(page, d, to_enter, day_cols, prepare)
                failures.extend(day_failures)
                if not can_save:
                    if reopened:
                        not_closed.append((d, "마감취소 했는데 고치지 못했습니다 — 마감이 풀린 채로 남아 있습니다"))
                    continue
                # 실패자가 있어도 저장은 그대로 한다 —
                # 여기서 막으면 이미 들어간 학생들까지 같이 날아간다.
                save_status, save_reason = await save_page(page)
                if save_status != SAVE_OK:
                    # 저장을 확인하지 못했으면 그날 «전원» 이 불확실하다.
                    # 이미 개별 사유로 잡힌 학생은 빼고 나머지를 통째로 올린다.
                    # 이름만으로 세면 동명이인이 명단에서 빠진다 — 번호까지 같이 본다.
                    # 건(분류) 단위로 센다 — 같은 학생의 지각은 넣고 조퇴만 ❌ 인 경우, 학생 단위로 세면
                    # 넣은 지각의 «저장 불확실» 이 명단에서 빠진다 (코덱스 검토 2026-09-11).
                    already = {(f["number"], f["name"], f["label"]) for f in failures if f["date"] == d}
                    print(f"  ❓  {d.strftime('%m/%d')} 저장 확인 실패 — {save_reason}")
                    for task in tasks:
                        if (task["number"], task["name"], task["label"]) in already:
                            continue
                        failures.append(_failure(d, UNSURE, task, save_reason))
                elif sync:
                    # 저장 뒤 표를 다시 읽어 파일과 같아졌는지 본다 (못 들어간 학생은 이미 ❌ 로 잡혀 있다)
                    import verify_attendance
                    grid = await verify_attendance.read_grid(page)
                    left_clear, left_enter = plan_sync(tasks, grid) if grid else ([], [])
                    already = {f["number"] for f in failures if f["date"] == d}
                    for t in [*left_clear, *left_enter]:
                        if t["number"] in already:
                            continue
                        already.add(t["number"])
                        failures.append(_failure(d, UNSURE, t, "저장 뒤 다시 읽어 보니 나이스가 아직 파일과 다릅니다"))
                    if grid and not (left_clear or left_enter):
                        print("  🔎  저장 뒤 다시 읽어 보니 파일과 같습니다")

                if reopened and keep_open and not do_close:
                    not_closed.append((d, "--마감풀어둠 — 마감취소 한 채로 둡니다"))
                elif do_close or reopened:
                    # 마감취소 한 날은 --마감 이 없어도 다시 마감한다 (원래 마감돼 있던 날이므로).
                    # 「변경된 내용이 없습니다」 = 이미 넣어 둔 날일 수 있다. 화면 마감 칸이 넣으려던 것과
                    # 전부 같으면 저장된 것으로 보고 마감한다. 하나라도 다르면 마감하지 않는다.
                    if (save_status != SAVE_OK and "변경된 내용이 없습니다" in save_reason
                            and await _day_matches(page, tasks)):
                        print("  ✅  화면의 마감 칸이 넣으려던 출결과 모두 같습니다 — 저장된 것으로 봅니다")
                        failures = [f for f in failures if not (f["date"] == d and f["kind"] == UNSURE)]
                        save_status = SAVE_OK
                    if save_status != SAVE_OK or any(f["date"] == d for f in failures):
                        left = " — 마감취소 했던 날이라 마감이 풀린 채로 남아 있습니다" if reopened else ""
                        not_closed.append((d, "저장이 확실하지 않거나 못 들어간 학생이 있어 마감하지 않음" + left))
                        continue
                    ok, why = await close_day(page)
                    if ok:
                        closed.append(d)
                    else:
                        not_closed.append((d, why))
                        print(f"  ⛔  {d.strftime('%m/%d')} 마감 못 함 — {why}")
                        print("      나머지 날짜는 손대지 않고 멈춥니다.")
                        close_stopped = True
                        break

            exit_code = report_failures(failures)
            if do_close or closed or not_closed:   # 마감취소 하고 다시 마감한 날도 여기서 알린다
                print(f"\n🔒  출결마감: {len(closed)}일 마감 "
                      + (f"({', '.join(x.strftime('%m/%d') for x in closed)})" if closed else ""))
                for x, why in not_closed:
                    print(f"  ⏸  {x.strftime('%m/%d')} 마감 안 함 — {why}")
                if not_closed:
                    exit_code = 1
            if close_stopped:
                print("\n⚠️  예상과 다른 창이 떠 있을 수 있습니다. 브라우저를 10분 동안 열어 둡니다 — 화면을 확인해 주세요.")
                try:
                    await page.wait_for_timeout(10 * 60_000)
                except Exception:
                    pass   # 선생님이 창을 닫았다
        except Exception as e:
            import traceback
            print(f"\n❌  오류 발생: {e}")
            traceback.print_exc()
            if failures:
                report_failures(failures)
            exit_code = 1
            print("\n⚠️  브라우저는 열려 있습니다. 확인 후 Enter를 누르세요.")
            try:
                input("브라우저 닫으려면 Enter...")
            except EOFError:
                pass
        finally:
            await context.close()

        return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
