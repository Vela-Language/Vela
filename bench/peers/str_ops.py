def run():
    s1 = "vela_native_compiler_x86_64_windows_pe32plus"
    s2 = "vela_native_compiler_x86_64_windows_pe32plus"
    s3 = "cpython_bytecode_interpreter_runtime"
    acc = 0
    i = 0
    while i < 100000:
        if s1 == s2 and s1 != s3:
            acc = acc + len(s1) + len(s3)
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
