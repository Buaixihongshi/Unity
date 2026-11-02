"""Allowlisted public diagnostics; provider output stays private until release."""
import re


MESSAGES = {
    'configuration': 'API 配置无效或凭据已更新，请检查连接与模型。',
    'endpoint_policy': 'API 出站端点未通过网络策略校验。',
    'tls_verification': 'API TLS 证书校验失败。',
    'network': 'API 网络请求失败。',
    'dns': 'API 端点 DNS 解析失败。',
    'dns_busy': 'API DNS 解析繁忙，请稍后重试。',
    'upstream': 'API 服务暂时不可用。',
    'protocol': 'API 返回了不兼容的协议数据。',
    'incomplete': 'API 流在完成前中断。',
    'response_limit': 'API 返回内容超过大小限制。',
    'unsupported_tools': 'API 连接仅支持纯文本，不能执行工具。',
    'empty_response': 'API 未返回有效文本。',
    'invalid_submission': 'API 未返回符合讨论协议的结构化结果。',
    'managed_preferences': 'CLI 无法读取 macOS 管理偏好，初始化失败。',
    'filesystem_permission': 'CLI 所需文件或临时目录访问被隔离规则拒绝。',
    'invalid_arguments': 'CLI 启动参数不兼容，尚未进入讨论。',
    'authentication': 'Provider 身份验证失败，请检查登录状态。',
    'network_policy': 'CLI 网络策略初始化失败。',
    'timeout': 'CLI 超时，未完成有效提交。',
    'stopped': '本回合已停止。',
    'missing_submission': 'CLI 已退出，但没有完成有效的结构化提交。',
    'execution_failed': 'CLI 运行失败，未完成有效提交。',
}


# Connection-level failures: the request never got an answer. `": eof` is a Go
# client's dropped connection after `Post "<url>"`.
_NETWORK = re.compile(
    r'econnreset|econnrefused|connection (dropped|reset|refused|closed)|failed to connect'
    r'|network is unreachable|socket hang up|": eof\b')


def failure_code(text, *, exit_code=None):
    value = str(text).lower()
    # Prefer the fatal startup error over secondary network warnings.
    if 'failed to synchronize managed preferences' in value:
        return 'managed_preferences'
    if 'operation not permitted' in value or 'permission denied' in value or 'eperm' in value:
        return 'filesystem_permission'
    if 'intended prompt' in value or 'unexpected argument' in value or 'unknown option' in value:
        return 'invalid_arguments'
    if '401' in value or 'not logged in' in value or 'authentication' in value:
        return 'authentication'
    if 'application network policy is unavailable' in value:
        return 'network_policy'
    if 'enotfound' in value or 'getaddrinfo' in value or 'name or service not known' in value:
        return 'dns'
    if _NETWORK.search(value):
        return 'network'
    if 'timed out' in value or 'timeout after' in value:
        return 'timeout'
    if 'overloaded' in value or 'service unavailable' in value or 'server_error' in value:
        return 'upstream'
    if 'discussion stopped' in value or '用户停止' in value or '本回合已中止' in value:
        return 'stopped'
    if 'did not call publish_message' in value:
        return 'missing_submission'
    return 'missing_submission' if exit_code == 0 else 'execution_failed'


def private_error_tail(stderr):
    """Keep the fatal suffix, never a prefix that hides the final error."""
    return stderr.strip()[-1000:]
