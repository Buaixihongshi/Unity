"""Disposable full-bundle Sparkle acceptance, including inherited real CLI.

Keeps private evidence under a fresh temporary directory. Never uses Keychain,
daily data, production bundle ID or production feed. Requires macOS and the
same CLI authentication as development. No provider/model routes are changed.
"""
import base64
import functools
import http.server
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.environment_cli_config import inherit_runtime_cli


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def wait(name, predicate, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            print('PASS', name, flush=True)
            return
        time.sleep(.25)
    raise RuntimeError('timeout: ' + name)


def api(port, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(f'http://127.0.0.1:{port}/api/{path}', data=data,
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def main():
    if sys.platform != 'darwin':
        raise RuntimeError('macOS only')
    work = Path(tempfile.mkdtemp(prefix='unity-sparkle-')).resolve()
    work.chmod(0o700)
    print('Private evidence:', work, flush=True)
    feed = work / 'feed'
    feed.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(feed))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    feed_url = f'http://127.0.0.1:{server.server_port}'
    bundle_id = 'io.github.buaixihongshi.unity.' + uuid.uuid4().hex + '.acceptance'
    openssl = os.environ.get('OPENSSL', '/opt/homebrew/opt/openssl@3/bin/openssl')
    if not Path(openssl).exists():
        openssl = shutil.which('openssl')
    key = work / 'ed.pem'
    run(openssl, 'genpkey', '-algorithm', 'ed25519', '-out', str(key))
    key.chmod(0o600)
    der = subprocess.check_output([openssl, 'pkey', '-in', str(key), '-pubout', '-outform', 'DER'])
    public = base64.b64encode(der[-32:]).decode()
    env = {**os.environ, 'OUT': str(work / 'out'), 'BUNDLE_ID': bundle_id,
           'SPARKLE_PUBLIC_KEY': public, 'SPARKLE_FEED_URL': feed_url + '/appcast.xml',
           'SPARKLE_TEST_FEED': '1', 'REQUIRE_UPDATES': '1'}
    app = work / 'out/build/Unity.app'
    run('bash', str(ROOT / 'scripts/package-macos.sh'), env=env)
    # Use only allowlisted CLI settings in this disposable app. No credentials
    # or database are copied. Re-sign every app after this test-only change.
    seed = work / 'seed'
    shutil.copytree(ROOT / 'config', seed / 'config')
    snapshot = inherit_runtime_cli(ROOT, seed, real=True)
    hub = seed / 'data/hub_settings.json'
    fixture = json.loads(hub.read_text())
    fixture['onboarding'] = {'completed_at': '2026-01-01T00:00:00+00:00'}
    hub.write_text(json.dumps(fixture))
    shutil.copy2(seed / 'config/cli-providers.json', app / 'Contents/Resources/app/config/cli-providers.json')
    framework = app / 'Contents/Frameworks'
    launcher = work / 'launcher'
    run('swiftc', '-O', '-target', os.uname().machine + '-apple-macos12.0',
        '-F', str(framework), '-framework', 'Sparkle', '-Xlinker', '-rpath',
        '-Xlinker', '@executable_path/../Frameworks', '-o', str(launcher),
        str(ROOT / 'packaging/macos/UnityApp.swift'), str(ROOT / 'packaging/macos/Updater.swift'),
        str(ROOT / 'packaging/macos/update-tests/main.swift'))
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    build = int(info['CFBundleVersion'])

    def stage(dest, next_version=False):
        run('ditto', str(app), str(dest))
        shutil.copy2(launcher, dest / 'Contents/MacOS/Unity')
        if next_version:
            updated = dict(info, CFBundleVersion=str(build + 1))
            (dest / 'Contents/Info.plist').write_bytes(plistlib.dumps(updated))
        run('codesign', '--force', '--sign', '-', '--entitlements',
            str(ROOT / 'packaging/macos/entitlements.plist'), str(dest))
        run('codesign', '--verify', '--deep', '--strict', str(dest))

    stage(work / 'v2/Unity.app', True)
    dmg = feed / 'Unity-v2.dmg'
    run('hdiutil', 'create', '-quiet', '-volname', 'Unity', '-srcfolder', str(work / 'v2'),
        '-fs', 'HFS+', '-format', 'UDZO', '-ov', str(dmg))
    signature = subprocess.check_output([openssl, 'pkeyutl', '-sign', '-inkey', str(key),
                                         '-rawin', '-in', str(dmg)])
    processes = []
    try:
        for mode in os.environ.get('SCENARIOS', 'auto manual bad').split():
            if mode not in {'auto', 'manual', 'bad'}:
                raise ValueError('unknown scenario ' + mode)
            print('SCENARIO', mode, flush=True)
            directory = work / mode
            directory.mkdir()
            installed = directory / 'Unity.app'
            stage(installed)
            support = directory / 'support'
            shutil.copytree(seed / 'data', support / 'data')
            sig = signature if mode != 'bad' else bytes([signature[0] ^ 1]) + signature[1:]
            (feed / 'appcast.xml').write_text(f'''<?xml version="1.0"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>
<title>Unity acceptance</title><item><title>next</title><sparkle:version>{build + 1}</sparkle:version>
<sparkle:shortVersionString>{info['CFBundleShortVersionString']}</sparkle:shortVersionString>
<enclosure url="{feed_url}/Unity-v2.dmg" length="{dmg.stat().st_size}" type="application/octet-stream"
sparkle:edSignature="{base64.b64encode(sig).decode()}"/></item></channel></rss>''')
            # This unique domain belongs only to this invocation, never to Unity.
            run('defaults', 'write', bundle_id, 'SUEnableAutomaticChecks', '-bool', 'true' if mode == 'auto' else 'false')
            gate = directory / 'cli-done'
            log = directory / 'harness.log'
            with log.open('w') as output:
                proc = subprocess.Popen([str(installed / 'Contents/MacOS/Unity')], stdout=output,
                                        stderr=subprocess.STDOUT, env={**os.environ,
                                        'UNITY_SUPPORT_DIR': str(support), 'UNITY_ACCEPTANCE_MODE': mode,
                                        'UNITY_ACCEPTANCE_GATE': str(gate)})
                processes.append(proc)

                def reached(marker):
                    content = log.read_text()
                    if 'FAIL ' in content or (proc.poll() is not None and marker not in content):
                        raise RuntimeError(f"harness exit={proc.poll()}: {content}")
                    return marker in content

                if mode != 'bad':
                    wait(mode + ' staged update', lambda: reached('READY_FOR_REAL_CLI'), 300)
                    port = int(re.search(r'BACKEND_PORT (\d+)', log.read_text())[1])
                    conversation = api(port, 'conversations', {'title': 'Sparkle real CLI acceptance'})
                    cid = conversation['id']
                    result = api(port, f'conversations/{cid}/missions', {
                        'goal': '整包验收，请直接使用 publish_message 发出 SPARKLE_CLI_OK，不读文件，不调用其他伙伴。',
                        'target_agent_id': os.environ.get('ACCEPTANCE_AGENT', 'baobao')})
                    mid = result['latest_mission_detail']['mission']['id']

                    def cli_done():
                        detail = api(port, 'missions/' + mid)
                        status = detail['mission']['status']
                        if status in {'failed', 'blocked'}:
                            (directory / 'cli-result.json').write_text(json.dumps(detail, ensure_ascii=False))
                            raise RuntimeError('real CLI ended ' + status)
                        messages = [m for m in detail['messages'] if m.get('role') == 'agent']
                        done = status in {'completed', 'completed_with_warnings'}
                        if done:
                            assert any(e.get('tool_name') == 'publish_message' for e in detail['tool_events']), 'no MCP publish call'
                            assert any('SPARKLE_CLI_OK' in m['content'] and m.get('provider_id') != 'fake' for m in messages)
                            (directory / 'cli-result.json').write_text(json.dumps(detail, ensure_ascii=False))
                        return done

                    wait(mode + ' real CLI publish', cli_done, 420)
                    gate.write_text(cid)
                wait(mode + ' harness complete', lambda: reached('ACCEPTANCE_DONE'), 300)
                proc.wait(timeout=90)
                assert proc.returncode == 0
            if mode == 'bad':
                time.sleep(3)
                assert plistlib.loads((installed / 'Contents/Info.plist').read_bytes())['CFBundleVersion'] == str(build)
                print('PASS rejected signature leaves old app', flush=True)
            else:
                wait(mode + ' installed new build', lambda: plistlib.loads(
                    (installed / 'Contents/Info.plist').read_bytes())['CFBundleVersion'] == str(build + 1))
                marker = directory / f'relaunched-{build + 1}'
                if mode == 'manual':
                    wait('Sparkle relaunched installed build', marker.exists, 90)
                else:
                    time.sleep(3)
                    assert not marker.exists(), 'install on quit unexpectedly relaunched'
            run('codesign', '--verify', '--deep', '--strict', str(installed))
            print(log.read_text(), flush=True)
        print('PASS full bundle acceptance; CLI snapshot', snapshot['digest'], flush=True)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=30)
        server.shutdown()
        key.unlink(missing_ok=True)
        run('defaults', 'delete', bundle_id)


if __name__ == '__main__':
    main()
