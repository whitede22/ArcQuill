"""单词朗读：Windows 语音合成（SAPI）的最小封装。

## 为什么不是 QTextToSpeech

PySide6 提供了 `QtTextToSpeech` 绑定，看起来是首选：跨平台、异步、免依赖。
实测不成立——官方 wheel 里**不带** `plugins/texttospeech` 目录，
`QTextToSpeech.availableEngines()` 返回空列表，构造出来的对象状态是
`State.Error`，调 `say()` 什么都不发生，也不报错。一个「点了没反应」的
按钮比没有按钮更糟。

## 为什么不是拉起一个 PowerShell 进程

`(New-Object -ComObject SAPI.SpVoice).Speak(...)` 十行就能跑，代价是每次
点击起一个进程：冷启动 200–400 ms，连点几个词就会叠一堆 `powershell.exe`。
朗读是低频动作，但「点了之后先卡一下、然后系统里多出一堆进程」不像一个
桌面软件该有的行为。

## 第一版错在哪

第一版直接读 `ISpVoice` 的虚表，按索引取函数指针，把 `Speak` 记成第 27 个
槽位。实测那个槽位不是 `Speak`：调用返回 `S_OK`，一个字母都没念出来，界面
收到成功之后连提示都不会弹。虚表索引错位不会抛异常，只会安静地做错事，
这种失败在自动化测试里几乎看不出来，除非有人真的打开音箱点一下。

`ISpVoice` 的继承链是 IUnknown → ISpNotifySource → ISpEventSource → ISpVoice，
每一层的函数个数都得数准，而每多一条新的验证路径就可能再错一次。

## 现在怎么做

改走 `IDispatch`，按方法名取 `DISPID` 再 `Invoke`，一个槽位数字都不碰。
SAPI 的 `SpVoice` 是 dual 接口，type library 把 `Speak`、`Pause`、
`WaitUntilDone`、`Status` 都以名字暴露，名字不随继承层数变化而错位。
代价是要自己拼 `VARIANT` 与 `DISPPARAMS`，但这部分结构布局一次写对就固定了。

**只在主线程调用。** 类级单例，第一次使用时初始化 COM（STA），之后复用同一个
对象。选 STA 是因为 SAPI 本来就是按 apartment 线程模型设计的；在 MTA 里创建
它会走代理，延迟更高。

## 失败时怎么办

`available()` 是唯一的判据，界面据此决定要不要显示喇叭按钮：

- 非 Windows 平台 → False；
- 系统里没装任何语音（精简版 Windows、Server Core）→ False；
- COM 初始化或创建对象失败 → False，并把原因留在 `error` 里。

`available()` 还会按名字取一次 `Speak` 的 DISPID。这一条是专门为上一个 bug
加的：能拿到 DISPID 才意味着这个方法真的存在，而不是「对象建出来了就算数」。

`speak()` 失败只返回 False，不抛异常：一次朗读失败不该让查词流程中断，
也不值得弹一个对话框——用户点第二下时多半就好了，真不好他也知道是发音坏了。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

# COM 常量。数值来自 Windows SDK，改名不改值。
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_ALL = 0x17
_RPC_E_CHANGED_MODE = 0x80010106

# IDispatch::Invoke 的调用方式位。
_DISPATCH_METHOD = 0x1
_DISPATCH_PROPERTYGET = 0x2

# VARIANT 的类型码。
_VT_I4 = 3
_VT_BSTR = 8
_VT_DISPATCH = 9

# SpeechVoiceSpeakFlags。异步返回（不阻塞界面）+ 打断正在念的那条。
# 两条都要：同步会让界面在朗读期间卡住；不打断的话连点三个词会排队念完，
# 用户听到的最后一个词与屏幕上显示的那个对不上。
_SVSF_ASYNC = 0x1
_SVSF_PURGE_BEFORE_SPEAK = 0x2


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    """把 `{96749377-3391-11D2-9EE3-00C04F797396}` 解析成 GUID 结构。

    手写解析而不是用 `uuid.UUID` + `bytes_le`：后者要额外处理大端小端，
    而这里的输入永远是同一份字面量，手写四段反而更不容易错。
    """
    body = text.strip().strip("{}")
    parts = body.split("-")
    data1 = int(parts[0], 16)
    data2 = int(parts[1], 16)
    data3 = int(parts[2], 16)
    tail = bytes.fromhex(parts[3] + parts[4])
    return _GUID(data1, data2, data3, (ctypes.c_ubyte * 8)(*tail))


# CLSID_SpVoice 与 IID_IDispatch。前者是 SAPI 5.x 的公开契约，自 Windows XP
# 起未变；写错的话 CoCreateInstance 返回 REGDB_E_CLASSNOTREG，表现是「系统
# 明明有语音包却不可用」。后者是每个 COM 组件都会实现的调度接口。
_CLSID_SPVOICE = _guid("{96749377-3391-11D2-9EE3-00C04F797396}")
_IID_IDISPATCH = _guid("{00020400-0000-0000-C000-000000000046}")
_IID_NULL = _guid("{00000000-0000-0000-0000-000000000000}")

# WINFUNCTYPE 只在 Windows 上存在；非 Windows 用 CFUNCTYPE 占位，
# 好让本模块在任何平台都能被导入（自检要 import 它来判断平台分支）。
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_HRESULT = ctypes.c_long


class _VariantValue(ctypes.Union):
    """VARIANT 的联合体部分。x64 下要占满 16 字节，否则整个结构只有 16 字节，
    后面几个字段会读到别的内存里去。"""

    _fields_ = [
        ("llVal", ctypes.c_longlong),
        ("dblVal", ctypes.c_double),
        ("bstrVal", ctypes.c_void_p),
        ("lVal", ctypes.c_long),
        ("boolVal", ctypes.c_short),
        ("pdispVal", ctypes.c_void_p),
        ("pad", ctypes.c_byte * 16),
    ]


class _VARIANT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", wintypes.WORD),
        ("r1", wintypes.WORD),
        ("r2", wintypes.WORD),
        ("r3", wintypes.WORD),
        ("value", _VariantValue),
    ]


class _DISPPARAMS(ctypes.Structure):
    _fields_ = [
        ("rgvarg", ctypes.POINTER(_VARIANT)),
        ("rgdispidNamedArgs", ctypes.POINTER(ctypes.c_long)),
        ("cArgs", wintypes.UINT),
        ("cNamedArgs", wintypes.UINT),
    ]


if sys.platform == "win32":
    _ole32 = ctypes.oledll.ole32
    # BSTR 的分配用 windll 而不是 oledll：oledll 会把返回值当 HRESULT 校验，
    # 而这里返回的是一个指针，走 oledll 会得到 "int too large" 的假报错。
    _oleaut32 = ctypes.windll.oleaut32
    _oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
    _oleaut32.SysAllocString.restype = ctypes.c_void_p
    _oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
    _oleaut32.SysFreeString.restype = None
else:  # pragma: no cover - 仅用于非 Windows 上的导入
    _ole32 = None
    _oleaut32 = None


def _i4(value: int) -> _VARIANT:
    v = _VARIANT()
    v.vt = _VT_I4
    v.lVal = value
    return v


class _Dispatch:
    """一个 IDispatch 指针的最小操作面：按名字取 DISPID，按名字调用。

    只暴露 `disp_id` 与 `call`。把一个 COM 接口完整包一遍没有意义，
    而每多一个方法就多一处需要验证的地方。
    """

    _GET_IDS = _FUNCTYPE(
        _HRESULT,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_wchar_p),
        wintypes.UINT,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_long),
    )
    _INVOKE = _FUNCTYPE(
        _HRESULT,
        ctypes.c_void_p,
        ctypes.c_long,
        ctypes.POINTER(_GUID),
        wintypes.DWORD,
        wintypes.WORD,
        ctypes.POINTER(_DISPPARAMS),
        ctypes.POINTER(_VARIANT),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.UINT),
    )
    _RELEASE = _FUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

    def __init__(self, pointer: ctypes.c_void_p) -> None:
        self._ptr = pointer
        vtable = ctypes.cast(
            pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        # IDispatch 的槽位固定：0 QueryInterface / 1 AddRef / 2 Release /
        # 3 GetTypeInfoCount / 4 GetTypeInfo / 5 GetIDsOfNames / 6 Invoke。
        # 这三个不再依赖任何 SAPI 内部布局，写错会立刻在第一次调用时炸出来。
        self._release_fn = self._RELEASE(vtable[2])
        self._get_ids = self._GET_IDS(vtable[5])
        self._invoke = self._INVOKE(vtable[6])
        self._ids: dict[str, int] = {}

    def disp_id(self, name: str) -> int:
        """取方法/属性的 DISPID。名字不存在时抛 OSError。"""
        cached = self._ids.get(name)
        if cached is not None:
            return cached
        dispid = ctypes.c_long(0)
        names = (ctypes.c_wchar_p * 1)(name)
        hr = self._get_ids(
            self._ptr, ctypes.byref(_IID_NULL), names, 1, 0, ctypes.byref(dispid)
        )
        if hr != 0:
            raise OSError(
                "找不到方法 %s（0x%08X）" % (name, hr & 0xFFFFFFFF)
            )
        self._ids[name] = dispid.value
        return dispid.value

    def call(self, name: str, args: list[_VARIANT], flags: int = _DISPATCH_METHOD):
        """按名字调用，返回结果 VARIANT。失败抛 OSError。"""
        dispid = self.disp_id(name)
        # DISPPARAMS 里的实参是逆序的：最后一个参数落在 rgvarg[0]。
        ordered = list(reversed(args))
        bag = (_VARIANT * max(1, len(ordered)))(*ordered) if ordered else (_VARIANT * 1)()
        params = _DISPPARAMS(bag, None, len(ordered), 0)
        result = _VARIANT()
        hr = self._invoke(
            self._ptr,
            dispid,
            ctypes.byref(_IID_NULL),
            0,
            flags,
            ctypes.byref(params),
            ctypes.byref(result),
            None,
            None,
        )
        # BSTR 与对象指针都归调用方释放，这里只把结果交出去，由调用方决定。
        if hr != 0:
            raise OSError("调用 %s 失败（0x%08X）" % (name, hr & 0xFFFFFFFF))
        return result

    def wrapped(self, name: str) -> "_Dispatch":
        """把返回 IDispatch 的属性包成新的 _Dispatch，用于访问子对象。"""
        result = self.call(name, [], flags=_DISPATCH_PROPERTYGET)
        if result.vt != _VT_DISPATCH or not result.pdispVal:
            raise OSError("%s 没有返回对象" % name)
        return _Dispatch(ctypes.c_void_p(result.pdispVal))

    def release(self) -> None:
        if self._ptr:
            try:
                self._release_fn(self._ptr)
            finally:
                self._ptr = ctypes.c_void_p()


class Speaker:
    """语音朗读的进程级单例。

    只支持 `say(text)` 与 `available()`，因为界面只需要这两件事。
    初始化推迟到第一次 `say()`：启动时创建 COM 对象要几十毫秒，
    而大部分用户一次都不会点那个喇叭。
    """

    def __init__(self) -> None:
        self._voice: _Dispatch | None = None
        self._tried = False
        self._error = ""
        # 上一条还在念的文本。异步 Speak 返回时 SAPI 只是接下了这串文字，
        # 真正的解析在后台线程做，此时把 BSTR 释放掉，它读到的是已回收的内存，
        # 结果是静默失败：HRESULT 全是成功，一个音都没有。
        self._pending: int = 0

    @property
    def error(self) -> str:
        """初始化失败的原因。给日志与自检看，不进界面。"""
        return self._error

    def available(self) -> bool:
        """系统上有可用的语音合成。只探测一次，结果缓存。"""
        if self._voice is not None:
            return True
        if self._tried:
            return False
        self._tried = True
        if sys.platform != "win32" or _ole32 is None:
            self._error = "仅支持 Windows"
            return False
        try:
            # 已经在别的线程模型里初始化过时返回 RPC_E_CHANGED_MODE。
            # 那不算失败：COM 本身是可用的，只是 apartment 与预期不同。
            hr = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
            if hr not in (0, 1, _RPC_E_CHANGED_MODE):
                self._error = "CoInitializeEx 返回 0x%08X" % (hr & 0xFFFFFFFF)
                return False
        except OSError:
            # ole32 在部分精简系统上会直接失败；这不是致命错误，只是没有朗读。
            pass

        try:
            pointer = ctypes.c_void_p()
            _ole32.CoCreateInstance(
                ctypes.byref(_CLSID_SPVOICE),
                None,
                _CLSCTX_ALL,
                ctypes.byref(_IID_IDISPATCH),
                ctypes.byref(pointer),
            )
        except OSError as exc:
            self._error = "创建语音对象失败：%s" % exc
            return False
        if not pointer:
            self._error = "创建语音对象失败：返回空指针"
            return False
        try:
            voice = _Dispatch(pointer)
            # 能取到 Speak 的 DISPID 才说明这个方法真的在，而不是「对象建出来了
            # 就算数」——上一版正是栽在这一点上。
            voice.disp_id("Speak")
        except (OSError, ValueError, TypeError, IndexError) as exc:
            self._error = "语音对象不可用：%s" % exc
            return False
        self._voice = voice
        return True

    def say(self, text: str) -> bool:
        """朗读一段文字。成功返回 True，失败（含系统没有语音）返回 False。"""
        phrase = " ".join((text or "").split())
        if not phrase:
            return False
        if self._voice is None and not self.available():
            return False
        assert self._voice is not None  # available() 为真时必然已建好
        bstr = _oleaut32.SysAllocString(phrase)
        if not bstr:
            self._error = "无法分配文本缓冲区"
            return False
        self._release_pending()
        try:
            payload = _VARIANT()
            payload.vt = _VT_BSTR
            payload.bstrVal = bstr
            self._voice.call(
                "Speak", [payload, _i4(_SVSF_ASYNC | _SVSF_PURGE_BEFORE_SPEAK)]
            )
        except OSError as exc:
            self._error = str(exc)
            _oleaut32.SysFreeString(bstr)
            return False
        # 交给下一次 say 或 shutdown 收尾：这条字符串可能还在被后台线程读。
        self._pending = bstr
        return True

    def _release_pending(self) -> None:
        """释放上一条朗读的文本。只在新一条覆盖或退出时调用。"""
        if self._pending:
            _oleaut32.SysFreeString(self._pending)
            self._pending = 0

    def wait(self, timeout_ms: int = 3000) -> int:
        """等到念完，返回实际等待的毫秒数。给自检取证用，界面不依赖它。

        念得越久说明文本越长；如果返回接近 0，说明前一次 `say()` 只是
        「成功地把文字交出去」，引擎根本没开始念。`timeout_ms` 必须给，
        不能无限等——真出问题时那会变成一个卡死的自检。
        """
        if self._voice is None:
            return 0
        started = time.time()
        try:
            self._voice.call("WaitUntilDone", [_i4(max(0, int(timeout_ms)))])
        except OSError:
            return 0
        return int((time.time() - started) * 1000)

    def shutdown(self) -> None:
        """释放 COM 对象。只在退出时调用，重复调用安全。"""
        self._release_pending()
        if self._voice is not None:
            self._voice.release()
            self._voice = None


_SPEAKER = Speaker()


def speaker() -> Speaker:
    """取进程级朗读器。界面与自检共用同一个实例。"""
    return _SPEAKER
