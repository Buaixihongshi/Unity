import gc
import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import asyncio

import pytest

from backend.app.evaluation import exam_room
from backend.app.evaluation.review_exam import SheetError, grade, judge_queue, load_key, parse_sheet

KEY = load_key({"paper_id": "p1", "kind": "bug", "bugs": [
    {"id": "b1", "file": "app/core.py", "line_start": 40, "line_end": 42, "description": "重启后锁未释放"},
]})
CLEAN = load_key({"paper_id": "p2", "kind": "clean", "bugs": []})


def sheet(*findings, verdict="issues"):
    return json.dumps({"verdict": verdict, "findings": [
        {"file": f, "line_start": a, "line_end": b, "summary": "问题"} for f, a, b in findings]})


def test_fenced_answer_is_read_but_two_blocks_are_rejected():
    body = sheet(("app/core.py", 41, 41))
    assert parse_sheet(f"说明文字\n```json\n{body}\n```\n").verdict == "issues"
    with pytest.raises(SheetError, match="more than one"):
        parse_sheet(f"```json\n{body}\n```\n```json\n{body}\n```")


@pytest.mark.parametrize("raw", [
    "没有问题",
    json.dumps({"verdict": "no_issues", "findings": [{"file": "a.py", "line_start": 1, "line_end": 1, "summary": "x"}]}),
    json.dumps({"verdict": "issues", "findings": []}),
    json.dumps({"verdict": "maybe", "findings": []}),
    json.dumps({"verdict": "no_issues", "findings": [], "notes": "extra"}),
    sheet(("../etc/passwd", 1, 1)),
    sheet(("/abs/core.py", 1, 1)),
    sheet(("app/core.py", 5, 4)),
    sheet(("app/core.py", 1, 400)),
    json.dumps({"verdict": "issues", "findings": [{"file": "a.py", "line_start": True, "line_end": 1, "summary": "x"}]}),
])
def test_malformed_sheet_is_invalid_and_misses_every_bug(raw):
    result = grade(raw, KEY)
    assert not result.valid and result.error
    assert result.missed == ["b1"] and not result.hits


def test_location_window_hit_duplicate_and_false_positive():
    result = grade(sheet(("app/core.py", 45, 47), ("./app/core.py", 40, 40), ("app/other.py", 41, 41),
                         ("app/core.py", 80, 82)), KEY)
    assert [hit["bug"] for hit in result.hits] == ["b1"]
    assert [item["finding"] for item in result.duplicates] == [1]
    assert [item["finding"] for item in result.false_positives] == [2, 3]
    assert result.missed == []


def test_finding_outside_tolerance_misses():
    result = grade(sheet(("app/core.py", 48, 50)), KEY)
    assert result.missed == ["b1"] and len(result.false_positives) == 1


def test_clean_paper_counts_every_finding_as_false_positive():
    assert grade(sheet(verdict="no_issues"), CLEAN).as_dict()["false_positives"] == []
    assert len(grade(sheet(("a.py", 1, 2)), CLEAN).false_positives) == 1


def test_key_rejects_bug_paper_without_bugs():
    with pytest.raises(ValueError):
        load_key({"paper_id": "x", "kind": "bug", "bugs": []})


def test_judge_queue_hides_arm_and_keeps_mapping_separate():
    hit = grade(sheet(("app/core.py", 41, 41)), KEY)
    queue, mapping = judge_queue([("cross_vendor", hit, KEY)], seed=1)
    assert "cross_vendor" not in json.dumps(queue, ensure_ascii=False)
    assert mapping[queue[0]["id"]]["arm"] == "cross_vendor"


def test_proxy_only_tunnels_allowlisted_hosts_on_443():
    proxy = exam_room.AllowlistProxy(frozenset({"API.anthropic.com"}))
    assert proxy.permits("api.anthropic.com", 443)
    assert not proxy.permits("api.anthropic.com", 80)
    assert not proxy.permits("github.com", 443)
    assert not proxy.permits("127.0.0.1", 443)


def test_profile_denies_by_default_and_opens_only_room_and_runtime(tmp_path):
    room = tmp_path / "room"
    room.mkdir()
    profile = exam_room.render_profile(room, 4567)
    assert "(deny default)" in profile and "(allow default)" not in profile
    assert '(import "system.sb")' in profile
    assert '(allow network-outbound (remote ip "localhost:4567"))' in profile
    # No Mach service beyond the system.sb baseline (pasteboard, keychain, ...).
    assert "mach-lookup" not in profile
    # system.sb allows sysctl-read; the process table must be denied after it, and nothing re-allows it.
    assert profile.index('(deny sysctl-read (sysctl-name-prefix "kern.proc"))') > profile.index('(import "system.sb")')
    assert "(allow sysctl-read" not in profile
    reads = next(line for line in profile.splitlines() if line.startswith("(allow file-read* "))
    assert f'(subpath "{room.resolve()}")' in reads and "/private/var/tmp" not in reads
    assert '(subpath "/Users")' not in reads and '(subpath "/usr/local")' not in reads
    metadata = next(line for line in profile.splitlines() if line.startswith("(allow file-read-metadata "))
    assert "(subpath" not in metadata and f'(literal "{room.resolve().parent}")' in metadata


@pytest.mark.parametrize("address, public", [
    ("93.184.216.34", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("10.0.0.8", False), ("192.168.1.2", False), ("172.16.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1%en0", False), ("fc00::1", False), ("::", False),
    ("::ffff:127.0.0.1", False), ("::ffff:93.184.216.34", True), ("64:ff9b::7f00:1", False),
    ("2002:7f00:1::", False), ("not-an-ip", False),
])
def test_public_address_rejects_loopback_private_link_local_and_wrapped_forms(address, public):
    assert exam_room.public_address(address) is public


def _connect_status(port, target):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        return conn.recv(256).split(b"\r\n", 1)[0].decode()


def _fake_resolver(*answers):
    calls = []

    async def resolve(host, port):
        calls.append(host)
        return list(answers[min(len(calls), len(answers)) - 1])
    return resolve, calls


@pytest.mark.parametrize("answer", [
    ["127.0.0.1"], ["::1"], ["10.1.2.3"], ["fe80::1"], ["::ffff:127.0.0.1"],
    ["93.184.216.34", "127.0.0.1"], ["2606:4700:4700::1111", "fc00::1"], [],
])
def test_proxy_refuses_allowlisted_host_resolving_to_non_public_address(monkeypatch, answer):
    opened = []

    async def never_open(address, port):
        opened.append(address)
        raise AssertionError("must not connect")
    monkeypatch.setattr(exam_room, "_open_upstream", never_open)
    resolve, _ = _fake_resolver(answer)
    with exam_room.AllowlistProxy(frozenset({"api.exam.invalid"}), resolve=resolve) as proxy:
        assert _connect_status(proxy.port, "api.exam.invalid:443").startswith("HTTP/1.1 403")
    assert opened == [] and proxy.attempts[0]["allowed"] is False


@pytest.mark.parametrize("text", ["127.0.0.0/8", "10.0.0.0/8", "0.0.0.0/0", "198.16.0.0/14", "169.254.0.0/16",
                                  "::ffff:198.18.0.0/111", "fd00::/8"])
def test_fake_ip_ranges_outside_benchmark_block_are_refused(text):
    with pytest.raises(ValueError):
        exam_room.fake_ip_networks([text])


@pytest.mark.parametrize("fake_ip, answer, allowed", [
    ((), ["198.18.1.37"], False),                                 # #98: off by default
    (("198.18.0.0/15",), ["198.18.1.37"], True),                  # fake-IP host
    (("198.18.0.0/15",), ["198.18.1.37", "127.0.0.1"], False),    # loopback stays refused
    (("198.18.0.0/16",), ["198.19.0.1"], False),                  # outside the trusted subnet
])
def test_proxy_trusts_fake_ip_answers_only_when_enabled(monkeypatch, fake_ip, answer, allowed):
    opened = []

    async def refuse_open(address, port):
        opened.append(address)
        raise OSError("fixture: no upstream")
    monkeypatch.setattr(exam_room, "_open_upstream", refuse_open)
    resolve, _ = _fake_resolver(answer)
    with exam_room.AllowlistProxy(frozenset({"api.exam.invalid"}), resolve=resolve,
                                  fake_ip=exam_room.fake_ip_networks(fake_ip)) as proxy:
        _connect_status(proxy.port, "api.exam.invalid:443")
    assert proxy.attempts[0]["allowed"] is allowed
    assert opened == (answer[:1] if allowed else [])


def test_proxy_connects_to_the_checked_address_without_resolving_again(monkeypatch):
    fixture = socket.socket()
    fixture.bind(("127.0.0.1", 0))
    fixture.listen()
    threading.Thread(target=lambda: fixture.accept()[0].sendall(b"fixture-upstream"), daemon=True).start()
    opened = []

    async def open_fixture(address, port):
        opened.append((address, port))
        return await asyncio.open_connection(*fixture.getsockname())
    monkeypatch.setattr(exam_room, "_open_upstream", open_fixture)
    # A second lookup would rebind to loopback; the proxy must use the first, checked answer.
    resolve, calls = _fake_resolver(["93.184.216.34"], ["127.0.0.1"])
    with exam_room.AllowlistProxy(frozenset({"api.exam.invalid"}), resolve=resolve) as proxy:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as conn:
            conn.sendall(b"CONNECT api.exam.invalid:443 HTTP/1.1\r\n\r\n")
            received = b""
            while b"fixture-upstream" not in received and (chunk := conn.recv(256)):
                received += chunk
    fixture.close()
    assert received.startswith(b"HTTP/1.1 200") and b"fixture-upstream" in received
    assert opened == [("93.184.216.34", 443)] and calls == ["api.exam.invalid"]


def test_proxy_exit_leaves_no_pending_handler(caplog):
    caplog.set_level(logging.ERROR, logger="asyncio")
    with exam_room.AllowlistProxy(frozenset()) as proxy:
        idle = socket.create_connection(("127.0.0.1", proxy.port))
        time.sleep(0.2)  # handler is now waiting for the request line
    idle.close()
    gc.collect()
    assert "pending" not in caplog.text and "Event loop is closed" not in caplog.text


def _repo(path):
    path.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    (path / "app.py").write_text("print('pr')\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "pr"], cwd=path, check=True)
    (path / "app.py").write_text("print('fixed')\n")
    subprocess.run(["git", "commit", "-qam", "fix: SECRET-FIX-TITLE"], cwd=path, check=True)
    return path


def _accept_until_closed(server):
    try:
        while True:
            server.accept()[0].close()
    except OSError:
        pass


@pytest.mark.skipif(not exam_room.sandbox_available(), reason="needs macOS sandbox-exec")
def test_fake_examinee_is_blocked_in_room_but_leaks_without_sandbox(tmp_path):
    source = _repo(tmp_path / "source")
    secret = tmp_path / "private" / "key.json"
    secret.parent.mkdir()
    secret.write_text("{}")
    # #95: shared roots outside the old deny list; the room itself sits under /private/tmp or /private/var/folders.
    var_tmp = Path(tempfile.mkdtemp(prefix="review-exam-test-", dir="/private/var/tmp"))
    (var_tmp / "synthetic-answer.json").write_text("fixture-only")
    hub = socket.socket()
    hub.bind(("127.0.0.1", 0))
    hub.listen()
    hub_port = hub.getsockname()[1]
    threading.Thread(target=_accept_until_closed, args=(hub,), daemon=True).start()
    room = exam_room.build_room(source, "HEAD~2", "HEAD~1", tmp_path / "room", instructions="task")
    assert exam_room.scan_canaries(room, ["SECRET-FIX-TITLE"]) == []
    script = exam_room.copy_probe(room)
    config = script.parent / "config.json"
    config.write_text(json.dumps({
        "forbidden_files": [["answer_key", str(secret)], ["source_git", str(source / ".git/HEAD")],
                            ["var_tmp", str(var_tmp / "synthetic-answer.json")]],
        "forbidden_dirs": [["answer_dir", str(secret.parent)]],
        "hub_ports": [hub_port], "canaries": ["SECRET-FIX-TITLE"],
    }))
    command = ["/usr/bin/python3", str(script), str(config)]

    try:
        with exam_room.synthetic_host_process("test") as fixture:
            config.write_text(json.dumps({**json.loads(config.read_text()), "host_fixture": fixture}))
            boxed = json.loads(exam_room.run_in_room(command, room, timeout=120)["stdout"])
            control = json.loads(exam_room.run_in_room(command, room, sandbox=False, timeout=120)["stdout"])
    finally:
        hub.close()
        (var_tmp / "synthetic-answer.json").unlink()
        var_tmp.rmdir()
    assert boxed["passed"], [item for item in boxed["checks"] if not item["ok"]]
    leaked = {item["id"] for item in control["checks"] if not item["ok"]}
    # The same checks must see each leak without the sandbox, otherwise "blocked" proves nothing.
    assert {"read:answer_key", "stat:answer_key", "read:source_git", "list:answer_dir", "read:var_tmp",
            f"hub_direct:{hub_port}", "write_outside_room", "mach:com.apple.pasteboard.1",
            "mach:com.apple.SecurityServer", "clipboard_pbpaste", "keychain_list", "host_preferences",
            "process_list_sysctl", "host_procargs", "host_procargs_legacy"} <= leaked


def _probe_procargs(room, fixture, *, sandbox):
    script = exam_room.copy_probe(room)
    config = script.parent / "procargs.json"
    config.write_text(json.dumps({"host_fixture": fixture}))
    result = exam_room.run_in_room(["/usr/bin/python3", str(script), str(config)], room, sandbox=sandbox, timeout=120)
    # A crashed or garbled probe must fail here, not pass as "blocked".
    assert result["returncode"] == 0, result["stderr"][-500:]
    checks = {item["id"]: item for item in json.loads(result["stdout"])["checks"]}
    return checks["host_procargs"], checks["host_procargs_legacy"]


@pytest.mark.skipif(not exam_room.sandbox_available(), reason="needs macOS sandbox-exec")
def test_host_process_args_leak_without_sandbox(tmp_path):
    room = exam_room.build_room(_repo(tmp_path / "source"), "HEAD~2", "HEAD~1", tmp_path / "room", instructions="task")
    with exam_room.synthetic_host_process("ctl") as fixture:
        checks = _probe_procargs(room, fixture, sandbox=False)
    for check in checks:
        assert check["observed"] == "allowed" and "markers_seen=[True, True]" in check["detail"], check


@pytest.mark.skipif(not exam_room.sandbox_available(), reason="needs macOS sandbox-exec")
def test_room_hides_host_process_args(tmp_path):
    # #97: KERN_PROCARGS2/KERN_PROCARGS go through process-info-pidinfo, which the room limits to self.
    room = exam_room.build_room(_repo(tmp_path / "source"), "HEAD~2", "HEAD~1", tmp_path / "room", instructions="task")
    with exam_room.synthetic_host_process("box") as fixture:
        checks = _probe_procargs(room, fixture, sandbox=True)
    for check in checks:
        assert check["observed"] == "blocked", check
        assert "errno=1 size=0 markers_seen=[False, False]" in check["detail"], check


@pytest.mark.skipif(not exam_room.sandbox_available(), reason="needs macOS sandbox-exec")
def test_room_tools_still_start_with_pidinfo_limited_to_self(tmp_path):
    # Denying process-info-pidinfo outright makes python3/git die at startup (-5); target self must keep them alive.
    room = exam_room.build_room(_repo(tmp_path / "source"), "HEAD~2", "HEAD~1", tmp_path / "room", instructions="task")
    script = "import subprocess; print(subprocess.run(['/usr/bin/git', '-C', 'repo', 'log', '--oneline'], capture_output=True).returncode)"
    result = exam_room.run_in_room(["/usr/bin/python3", "-c", script], room, timeout=120)
    assert (result["returncode"], result["stdout"].strip()) == (0, "0"), result["stderr"][-500:]


def test_canary_scan_finds_leaked_answer(tmp_path):
    (tmp_path / "notes.txt").write_text("see fix abc123")
    assert exam_room.scan_canaries(tmp_path, ["abc123"]) == [{"file": "notes.txt", "canary": "abc123"}]


def test_timeout_kills_the_whole_examinee_tree(tmp_path):
    # A CLI launcher (node) runs the real examinee as a child; a timeout must not leave it running.
    room = tmp_path / "room"
    (room / "out").mkdir(parents=True)
    result = exam_room.run_in_room(["/bin/sh", "-c", "sleep 60 & echo $! > out/child; wait"], room,
                                   sandbox=False, timeout=2)
    child = int((room / "out/child").read_text())
    time.sleep(0.2)
    assert result["timed_out"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_timeout_reaches_a_child_that_escaped_the_group_with_setsid(tmp_path):
    # #101: a descendant that calls setsid leaves the examinee's process group, so killpg alone
    # misses it and the second communicate() would block on the pipes it still holds. The tree
    # snapshot must reach it by PID, and run_in_room must return promptly either way.
    room = tmp_path / "room"
    (room / "out").mkdir(parents=True)
    script = ("import os, sys, time\n"
              "if os.fork() == 0:\n"
              "    os.setsid()\n"
              "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
              "    time.sleep(60)\n"
              "else:\n"
              "    time.sleep(60)\n")
    start = time.monotonic()
    result = exam_room.run_in_room(["/usr/bin/python3", "-c", script, "out/child"], room,
                                   sandbox=False, timeout=2)
    elapsed = time.monotonic() - start
    child = int((room / "out/child").read_text())
    time.sleep(0.2)
    assert result["timed_out"] is True
    assert elapsed < 30  # bounded: it did not wait out the 60s descendant
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
