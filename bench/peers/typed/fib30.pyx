cdef long long fib(long long n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def run(long long n):
    return fib(n)


def bench():
    cdef long long total = 0
    cdef long long k = 0
    cdef long long n = 30
    while k < 5:
        total += run(n)
        k += 1
    print(total)


bench()
