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

파일을 지정하지 않으면 이 폴더와 「다운로드」 폴더에서 가장 최근의 attendance_neis_*.json 을 쓴다.
키트의 다른 옵션(--dry-run, --future)은 그대로 넘긴다.
"""

import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

FORMAT = "attendance-app/neis-v1"
PATTERN = "attendance_neis_*.json"
HERE = Path(__file__).resolve().parent


def find_latest_file() -> Path | None:
    candidates = list(HERE.glob(PATTERN)) + list((Path.home() / "Downloads").glob(PATTERN))
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


def main() -> int:
    if not (HERE / "main.py").exists():
        print("❌  이 파일을 neis-automation-kit 폴더(main.py 가 있는 곳)에 두고 실행하세요.")
        return 1
    sys.path.insert(0, str(HERE))
    import main as kit  # 키트의 나이스 입력기

    args = sys.argv[1:]
    file_arg = next((a for a in args if a.lower().endswith(".json")), None)
    path = Path(file_arg) if file_arg else find_latest_file()
    if not path or not path.exists():
        print("❌  attendance_neis_*.json 파일을 찾지 못했습니다.")
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
