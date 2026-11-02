# -*- coding: utf-8 -*-
"""나이스 월 출결마감 — 그 달 일마감(한 번에) → 월마감 → 근태신고서철 출력물 내려받기.

  python month_close.py 2026-10                 상태만 본다 (아무것도 안 누른다)
  python month_close.py 2026-10 --close         일마감 + 월마감을 실제로 한다
  python month_close.py 2026-10 --download      학급별 출결현황(PDF) · 월별 출결현황(엑셀)을 받는다
  python month_close.py 2026-10 --close --download   둘 다 (로그인 한 번)

받은 파일은 이 폴더의 `출력/` 에 놓인다 (gitignore — 학생 이름이 들어 있다).
  출력/2026-10_학급별출결현황.pdf
  출력/2026-10_월별출결현황.xlsx

출결 «입력»은 main.py 가 한다. 이 스크립트는 입력을 고치지 않는다 —
마감 전에 verify_attendance.py 로 캘린더와 나이스가 맞는지 먼저 본다 (docs/지시서/5_나이스-월마감.md).

마지막 줄에 항상 RESULT_JSON {...} 을 찍는다.
"""
import asyncio
import calendar
import json
import re
import sys
from datetime import date
from pathlib import Path

from playwright.async_api import async_playwright

import main as M
import teacher_config

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "출력"
SHOT_DIR = HERE / "캡처"

RESULT = {"ok": False, "month": None, "day_close": None, "month_close": None, "files": []}


def log(*a):
    print(*a, flush=True)


async def click_confirm(page, timeout=3000) -> bool:
    btn = page.locator("a:visible, button:visible, [role=button]:visible", has_text=re.compile(r"^확인$"))
    try:
        await btn.last.click(force=True, timeout=timeout)
        return True
    except Exception:
        return False


POPUP = """() => {
  const o=[];
  document.querySelectorAll('[role=dialog], .cl-popup, .cl-messagebox, .cl-dialog').forEach(e=>{
    const b=e.getBoundingClientRect();
    if(b.width>0&&b.height>0){const t=(e.innerText||'').trim(); if(t) o.push(t.slice(0,300));}
  });
  return o;
}"""


async def show_popup(page, tag):
    try:
        ps = await page.evaluate(POPUP)
    except Exception:
        ps = []
    for t in ps:
        log(f"  [창/{tag}] " + " / ".join(t.splitlines())[:300])
    return ps


async def shot(page, name):
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        await page.screenshot(path=str(SHOT_DIR / name))
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────
# 1) 일마감 — 반별일출결마감관리 → 조회 → 전체선택 → 저장
# ──────────────────────────────────────────────────────────────
CHK = """() => { const o={on:0,off:0}; document.querySelectorAll('[role=checkbox]').forEach(c=>{
    const r=c.getBoundingClientRect(); if(r.width>0&&r.y>250){ c.getAttribute('aria-checked')==='true'?o.on++:o.off++; }}); return o; }"""

COMBO_LABEL = """() => { const c=[...document.querySelectorAll('[role=combobox]')].find(e=>{
    const r=e.getBoundingClientRect(); return r.width>0&&/^월,/.test(e.getAttribute('aria-label')||'');});
    return c ? c.getAttribute('aria-label') : null; }"""


async def set_month_combo(page, m) -> bool:
    """반별일출결마감관리의 「월」 콤보(aria-label «월, 10월»)를 m월로. 나이스 콤보는 select_option 이 안 먹는다."""
    want = f"월, {m}월"
    if await page.evaluate(COMBO_LABEL) == want:
        return True
    combo = page.locator("[role=combobox][aria-label^='월,']").filter(visible=True).first
    for _ in range(2):
        try:
            await combo.click()
            await page.wait_for_timeout(700)
            opt = page.locator("[role=option]").filter(visible=True).filter(has_text=re.compile(rf"^{m}월$")).first
            if await opt.count():
                await opt.click()
            else:
                await page.get_by_text(f"{m}월", exact=True).filter(visible=True).last.click()
            await page.wait_for_timeout(900)
        except Exception as e:
            log("  월 콤보 오류:", str(e)[:90])
        if await page.evaluate(COMBO_LABEL) == want:
            return True
    return False


async def day_close(page, y, m, commit):
    log("\n[1] 일출결마감 — 반별일출결마감관리")
    await M.dismiss_alert_popup(page, timeout=1500)
    await page.get_by_text("반별일출결마감관리", exact=True).first.click()
    await page.wait_for_timeout(2500)
    await M.dismiss_alert_popup(page, timeout=1500)
    tp = page.get_by_role("tabpanel")

    # 「월」 콤보를 마감할 달로 맞춘다 (처음엔 이번 달로 떠 있다). 못 맞추면 누르지 않는다.
    if not await set_month_combo(page, m):
        log(f"  ⛔ 「월」 칸을 {m}월로 못 바꿨습니다 — 다른 달을 마감할까 봐 멈춥니다")
        await shot(page, "월마감_일마감화면.png")
        return {"status": "month_not_set"}

    await tp.locator("a,[role=button]").filter(has_text=re.compile(r"^조회$")).filter(visible=True).first.click()
    await page.wait_for_timeout(2500)
    before = await page.evaluate(CHK)
    log(f"  {m}월 조회 — 체크된 날 {before['on']} · 안 된 칸 {before['off']}")

    if before["on"] > 0 and before["off"] <= 1:      # 달력 밖 체크칸이 하나 있어 off=1 이 «전부 마감»
        log("  ✅ 이미 모든 날이 일마감돼 있습니다")
        return {"status": "saved", "before": before, "after": before}
    if not commit:
        log("  (상태만 보기 — 전체선택·저장은 안 누름)")
        return {"status": "dry", "before": before}

    await tp.locator("a,[role=button]").filter(has_text=re.compile(r"^전체선택$")).filter(visible=True).first.click()
    await page.wait_for_timeout(1200)
    log("  전체선택 후:", await page.evaluate(CHK))
    await tp.locator("a,[role=button]").filter(has_text=re.compile(r"^저장$")).filter(visible=True).first.click()
    await page.wait_for_timeout(1500)
    await show_popup(page, "저장")
    await click_confirm(page, timeout=4000)
    await page.wait_for_timeout(2500)
    await show_popup(page, "확인1")
    await click_confirm(page, timeout=4000)
    await page.wait_for_timeout(2000)
    await tp.locator("a,[role=button]").filter(has_text=re.compile(r"^조회$")).filter(visible=True).first.click()
    await page.wait_for_timeout(2500)
    if await page.evaluate(COMBO_LABEL) != f"월, {m}월":
        await set_month_combo(page, m)
        await tp.locator("a,[role=button]").filter(has_text=re.compile(r"^조회$")).filter(visible=True).first.click()
        await page.wait_for_timeout(2500)
    after = await page.evaluate(CHK)
    log("  저장 후 다시 조회:", after, "(달력 밖 체크칸이 하나 있어 off=1 이 정상)")
    await shot(page, "월마감_일마감후.png")
    if not (after["on"] > 0 and after["off"] <= 1):
        log("  ❌ 일마감이 다 되지 않았습니다 — 월마감은 하지 않습니다")
        return {"status": "not_all", "before": before, "after": after}
    return {"status": "saved", "before": before, "after": after}


# ──────────────────────────────────────────────────────────────
# 2) 월마감 — 반별월출결마감관리 → 그 달 행 클릭 → 마감
# ──────────────────────────────────────────────────────────────
GRID = """() => {
  const o=[];
  document.querySelectorAll('.cl-grid-row').forEach(r=>{
    const k=Array.from(r.children).map(d=>(d.innerText||'').trim());
    if(k.length===3){const b=r.getBoundingClientRect(); o.push({k, x:Math.round(b.x), y:Math.round(b.y), h:Math.round(b.height)});}
  });
  return o;
}"""

# 마감·취소 버튼만 찾는다 (aria/텍스트가 정확히 그 글자인 말단 요소)
BTNS = """() => {
  const o=[];
  document.querySelectorAll('div[role=button], a, button, .cl-button, .cl-text').forEach(e=>{
    if(e.children.length) return;
    const t=(e.innerText||'').trim(); const r=e.getBoundingClientRect();
    if(r.width>0&&r.height>0&&/^(마감|취소|출결마감자료확인)$/.test(t)&&r.x>700)
      o.push({t, cls:(e.className||'').toString(), x:Math.round(r.x), y:Math.round(r.y)});
  });
  return o;
}"""


async def open_month_tab(page):
    await M.dismiss_alert_popup(page, timeout=1500)
    await page.get_by_role("link", name="반별월출결마감관리", exact=True).first.click()
    await page.wait_for_timeout(2200)
    await M.dismiss_alert_popup(page, timeout=1500)
    await page.get_by_role("tabpanel").locator("a").filter(has_text=re.compile(r"^조회$")).first.click()
    await page.wait_for_timeout(2500)


async def month_buttons(page):
    btns = await page.evaluate(BTNS)
    cancel = next((b for b in btns if b["t"] == "취소"), None)
    mg = next((b for b in btns if b["t"] == "마감" and cancel and abs(b["y"] - cancel["y"]) < 10), None)
    return mg, cancel


async def click_month_row(page, sem, mm):
    await page.evaluate("""([s,mm]) => { const r=[...document.querySelectorAll('.cl-grid-row')].find(r=>{
        const k=Array.from(r.children).map(d=>(d.innerText||'').trim()); return k.length===3&&k[0]===s&&k[1]===mm;});
        if(r) r.scrollIntoView({block:'center'}); }""", [sem, mm])
    await page.wait_for_timeout(800)
    row = next((r for r in await page.evaluate(GRID) if r["k"][0] == sem and r["k"][1] == mm), None)
    if not row:
        return None
    await page.mouse.click(row["x"] + 60, row["y"] + row["h"] // 2)
    await page.wait_for_timeout(1200)
    return row


async def month_close(page, y, m, commit):
    log("\n[2] 월출결마감 — 반별월출결마감관리")
    mm = f"{m:02d}"
    last = calendar.monthrange(y, m)[1]
    await open_month_tab(page)
    rows = await page.evaluate(GRID)
    log("  표:", [r["k"] for r in rows])
    targets = [r for r in rows if r["k"][1] == mm]
    if not targets:
        log(f"  ⛔ {mm}월 줄이 없습니다")
        await shot(page, "월마감_월마감화면.png")
        return {"status": "no_row"}

    out = []
    for t in sorted(targets, key=lambda r: r["k"][0]):        # 8월처럼 1·2학기가 섞인 달은 1학기부터
        sem = t["k"][0]
        row = await click_month_row(page, sem, mm)
        di = page.locator("input[aria-label='일']").filter(visible=True).first
        day_val = ""
        if await di.count():
            day_val = (await di.input_value()).strip()
        mg, cancel = await month_buttons(page)
        state = ("마감됨" if mg and "disabled" in mg["cls"] and cancel and "disabled" not in cancel["cls"]
                 else "마감 전" if mg and "disabled" not in mg["cls"] else "알 수 없음")
        log(f"  {sem}학기 {mm}월 — 표 글자 «{row['k'][2] if row else '?'}» · 마감일 칸 «{day_val}» · 상태 {state}")
        info = {"semester": sem, "cell": row["k"][2] if row else "", "day": day_val, "state": state}
        if state != "마감 전":
            out.append(info)
            continue
        if not commit:
            log("    (상태만 보기 — 마감은 안 누름)")
            out.append(info)
            continue
        # 마감일 칸: 비어 있을 때만 그 달 말일을 넣는다 (학기 끝나는 달은 나이스가 학기종료일을 채워 둔다)
        if await di.count() and not day_val:
            await di.click(); await di.press("Control+a"); await di.type(str(last), delay=50); await di.press("Tab")
            await page.wait_for_timeout(600)
            info["day"] = (await di.input_value()).strip()
            log(f"    마감일 칸이 비어 있어 {info['day']} 을 넣었습니다")
        await page.mouse.click(mg["x"] + 10, mg["y"] + 6)
        await page.wait_for_timeout(1500)
        await show_popup(page, "마감")
        await click_confirm(page, timeout=4000); await page.wait_for_timeout(2500)
        await show_popup(page, "확인1")
        await click_confirm(page, timeout=4000); await page.wait_for_timeout(2500)
        await show_popup(page, "확인2")
        # 다시 읽어 버튼 상태로 판정한다 (2학기는 표에 날짜 대신 «마감» 글자만 뜬다)
        await open_month_tab(page)
        await click_month_row(page, sem, mm)
        mg2, cancel2 = await month_buttons(page)
        done = bool(mg2 and "disabled" in mg2["cls"] and cancel2 and "disabled" not in cancel2["cls"])
        info["state"] = "마감됨" if done else "마감 안 됨"
        log(f"    → {'✅ 월마감 완료' if done else '❌ 마감이 안 됐습니다'}")
        if not done:
            log("    흔한 원인: 그 달에 일마감 안 된 날이 남아 있다 (1단계 결과를 다시 보세요)")
            await shot(page, "월마감_실패.png")
        out.append(info)
    log("  다시 읽은 표:", [r["k"] for r in await page.evaluate(GRID) if r["k"][1] == mm])
    return {"status": "done" if commit else "dry", "rows": out}


# ──────────────────────────────────────────────────────────────
# 3) 출력물 — 출결현황및통계 → 학급별출결현황
# ──────────────────────────────────────────────────────────────
LEAF = """() => {
  const o=[];
  document.querySelectorAll('div, span, a, label').forEach(e=>{ if(e.children.length) return;
    const r=e.getBoundingClientRect(); const t=(e.innerText||'').trim();
    if(r.width>0&&r.height>0&&t&&t.length<22)
      o.push({t, x:Math.round(r.x), y:Math.round(r.y), cls:(e.className||'').toString().slice(0,40)});
  });
  return o;
}"""


async def click_text(page, text, ymin=0, ymax=99999):
    for it in await page.evaluate(LEAF):
        if it["t"] == text and ymin <= it["y"] <= ymax and "disabled" not in it["cls"]:
            await page.mouse.click(it["x"] + 8, it["y"] + 8)
            return True
    log(f"    ❌ '{text}' 못 찾음 (같은 글자 위치: {[(it['x'], it['y'], it['cls']) for it in await page.evaluate(LEAF) if it['t'] == text]})")
    return False


async def open_stats(page, y, m):
    try:
        await page.get_by_role("link", name="출결현황및통계 2단계 메뉴항목").first.click(timeout=8000)
    except Exception:
        await page.locator("a").filter(has_text="출결현황및통계").first.click(force=True)
    await page.wait_for_timeout(1800)
    await page.locator("a").filter(has_text="출결현황및통계 3단계").first.click()
    await page.wait_for_timeout(3500)
    await M.dismiss_alert_popup(page, timeout=1500)
    # 월별 입력칸(2026.10. 모양)에 그 달을 친다 → 기간이 그 달 1일~말일로 바뀐다
    inputs = page.locator("input").filter(visible=True)
    n = await inputs.count()
    for k in range(n):
        v = await inputs.nth(k).input_value()
        if re.fullmatch(r"\d{4}\.\d{2}\.", v or ""):
            box = inputs.nth(k)
            await box.click(); await box.press("Control+a")
            await box.type(f"{y}.{m:02d}.", delay=60); await box.press("Tab")
            await page.wait_for_timeout(1200)
            vals = [await inputs.nth(j).input_value() for j in range(n)]
            log("  월·기간:", [v for v in vals if v and re.match(r"\d{4}\.\d{2}", v)])
            break
    else:
        log("  ❌ 월 입력칸을 못 찾았습니다")
        return False
    await click_text(page, "인정내역 표시")
    await page.wait_for_timeout(400)
    await click_text(page, "조회", ymin=200, ymax=420)   # 2026-11-02 실측 y=351 — 350 으로 막았다가 놓침
    await page.wait_for_timeout(3000)
    await M.dismiss_alert_popup(page, timeout=1500)
    await click_text(page, "음영표시")
    await page.wait_for_timeout(500)
    return True


EXPORT_URLS = ("/report/service", "grid_excel_export.do")   # 학급별출력 PDF · 월별 출결현황 엑셀 (2026-10-06 실측)


async def _save_on_download(page, out: Path, click, wait_ms=20000) -> bool:
    """클릭 → 나이스가 파일을 돌려주는 요청을 «가로채서» 몸통을 바로 파일로 쓴다.

    나이스는 내려받기가 시작되면 곧 브라우저를 닫아 버린다 (2026-10-06 실측: 내려받기 이벤트 3초 뒤 창이 닫힘).
    브라우저의 내려받기가 끝나기를 기다리면(save_as) 그 사이에 닫혀 «Target closed» 로 실패한다.
    그래서 응답을 우리가 직접 받아(route.fetch) 저장한 뒤 브라우저에 넘겨준다."""
    ctx = page.context
    got = {}

    async def handler(route):
        req = route.request
        if req.method != "POST" or got.get("done"):
            await route.continue_()
            return
        try:
            resp = await route.fetch()
        except Exception as e:
            log("  응답 받기 오류:", str(e)[:90])
            try:
                await route.continue_()
            except Exception:
                pass
            return
        ctype = (resp.headers.get("content-type") or "").lower()
        disp = (resp.headers.get("content-disposition") or "").lower()
        if "attachment" in disp or "pdf" in ctype or "spreadsheet" in ctype:
            try:
                body = await resp.body()
                out.write_bytes(body)
                got["done"] = True
            except Exception as e:
                log("  파일 쓰기 오류:", str(e)[:90])
        try:
            await route.fulfill(response=resp)
        except Exception:
            pass

    def match(url):
        return any(k in url for k in EXPORT_URLS)

    await ctx.route(match, handler)
    try:
        try:
            await click()
        except Exception as e:
            log("  (클릭 뒤 예외 — 무시하고 파일을 확인합니다)", str(e)[:80])
        for _ in range(wait_ms // 500):
            if got.get("done"):
                break
            await asyncio.sleep(0.5)
    finally:
        try:
            await ctx.unroute(match, handler)
        except Exception:
            pass
    return out.exists() and out.stat().st_size > 0


async def download_pdf(page, out: Path) -> bool:
    """[학급별출력] → 리포트 뷰어(Crownix) [저장] → PDF."""
    if not await click_text(page, "학급별출력"):
        return False
    fr = None
    for _ in range(30):
        fr = next((f for f in page.frames if "crownix" in f.url), None)
        if fr:
            break
        await page.wait_for_timeout(500)
    if not fr:
        log("  ❌ 리포트 화면(crownix)이 안 떴습니다")
        return False
    await page.wait_for_timeout(3000)
    await fr.locator("#crownix-toolbar-save button, #crownix-toolbar-save").first.click()
    await page.wait_for_timeout(2500)
    hit = fr.get_by_text("PDF", exact=True).filter(visible=True).first
    if not await hit.count():
        hit = page.get_by_text("PDF", exact=True).filter(visible=True).first
    if not await hit.count():
        log("  ❌ 저장 형식 메뉴에서 PDF 를 못 찾았습니다")
        return False
    return await _save_on_download(page, out, hit.click, wait_ms=40000)


async def download_xlsx(page, out: Path) -> bool:
    """[월별 출결현황] 창 → 조회 → [엑셀다운로드]."""
    if not await click_text(page, "월별 출결현황"):
        return False
    await page.wait_for_timeout(4000)
    anchor = None
    for it in await page.evaluate(LEAF):
        if it["t"] == "엑셀다운로드":
            anchor = it
    if not anchor:
        log("  ❌ 월별 출결현황 창을 못 찾았습니다")
        return False
    ups = [it for it in await page.evaluate(LEAF) if it["t"] == "조회" and it["y"] < anchor["y"]]
    if ups:
        pick = max(ups, key=lambda c: c["y"])        # 창 안의 [조회] = 엑셀다운로드보다 위에서 가장 가까운 것
        await page.mouse.click(pick["x"] + 10, pick["y"] + 8)
        await page.wait_for_timeout(4000)
    return await _save_on_download(page, out, lambda: page.mouse.click(anchor["x"] + 20, anchor["y"] + 8))


# ──────────────────────────────────────────────────────────────
class Session:
    """전용 크롬을 띄워 나이스에 로그인한 상태로 출결관리까지 간다. 내려받기 하나마다 새로 띄운다."""
    def __init__(self, p):
        self.p, self.ctx, self.page = p, None, None

    async def __aenter__(self):
        self.ctx = await self.p.chromium.launch_persistent_context(
            user_data_dir=str(teacher_config.CONFIG_DIR / "chrome_profile"), channel="chrome",
            headless=False, slow_mo=40,
            args=["--profile-directory=Default", "--disable-blink-features=AutomationControlled",
                  "--disable-features=PrivateNetworkAccessForNavigations,PrivateNetworkAccessPermissionPrompt,PermissionChip",
                  "--disable-web-security", "--allow-running-insecure-content", "--test-type",
                  "--start-maximized", "--noerrdialogs"],
            ignore_default_args=["--enable-automation", "--no-sandbox"], viewport=None,
            accept_downloads=True)
        await self.ctx.grant_permissions(["notifications"], origin=M._origin_of(M.NEIS_URL))
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        await self.page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        await self.page.goto(M.NEIS_URL)
        await M.auto_login(self.page)
        await M.wait_for_attendance_page(self.page)
        await M.dismiss_alert_popup(self.page, timeout=1500)
        return self.page

    async def __aexit__(self, *a):
        try:
            await self.ctx.close()
        except Exception:
            pass


async def download(p, y, m):
    log("\n[3] 출력물 내려받기 — 출결현황및통계 › 학급별출결현황")
    OUT_DIR.mkdir(exist_ok=True)
    tag = f"{y}-{m:02d}"
    files = []
    for label, out, fn in ((f"학급별 출결현황 PDF", OUT_DIR / f"{tag}_학급별출결현황.pdf", download_pdf),
                           (f"월별 출결현황 엑셀", OUT_DIR / f"{tag}_월별출결현황.xlsx", download_xlsx)):
        ok = False
        for attempt in (1, 2):
            if out.exists():
                out.unlink()
            try:
                async with Session(p) as page:
                    if await open_stats(page, y, m):
                        ok = await fn(page, out)
            except Exception as e:
                log(f"  {label} 받기 오류:", str(e)[:120])
            if ok:
                break
            if attempt == 1:
                log(f"  ↻ {label} 한 번 더")
        log(f"  {'✅' if ok else '❌'} {label} → 출력/{out.name}")
        if ok:
            files.append(str(out))
    return files


async def run(y, m, do_close, do_download):
    M.load_teacher_settings()
    async with async_playwright() as p:
        page = None
        try:
            async with Session(p) as page:
                RESULT["day_close"] = await day_close(page, y, m, do_close)
                dc = RESULT["day_close"]["status"]
                if do_close and dc != "saved":
                    log("\n⛔ 일마감을 못 해서 월마감은 하지 않았습니다")
                    RESULT["month_close"] = {"status": "skipped"}
                else:
                    RESULT["month_close"] = await month_close(page, y, m, do_close)
        except Exception as e:
            import traceback
            traceback.print_exc()
            RESULT["error"] = str(e)[:200]
            if page is not None:
                await shot(page, "월마감_오류.png")
        if do_download and "error" not in RESULT:
            RESULT["files"] = await download(p, y, m)
        mc = RESULT["month_close"] or {}
        closed_ok = bool(mc.get("rows")) and all(r.get("state") == "마감됨" for r in mc.get("rows", []))
        RESULT["ok"] = ("error" not in RESULT and ((not do_close) or closed_ok)
                        and ((not do_download) or len(RESULT["files"]) == 2))


def main():
    args = sys.argv[1:]
    ym = next((a for a in args if re.fullmatch(r"\d{4}-\d{1,2}", a)), None)
    if not ym:
        print(__doc__)
        print("RESULT_JSON " + json.dumps({"ok": False, "error": "달을 2026-10 처럼 주세요"}, ensure_ascii=False))
        return 2
    y, m = map(int, ym.split("-"))
    RESULT["month"] = f"{y}-{m:02d}"
    do_close, do_download = "--close" in args, "--download" in args
    if (y, m) > (date.today().year, date.today().month):
        print("⛔ 아직 오지 않은 달은 마감하지 않습니다")
        print("RESULT_JSON " + json.dumps({"ok": False, "error": "future_month"}, ensure_ascii=False))
        return 2
    last_weekday = max(date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)
                       if date(y, m, d).weekday() < 5)
    if do_close and date.today() < last_weekday and "--force" not in args:
        print(f"⛔ {m}월의 마지막 평일({last_weekday.month}/{last_weekday.day}) 전이라 마감하지 않습니다.")
        print("   그 전에 꼭 마감해야 하면 --force 를 붙이세요 (남은 날의 출결은 마감 뒤에 못 넣습니다).")
        print("RESULT_JSON " + json.dumps({"ok": False, "error": "month_not_over"}, ensure_ascii=False))
        return 2
    log("=" * 52)
    log(f"🏫  나이스 월 출결마감 — {y}년 {m}월"
        + ("" if (do_close or do_download) else "  (상태만 보기)"))
    log("=" * 52)
    try:
        asyncio.run(run(y, m, do_close, do_download))
    except teacher_config.ConfigMissingError as e:
        log(f"❌ {e}")
        RESULT["error"] = "config"
    print("RESULT_JSON " + json.dumps(RESULT, ensure_ascii=False))
    return 0 if RESULT["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
