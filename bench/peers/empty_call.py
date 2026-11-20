def empty():
    return 0


def run():
    i = 0
    while i < 1000000:
        empty()
        i = i + 1
    return i


def bench():
    total = 0
    k = 0
    while k < 20:
        total = total + run()
        k = k + 1
    print(total)


bench()
