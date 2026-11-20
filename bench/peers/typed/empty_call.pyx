def empty():
    return 0


def run(long long n):
    cdef long long i = 0
    while i < n:
        empty()
        i += 1
    return i


def bench():
    cdef long long total = 0
    cdef long long k = 0
    cdef long long n = 1000000
    while k < 20:
        total += run(n)
        k += 1
    print(total)


bench()
