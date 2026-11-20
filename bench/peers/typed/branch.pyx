def run(long long n):
    cdef long long c = 0
    cdef long long i = 0
    while i < n:
        if i % 2 == 0:
            c += 1
        else:
            c -= 1
        i += 1
    return c


def bench():
    cdef long long total = 0
    cdef long long k = 0
    cdef long long n = 10000000
    while k < 2:
        total += run(n)
        k += 1
    print(total)


bench()
