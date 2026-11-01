"""Checador FALSO para testes: falha (código 1) se algum arquivo contiver 'BAD'. Não é uma ferramenta real."""
import sys

bad = [f for f in sys.argv[1:] if "BAD" in open(f, encoding="utf-8").read()]
if bad:
    print("\n".join(f"{f}:1: erro simulado: BAD encontrado" for f in bad))
    sys.exit(1)
print("ok")
