# -*- coding: utf-8 -*-
"""나이스 출결 검증 (읽기 전용).

캘린더 기준 기대값 vs 나이스 화면 실제값을 날짜별로 비교한다.
아무것도 입력·저장하지 않는다.

사용: python verify_attendance.py 2026-07-01 2026-07-31
"""
import asyncio, sys, json, re
from datetime import date, timedelta
from playwright.async_api import async_playwright
import main as M
import teacher_config

# 데이터 행의 children 인덱스 (헤더 12칸과 달리 데이터 행은 index 2에 빈 칸이 하나 더 있다 — 실측)
IDX = {"번호":0, "성명":1, "마감":3, "조회":4, "1교시":5, "2교시":6, "3교시":7,
       "4교시":8, "5교시":9, "6교시":10, "종례":11, "사유":12}
COLS = list(IDX)

JS_READ = """
() => {
  const out = [];
  document.querySelectorAll('.cl-grid-row').forEach(r => {
    const kids = Array.from(r.children).map(d => (d.innerText || '').trim());
    if (kids.length) out.push(kids);
  });
  return out;
}
"""

JS_HEADER = r"""
() => {
  const heads = [];
  document.querySelectorAll('[class*=grid-head], [class*=grid-column-head], .cl-grid-header').forEach(h => {
    const t = (h.innerText || '').trim();
    if (t) heads.push(t.split(String.fromCharCode(10)).map(x => x.trim()).filter(Boolean));
  });
  return heads;
}
"""

JS_DATEVAL = r"""
() => {
  const vals = [];
  document.querySelectorAll('input').forEach(i => {
    if (i.value && /^[0-9]{4}[.][0-9]{2}[.][0-9]{2}/.test(i.value)) vals.push(i.value);
  });
  return vals;
}
"""


async def read_grid(page):
    """그리드 전체를 번호 기준 dict로. 가상 스크롤 대비해 스크롤하며 여러 번 수집."""
    # 그리드가 그려질 때까지 대기 (조회 직후엔 비어 있음 → 0행으로 오판하던 원인)
    for _ in range(30):
        if await page.locator(".cl-grid-row").count() > 0:
            break
        await page.wait_for_timeout(500)
    await page.wait_for_timeout(600)
    rows = {}
    hdr_cache = {}

    async def collect():
        raw = await page.evaluate(JS_READ)
        # 그날 시간표 교시 수에 따라 열 개수가 달라진다(실측: 7/16·7/21은 1칸 적음).
        # → 헤더 행에서 실제 컬럼명을 읽어 매핑한다.
        header = None
        for kids in raw:
            if kids and kids[0].strip() == "번호":
                header = [k.split(chr(10))[0].strip() for k in kids]
                break
        if header:
            hdr_cache["names"] = header
        header = header or hdr_cache.get("names")

        for kids in raw:
            if not header or len(kids) != len(header) + 1:
                continue                      # 헤더/장식 행 (데이터 행은 index 2에 빈 칸이 하나 더 있음)
            cells = kids
            num = cells[0].strip()
            if not num.isdigit():
                continue
            named = {}
            for i, nm in enumerate(header):
                named[nm] = (cells[i] if i < 2 else cells[i + 1]).strip()
            if num in rows and len(rows[num]) >= len(named):
                continue
            rows[num] = named

    await collect()
    for _ in range(6):
        await page.mouse.wheel(0, 600)
        await page.wait_for_timeout(250)
        await collect()
    await page.keyboard.press("Home")
    await page.wait_for_timeout(200)
    return rows


def marks_of(named):
    """행에서 비어있지 않은 출결 표시만 뽑기 → {컬럼명: 값}"""
    return {k: v for k, v in named.items()
            if k not in ("번호", "성명") and v}


PERIOD_RE = re.compile(r"^(\d+)교시$")
MORNING_COL = "조회"   # 조회 칸은 0교시로 센다 (입력기 parse_gyosi_num 도 '조회' → 0)


def _period_label(n):
    return "조회" if n == 0 else f"{n}교시"


def _marked_periods(marks):
    """나이스 화면에서 '/' 로 표시된 교시 번호 (= 빠진 교시). 조회 칸은 0.

    ⚠️ 예전에는 N교시 열만 세서 «조회» 칸의 '/' 를 놓쳤다 — 조회 지각은 늘
       「빠진 교시 표시(/)가 하나도 없음」 으로 나왔다
       (2026-09-11 실측: 조회 지각 행 셀 index 4 = 조회 에 '/').
    """
    out = []
    for col, val in marks.items():
        if val != "/":
            continue
        if col == MORNING_COL:
            out.append(0)
            continue
        m = PERIOD_RE.match(col)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _check_periods(jongryu, note, marks):
    """교시 범위가 맞는지. 맞으면 None, 틀리면 사유 문자열.

    조퇴 `N교시~` = N교시부터 빠짐  → '/' 가 N교시에서 시작해야 한다
    지각 `~N교시` = N교시까지 빠짐  → '/' 가 N교시에서 끝나야 한다 (조회 지각은 조회 칸에서)
    기대 교시는 입력기와 같은 함수(M.parse_gyosi_num)로 읽는다 — 넣는 쪽과 보는 쪽이 어긋나지 않게.
    """
    got = _marked_periods(marks)
    if not got:
        return "빠진 교시 표시(/)가 하나도 없음"
    if jongryu not in ("조퇴", "지각"):
        return None                       # 결석은 전 교시가 대상이라 범위를 따로 보지 않는다
    want = M.parse_gyosi_num(note, jongryu)
    if want is None:
        return None                       # 교시 표기가 없으면 범위는 검사하지 않는다
    edge, word = (min(got), "부터") if jongryu == "조퇴" else (max(got), "까지")
    if edge != want:
        return f"{_period_label(want)}{word}여야 하는데 나이스는 {_period_label(edge)}{word}"
    return None


def compare_day(expected, actual):
    """하루치 대조 → [(상태, 메시지)]. 상태: ok / 빠짐 / 분류다름 / 교시다름 / 추가"""
    rows = []
    seen = set()
    for t in expected:
        num = str(t["number"])
        want = f"{t['gubun']}{t['jongryu']}"
        note = (t.get("note") or "").strip()
        seen.add(num)
        head = f"{num}번 {t['name']}  {want}" + (f" {note}" if note else "")
        marks = actual.get(num)
        if not marks:
            rows.append(("빠짐", f"{head} → 나이스에 없음"))
            continue
        got = marks.get("마감", "")
        if not got:
            rows.append(("빠짐", f"{head} → 나이스에 안 들어갔습니다 (그 줄이 비어 있음)"))
            continue
        if got != want:
            rows.append(("분류다름", f"{head} → 나이스에는 '{got}' 입니다"))
            continue
        why = _check_periods(t["jongryu"], note, marks)
        if why:
            rows.append(("교시다름", f"{head} → {why}"))
        else:
            rows.append(("ok", head))
    for num, marks in actual.items():
        got = marks.get("마감", "")
        if got and num not in seen:
            rows.append(("추가", f"{num}번  나이스에만 '{got}' 있음 (캘린더엔 없음)"))
    return rows


def print_report(report):
    """대조 결과를 사람이 읽을 수 있는 표로 출력한다."""
    MARK = {"ok": "  일치  ", "빠짐": "  빠짐  ", "분류다름": " 분류다름 ",
            "교시다름": " 교시다름 ", "추가": "  추가  "}
    print("")
    print("=" * 62)
    print("  검증 결과 — 캘린더(기대값) vs 나이스(실제값)")
    print("=" * 62)
    counts = {}
    for d, rows in report:
        print("")
        print(f"  {d.strftime('%m/%d')} ({'월화수목금토일'[d.weekday()]})")
        if not rows:
            print("      (이 날은 대조할 항목이 없습니다)")
        for st, msg in rows:
            print(f"    [{MARK[st]}] {msg}")
            counts[st] = counts.get(st, 0) + 1
    ok = counts.get("ok", 0)
    bad = sum(counts.values()) - ok
    print("")
    print("-" * 62)
    if bad == 0:
        print(f"  전부 일치합니다 — {ok}건 확인. 고칠 것 없습니다.")
    else:
        parts = ", ".join(f"{k} {v}건" for k, v in counts.items() if k != "ok")
        print(f"  확인이 필요합니다 — {bad}건 ({parts}) / 일치 {ok}건")
        print("  고치는 방법은 설명서 4장 '④ 틀린 곳을 고칩니다' 를 보세요.")
    print("-" * 62)


async def run(start, end, days=None):
    """days: 출결이 없어도 볼 날짜 (출결 앱 파일 기간 — 나이스에만 남은 출결을 찾으려고)."""
    M.load_teacher_settings()
    recs = M.load_from_calendar(start, end)
    task_map = M.build_task_map(recs)
    holidays = M.load_holidays()

    dates = sorted(d for d in set(task_map) | set(days or ())
                   if start <= d <= end and d.weekday() < 5 and d not in holidays)
    print(f"검증 대상 {len(dates)}일: " + ", ".join(d.strftime('%m/%d') for d in dates))

    PROFILE_DIR = str(teacher_config.CONFIG_DIR / "chrome_profile")
    result = {}
    report = []
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR, channel="chrome", headless=False, slow_mo=40,
            args=["--profile-directory=Default",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-features=PrivateNetworkAccessForNavigations,PrivateNetworkAccessPermissionPrompt,PermissionChip",
                  "--disable-web-security", "--allow-running-insecure-content",
                  "--test-type", "--start-maximized", "--noerrdialogs"],
            ignore_default_args=["--enable-automation", "--no-sandbox"], viewport=None)
        await ctx.grant_permissions(["notifications"], origin=M._origin_of(M.NEIS_URL))
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        try:
            await page.goto(M.NEIS_URL)
            await M.auto_login(page)
            await M.wait_for_attendance_page(page)
            result["_header"] = await page.evaluate(JS_HEADER)

            for d in dates:
                await M.dismiss_alert_popup(page, timeout=800)
                await M.set_date_and_search(page, d)
                await page.wait_for_timeout(500)
                grid = await read_grid(page)
                dateval = await page.evaluate(JS_DATEVAL)
                actual = {num: marks_of(c) for num, c in grid.items() if marks_of(c)}
                result[d.isoformat()] = {
                    "rows_read": len(grid),
                    "date_on_screen": dateval,
                    "raw": grid,
                    "actual": actual,
                    "expected": [
                        {"number": t["number"], "name": t["name"],
                         "gubun": t["gubun"], "jongryu": t["jongryu"], "note": t["note"]}
                        for t in task_map.get(d, [])],
                }
                rows = compare_day(result[d.isoformat()]["expected"], actual)
                result[d.isoformat()]["diff"] = rows
                report.append((d, rows))
                print(f"  {d.strftime('%m/%d')} 읽음: {len(grid)}행, 표시된 학생 {len(actual)}명, 화면날짜={dateval}")
        finally:
            await ctx.close()

    print_report(report)

    with open("verify_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("")
    print("자세한 내용은 verify_result.json 에 저장했습니다.")
    print("(이 파일에는 학생 이름이 들어 있습니다 — 남에게 주거나 커밋하지 마세요)")


if __name__ == "__main__":
    s = date.fromisoformat(sys.argv[1]); e = date.fromisoformat(sys.argv[2])
    asyncio.run(run(s, e))
