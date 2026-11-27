def is_prime(n):
    if n < 2:
        return False
    d = 2
    while d * d <= n:
        if n % d == 0:
            return False
        d = d + 1
    return True


def main():
    count = 0
    for i in range(1, 200000):
        if is_prime(i):
            count = count + 1
    print(count)


main()
