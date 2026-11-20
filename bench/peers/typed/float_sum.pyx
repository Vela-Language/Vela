def run(double x, long long n):
    cdef double f = 0.0
    cdef long long i = 0
    while i < n:
        f += x
        i += 1
    return f


def bench():
    cdef double total = 0.0
    cdef long long k = 0
    cdef double x = 1.5
    cdef long long n = 1000000
    while k < 20:
        total += run(x, n)
        k += 1
    print(total)


bench()
