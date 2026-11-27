class Counter:
    def __init__(self, base: int, step: int):
        self.base = base
        self.step = step

    def calc(self, x: int) -> int:
        return x * 3 + 7


def run():
    c = Counter(10, 2)
    acc = c.base
    st = c.step
    i = 0
    while i < 100000:
        acc = acc + c.calc(i & 15) + st
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
