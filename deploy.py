"""Confere, testa e manda o projeto para o Pi, num comando só (e numa permissão só).

    py -3.14 deploy.py           pyflakes + self-check + copia para o Pi + reinicia o serviço + mostra o status
    py -3.14 deploy.py --docs    só copia README e PLANO (não reinicia nada)
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

PI = os.environ.get("F1TEL_PI")  # usuario@ip do Pi; no Windows: setx F1TEL_PI usuario@ip
PASTA = Path(__file__).parent
CODIGO = ["f1tel.py", "index.html", "pistas.json", "bordas.py"]
DOCS = ["README.md", "PLANO.md"]


def rodar(*cmd, **kw):
    print("$", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=PASTA, **kw)
    if r.returncode != 0:
        sys.exit(f"Falhou: {' '.join(cmd)}")
    return r


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if not PI:
        sys.exit("Defina a variavel de ambiente F1TEL_PI (usuario@ip do Pi).")
    if "--docs" in sys.argv:
        rodar("scp", "-q", *DOCS, f"{PI}:f1tel/")
        return print("Docs copiados.")
    rodar(sys.executable, "-m", "pyflakes", "f1tel.py", "bordas.py", "deploy.py")
    rodar(sys.executable, "f1tel.py", "--teste")
    rodar("ssh", PI, "mkdir -p ~/f1tel/bordas")
    rodar("scp", "-q", *CODIGO, *DOCS, f"{PI}:f1tel/")
    bordas = sorted(str(p.relative_to(PASTA)) for p in (PASTA / "bordas").glob("*.json"))
    if bordas:
        rodar("scp", "-q", *bordas, f"{PI}:f1tel/bordas/")
    rodar("ssh", PI, "cd ~/f1tel && python3 f1tel.py --teste >/dev/null && systemctl --user restart f1tel "
                     "&& sleep 2 && systemctl --user is-active f1tel")
    estado = json.load(urllib.request.urlopen(f"http://{PI.split('@')[1]}:8000/estado", timeout=10))
    print("Pi no ar:", json.dumps(estado, ensure_ascii=False))


if __name__ == "__main__":
    main()
