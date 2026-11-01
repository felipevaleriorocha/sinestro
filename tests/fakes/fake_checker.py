"""Checador FALSO para testes: falha (código 1) se algum arquivo contiver 'BAD'. Não é uma ferramenta real.
A saída inclui a primeira linha do arquivo, para que falhas diferentes gerem assinaturas diferentes."""
import sys

failed = False
for f in sys.argv[1:]:
    text = open(f, encoding="utf-8").read()
    if "BAD" in text:
        failed = True
        print(f"{f}:1: erro simulado: BAD encontrado ({text.splitlines()[0][:40]})")
print("ok" if not failed else "")
sys.exit(1 if failed else 0)
