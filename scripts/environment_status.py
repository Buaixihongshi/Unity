"""Standard-library-only local inventory, including services predating identity."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.runtime_identity import frontend_identity, git, source_snapshot

DEFAULT_PAIRS = list(dict.fromkeys((p["backend_port"], p["frontend_port"]) for p in
    json.loads((ROOT / "config/environments.json").read_text()).values()))


def command(*args: str) -> str:
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def get_json(port: int, path: str) -> dict | None:
    # Never follow a redirect out of the local inspection boundary.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        probe = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-AgentClub-Probe": "1"})
        with opener.open(probe, timeout=1) as response:
            data = json.loads(response.read(256_000))
            return data if isinstance(data, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def listeners() -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {}
    pid = None
    for line in command("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn").splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            match = re.search(r":(\d+)$", line)
            if match:
                result.setdefault(int(match.group(1)), []).append({"pid": pid})
    return result


def process_info(process: dict) -> dict:
    pid = process["pid"]
    cwd = next((x[1:] for x in command("lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn").splitlines() if x.startswith("n")), None)
    return {"pid": pid, "checkout": cwd,
            "started_at_local": command("ps", "-p", str(pid), "-o", "lstart=") or None}


def worktrees() -> list[dict]:
    rows = []
    for block in (git(ROOT, "worktree", "list", "--porcelain") or "").split("\n\n"):
        row = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree": row["path"] = value
            elif key == "prunable": row["prunable"] = True
        if row.get("path"): rows.append(row)
    return rows


def checkout_info(path: Path, prunable: bool = False) -> dict:
    exists = path.is_dir()
    snapshot = source_snapshot(path) if exists else {}
    return {"path": str(path), "exists": exists, "prunable": prunable, **snapshot}


def inspect_service(pair: tuple[int, int], listening: dict) -> dict:
    backend, frontend = pair
    health = get_json(backend, "/api/health")
    front = get_json(frontend, "/__environment") if frontend is not None else None
    via_front = get_json(frontend, "/api/health") if frontend is not None else None
    # Keep only identity fields; do not archive conversations or provider config.
    return {
        "backend_port": backend, "frontend_port": frontend,
        "backend_processes": [process_info(p) for p in listening.get(backend, [])],
        "frontend_processes": [process_info(p) for p in listening.get(frontend, [])],
        "healthy": health.get("status") == "ok" if health else False,
        "runtime": health.get("runtime") if health else None,
        "frontend": front,
        "proxy_runtime": via_front.get("runtime") if via_front else None,
        "proxy_healthy": via_front.get("status") == "ok" if via_front else False,
    }


def inventory(extra: list[str], pairs: list[tuple[int, int]]) -> dict:
    registered = worktrees()
    candidates = {str(ROOT): False, **{r["path"]: r.get("prunable", False) for r in registered}}
    # Discover the existing sibling clones without walking the user's home.
    for row in registered:
        for name in ("development", "dev", "test", "runtime"):
            path = Path(row["path"]).parent / "agentclub-environments" / name
            if (path / ".git").exists(): candidates[str(path)] = False
    candidates.update({str(Path(p).expanduser().resolve()): False for p in extra})
    from scripts.environment_common import home
    pairs = list(pairs)
    for pointer in (home() / ".instances").glob("*.json"):
        try:
            record = json.loads(Path(json.loads(pointer.read_text())["record"]).read_text())
            candidates.setdefault(record["checkout"], False)
            pair = (record["backend_port"], record["frontend_port"])
            if record.get("status") != "stopped" and pair not in pairs: pairs.append(pair)
        except (OSError, ValueError, KeyError): pass
    listening = listeners()
    with ThreadPoolExecutor(max_workers=4) as pool:
        services = list(pool.map(lambda pair: inspect_service(pair, listening), pairs))
        for service in services:
            for proc in service["backend_processes"] + service["frontend_processes"]:
                if proc["checkout"] and (Path(proc["checkout"]) / ".git").exists():
                    candidates.setdefault(proc["checkout"], False)
        checkouts = list(pool.map(lambda item: checkout_info(Path(item[0]), item[1]), candidates.items()))
    return {"registered_worktrees": len(registered), "checkouts": checkouts, "services": services}


def render(report: dict) -> str:
    lines = [f"登记 worktree：{report['registered_worktrees']}；本次只读检查，不启动或清理服务。"]
    for service in report["services"]:
        lines += ["", f"服务 {service['backend_port']} / {service['frontend_port']}："
                  + ("健康" if service["healthy"] else "未就绪 / 未运行")]
        runtime = service["runtime"]
        if runtime:
            started = runtime["started"]
            lines += [f"  环境：{started['environment']}；启动 SHA：{started.get('git_sha') or '未知'}；PID：{started['pid']}",
                      f"  启动：{started['captured_at']}；Python：{started['python']}；演示开关：{started['use_fake_cli']}",
                      f"  目录：{started['checkout']}",
                      f"  当前磁盘 SHA：{runtime['current'].get('git_sha') or '未知'}；dirty：{runtime['current'].get('dirty')}；启动后源码变化：{runtime['source_changed']}",
                      f"  状态路径：DB={started['database']}；config={started['config']}；artifacts={started['artifacts']}"]
            if started.get("runtime_dir"): lines += [f"  日志：{started['runtime_dir']}"]
            if runtime.get("activity") is not None: lines += [f"  活跃工作：{runtime['activity']}；停止接收写操作：{runtime.get('draining')}"]
        else:
            lines += ["  启动身份未知（旧服务或未运行）；磁盘 HEAD 不能证明运行版本。"]
        front = service["frontend"]
        lines += ["  仅后端实例，未启用前端。" if service["frontend_port"] is None else
                  f"  前端启动/构建 SHA：{front.get('git_sha') or '未知'}" if front else "  前端启动/构建身份未知。"]
        proxy = service["proxy_runtime"]
        if runtime and proxy:
            lines += [f"  前端代理命中本后端：{proxy['started'] == runtime['started']}"]
        elif service["frontend_port"] is not None:
            lines += [f"  前端代理健康：{service['proxy_healthy']}（缺少身份时不能确认版本一致）"]
        for kind in ("backend", "frontend"):
            for proc in service[f"{kind}_processes"]:
                lines += [f"  {kind} PID={proc['pid']}；启动={proc['started_at_local']}；cwd={proc['checkout']}"]
    lines += ["", "工作目录（dirty 包括未提交及未忽略的新文件；远端分支消失不代表可以删除）："]
    for row in report["checkouts"]:
        lines += [f"  {row['path']} | {row.get('branch') or 'detached/未知'} | {(row.get('git_sha') or '未知')[:12]} | dirty={row.get('dirty')} | exists={row['exists']} | prunable={row['prunable']}"]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "identity"))
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON；可能含本机绝对路径")
    parser.add_argument("--checkout", action="append", default=[], help="追加独立 clone（可重复）")
    parser.add_argument("--ports", action="append", default=[], help="追加 backend:frontend 端口对")
    args = parser.parse_args()
    pairs = list(DEFAULT_PAIRS)
    for value in args.ports:
        if not re.fullmatch(r"\d+:\d+", value): parser.error("--ports 应为 backend:frontend")
        pair = tuple(map(int, value.split(":")))
        if not all(1 <= p <= 65535 for p in pair): parser.error("端口必须在 1..65535")
        if pair not in pairs: pairs.append(pair)
    if args.action == "identity":
        print(json.dumps(frontend_identity(), ensure_ascii=False))
        return
    report = inventory(args.checkout, pairs)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))


if __name__ == "__main__":
    main()
