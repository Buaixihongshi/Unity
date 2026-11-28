"""Direct, DNS-pinned HTTP transport. No proxy environment or redirects."""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from backend.app.storage.api_connections import PROTOCOLS, validate_endpoint


class APIError(Exception):
    """Only allowlisted diagnostics cross this boundary."""
    def __init__(self, code, *, retryable=False):
        self.code = code
        self.retryable = retryable
        super().__init__(f"API request failed: {code}")


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, endpoint, addresses, timeout):
        super().__init__(endpoint.hostname, endpoint.port or (443 if endpoint.scheme == 'https' else 80), timeout=timeout)
        self.endpoint = endpoint
        self.addresses = addresses
        self.aborted = threading.Event()
        self.active_socket = None

    def abort(self):
        self.aborted.set()
        active = self.active_socket
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            active.close()

    def connect(self):
        # Connect the numeric sockaddr returned by the *validated* resolution.
        # HTTP Host and TLS SNI/certificate verification retain the original host.
        for family, socktype, proto, _, address in self.addresses:
            if self.aborted.is_set():
                raise APIError('stopped')
            candidate = socket.socket(family, socktype, proto)
            self.sock = candidate
            self.active_socket = candidate
            candidate.settimeout(self.timeout)
            try:
                candidate.connect(address)
                if self.endpoint.scheme == 'https':
                    self.sock = ssl.create_default_context().wrap_socket(candidate, server_hostname=self.host, do_handshake_on_connect=False)
                    self.active_socket = self.sock
                    if self.aborted.is_set():
                        self.abort()
                        raise APIError('stopped')
                    self.sock.do_handshake()
                if self.aborted.is_set():
                    self.abort()
                    raise APIError('stopped')
                return
            except ssl.SSLCertVerificationError:
                candidate.close()
                raise APIError('tls_verification') from None
            except OSError:
                candidate.close()
        raise APIError('network', retryable=True)


_dns_slots = threading.BoundedSemaphore(16)


def resolve_endpoint(connection, cancelled=lambda: False, deadline=None):
    endpoint = urlsplit(validate_endpoint(connection['base_url'], connection.get('allow_private', False)))
    if not _dns_slots.acquire(blocking=False):
        raise APIError('dns_busy', retryable=True)
    resolved = threading.Event()
    result = []
    def resolve():
        try:
            result.extend(socket.getaddrinfo(endpoint.hostname, endpoint.port or (443 if endpoint.scheme == 'https' else 80), type=socket.SOCK_STREAM))
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            _dns_slots.release()
            resolved.set()
    threading.Thread(target=resolve, daemon=True).start()
    while not resolved.wait(.05):
        if cancelled():
            raise APIError('stopped')
        if deadline is not None and time.monotonic() >= deadline:
            raise APIError('timeout', retryable=True)
    if not result:
        raise APIError('dns', retryable=True)
    if (not connection.get('allow_private') and
            any(not ipaddress.ip_address(a[4][0]).is_global for a in result)):
        raise APIError('endpoint_policy')
    return endpoint, result


@contextmanager
def request(connection, key, method, path, payload=None, cancelled=lambda: False):
    if (not connection.get('enabled', True) or not isinstance(key, str) or not key.strip()
            or len(key) > 8192 or any(ord(c) < 32 for c in key)
            or connection.get('protocol') not in PROTOCOLS
            or not isinstance(connection.get('base_url'), str)):
        raise APIError('configuration')
    if cancelled():
        raise APIError('stopped')
    timeout = connection.get('timeout', 120)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 600:
        raise APIError('configuration')
    from backend.app.adapters.turn_budget import remaining_timeout
    timeout = remaining_timeout(timeout)
    if timeout <= 0:
        raise APIError("timeout", retryable=False)
    deadline = time.monotonic() + timeout
    try:
        endpoint, addresses = resolve_endpoint(connection, cancelled, deadline)
    except ValueError:
        raise APIError('endpoint_policy') from None
    client = PinnedConnection(endpoint, addresses, max(.01, deadline - time.monotonic()))
    done = threading.Event()
    reason = []

    def watch():
        while not done.wait(.05):
            if cancelled() or time.monotonic() >= deadline:
                reason.append('stopped' if time.monotonic() < deadline else 'timeout')
                client.abort()
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    response = None
    headers = {'Accept': 'text/event-stream' if payload is not None else 'application/json',
               'Content-Type': 'application/json'}
    if connection['protocol'] == 'anthropic':
        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
    else:
        headers['Authorization'] = 'Bearer ' + key
    try:
        client.request(method, endpoint.path.rstrip('/') + path,
                       body=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        response = client.getresponse()
        if not 200 <= response.status < 300:
            code = 'authentication' if response.status in (401, 403) else ('upstream' if response.status >= 500 or response.status == 429 else 'configuration')
            raise APIError(code, retryable=code == 'upstream')
        yield response
        if reason:
            raise APIError(reason[0], retryable=reason[0] == 'timeout')
    except APIError as exc:
        if reason:
            raise APIError(reason[0], retryable=reason[0] == 'timeout') from None
        if exc.code == 'network' and time.monotonic() >= deadline:
            raise APIError('timeout', retryable=True) from None
        raise
    except (OSError, http.client.HTTPException, ValueError, UnicodeError) as exc:
        code = reason[0] if reason else ('timeout' if isinstance(exc, TimeoutError)
                                        or time.monotonic() >= deadline else 'network')
        raise APIError(code, retryable=code in {'network', 'timeout'}) from None
    finally:
        done.set()
        client.abort()
        if response is not None:
            response.close()
        client.close()
        watcher.join(timeout=.2)


def stream_text(connection, key, model, prompt, on_text, cancelled=lambda: False, *, images=()):
    protocol = connection['protocol']
    payload = {'model': model, 'stream': True}
    if protocol == 'openai_responses':
        path = '/responses'
        payload['input'] = prompt
    else:
        path = '/messages' if protocol == 'anthropic' else '/chat/completions'
        payload['messages'] = [{'role': 'user', 'content': prompt}]
        if protocol == 'anthropic':
            payload['max_tokens'] = 8192
    if images:
        from backend.app.attachments.delivery import encoded
        if protocol == 'anthropic':
            content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': item['mime'], 'data': encoded(item)}} for item in images]
            content.append({'type': 'text', 'text': prompt})
        else:
            content = [{'type': 'input_text' if protocol == 'openai_responses' else 'text', 'text': prompt}]
            for item in images:
                url = 'data:' + item['mime'] + ';base64,' + encoded(item)
                content.append({'type': 'input_image', 'image_url': url} if protocol == 'openai_responses' else {'type': 'image_url', 'image_url': {'url': url}})
        if protocol == 'openai_responses':
            payload['input'] = [{'role': 'user', 'content': content}]
        else:
            payload['messages'][0]['content'] = content
    with request(connection, key, 'POST', path, payload, cancelled) as response:
        data = []
        size = 0
        finished = False
        while True:
            line = response.readline(1_000_001)
            if not line:
                break
            if cancelled():
                raise APIError('stopped')
            size += len(line)
            if len(line) > 1_000_000 or size > 8_000_000:
                raise APIError('response_limit')
            line = line.decode('utf-8').rstrip('\r\n')
            if line.startswith('data:'):
                data.append(line[5:].lstrip())
            elif not line and data:
                raw, data = '\n'.join(data), []
                if raw == '[DONE]':
                    finished = True
                    break
                try:
                    event = json.loads(raw)
                    kind = event.get('type')
                    if kind in {'error', 'response.failed', 'response.incomplete'} or event.get('error'):
                        raise APIError('upstream', retryable=True)
                    if (kind == 'content_block_start' and str(event.get('content_block', {}).get('type', '')).endswith('tool_use')
                            or kind == 'response.output_item.added' and str(event.get('item', {}).get('type', '')).endswith('_call')):
                        raise APIError('unsupported_tools')
                    text = ''
                    if protocol == 'openai_responses':
                        if kind == 'response.output_text.delta':
                            text = event.get('delta', '')
                        if kind == 'response.completed':
                            finished = True
                    elif protocol == 'anthropic':
                        if kind == 'content_block_delta' and event.get('delta', {}).get('type') == 'text_delta':
                            text = event['delta'].get('text', '')
                        if kind == 'message_delta' and event.get('delta', {}).get('stop_reason') in {'max_tokens', 'refusal'}:
                            raise APIError('incomplete')
                        if kind == 'message_stop':
                            finished = True
                    else:
                        for choice in event.get('choices', []):
                            if choice.get('finish_reason') in {'length', 'content_filter'}:
                                raise APIError('incomplete')
                            if choice.get('delta', {}).get('tool_calls') or choice.get('delta', {}).get('function_call'):
                                raise APIError('unsupported_tools')
                            text += choice.get('delta', {}).get('content') or ''
                    if not isinstance(text, str):
                        raise APIError('protocol')
                    if text:
                        on_text(text)
                    if finished:
                        break
                except (ValueError, TypeError, AttributeError):
                    raise APIError('protocol') from None
        if cancelled():
            raise APIError('stopped')
        if not finished:
            raise APIError('incomplete', retryable=True)
