def run():
    r = 0
    i = 0
    while i < 1000000:
        r = (r * 7 + 13) % 1000003
        i = i + 1
    return r


def bench():
    total = 0
    k = 0
    while k < 5:
        total = total + run()
        k = k + 1
    print(total)


bench()
