from support.fixtures import words

def program(operations):
    instructions, literals, reads = [], [], []

    def literal(register, value):
        literals.append((len(instructions), register, value))
        instructions.append(0)

    for kind, size, address, value in operations:
        literal(1, address)
        if kind == 'write':
            literal(0, value)
            instructions.append({1:0xE5C10000, 2:0xE1C100B0, 4:0xE5810000}[size])
        else:
            reads.append((len(instructions), address, size))
            instructions.append({1:0xE5D12000, 2:0xE1D120B0, 4:0xE5912000}[size])
    instructions.append(0xEAFFFFFE)
    limit = len(instructions)
    pool = []
    for instruction, register, value in literals:
        displacement = (len(instructions)+len(pool))*4-instruction*4-8
        assert 0 <= displacement <= 4095
        instructions[instruction] = 0xE59F0000 | register << 12 | displacement
        pool.append(value)
    return words(*instructions, *pool), limit, reads
