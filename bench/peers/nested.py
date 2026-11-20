def run():
    n = 0
    a = 0
    while a < 1000:
        b = 0
        while b < 1000:
            n = n + 1
            b = b + 1
        a = a + 1
    return n


def bench():
    total = 0
    k = 0
    while k < 20:
        total = total + run()
        k = k + 1
    print(total)


bench()
