#!/usr/bin/env python3
"""
출결 앱 ↔ 학교 PC 자동 연동 (깃허브 비공개 저장소 경유)

휴대폰 출결 앱에서 「나이스 반영」 을 누르면, 앱이 출결 파일을 선생님 비밀번호로 잠가
비공개 깃허브 저장소(inbox/)에 올린다. 학교 PC 에서는 이 스크립트가 몇 분마다 inbox 를 확인해
새 파일이 있으면 풀어서 나이스에 넣고(app_to_neis 와 같은 방식), 결과(건수만, 이름 없음)를
results/ 에 올린 뒤 inbox 파일을 지운다. 휴대폰 앱은 results/ 를 읽어 「반영 완료」 를 보여준다.

    .\\.venv\\Scripts\\python.exe -X utf8 app_sync.py --설정      ← 저장소·토큰·비밀번호 (새 창에서 입력)
    .\\.venv\\Scripts\\python.exe -X utf8 app_sync.py --확인      ← 연결 확인, 기다리는 파일 수 (읽기만)
    .\\.venv\\Scripts\\python.exe -X utf8 app_sync.py --한번      ← 지금 한 번 확인해서 있으면 넣기
    .\\.venv\\Scripts\\python.exe -X utf8 app_sync.py --켜기 3    ← 평일 07:30~18:00 에 3분마다 자동 확인
    .\\.venv\\Scripts\\python.exe -X utf8 app_sync.py --끄기

토큰·비밀번호는 윈도우 자격 증명 관리자(keyring)에만 둔다. 학생 이름이 든 기록은
%LOCALAPPDATA%\\neis-automation\\app_runs\\ 에만 남는다 (저장소 밖).
마감은 하지 않는다 — 선생님이 나이스에서 확인하고 마감한다.
"""

import asyncio
import base64
import builtins
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import keyring          # noqa: E402
import teacher_config   # noqa: E402

API = os.environ.get("NEIS_SYNC_API", "https://api.github.com")   # 시험용으로만 바꾼다
ENC_FORMAT = "attendance-app/neis-enc-v1"
RESULT_FORMAT = "attendance-app/neis-result-v1"
KEY_TOKEN = "app-sync-token"
KEY_PASS = "app-sync-passphrase"
DEFAULT_REPO = "YeoBBun/school-attendance-sync"
TASK_NAME = "AttendanceAppSync"
LOCK_MAX_AGE = 40 * 60   # 앞 실행이 이보다 오래 잡고 있으면 죽은 것으로 본다


# ─── 설정 ────────────────────────────────────────────────────

def _config_file() -> Path:
    return teacher_config.CONFIG_DIR / "app_sync.json"


def load_sync_config() -> tuple[str, str, str]:
    """(저장소, 토큰, 비밀번호). 없으면 SystemExit."""
    f = _config_file()
    repo = json.loads(f.read_text(encoding="utf-8")).get("repo") if f.exists() else None
    token = keyring.get_password(teacher_config.KEYRING_SERVICE, KEY_TOKEN)
    passphrase = keyring.get_password(teacher_config.KEYRING_SERVICE, KEY_PASS)
    if not (repo and token and passphrase):
        raise SystemExit("❌  연동 설정이 없습니다. 먼저  app_sync.py --설정  을 돌려 주세요.")
    return repo, token, passphrase


def setup() -> int:
    """새 창에서 저장소·토큰·비밀번호를 받는다 (대화에 남지 않게)."""
    if os.name == "nt" and not os.environ.get("NEIS_SYNC_WINDOW"):
        env = dict(os.environ, NEIS_SYNC_WINDOW="1")
        print("🪟  설정 창을 새로 띄웁니다. 작업표시줄에 새로 뜬 검은 창에서 답해 주세요.", flush=True)
        proc = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__)), "--설정"], env=env,
                                creationflags=subprocess.CREATE_NEW_CONSOLE, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        while proc.poll() is None:
            time.sleep(2)
        return check()
    if os.environ.get("NEIS_SYNC_WINDOW"):
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = sys.stdout
    print("=" * 56)
    print("📱→💻  출결 앱 자동 연동 설정")
    print("=" * 56)
    f = _config_file()
    old = json.loads(f.read_text(encoding="utf-8")).get("repo") if f.exists() else DEFAULT_REPO
    repo = input(f"깃허브 저장소 [{old}]: ").strip() or old
    token = teacher_config.secret_input("깃허브 토큰 (그대로 두려면 Enter): ").strip()
    passphrase = teacher_config.secret_input("앱에서 정한 잠금 비밀번호 (그대로 두려면 Enter): ").strip()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"repo": repo}, ensure_ascii=False), encoding="utf-8")
    if token:
        keyring.set_password(teacher_config.KEYRING_SERVICE, KEY_TOKEN, token)
    if passphrase:
        keyring.set_password(teacher_config.KEYRING_SERVICE, KEY_PASS, passphrase)
    print("\n✅  저장했습니다. 이 창은 10초 뒤 닫힙니다.")
    time.sleep(10)
    return 0


# ─── 깃허브 ──────────────────────────────────────────────────

def gh(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, object]:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "neis-automation-kit",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, None


def list_inbox(repo: str, token: str) -> list[dict]:
    status, data = gh("GET", f"/repos/{repo}/contents/inbox", token)
    if status == 404:
        return []   # 폴더가 아직 없거나 비었다
    if status != 200:
        raise RuntimeError(f"깃허브 inbox 를 읽지 못했습니다 (HTTP {status})")
    return sorted((x for x in data if x.get("type") == "file" and x["name"].endswith(".json")), key=lambda x: x["name"])


def read_file(repo: str, token: str, path: str) -> tuple[dict, str]:
    status, data = gh("GET", f"/repos/{repo}/contents/{path}", token)
    if status != 200:
        raise RuntimeError(f"{path} 를 읽지 못했습니다 (HTTP {status})")
    return json.loads(base64.b64decode(data["content"])), data["sha"]


def delete_file(repo: str, token: str, path: str, sha: str) -> None:
    gh("DELETE", f"/repos/{repo}/contents/{path}", token, {"message": f"처리 완료: {path}", "sha": sha})


def put_result(repo: str, token: str, name: str, result: dict) -> None:
    content = base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
    gh("PUT", f"/repos/{repo}/contents/results/{name}", token, {"message": f"결과: {name}", "content": content})


# ─── 잠금 해제 ────────────────────────────────────────────────

def decrypt(envelope: dict, passphrase: str) -> dict:
    """앱(WebCrypto)이 잠근 파일을 푼다: PBKDF2-SHA256 → AES-256-GCM."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if envelope.get("format") != ENC_FORMAT:
        raise ValueError("출결 앱이 잠근 파일이 아닙니다")
    salt = base64.b64decode(envelope["salt"])
    iv = base64.b64decode(envelope["iv"])
    ct = base64.b64decode(envelope["ct"])
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, int(envelope["iter"]), 32)
    return json.loads(AESGCM(key).decrypt(iv, ct, None))


# ─── 한 번 확인해서 넣기 ──────────────────────────────────────

class _Tee:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s]
    def write(self, text):
        for s in self.streams:
            s.write(text)
    def flush(self):
        for s in self.streams:
            s.flush()


def _lock_path() -> Path:
    return teacher_config.CONFIG_DIR / "app_sync.lock"


def run_once(dry_run: bool = False) -> int:
    repo, token, passphrase = load_sync_config()
    inbox = list_inbox(repo, token)
    if not inbox:
        print("✅  기다리는 출결이 없습니다")
        return 0

    lock = _lock_path()
    if lock.exists() and time.time() - lock.stat().st_mtime < LOCK_MAX_AGE:
        print("⏳  앞의 입력이 아직 진행 중입니다 — 이번엔 건너뜁니다")
        return 0
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))

    runs = teacher_config.CONFIG_DIR / "app_runs"
    runs.mkdir(parents=True, exist_ok=True)
    log = open(runs / f"{datetime.now():%Y%m%d-%H%M%S}-sync.log", "w", encoding="utf-8")
    out, err = sys.stdout, sys.stderr
    sys.stdout = _Tee(out, log)
    sys.stderr = _Tee(err, log)
    builtins.input = lambda *a, **k: ""   # 아무도 없는 자리 — 키트가 Enter 를 기다리며 멈추지 않게

    try:
        import app_to_neis
        import main as kit

        jobs = []   # (이름, sha, data)
        for item in inbox:
            name = item["name"]
            try:
                envelope, sha = read_file(repo, token, f"inbox/{name}")
                data = decrypt(envelope, passphrase)
                if data.get("format") != app_to_neis.FORMAT:
                    raise ValueError("나이스 입력 파일이 아닙니다")
                jobs.append((name, sha, data))
                print(f"📥  {name}  ({data['from']} ~ {data['to']}, {len(data['records'])}건)")
            except Exception as e:   # 비밀번호가 다르거나 깨진 파일 — 결과에 알리고 치운다
                msg = "잠금 비밀번호가 학교 PC 설정과 다릅니다" if type(e).__name__ == "InvalidTag" else f"파일을 열지 못했습니다 ({type(e).__name__})"
                print(f"❌  {name}: {msg}")
                if not dry_run:
                    put_result(repo, token, name, {"format": RESULT_FORMAT, "status": "error", "message": msg,
                                                   "count": 0, "manual": 0, "finishedAt": datetime.now().isoformat()})
                    _, sha = read_file(repo, token, f"inbox/{name}")
                    delete_file(repo, token, f"inbox/{name}", sha)
        if not jobs:
            return 1

        # 여러 건이면 합쳐서 한 번에 넣는다 (같은 날·학생·분류는 나중 것)
        merged: dict[tuple, dict] = {}
        skipped = []
        for _, _, data in jobs:
            for r in app_to_neis.parse_app_data(data):
                merged[(r["start_date"], r["number"], r["att_type"])] = r
            skipped += data.get("skipped", [])
        records = list(merged.values())
        start = min(data["from"] for _, _, data in jobs)
        if skipped:
            print(f"⚠️   자동으로 못 넣는 {len(skipped)}건 — 나이스에서 직접 넣어 주세요")

        kit.load_from_calendar = lambda start_date, end_date: records
        # 미래 날짜는 넣지 않는다 (키트 기본). 미리 넣고 싶은 건 그날 다시 보내면 된다.
        future = sum(1 for r in records if r["start_date"] > datetime.now().date())
        if future:
            print(f"⏭   미래 날짜 {future}건은 넣지 않습니다 — 그날 다시 보내 주세요")
        sys.argv = [str(HERE / "main.py"), *(["--dry-run"] if dry_run else []), start]
        code = asyncio.run(kit.main()) or 0
        if dry_run:
            return code

        status = "done" if code == 0 else "check"
        message = "나이스에 넣었어요. 확인 후 마감해 주세요." if code == 0 else "못 넣은 건이 있어요. 학교 PC 기록이나 나이스 화면을 확인해 주세요."
        if future:
            message += f" 미래 날짜 {future}건은 넣지 않았어요 — 그날 다시 보내 주세요."
        for name, sha, data in jobs:
            put_result(repo, token, name, {
                "format": RESULT_FORMAT, "status": status, "message": message,
                "count": len(data["records"]), "manual": len(data.get("skipped", [])),
                "finishedAt": datetime.now().isoformat(),
            })
            delete_file(repo, token, f"inbox/{name}", sha)
        print(f"📤  결과를 휴대폰으로 보냈습니다 ({status})")
        return code
    finally:
        sys.stdout, sys.stderr = out, err
        log.close()
        lock.unlink(missing_ok=True)


def check() -> int:
    """연결과 설정만 확인한다 (읽기만)."""
    repo, token, passphrase = load_sync_config()
    status, _ = gh("GET", f"/repos/{repo}", token)
    if status != 200:
        print(f"❌  저장소 {repo} 에 접속하지 못했습니다 (HTTP {status}). 토큰 권한·저장소 이름을 확인해 주세요.")
        return 1
    print(f"✅  깃허브 저장소 {repo} 연결됨 · 토큰 ✅ · 잠금 비밀번호 {'✅' if passphrase else '❌'}")
    print(f"📥  기다리는 출결 파일 {len(list_inbox(repo, token))}개")
    return 0


# ─── 예약 (작업 스케줄러) ──────────────────────────────────────

def enable(every: int) -> int:
    """평일 07:30~18:00 에 every 분마다 조용히 확인하도록 등록한다 (로그인 중일 때만).
    확인만 할 때는 창이 뜨지 않게 pythonw 로 돈다. 새 출결이 있을 때만 크롬이 뜬다."""
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    command = f'"{pyw if pyw.exists() else py}" -X utf8 "{HERE / "app_sync.py"}" --한번'
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command, "/SC", "WEEKLY",
         "/D", "MON,TUE,WED,THU,FRI", "/ST", "07:30", "/RI", str(every), "/DU", "10:30", "/IT", "/F"],
        capture_output=True, text=True,
    )
    print(result.stdout or result.stderr)
    if result.returncode == 0:
        print(f"✅  평일 07:30~18:00 에 {every}분마다 휴대폰에서 보낸 출결을 확인해 나이스에 넣습니다.")
        print("    PC가 켜져 있고 윈도우에 로그인돼 있어야 합니다. 마감은 선생님이 하세요.")
    return result.returncode


def disable() -> int:
    result = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
    print(result.stdout or result.stderr)
    return result.returncode


def main() -> int:
    # pythonw 로 돌면 화면이 없다 — print 가 죽지 않게 받아 줄 곳을 만든다
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    args = sys.argv[1:]
    if "--설정" in args:
        return setup()
    if "--확인" in args:
        return check()
    if "--켜기" in args:
        i = args.index("--켜기")
        every = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 3
        return enable(every)
    if "--끄기" in args:
        return disable()
    if "--한번" in args:
        return run_once(dry_run="--dry-run" in args)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
