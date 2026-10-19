#!/usr/bin/env python3
"""
출결 앱 → 나이스 자동 입력 연결 스크립트

출결 앱(https://yeobbun.github.io/attendance/)에서 만든 「나이스 입력 파일」을 읽어
neis-automation-kit 의 나이스 입력기(main.py)로 넣는다.
키트는 원래 구글 캘린더에서 출결을 읽는데, 그 부분만 이 파일로 바꿔 끼운다.
키트 코드는 고치지 않으므로 키트를 업데이트해도 그대로 쓸 수 있다.

이 파일을 neis-automation-kit 폴더(main.py 가 있는 곳)에 두고 실행한다.

    .\\.venv\\Scripts\\python.exe -X utf8 app_to_neis.py --dry-run     ← 목록만 확인 (나이스 안 엶)
    .\\.venv\\Scripts\\python.exe -X utf8 app_to_neis.py               ← 실제로 넣기
    .\\.venv\\Scripts\\python.exe -X utf8 app_to_neis.py 파일.json     ← 파일을 직접 지정
    .\\.venv\\Scripts\\python.exe -X utf8 app_to_neis.py --마감        ← 저장 뒤 그날 [출결마감] 까지 (말할 때만)
    .\\.venv\\Scripts\\python.exe -X utf8 app_to_neis.py --대조        ← 파일과 나이스가 맞는지만 (월마감 1단계, 읽기 전용)

    .\\.venv\\Scripts\\python.exe -X utf8 app_to_neis.py --auto        ← 자동 실행: 아직 안 넣은 새 파일만 넣고 결과를 기록
    .\\.venv\\Scripts\\python.exe -X utf8 app_to_neis.py --예약 16:30   ← 평일 그 시각에 --auto 가 돌도록 작업 스케줄러에 등록
    .\\.venv\\Scripts\\python.exe -X utf8 app_to_neis.py --예약해제     ← 예약 지우기

파일을 지정하지 않으면 이 폴더와 「다운로드」 폴더에서 가장 최근의 attendance_neis_*.json(.txt) 을 쓴다.
(.txt 는 휴대폰에서 「드라이브로 보내기」 로 공유한 파일 — 내용은 같다)
키트의 다른 옵션(--dry-run, --future)은 그대로 넘긴다.
"""

import asyncio
import builtins
import hashlib
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

FORMAT = "attendance-app/neis-v1"
PATTERNS = ("attendance_neis_*.json", "attendance_neis_*.txt")
HERE = Path(__file__).resolve().parent
TASK_NAME = "AttendanceAppNeis"


def candidate_files() -> list[Path]:
    found = []
    for folder in (HERE, Path.home() / "Downloads"):
        for pattern in PATTERNS:
            found += folder.glob(pattern)
    return found


def find_latest_file() -> Path | None:
    candidates = candidate_files()
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def load_app_file(path: Path) -> tuple[list[dict], dict]:
    """앱 파일을 키트의 load_from_calendar() 가 돌려주는 모양으로 바꾼다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise SystemExit(f"❌  출결 앱에서 만든 나이스 입력 파일이 아닙니다: {path.name}")
    records = []
    for r in data["records"]:
        d = date.fromisoformat(r["date"])
        records.append({
            "number":     str(r["number"]),
            "name":       r["name"],
            "att_type":   r["att_type"],
            "start_date": d,
            "end_date":   d,
            "note":       r.get("note", ""),
            "memo":       r.get("memo", ""),   # 상세 사유 → 나이스 「사유」 칸
        })
    return records, data


# ─── 자동 실행 (작업 스케줄러) ────────────────────────────────
# 학생 이름이 든 기록·로그는 저장소 밖(%LOCALAPPDATA%\neis-automation)에만 둔다.

def _state_dir() -> Path:
    import teacher_config
    d = teacher_config.CONFIG_DIR / "app_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_done() -> set[str]:
    f = _state_dir() / "processed.json"
    return set(json.loads(f.read_text(encoding="utf-8"))) if f.exists() else set()


def _save_done(done: set[str]) -> None:
    (_state_dir() / "processed.json").write_text(json.dumps(sorted(done)), encoding="utf-8")


class _Tee:
    """화면과 로그 파일에 같이 쓴다."""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, text):
        for s in self.streams:
            s.write(text)
    def flush(self):
        for s in self.streams:
            s.flush()


def run_auto(kit, extra_args: list[str]) -> int:
    """아직 넣지 않은 파일을 모두 모아 한 번에 넣는다. 새 파일이 없으면 아무것도 하지 않는다."""
    done = _load_done()
    new_files = sorted({p.resolve() for p in candidate_files()}, key=lambda p: p.stat().st_mtime)
    new_files = [p for p in new_files if _digest(p) not in done]
    if not new_files:
        print("✅  새 출결 파일이 없습니다 — 할 일 없음")
        return 0

    log_path = _state_dir() / f"{datetime.now():%Y%m%d-%H%M}.log"
    log = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log)
    sys.stderr = _Tee(sys.__stderr__, log)
    # 아무도 없는 자리에서 돈다 — 키트가 «Enter 를 누르세요» 하고 멈추지 않게 한다.
    builtins.input = lambda *a, **k: ""

    try:
        records, merged_from, merged_skipped = [], None, []
        seen = set()
        for path in new_files:
            recs, data = load_app_file(path)
            print(f"📄  {path.name}  ({data['from']} ~ {data['to']}, {len(recs)}건)")
            for r in recs:   # 여러 파일에 같은 날·같은 학생·같은 분류가 있으면 나중 파일 것을 쓴다
                key = (r["start_date"], r["number"], r["att_type"])
                if key in seen:
                    records = [x for x in records if (x["start_date"], x["number"], x["att_type"]) != key]
                seen.add(key)
                records.append(r)
            merged_skipped += data.get("skipped", [])
            merged_from = min(merged_from or data["from"], data["from"])

        if merged_skipped:
            print(f"\n⚠️   자동으로 못 넣는 {len(merged_skipped)}건 — 나이스에서 직접 넣어 주세요")
            for s in merged_skipped:
                print(f"    {s['date']}  {s['number']}번 {s['name']} {s['label']}: {s['reason']}")

        kit.load_from_calendar = lambda start_date, end_date: records
        sys.argv = [str(HERE / "main.py"), *extra_args, merged_from]
        code = asyncio.run(kit.main()) or 0
        if "--dry-run" in extra_args:
            print(f"📝  기록: {log_path}  (연습이라 파일은 그대로 둡니다)")
            return code

        # 다시 넣지 않도록 기록한다. 실패가 있으면 파일은 남겨 둔다 (직접 확인 후 다시 돌릴 수 있게).
        _save_done(done | {_digest(p) for p in new_files})
        if code == 0:
            for p in new_files:
                p.unlink(missing_ok=True)
            print(f"\n🗑   넣은 파일 {len(new_files)}개를 지웠습니다.")
        else:
            print("\n❗  못 넣은 건이 있어 파일을 남겨 둡니다. 나이스에서 확인해 주세요.")
        print(f"📝  기록: {log_path}")
        return code
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        log.close()


def install_task(at: str) -> int:
    """평일 at(HH:MM)에 --auto 가 돌도록 윈도우 작업 스케줄러에 등록한다.
    로그인해 있을 때만 돈다 (크롬 화면을 띄워 나이스를 조작하므로)."""
    if not re.fullmatch(r"\d{2}:\d{2}", at):
        print("❌  시각은 16:30 처럼 적어 주세요.")
        return 1
    command = f'"{sys.executable}" -X utf8 "{HERE / "app_to_neis.py"}" --auto'
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command,
         "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", at, "/IT", "/F"],
        capture_output=True, text=True,
    )
    print(result.stdout or result.stderr)
    if result.returncode == 0:
        print(f"✅  평일 {at} 에 출결 파일을 자동으로 넣도록 예약했습니다. 그 시각에 PC가 켜져 있고 로그인돼 있어야 합니다.")
    return result.returncode


def remove_task() -> int:
    result = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
    print(result.stdout or result.stderr)
    return result.returncode


def main() -> int:
    if not (HERE / "main.py").exists():
        print("❌  이 파일을 neis-automation-kit 폴더(main.py 가 있는 곳)에 두고 실행하세요.")
        return 1
    sys.path.insert(0, str(HERE))

    args = sys.argv[1:]
    if "--예약해제" in args:
        return remove_task()
    if "--예약" in args:
        i = args.index("--예약")
        return install_task(args[i + 1] if i + 1 < len(args) else "")

    import main as kit  # 키트의 나이스 입력기

    if "--auto" in args or "--자동" in args:
        return run_auto(kit, [a for a in args if a in ("--마감", "--future", "--dry-run")])

    file_arg = next((a for a in args if a.lower().endswith((".json", ".txt"))), None)
    path = Path(file_arg) if file_arg else find_latest_file()
    if not path or not path.exists():
        print("❌  attendance_neis_*.json(.txt) 파일을 찾지 못했습니다.")
        print("    앱 「출결 기록 → 나이스 파일」에서 저장한 뒤 이 폴더나 다운로드 폴더에 두세요.")
        return 1

    records, data = load_app_file(path)
    print(f"📄  {path.name}  ({data['from']} ~ {data['to']}, {len(records)}건)")
    with_memo = sum(1 for r in records if r["memo"])
    if with_memo < len(records):
        print(f"📝  사유 칸: {with_memo}건은 채우고, {len(records) - with_memo}건은 상세 사유가 없어 비워 둡니다")
    else:
        print(f"📝  사유 칸: {with_memo}건 모두 채웁니다")

    skipped = data.get("skipped", [])
    if skipped:
        print(f"\n⚠️   자동으로 못 넣는 {len(skipped)}건 — 나이스에서 직접 넣어 주세요")
        for s in skipped:
            print(f"    {s['date']}  {s['number']}번 {s['name']} {s['label']}: {s['reason']}")
        print()

    # 키트는 캘린더에서 읽는다 — 그 자리에 파일 내용을 넣는다.
    kit.load_from_calendar = lambda start_date, end_date: records

    # --대조: 넣지 않고, 파일과 나이스 화면이 맞는지만 본다 (월마감 1단계). 읽기 전용.
    if "--대조" in args:
        import verify_attendance
        asyncio.run(verify_attendance.run(date.fromisoformat(data["from"]), date.fromisoformat(data["to"])))
        return 0

    # 키트는 날짜를 안 주면 «이번 달 1일부터» 를 넣는다. 파일 기간의 시작일을 넘겨 준다.
    rest = [a for a in args if a != file_arg]
    if not any(re.match(r"\d{4}-\d{2}-\d{2}$", a) for a in rest):
        rest.append(data["from"])
    sys.argv = [str(HERE / "main.py"), *rest]

    code = asyncio.run(kit.main()) or 0
    if "--dry-run" not in rest:
        print(f"\n🗑   다 넣었으면 {path.name} 은 지워 주세요 (학생 이름과 출결이 들어 있습니다).")
    return code


if __name__ == "__main__":
    sys.exit(main())
