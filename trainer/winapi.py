"""Win32 DLL handles.

Off Windows (offline tests on another system) the modules still import, and
every Win32 function returns 0, the value Win32 uses for failure, so callers
take their normal error paths.
"""
import ctypes


class _Missing:
    def __init__(self, dll, name):
        self.dll, self.name = dll, name

    def __call__(self, *_args, **_kwargs):
        return 0


class _Unavailable:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        fn = _Missing(self._name, attr)
        setattr(self, attr, fn)  # keep restype/argtypes and test patches on one object
        return fn


def load(name):
    windll = getattr(ctypes, "WinDLL", None)
    return windll(name, use_last_error=True) if windll else _Unavailable(name)
