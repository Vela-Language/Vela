cdef long long collatz_len(long long n):
    cdef long long steps = 0
    while n != 1:
        if n % 2 == 0:
            n = n // 2
        else:
            n = 3 * n + 1
        steps = steps + 1
    return steps


def main():
    cdef long long best = 0
    cdef long long best_i = 0
    cdef long long i, s
    for i in range(1, 300000):
        s = collatz_len(i)
        if s > best:
            best = s
            best_i = i
    print(best_i, best)


main()
