def run(long long n):
    cdef long long r = 0
    cdef long long i = 0
    while i < n:
        r = (r * 7 + 13) % 1000003
        i += 1
    return r


def bench():
    cdef long long total = 0
    cdef long long k = 0
    cdef long long n = 1000000
    while k < 5:
        total += run(n)
        k += 1
    print(total)


bench()
