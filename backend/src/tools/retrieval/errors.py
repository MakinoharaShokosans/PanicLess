"""检索模型调用错误：失败不得解释成没有相关内容。错误消息不携带请求或鉴权值。"""


class RetrievalError(Exception):
    """模型接口错误基类。"""


class RetrievalNotConfiguredError(RetrievalError):
    """服务未配置或已禁用。"""


class RetrievalConfigurationError(RetrievalError):
    """启用服务的运行环境配置不完整。"""


class RetrievalTimeoutError(RetrievalError):
    """模型请求超时。"""


class RetrievalAuthenticationError(RetrievalError):
    """模型服务拒绝鉴权。"""


class RetrievalRateLimitError(RetrievalError):
    """模型服务限流。"""


class RetrievalServiceError(RetrievalError):
    """网络故障或模型服务返回非成功状态。"""


class RetrievalInvalidResponseError(RetrievalError):
    """响应协议或结果完整性不符合契约。"""
