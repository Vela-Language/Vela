def collatz_len(n):
    steps = 0
    while n != 1:
        if n % 2 == 0:
            n = n // 2
        else:
            n = 3 * n + 1
        steps = steps + 1
    return steps


def main():
    best = 0
    best_i = 0
    for i in range(1, 300000):
        s = collatz_len(i)
        if s > best:
            best = s
            best_i = i
    print(best_i, best)


main()
