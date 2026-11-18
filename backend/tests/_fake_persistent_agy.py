"""Real pipe/stdio-MCP peer for worker supervision and fencing tests."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


# Resolve the MCP server the way agy 1.4 does: workspace plugins.json entries.
entry = json.loads(Path('.agents/plugins.json').read_text())['entries'][0]
plugin = Path(entry['path']) / entry['include_only'][0]
spec = json.loads((plugin / 'mcp_config.json').read_text())['mcpServers']['hub']
# Real agy also loads global servers; record what it would have seen.
global_config = Path(os.environ.get('AGY_TEST_GLOBAL', '/nonexistent'))
global_servers = sorted(json.loads(global_config.read_text())['mcpServers']) if global_config.is_file() else []
env = os.environ.copy(); env.update(spec['env'])
mcp = subprocess.Popen([spec['command'], *spec['args']], env=env,
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def rpc(method, params):
    mcp.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}) + '\n')
    mcp.stdin.flush()
    return json.loads(mcp.stdout.readline())


def emit(value):
    print(json.dumps(value), flush=True)


record = Path(os.environ['AGY_TEST_RECORD'])
mode = os.environ.get('AGY_TEST_MODE', 'normal')
old_token = None
turn_number = 0
time.sleep(float(os.environ.get('AGY_TEST_INIT_DELAY', '0')))
emit({'event': 'init', 'conversation_id': 'native-' + str(os.getpid()), 'init': {'tools': ['call_mcp_tool']}})
try:
    for line in sys.stdin:
        turn_number += 1
        prompt = json.loads(line)['message']['content']
        token = re.search(r'context_token=([A-Za-z0-9_-]+)', prompt).group(1)
        with record.open('a') as stream:
            stream.write(json.dumps({'pid': os.getpid(), 'recovery': '【中断恢复】' in prompt,
                                     'started': time.time(), 'global_servers': global_servers}) + '\n')
        if old_token:
            stale = rpc('tools/call', {'name': 'publish_message', 'arguments': {
                'context_token': old_token, 'operation_id': 'late', 'content': 'stale'}})
            if not stale['result'].get('isError'):
                sys.exit(9)
        if mode == 'crash_before':
            sys.exit(3)
        if mode == 'hang':
            time.sleep(60)
        time.sleep(float(os.environ.get('AGY_TEST_TURN_DELAY', '0')))
        if mode == 'long_tools':
            for index in range(6):
                emit({'event': 'step_update', 'step_update': {
                    'step_type': 'tool', 'step_index': index + 10, 'state': 'DONE',
                    'tool_name': 'view_file', 'tool_info': {'output': 'read result'}}})
                time.sleep(0.25)
        for state in ['ACTIVE', 'DONE']:
            emit({'event': 'step_update', 'step_update': {
                'step_type': 'tool', 'step_index': 1, 'state': state, 'tool_name': 'view_file',
                'tool_info': {'parameters': {'AbsolutePath': '/workspace/test.txt'}}}})
        result = rpc('tools/call', {'name': 'publish_message', 'arguments': {
            'context_token': token, 'operation_id': 'final', 'content': 'verified reply'}})
        if result.get('result', {}).get('isError'):
            sys.stderr.write(json.dumps(result)); sys.exit(5)
        if mode == 'publish_then_crash':
            sys.exit(4)
        repeat = rpc('tools/call', {'name': 'publish_message', 'arguments': {
            'context_token': token, 'operation_id': 'another-id', 'content': 'verified reply'}})
        if repeat != result:
            sys.exit(8)
        old_token = token
        emit({'event': 'result', 'result': {'status': 'SUCCESS', 'usage': {
            'input_tokens': turn_number * 100, 'cache_read_tokens': turn_number * 50,
            'output_tokens': turn_number * 10, 'thinking_tokens': turn_number * 5}}})
finally:
    mcp.stdin.close()
    try:
        mcp.wait(timeout=2)
    except subprocess.TimeoutExpired:
        mcp.kill()
