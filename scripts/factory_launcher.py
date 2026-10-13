#!/usr/bin/env python3
"""Web-only startup. Never starts external apps or resumes production queues."""
import argparse
import hashlib
import json
import os
import shlex
from pathlib import Path, PureWindowsPath
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
HTTP = build_opener(ProxyHandler({}))


def command(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    if result.returncode:
        raise RuntimeError(f"本地检查失败: {args[0]} (exit {result.returncode})")
    return result.stdout.strip()


def powershell(script):
    return command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); " + script])


def process_info(pid):
    if WINDOWS:
        raw = powershell(f"Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}' | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine,ExecutablePath | ConvertTo-Json -Compress")
        if not raw:
            return None
        item = json.loads(raw)
        return {"pid": pid, "start": str(item["CreationDate"]), "command": item.get("CommandLine") or "", "executable": item.get("ExecutablePath") or "", "parent": item["ParentProcessId"]}
    try:
        started = command(["ps", "-p", str(pid), "-o", "lstart="])
        cmd = command(["ps", "-p", str(pid), "-o", "command="])
        cwd_rows = command(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines()
    except RuntimeError:
        return None
    parent = int(command(["ps", "-p", str(pid), "-o", "ppid="]))
    return {"pid": pid, "start": started, "command": cmd, "parent": parent, "cwd": next((row[1:] for row in cwd_rows if row.startswith("n")), "")}


def listeners(port):
    if WINDOWS:
        raw = powershell(f"@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq {int(port)} | Select-Object -ExpandProperty OwningProcess -Unique) | ConvertTo-Json -Compress")
        pids = json.loads(raw) if raw else []
        return pids if isinstance(pids, list) else [pids]
    result = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], shell=False, capture_output=True, text=True, timeout=10)
    if result.returncode not in (0, 1):
        raise RuntimeError("无法检查端口所有者；未启动或终止任何服务")
    return sorted({int(value) for value in result.stdout.split()})


def belongs(info, root, role):
    if not info:
        return False
    cmd = info["command"].lower()
    if role == "backend":
        expected = "uvicorn" in cmd and "app.main:app" in cmd
    else:
        expected = "next" in cmd
    if WINDOWS:
        # CIM has no cwd; verify the executable and project-specific command instead.
        cmd = cmd.replace("\\", "/")
        actual_executable = info.get("executable", "").replace("\\", "/").casefold()
        if role == "backend":
            return windows_backend_belongs(info, root)
        next_path = str(root / "frontend/node_modules/next/").replace("\\", "/").casefold()
        return expected and actual_executable.rsplit("/", 1)[-1] == "node.exe" and next_path in cmd
    return expected and Path(info.get("cwd", "")).resolve() == (root / ("backend" if role == "backend" else "frontend")).resolve()


def windows_backend_belongs(info, root):
    def python_command(process):
        try:
            args = shlex.split(process.get("command", ""), posix=False)
        except ValueError:
            return None
        if len(args) < 4 or args[1:4] != ["-m", "uvicorn", "app.main:app"]:
            return None
        return args[0].strip('"').replace("\\", "/").casefold()

    first_argument = python_command(info)
    if first_argument is None:
        return False
    project_python = str(root / "backend/.venv/Scripts/python.exe").replace("\\", "/").casefold()
    actual = info.get("executable", "").replace("\\", "/").casefold()
    if actual == project_python:
        return True
    # Windows venv redirectors may spawn the base interpreter named by pyvenv.cfg.
    try:
        lines = (root / "backend/.venv/pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    values = {key.strip().casefold(): value.strip() for line in lines if "=" in line for key, value in [line.split("=", 1)]}
    base_candidates = set()
    if values.get("home") and PureWindowsPath(values["home"]).is_absolute():
        base_candidates.add(str(PureWindowsPath(values["home"]) / "python.exe").replace("\\", "/").casefold())
    if values.get("executable") and PureWindowsPath(values["executable"]).is_absolute():
        base_candidates.add(values["executable"].replace("\\", "/").casefold())
    if len(base_candidates) != 1 or actual not in base_candidates:
        return False
    if first_argument == project_python:
        return True
    parent_pid = info.get("parent", 0)
    if parent_pid <= 1 or parent_pid == info.get("pid"):
        return False
    parent = process_info(parent_pid)
    if not parent or python_command(parent) != project_python:
        return False
    parent_executable = parent.get("executable", "").replace("\\", "/").casefold()
    return parent_executable == project_python or parent_executable in base_candidates


def descendant(info, parent_pid):
    for _ in range(8):
        if not info:
            return False
        if info["pid"] == parent_pid:
            return True
        parent = info.get("parent", 0)
        if parent <= 1 or parent == info["pid"]:
            return False
        info = process_info(parent)
    return False


def frontend_api_matches(url, expected_port):
    with HTTP.open(url + "api/launcher", timeout=3) as response:
        data = json.load(response)
    api = urlparse(data.get("api_origin", ""))
    same_origin = data.get("api_transport") == "same-origin" and data.get("api_base_path") == "/factory-api"
    compatible_local = not WINDOWS and data.get("api_base_path") == "/"
    return data.get("application") == "AI Video Factory" and api.hostname in ("localhost", "127.0.0.1") and api.port == expected_port and (same_origin or compatible_local)


def backend_database_matches(url, info, database):
    with HTTP.open(url, timeout=3) as response:
        data = json.load(response)
    identity = hashlib.sha256(str(database).encode()).hexdigest()
    if data.get("database_identity"):
        if WINDOWS and (data.get("app_mode") != "integration" or data.get("integration_dry_run") is not True
                        or data.get("production_execution_enabled") is not False or data.get("external_recovery_enabled") is not False):
            return False
        return data["database_identity"] == identity
    if WINDOWS:
        return False
    rows = command(["lsof", "-a", "-p", str(info["pid"]), "-Fn"]).splitlines()
    return "n" + str(database) in rows


def health(url, role):
    try:
        with HTTP.open(url, timeout=2) as response:
            data = response.read(262144)
            if response.status != 200:
                return False
            if role == "backend":
                body = json.loads(data)
                return body.get("status") == "healthy" and body.get("api") == "online"
            return b"AI Video Factory" in data
    except (OSError, ValueError, URLError):
        return False


def wait_ready(url, role, timeout, child=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child and child.poll() is not None:
            raise RuntimeError(f"{role} 提前退出，exit={child.returncode}，请看本次服务日志")
        if health(url, role):
            return
        time.sleep(0.4)
    raise RuntimeError(f"{role} 健康检查超时: {url}")


def active_work(database):
    if not database.exists():
        return ["数据库不存在，不能验证停止安全性"]
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3) as db:
        result = []
        for table in ("processing_runs", "publish_runs", "description_generations", "publishing_tasks", "publishing_step_executions", "publishing_batches"):
            columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if "status" in columns:
                count = db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE upper(status) IN (\'RUNNING\',\'QUEUED\',\'SUBMITTED\',\'SUBMITTING\',\'GENERATING\',\'SYNCING\',\'READING_SOURCE\',\'UNKNOWN_EXTERNAL_REQUEST\',\'UNKNOWN_EXTERNAL_RESULT\')').fetchone()[0]
                if count:
                    result.append(f"{table}: {count} 活动/未确认执行")
            if "error_code" in columns:
                count = db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE error_code LIKE \'UNKNOWN_EXTERNAL%\'').fetchone()[0]
                if count:
                    result.append(f"{table}: {count} 外部请求状态未确认，先对账")
            if "submission_state" in columns:
                count = db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE upper(submission_state) IN (\'SUBMITTING\',\'SUBMITTED\',\'UNKNOWN_EXTERNAL_RESULT\')').fetchone()[0]
                if count:
                    result.append(f"{table}: {count} 已提交发布待核实，不能重交或中断")
        columns = {row[1] for row in db.execute('PRAGMA table_info("production_batches")')}
        if "status" in columns:
            count = db.execute("SELECT COUNT(*) FROM production_batches WHERE upper(status) IN ('RUNNING','QUEUED')").fetchone()[0]
            if count:
                result.append(f"production_batches: {count} 活动批次")
        return result


def load_config(root, path):
    defaults = {"backend_port": 8000, "frontend_port": 3000, "timeout_seconds": 90, "frontend_mode": "dev", "open_browser": True,
                "environment": "acceptance" if WINDOWS else "production", "production_cutover_confirmed": False}
    path = path or root / "config/launcher.local.json"
    if path.exists():
        defaults.update(json.loads(path.read_text(encoding="utf-8")))
    elif path != root / "config/launcher.local.json":
        raise ValueError(f"明确指定的启动配置不存在: {path}；不回退到默认配置")
    if defaults["environment"] not in ("acceptance", "production"):
        raise ValueError("environment 只能 acceptance 或 production")
    if WINDOWS and defaults["environment"] == "production" and defaults["production_cutover_confirmed"] is not True:
        raise ValueError("Windows 正式切换未确认；先用 acceptance 副本验收，并确认 Mac 不再消费队列")
    acceptance = defaults["environment"] == "acceptance"
    defaults.setdefault("database_path", "backend/factory.windows-acceptance.db" if acceptance else "backend/factory.db")
    defaults.setdefault("storage_root", "storage/windows-acceptance" if acceptance else "storage")
    for key in ("backend_port", "frontend_port"):
        if type(defaults[key]) is not int or not 1 <= defaults[key] <= 65535:
            raise ValueError(f"非法端口: {key}")
    if defaults["backend_port"] == defaults["frontend_port"]:
        raise ValueError("前后端端口不能相同")
    if defaults["frontend_mode"] not in ("dev", "start"):
        raise ValueError("frontend_mode 只能 dev 或 start")
    if type(defaults["timeout_seconds"]) not in (int, float) or not 5 <= defaults["timeout_seconds"] <= 600:
        raise ValueError("健康检查 timeout_seconds 必须为 5–600")
    if type(defaults["open_browser"]) is not bool or type(defaults["production_cutover_confirmed"]) is not bool:
        raise ValueError("open_browser 和 production_cutover_confirmed 必须为 JSON boolean")
    return defaults


def runtime_paths(root, config):
    paths = []
    for name in ("database_path", "storage_root"):
        value = Path(config[name])
        paths.append((root / value).resolve() if not value.is_absolute() else value.resolve())
    database, storage = paths
    if config["environment"] == "acceptance":
        if database == (root / "backend/factory.db").resolve() or storage == (root / "storage").resolve():
            raise RuntimeError("隔离验收不能指向正式 factory.db 或正式 storage 根；请使用已验证的副本和隔离路径")
    if not database.is_file():
        raise RuntimeError(f"迁移数据库不存在: {database}；未创建空库。先制作/验证备份及路径映射")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("迁移数据库 quick_check 失败；未启动后端")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("迁移数据库 foreign_key_check 失败；保留副本，先核对外键")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"tasks", "processing_runs", "asset_identities", "collection_identities"}.issubset(tables):
            raise RuntimeError("缺少 Factory 核心表/历史查重身份表；先在隔离副本执行明确迁移，不初始化空库")
    if not storage.is_dir():
        raise RuntimeError(f"媒体根目录不存在: {storage}；按已验证 manifest 传输媒体后重试")
    return database, storage


def redacted_command(info):
    if not info:
        return "UNKNOWN"
    # Unknown arguments may contain credentials without recognisable key names.
    try:
        args = shlex.split(info.get("command", ""), posix=not WINDOWS)
    except ValueError:
        return (info.get("executable") or "UNKNOWN") + " [arguments unavailable]"
    if not args:
        return info.get("executable") or "UNKNOWN"
    safe = [info.get("executable") or args[0]]
    allowed = {"-m", "uvicorn", "app.main:app", "--host", "--hostname", "--port", "--no-access-log", "--reload", "dev", "start", "127.0.0.1", "localhost"}
    for index, arg in enumerate(args[1:], 1):
        value = arg.strip('"').replace("\\", "/")
        if value in allowed or (args[index - 1] == "--port" and value.isdigit() and 1 <= int(value) <= 65535):
            safe.append(value)
        elif value.endswith("/frontend/node_modules/next/dist/bin/next"):
            safe.append(value)
        else:
            safe.append("[REDACTED]")
    return " ".join(safe)[:500]


def executable(value, default):
    target = Path(value) if value else Path(default)
    if not target.is_file():
        raise RuntimeError(f"缺少可执行文件: {target}；请按对应系统教程安装到项目环境")
    return str(target.resolve()) if not target.is_symlink() else str(target.absolute())


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def stop_services(root, state_path, log):
    if not state_path.exists():
        log("没有本启动器记录；不停止现有服务")
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state["root"] != str(root):
        raise RuntimeError("启动记录项目根目录不一致，拒绝停止")
    if any(row["role"] == "backend" and row["managed"] for row in state["services"]):
        work = active_work(Path(state["database_path"]))
        if work:
            raise RuntimeError("拒绝停止活动生产: " + "; ".join(work) + "。先在 UI 安全暂停并等待阶段结束")
    for row in reversed(state["services"]):
        if not row["managed"]:
            log(f"保留复用服务 {row['role']} PID={row['listener']['pid']}")
            continue
        saved_processes = {item["pid"]: item for item in (row["listener"], row["parent"])}
        for saved in saved_processes.values():
            info = process_info(saved["pid"])
            if not info:
                continue
            if info["start"] != saved["start"] or not belongs(info, root, row["role"]):
                raise RuntimeError(f"PID={saved['pid']} 身份改变；拒绝发送退出信号")
            # Never force-kill or signal an unverified process group.
            if WINDOWS:
                powershell(f"Stop-Process -Id {int(saved['pid'])} -ErrorAction Stop")
            else:
                os.kill(saved["pid"], signal.SIGTERM)
            log(f"停止本启动器拥有的 {row['role']} PID={saved['pid']}")
        deadline = time.monotonic() + 15
        while listeners(row["port"]) and time.monotonic() < deadline:
            time.sleep(0.3)
        if listeners(row["port"]):
            raise RuntimeError(f"端口 {row['port']} 仍有监听，未强制终止；请核实日志")
    state["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_state(state_path, state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "check"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        config = load_config(root, args.config)
    except (OSError, ValueError) as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 1
    log_dir = root / "logs/launcher"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    log_path = log_dir / f"{args.action}-{stamp}.log"
    def log(message):
        # Only fixed diagnostic fields are logged, not env, config content or HTTP bodies.
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    state_path = log_dir / f"owned-{config['backend_port']}-{config['frontend_port']}.json"
    lock_path = log_dir / f"command-{config['backend_port']}-{config['frontend_port']}.lock"
    lock_fd = None
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(lock_fd, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            raise RuntimeError(f"另一个启动/停止命令占有 {lock_path}；核实锁内 PID 后再处理，未重复启动") from None
        log(f"启动配置模式={config['environment']}；网页启动不会自动恢复生产/外部队列")
        if args.action == "stop":
            stop_services(root, state_path, log)
            return 0
        python = executable(config.get("python"), root / ("backend/.venv/Scripts/python.exe" if WINDOWS else "backend/.venv/bin/python"))
        node = executable(config.get("node"), shutil.which("node") or root / "node-NOT_FOUND")
        log("Python " + command([python, "--version"]))
        log("Node " + command([node, "--version"]))
        database, storage = runtime_paths(root, config)
        log(f"模式={config['environment']} DB={database} storage={storage}")
        log("冷启动边界: integration/dry-run；PRODUCTION_RECOVERY_ENABLED=false；EXTERNAL_RECOVERY_ENABLED=false")
        log("数据库 quick_check/foreign_key_check/身份表 PASS_LOCAL；不是生产验收")
        # Check pinned web dependencies without importing app.main or touching the database.
        command([python, "-c", "import fastapi,pydantic,pydantic_settings,uvicorn,sqlalchemy,httpx,yaml,playwright,websockets,multipart"])
        next_bin = root / "frontend/node_modules/next/dist/bin/next"
        if not next_bin.is_file():
            raise RuntimeError("缺少 frontend/node_modules/next；在 frontend 执行 npm ci")
        log("Next " + command([node, str(next_bin), "--version"]))
        publishing_config = None
        if WINDOWS or config.get("publishing_config"):
            publishing_config = Path(config.get("publishing_config", "config/publishing.windows.local.yaml"))
            publishing_config = (root / publishing_config).resolve() if not publishing_config.is_absolute() else publishing_config.resolve()
            if not publishing_config.is_file():
                raise RuntimeError(f"本地安全发布配置不存在: {publishing_config}；请建立明确的开发配置，不读取旧生产许可")
            command([python, "-c", "from app.integrations.publishing.config import load_publishing_config; import sys; c=load_publishing_config(sys.argv[1]); assert not c.allow_publish and c.stop_after_upload and not c.description_ai.enabled, 'WEB_START_REQUIRES_EXTERNAL_ACTIONS_DISABLED'", str(publishing_config)], cwd=root / "backend")
            log("本地开发发布策略：公开发布/Description AI disabled；不沿用旧生产许可")
        log("Docker " + ("已找到（本项目网页启动不依赖容器）" if shutil.which("docker") else "未找到（非网页必需，不自动启动）"))
        if shutil.which("docker"):
            try:
                result = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], shell=False, capture_output=True, text=True, timeout=10)
                log("Docker daemon " + ("可连接" if result.returncode == 0 else "未连接（不阻止本地 SQLite 网页）"))
            except subprocess.TimeoutExpired:
                log("Docker daemon 超时（可选组件，不阻止本地 SQLite 网页）")
        if args.action == "check":
            for role, port in (("backend", config["backend_port"]), ("frontend", config["frontend_port"])):
                pids = listeners(port)
                if not pids:
                    log(f"{role} port={port} NOT_RUNNING（检查命令不启动服务）")
                    continue
                info = process_info(pids[0])
                log(f"{role} port={port} PID={pids} command={redacted_command(info)}")
                if len(pids) != 1 or not belongs(info, root, role):
                    raise RuntimeError(f"端口 {port} 所有者不属于可验证项目，未发送任何退出信号")
                url = f"http://127.0.0.1:{port}/" + ("api/health" if role == "backend" else "")
                if not health(url, role):
                    raise RuntimeError(f"{role} 有监听但健康检查失败")
                if role == "backend" and not backend_database_matches(url, info, database):
                    raise RuntimeError("已有后端的数据库或 Windows web-only 恢复边界不匹配")
                if role == "frontend" and not frontend_api_matches(url, config["backend_port"]):
                    raise RuntimeError("已有前端实际 API 配置不匹配，Windows 必须同源 /factory-api")
                log(f"{role} PASS_LOCAL（只读核实、未启动或恢复队列）")
            log("PASS_LOCAL: 网页依赖检查完成；没有启动服务或队列")
            return 0
        env = os.environ.copy()
        env.update(APP_MODE="integration", INTEGRATION_DRY_RUN="true", PRODUCTION_RECOVERY_ENABLED="false", EXTERNAL_RECOVERY_ENABLED="false",
                   DATABASE_URL="sqlite:///" + database.as_posix(), STORAGE_ROOT=str(storage),
                   FRONTEND_ORIGIN=f"http://127.0.0.1:{config['frontend_port']}",
                   NEXT_PUBLIC_API_URL="/factory-api", FACTORY_API_ORIGIN=f"http://127.0.0.1:{config['backend_port']}", NEXT_PUBLIC_APP_MODE="integration")
        if publishing_config:
            env["VIDEO_PUBLISH_CONFIG"] = str(publishing_config)
        previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
        if previous and previous["root"] != str(root):
            raise RuntimeError("既有启动记录根目录不一致；保留进程，不复用所有权")
        state = {"root": str(root), "database_path": str(database), "storage_root": str(storage), "environment": config["environment"],
                 "services": list((previous or {}).get("services", [])), "web_only": True}
        def record(row):
            state["services"] = [item for item in state["services"] if item["role"] != row["role"]] + [row]
        for role, port, cmd, cwd, suffix in (
            ("backend", config["backend_port"], [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(config["backend_port"]), "--no-access-log"], root / "backend", "/api/health"),
            ("frontend", config["frontend_port"], [node, str(next_bin), config["frontend_mode"], "--hostname", "127.0.0.1", "--port", str(config["frontend_port"])], root / "frontend", "/"),
        ):
            url = f"http://127.0.0.1:{port}{suffix}"
            pids = listeners(port)
            if pids:
                info = process_info(pids[0])
                if len(pids) != 1 or not belongs(info, root, role):
                    raise RuntimeError(f"端口冲突 {port} PID={pids} command={redacted_command(info)}（不是本项目可核实的 {role}）。保留进程，未杀进程")
                wait_ready(url, role, config["timeout_seconds"])
                if role == "backend" and not backend_database_matches(url, info, database):
                    raise RuntimeError("已有后端数据库身份/Windows安全恢复标记不匹配；保留进程，拒绝复用")
                if role == "frontend" and not frontend_api_matches(url, config["backend_port"]):
                    raise RuntimeError("已有前端实际 API 地址不匹配；保留进程，按教程重新构建/启动对应配置")
                owned = next((row for row in (previous or {}).get("services", []) if row["role"] == role and row["listener"]["pid"] == info["pid"] and row["listener"]["start"] == info["start"] and row["managed"]), None)
                record(owned or {"role": role, "port": port, "managed": False, "listener": info, "parent": info})
                log(f"复用 {role} PID={info['pid']} port={port}；未重启、未改变现有消费者")
            else:
                if role == "frontend" and config["frontend_mode"] == "start" and not (cwd / ".next/BUILD_ID").exists():
                    raise RuntimeError("frontend_mode=start 需要已有 npm run build；未自动构建或降级")
                output_path = log_dir / f"{role}-{stamp}.log"
                with output_path.open("ab") as output:
                    child = subprocess.Popen(cmd, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                             creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if WINDOWS else 0, start_new_session=not WINDOWS)
                parent = process_info(child.pid)
                if not parent:
                    raise RuntimeError(f"{role} 进程身份核验失败；日志 {output_path}")
                pending = {"role": role, "port": port, "managed": True, "parent": parent, "listener": parent, "log_path": str(output_path)}
                record(pending)
                save_state(state_path, state)
                log(f"启动 {role} PID={child.pid} port={port} log={output_path}")
                wait_ready(url, role, config["timeout_seconds"], child)
                live = listeners(port)
                listener = process_info(live[0]) if len(live) == 1 else None
                if not belongs(listener, root, role) or not descendant(listener, child.pid):
                    raise RuntimeError(f"{role} 监听者身份核验失败；不自动停止未知进程")
                if role == "backend" and not backend_database_matches(url, listener, database):
                    raise RuntimeError("新启动后端的数据库身份不匹配")
                if role == "frontend" and not frontend_api_matches(url, config["backend_port"]):
                    raise RuntimeError("新启动前端实际 API 地址不匹配（生产构建需重建）")
                pending["listener"] = listener
                save_state(state_path, state)
        save_state(state_path, state)
        log(f"PASS_LOCAL: 网页已就绪 http://127.0.0.1:{config['frontend_port']}/；没有恢复队列或外部请求")
        if config["open_browser"] and not args.no_browser:
            opened = webbrowser.open(f"http://127.0.0.1:{config['frontend_port']}/", new=2)
            log("默认浏览器已接收打开请求" if opened else f"浏览器未能自动打开，请访问 http://127.0.0.1:{config['frontend_port']}/")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, sqlite3.Error) as error:
        log("FAIL: " + str(error))
        log(f"诊断日志: {log_path}；已启动服务身份保留，可用本项目停止脚本处理")
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
