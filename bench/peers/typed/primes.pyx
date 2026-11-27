cdef bint is_prime(long long n):
    if n < 2:
        return False
    cdef long long d = 2
    while d * d <= n:
        if n % d == 0:
            return False
        d = d + 1
    return True


def main():
    cdef long long count = 0
    cdef long long i
    for i in range(1, 200000):
        if is_prime(i):
            count = count + 1
    print(count)


main()
