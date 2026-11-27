def run():
    xs = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9, 3]
    acc = 0
    i = 0
    while i < 100000:
        acc = acc + xs[i & 15] + len(xs)
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
