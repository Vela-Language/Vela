def run():
    f = 0.0
    i = 0
    while i < 1000000:
        f = f + 1.5
        i = i + 1
    return f


def bench():
    total = 0.0
    k = 0
    while k < 20:
        total = total + run()
        k = k + 1
    print(total)


bench()
