# -*- coding: utf-8 -*-
"""
django-q2 Windows 任务超时补丁

django_q.timeout.TimeoutHandler 依赖 POSIX signal.SIGALRM 在任务内部实现超时。
Windows 没有 SIGALRM，django-q2 在 timeout.py 里把 signal.signal 的异常静默吞掉，
于是 Windows 上任务超时只能靠 qcluster sentinel 守护循环对 timer.value 的递减来
**强杀 worker 进程**：任务既不优雅失败、结果也不落库；再叠加 timeout=-1 的任务
（timer 永不递减），卡死的 worker 会永久占住槽位，后台队列只进不出。

本补丁用「守护线程 + 跨线程异常注入 + 兜底进程强杀」在 Windows 上等价复刻
SIGALRM 行为：

  1. 任务超时时，通过 PyThreadState_SetAsyncExc 向任务线程注入 TimeoutException。
     TimeoutException 继承 SystemExit，会沿栈传播到 worker 的
     ``except (Exception, TimeoutException)``，任务走 django-q2 既有的超时失败分支
     （失败结果落库、worker 优雅退出）。
  2. 若任务阻塞在 C 层 I/O（socket/DB 连接，异常注入不生效）或吞掉了异常，
     缓冲期后任务线程仍未退出，则 os._exit(1) 强制回收该 worker
     （sentinel 检测到进程死亡后 reincarnate）。

语义与 Linux 保持一致：timeout=-1 表示不设超时，本补丁同样跳过。

在 settings.py 末尾调用 install() 即可，qcluster / runserver / spawn 出的 worker
子进程都会加载 settings，补丁随之生效。
"""
import ctypes
import os
import threading
import time

from django_q.exceptions import TimeoutException
import django_q.timeout as _django_q_timeout

# 注入异常后，再给任务多少秒优雅退出，超时则强杀 worker 进程
_GRACE_SECONDS = 5

# 记录每个任务线程当前任务的 token，避免 watchdog 误杀已切换到下一个任务的线程
_active = {}

_patched = False


def _async_raise(thread_id, exc_type):
    """通过 PyThreadState_SetAsyncExc 向指定线程注入异常。

    注意：CPython 会把 async_exc 直接当异常类型抛出，因此这里必须传异常**类**，
    传实例会导致 ``_PyErr_SetObject: ... is not a BaseException subclass``。
    """
    ctypes.pythonapi.PyThreadState_SetAsyncExc.argtypes = [
        ctypes.c_long,
        ctypes.py_object,
    ]
    ctypes.pythonapi.PyThreadState_SetAsyncExc.restype = ctypes.c_int
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread_id), exc_type)
    if res == 0:
        raise ValueError("invalid thread id")
    if res != 1:
        # 异常被注入到多个线程，撤销以免误伤
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread_id), None)
        raise SystemError("PyThreadState_SetAsyncExc failed")


def _watchdog(thread_id, token, timeout):
    time.sleep(timeout)
    if _active.get(thread_id) != token:
        return  # 任务已正常结束，或线程已切换到下一个任务
    try:
        _async_raise(thread_id, TimeoutException)
    except Exception:
        pass
    # 给任务一段缓冲优雅退出；若仍卡死（阻塞 I/O / 异常被吞）则强杀 worker
    for _ in range(_GRACE_SECONDS):
        time.sleep(1)
        if _active.get(thread_id) != token:
            return  # 任务已退出
    # 任务线程仍存活 → 强制回收 worker 进程（sentinel 检测后 reincarnate）
    os._exit(1)


def _patched_enter(self):
    if self._timeout == -1:
        return None
    thread_id = threading.get_ident()
    token = object()
    _active[thread_id] = token
    self._win_tid = thread_id
    self._win_token = token
    watchdog = threading.Thread(
        target=_watchdog,
        args=(thread_id, token, self._timeout),
        daemon=True,
        name="win-task-timeout-watchdog",
    )
    self._win_watchdog = watchdog
    watchdog.start()
    return None


def _patched_exit(self, exc_type, exc_value, traceback):
    if self._timeout == -1:
        return None
    # 正常退出：注销 token，watchdog 醒来发现不匹配即返回
    if _active.get(self._win_tid) == self._win_token:
        _active.pop(self._win_tid, None)
    self._win_watchdog = None
    return None


def install():
    """就地替换 TimeoutHandler.__enter__/__exit__，仅在 Windows 下生效。"""
    global _patched
    if _patched:
        return
    if os.name != "nt":
        return
    _django_q_timeout.TimeoutHandler.__enter__ = _patched_enter
    _django_q_timeout.TimeoutHandler.__exit__ = _patched_exit
    _patched = True
