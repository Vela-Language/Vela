def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def run():
    return fib(30)


def bench():
    total = 0
    k = 0
    while k < 5:
        total = total + run()
        k = k + 1
    print(total)


bench()
