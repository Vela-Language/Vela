def run():
    cdef list xs = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9, 3]
    cdef long long acc = 0
    cdef long long i = 0
    while i < 100000:
        acc = acc + <long long>xs[i & 15] + len(xs)
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
