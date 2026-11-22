"""Vela pure-Python standard library (A3 subset) & interpreter services (v0.7)."""
from __future__ import annotations

STDLIB_SOURCES: dict[str, str] = {
    "math": '''
pi = 3.141592653589793
e = 2.718281828459045
tau = 6.283185307179586
inf = 1e300

def sqrt(x: float) -> float:
    return __math_sqrt(x)

def fabs(x: float) -> float:
    return abs(x)

def trunc(x: float) -> int:
    return int(x)

def floor(x: float) -> int:
    t = int(x)
    if x < 0.0 and float(t) != x:
        return t - 1
    return t

def ceil(x: float) -> int:
    t = int(x)
    if x > 0.0 and float(t) != x:
        return t + 1
    return t

def gcd(a: int, b: int) -> int:
    a = abs(a)
    b = abs(b)
    while b != 0:
        t = a % b
        a = b
        b = t
    return a

def lcm(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return abs(a * b) // gcd(a, b)

def factorial(n: int) -> int:
    res = 1
    i = 2
    while i <= n:
        res = res * i
        i = i + 1
    return res

def isqrt(n: int) -> int:
    if n < 2:
        return n
    x = n
    y = (x + 1) // 2
    while y < x:
        x = y
        y = (x + n // x) // 2
    return x

def comb(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    if k == 0 or k == n:
        return 1
    if k > n - k:
        k = n - k
    res = 1
    i = 1
    while i <= k:
        res = (res * (n - k + i)) // i
        i = i + 1
    return res

def perm(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    res = 1
    i = 0
    while i < k:
        res = res * (n - i)
        i = i + 1
    return res
''',

    "random": '''
_SEED = 123456789

def seed(s: int) -> int:
    global _SEED
    if s == 0:
        _SEED = 1
    else:
        _SEED = abs(s)
    return _SEED

def _next() -> int:
    global _SEED
    _SEED = (_SEED * 1103515245 + 12345) & 2147483647
    return _SEED

def randrange(start: int, stop: int) -> int:
    span = stop - start
    if span <= 0:
        return start
    return start + (_next() % span)

def randint(a: int, b: int) -> int:
    return randrange(a, b + 1)

def choice(seq):
    idx = _next() % len(seq)
    return seq[idx]

def random() -> float:
    return float(_next()) / 2147483648.0
''',

    "collections": '''
def Counter(seq):
    counts = {}
    for x in seq:
        counts[x] = counts.get(x, 0) + 1
    return counts

class deque:
    def __init__(self, init_list):
        self._data = list(init_list)
    def append(self, x):
        self._data.append(x)
    def appendleft(self, x):
        res = [x]
        res.extend(self._data)
        self._data = res
    def pop(self):
        return self._data.pop()
    def popleft(self):
        return self._data.pop(0)
    def __len__(self):
        return len(self._data)
    def __getitem__(self, idx):
        return self._data[idx]
    def to_list(self):
        return self._data
''',

    "itertools": '''
def chain(a, b):
    res = list(a)
    res.extend(b)
    return res

def repeat(val: int, times: int):
    res = []
    i = 0
    while i < times:
        res.append(val)
        i = i + 1
    return res

def islice(seq, start: int, stop: int):
    return seq[start:stop]

def accumulate(seq):
    res = []
    acc = 0
    for x in seq:
        acc = acc + x
        res.append(acc)
    return res

def product_sum(a, b) -> int:
    tot = 0
    for x in a:
        for y in b:
            tot = tot + x * y
    return tot
''',

    "functools": '''
def reduce(fn, seq, initial: int = 0) -> int:
    acc = initial
    for x in seq:
        acc = fn(acc, x)
    return acc
''',

    "json": '''
def dumps_int_list(xs) -> str:
    s = "["
    n = len(xs)
    i = 0
    while i < n:
        if i > 0:
            s = s + ", "
        s = s + str(xs[i])
        i = i + 1
    return s + "]"

def loads_int_list(text: str):
    res = []
    n = len(text)
    cur = 0
    in_num = 0
    sign = 1
    i = 0
    while i < n:
        ch = text[i]
        if ch == "-":
            sign = -1
        elif ch >= "0" and ch <= "9":
            d = 0
            if ch == "1":
                d = 1
            elif ch == "2":
                d = 2
            elif ch == "3":
                d = 3
            elif ch == "4":
                d = 4
            elif ch == "5":
                d = 5
            elif ch == "6":
                d = 6
            elif ch == "7":
                d = 7
            elif ch == "8":
                d = 8
            elif ch == "9":
                d = 9
            cur = cur * 10 + d
            in_num = 1
        else:
            if in_num == 1:
                res.append(cur * sign)
                cur = 0
                in_num = 0
                sign = 1
        i = i + 1
    if in_num == 1:
        res.append(cur * sign)
    return res

def dumps(xs) -> str:
    return dumps_int_list(xs)

def loads(text: str):
    return loads_int_list(text)
''',

    "re": '''
def _is_digit(ch: str) -> bool:
    return ch >= "0" and ch <= "9"

def _is_word(ch: str) -> bool:
    return (ch >= "a" and ch <= "z") or (ch >= "A" and ch <= "Z") or (ch >= "0" and ch <= "9") or ch == "_"

def _is_space(ch: str) -> bool:
    return ch == " " or ch == "\\t" or ch == "\\n" or ch == "\\r"

def search(pattern: str, text: str) -> int:
    if pattern.startswith("^"):
        sub = pattern[1:]
        if text.startswith(sub):
            return 0
        return -1
    if pattern.endswith("$"):
        sub = pattern[:-1]
        if text.endswith(sub):
            return len(text) - len(sub)
        return -1
    if pattern == "\\\\d+":
        n = len(text)
        i = 0
        while i < n:
            if _is_digit(text[i]):
                return i
            i = i + 1
        return -1
    if pattern == "\\\\w+":
        n = len(text)
        i = 0
        while i < n:
            if _is_word(text[i]):
                return i
            i = i + 1
        return -1
    return text.find(pattern)

def match(pattern: str, text: str) -> bool:
    if pattern.startswith("^"):
        pattern = pattern[1:]
    if pattern == "\\\\d+":
        return len(text) > 0 and _is_digit(text[0])
    if pattern == "\\\\w+":
        return len(text) > 0 and _is_word(text[0])
    return text.startswith(pattern)

def findall_digits(text: str):
    res = []
    n = len(text)
    cur = 0
    in_d = 0
    i = 0
    while i < n:
        ch = text[i]
        if _is_digit(ch):
            d = int(ch == "1") + int(ch == "2") * 2 + int(ch == "3") * 3 + int(ch == "4") * 4 + int(ch == "5") * 5 + int(ch == "6") * 6 + int(ch == "7") * 7 + int(ch == "8") * 8 + int(ch == "9") * 9
            cur = cur * 10 + d
            in_d = 1
        else:
            if in_d == 1:
                res.append(cur)
                cur = 0
                in_d = 0
        i = i + 1
    if in_d == 1:
        res.append(cur)
    return res

def sub(pattern: str, repl: str, text: str) -> str:
    idx = text.find(pattern)
    if idx < 0:
        return text
    plen = len(pattern)
    return text[:idx] + repl + text[idx + plen:]
''',

    "datetime": '''
def _pad2(n: int) -> str:
    if n < 10:
        return "0" + str(n)
    return str(n)

class date:
    def __init__(self, year: int, month: int, day: int):
        self.year = year
        self.month = month
        self.day = day
    def isoformat(self) -> str:
        return str(self.year) + "-" + _pad2(self.month) + "-" + _pad2(self.day)
    def toordinal(self) -> int:
        return self.year * 365 + self.month * 30 + self.day
    def __eq__(self, other) -> bool:
        return self.year == other.year and self.month == other.month and self.day == other.day
    def __str__(self) -> str:
        return self.isoformat()

class timedelta:
    def __init__(self, days: int):
        self.days = days
    def total_seconds(self) -> int:
        return self.days * 86400
    def __add__(self, other):
        return timedelta(self.days + other.days)
    def __eq__(self, other) -> bool:
        return self.days == other.days

class datetime:
    def __init__(self, year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0):
        self.year = year
        self.month = month
        self.day = day
        self.hour = hour
        self.minute = minute
        self.second = second
    def isoformat(self) -> str:
        return str(self.year) + "-" + _pad2(self.month) + "-" + _pad2(self.day) + "T" + _pad2(self.hour) + ":" + _pad2(self.minute) + ":" + _pad2(self.second)
    def __str__(self) -> str:
        return self.isoformat()
''',

    "gc": '''
_GC_ENABLED = 1

def collect() -> int:
    __gc_collect()
    return 0

def enable() -> int:
    global _GC_ENABLED
    _GC_ENABLED = 1
    return 0

def disable() -> int:
    global _GC_ENABLED
    _GC_ENABLED = 0
    return 0

def isenabled() -> bool:
    global _GC_ENABLED
    return _GC_ENABLED != 0

def get_count() -> int:
    return __gc_live()
''',

    "weakref": '''
class ref:
    def __init__(self, ptr: int):
        self._ptr = ptr
    def __call__(self) -> int:
        if self._ptr < 65536 or __gc_is_alive(self._ptr):
            return self._ptr
        return 0
    def alive(self) -> bool:
        return self._ptr < 65536 or __gc_is_alive(self._ptr)

def finalize(obj, cb_name: str) -> int:
    return 1
''',

    "traceback": '''
def print_exc() -> int:
    __dump_frames()
    return 0

def format_exc() -> str:
    return __exc_desc()
''',

    "inspect": '''
def isfunction(obj) -> bool:
    return True
''',

    "threading": '''
_ACTIVE = 1

class Lock:
    def __init__(self):
        self._locked = 0
    def acquire(self) -> bool:
        self._locked = 1
        return True
    def release(self) -> int:
        self._locked = 0
        return 0
    def locked(self) -> bool:
        return self._locked != 0
    def __enter__(self) -> int:
        self._locked = 1
        return 1
    def __exit__(self, et: int, ev: int, tb: int) -> int:
        self._locked = 0
        return 0

class Thread:
    def __init__(self, target, arg: int = 0):
        self.target = target
        self.arg = arg
        self.done = 0
        self.result = 0
    def start(self) -> int:
        f = self.target
        self.result = f(self.arg)
        self.done = 1
        return 0
    def join(self) -> int:
        return self.result
    def is_alive(self) -> bool:
        return self.done == 0

def active_count() -> int:
    return _ACTIVE
''',

    "signal": '''
SIGINT = 2
SIGTERM = 15
SIGABRT = 22
_LAST_SIG = 0

def signal(sig: int, handler: int) -> int:
    global _LAST_SIG
    _LAST_SIG = sig
    return handler

def getsignal(sig: int) -> int:
    global _LAST_SIG
    return _LAST_SIG

def raise_signal(sig: int) -> int:
    global _LAST_SIG
    _LAST_SIG = sig
    return sig
''',

    "faulthandler": '''
_FH_ENABLED = 0

def enable() -> int:
    global _FH_ENABLED
    _FH_ENABLED = 1
    return 1

def disable() -> int:
    global _FH_ENABLED
    _FH_ENABLED = 0
    return 0

def is_enabled() -> bool:
    global _FH_ENABLED
    return _FH_ENABLED != 0

def dump_traceback() -> int:
    __dump_frames()
    return 0
''',

    "dataclasses": '''
def dataclass(cls):
    return cls
''',

    "typing": '''
Any = 0
Optional = 0
Union = 0
List = 0
Dict = 0
Tuple = 0
Set = 0
Callable = 0
Iterable = 0
Iterator = 0
Generator = 0
TypeVar = 0
Generic = 0
Final = 0
ClassVar = 0

def cast(typ: int, val):
    return val
''',

    "unittest": '''
class TestCase:
    def assertEqual(self, a, b):
        assert a == b
    def assertNotEqual(self, a, b):
        assert a != b
    def assertTrue(self, x):
        assert bool(x)
    def assertFalse(self, x):
        assert not bool(x)
    def assertIn(self, a, b):
        assert a in b
    def assertNotIn(self, a, b):
        assert a not in b
    def assertGreater(self, a, b):
        assert a > b
    def assertGreaterEqual(self, a, b):
        assert a >= b
    def assertLess(self, a, b):
        assert a < b
    def assertLessEqual(self, a, b):
        assert a <= b
    def assertIs(self, a, b):
        assert a == b
''',

    "importlib": '''
def import_module(name: str) -> str:
    return name
''',

    "asyncio": '''
def run(coro_val):
    return coro_val

def sleep(delay: int) -> int:
    return delay
''',
}

STDLIB_MODULES: set[str] = set(STDLIB_SOURCES) | {"sys"}
