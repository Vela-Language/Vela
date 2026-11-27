def run():
    d = {0: 10, 1: 20, 2: 30, 3: 40, 4: 50, 5: 60, 6: 70, 7: 80}
    acc = 0
    i = 0
    while i < 100000:
        acc = acc + d[i & 7] + len(d)
        i = i + 1
    return acc


def bench():
    total = 0
    k = 0
    while k < 20:
        total = total + run()
        k = k + 1
    print(total)


bench()
