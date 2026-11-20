def run():
    c = 0
    i = 0
    while i < 10000000:
        if i % 2 == 0:
            c = c + 1
        else:
            c = c - 1
        i = i + 1
    return c


def bench():
    total = 0
    k = 0
    while k < 2:
        total = total + run()
        k = k + 1
    print(total)


bench()
