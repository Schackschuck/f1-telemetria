"""Bordas da pista a partir do TUMFTM racetrack-database, encaixadas nas coordenadas do jogo.

    py bordas.py 16          (trackId do jogo; precisa de uma volta gravada dessa pista em voltas/)

O dataset tem a linha central da pista real (OpenStreetMap, suavizada) e a largura para cada lado
(medida em imagem de satélite). O script acha a rotação, o deslocamento e a escala que levam essa
linha até a sua melhor volta, refina com ICP e grava bordas/<trackId>.json com as bordas em x, z do jogo.
Fonte: github.com/TUMFTM/racetrack-database (LGPL-3.0).
"""
import json
import math
import sys
import urllib.request
from pathlib import Path

PASTA = Path(__file__).parent
URL = "https://raw.githubusercontent.com/TUMFTM/racetrack-database/master/tracks/{}.csv"
# trackId do jogo -> arquivo do dataset (só as pistas que existem lá)
TUMFTM = {0: "Melbourne", 2: "Shanghai", 3: "Sakhir", 4: "Catalunya", 6: "Montreal", 7: "Silverstone",
          8: "Hockenheim", 9: "Budapest", 10: "Spa", 11: "Monza", 13: "Suzuka", 14: "YasMarina",
          15: "Austin", 16: "SaoPaulo", 17: "Spielberg", 18: "Sochi", 19: "MexicoCity", 26: "Zandvoort"}


def comprimento(p):
    return [0.0] + [sum(math.dist(p[i], p[i + 1]) for i in range(k)) for k in range(1, len(p))]


def reamostrar(p, n, fechada):
    """n pontos igualmente espaçados ao longo da linha."""
    q = p + [p[0]] if fechada else p
    acum = [0.0]
    for i in range(1, len(q)):
        acum.append(acum[-1] + math.dist(q[i - 1], q[i]))
    total, out, j = acum[-1], [], 0
    for k in range(n):
        s = total * k / (n if fechada else n - 1)
        while j < len(acum) - 2 and acum[j + 1] < s:
            j += 1
        f = (s - acum[j]) / ((acum[j + 1] - acum[j]) or 1)
        out.append((q[j][0] + f * (q[j + 1][0] - q[j][0]), q[j][1] + f * (q[j + 1][1] - q[j][1])))
    return out


def ajustar(a, b):
    """Escala, rotação e deslocamento que levam os pontos a até os pontos b (mínimos quadrados, 2D)."""
    n = len(a)
    ma = (sum(p[0] for p in a) / n, sum(p[1] for p in a) / n)
    mb = (sum(p[0] for p in b) / n, sum(p[1] for p in b) / n)
    dot = cruz = norma = 0.0
    for (ax, ay), (bx, by) in zip(a, b):
        ax, ay, bx, by = ax - ma[0], ay - ma[1], bx - mb[0], by - mb[1]
        dot += ax * bx + ay * by
        cruz += ax * by - ay * bx
        norma += ax * ax + ay * ay
    th = math.atan2(cruz, dot)
    s = (math.cos(th) * dot + math.sin(th) * cruz) / (norma or 1)
    c, sn = s * math.cos(th), s * math.sin(th)
    t = (mb[0] - (c * ma[0] - sn * ma[1]), mb[1] - (sn * ma[0] + c * ma[1]))
    f = lambda p: (c * p[0] - sn * p[1] + t[0], sn * p[0] + c * p[1] + t[1])
    erro = math.sqrt(sum(math.dist(f(p), q) ** 2 for p, q in zip(a, b)) / n)
    return f, s, erro


def mais_proximo(p, pts):
    return min(pts, key=lambda q: (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2)


def main(pista):
    nome = TUMFTM.get(pista)
    if not nome:
        sys.exit(f"Pista {pista} não existe no TUMFTM racetrack-database.")
    voltas = [json.loads(a.read_text()) for a in (PASTA / "voltas").glob(f"{pista:02d}_*_eu_*.json")]
    voltas = sorted((v for v in voltas if not v["invalida"]), key=lambda v: v["tempo_ms"])
    if not voltas:
        sys.exit("Precisa de uma volta válida dessa pista em voltas/.")
    volta = voltas[0]

    linhas = urllib.request.urlopen(URL.format(nome), timeout=30).read().decode().splitlines()
    dados = [tuple(map(float, l.split(","))) for l in linhas if l and not l.startswith("#")]
    centro = [(x, y) for x, y, _, _ in dados]

    # Jogo: (x, z). Testa com e sem espelho, os dois sentidos e todos os pontos de largada.
    N = 300
    jogo = reamostrar(list(zip(volta["x"], volta["z"])), N, fechada=False)
    melhor = None
    for espelho in (1, -1):
        g = [(x, espelho * z) for x, z in jogo]
        for sentido in (1, -1):
            c = reamostrar(centro[::sentido], N, fechada=True)
            for k in range(N):
                f, s, erro = ajustar(c[k:] + c[:k], g)
                if not melhor or erro < melhor[0]:
                    melhor = (erro, espelho, f)
    erro, espelho, f = melhor
    print(f"Encaixe inicial: erro médio {erro:.1f} m (espelho {'sim' if espelho < 0 else 'não'})")

    # ICP: cada ponto da volta casa com o ponto mais próximo da linha real, e o ajuste é refeito.
    g = [(x, espelho * z) for x, z in reamostrar(list(zip(volta["x"], volta["z"])), 600, fechada=False)]
    denso = reamostrar(centro, 2000, fechada=True)
    for _ in range(12):
        movido = [f(p) for p in denso]
        pares = [denso[movido.index(mais_proximo(q, movido))] for q in g]
        f, s, erro = ajustar(pares, g)
    print(f"Depois do ICP: erro médio {erro:.1f} m, escala {s:.3f}")
    if abs(s - 1) > 0.03:
        print("Atenção: escala longe de 1. O traçado do dataset pode ser diferente do jogo.")

    # Bordas no sistema do dataset: centro ± normal × largura (direita/esquerda no sentido dos pontos).
    n, esq, dir_ = len(dados), [], []
    for i, (x, y, wd, we) in enumerate(dados):
        x0, y0 = dados[i - 1][:2]
        x1, y1 = dados[(i + 1) % n][:2]
        m = math.hypot(x1 - x0, y1 - y0) or 1
        tx, ty = (x1 - x0) / m, (y1 - y0) / m
        dir_.append((x + ty * wd, y - tx * wd))
        esq.append((x - ty * we, y + tx * we))
    # Um ponto por metro: as zebras marcadas na borda começam e terminam no lugar certo.
    um_por_metro = lambda pts: reamostrar(pts, int(sum(math.dist(pts[i - 1], pts[i]) for i in range(len(pts)))), True)
    esq, dir_ = um_por_metro(esq), um_por_metro(dir_)
    para_jogo = lambda p: [round(f(p)[0], 1), round(espelho * f(p)[1], 1)]

    # Conferência: quanto da sua volta ficou entre as bordas.
    centro_jogo = [f(p) for p in centro]
    dentro = 0
    for q in g[::5]:
        i = centro_jogo.index(mais_proximo(q, centro_jogo))
        dentro += math.dist(q, centro_jogo[i]) <= max(dados[i][2], dados[i][3]) * s + 1.0
    print(f"Pontos da volta dentro das bordas: {100 * dentro / len(g[::5]):.0f}%")

    saida = PASTA / "bordas"
    saida.mkdir(exist_ok=True)
    (saida / f"{pista}.json").write_text(json.dumps({
        "fonte": f"TUMFTM racetrack-database ({nome}.csv), LGPL-3.0",
        "erro_m": round(erro, 2), "escala": round(s, 4),
        "esq": [para_jogo(p) for p in esq], "dir": [para_jogo(p) for p in dir_],
    }))
    print(f"Gravado bordas/{pista}.json")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 16)
