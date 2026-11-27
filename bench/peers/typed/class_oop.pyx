cdef class Counter:
    cdef public long long base
    cdef public long long step

    def __init__(self, long long base, long long step):
        self.base = base
        self.step = step

    cpdef long long calc(self, long long x):
        return x * 3 + 7


def run():
    cdef Counter c = Counter(10, 2)
    cdef long long acc = c.base
    cdef long long st = c.step
    cdef long long i = 0
    while i < 100000:
        acc = acc + c.calc(i & 15) + st
        i = i + 1
    return acc


def bench():
    cdef long long total = 0
    cdef long long k = 0
    while k < 20:
        total = total + run()
        k = k + 1
    print(total)


bench()
