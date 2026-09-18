"""方法级并发与持久化装饰器。"""

import functools


def locked(fn):
    """在实例锁下执行，保证并发改签、分配与回调串行化。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


def persisted(fn):
    """方法成功返回后写快照；抛异常时不落盘，避免写入半截状态。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        result = fn(self, *args, **kwargs)
        self._persist()
        return result

    return wrapper
