def main():
    cdef long long total = 0
    cdef long long i = 0
    while i < 100000000:
        total = total + i
        i = i + 1
    print(total)


main()
