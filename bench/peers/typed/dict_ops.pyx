def run():
    cdef dict d = {0: 10, 1: 20, 2: 30, 3: 40, 4: 50, 5: 60, 6: 70, 7: 80}
    cdef long long acc = 0
    cdef long long i = 0
    while i < 100000:
        acc = acc + <long long>d[i & 7] + len(d)
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
