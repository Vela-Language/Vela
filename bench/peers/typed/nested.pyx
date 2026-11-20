def run(long long n, long long m):
    cdef long long total = 0
    cdef long long a = 0
    cdef long long b = 0
    while a < n:
        b = 0
        while b < m:
            total += 1
            b += 1
        a += 1
    return total


def bench():
    cdef long long total = 0
    cdef long long k = 0
    cdef long long n = 1000
    cdef long long m = 1000
    while k < 20:
        total += run(n, m)
        k += 1
    print(total)


bench()
