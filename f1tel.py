"""F1 24: recebe a telemetria UDP, grava as voltas e compara numa página local.

    py f1tel.py          escuta a porta UDP 20777 e serve a página em http://<ip>:8000
    py f1tel.py --teste  self-check da análise
"""
import bisect
import functools
import io
import json
import math
import re
import statistics
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORTA_UDP = 20777
PORTA_HTTP = 8000
PASTA = Path(__file__).parent
VOLTAS = PASTA / "voltas"
PASSO = 5.0  # metros entre as amostras na comparação

# Diferença mínima para virar dica. Calibre aqui.
LIM_FREIO_M = 5
LIM_VEL_KMH = 3
LIM_ACEL_M = 10
LIM_LADO_M = 0.7
LIM_PEDAL = 0.15  # força média do freio (0 a 1)

# Superfície embaixo de cada roda (surfaceType do jogo).
ZEBRA = {1}
FORA = {3, 4, 5, 6, 7}  # pedra, brita, lama, areia, grama
# Geometria aproximada do carro, para achar cada roda a partir do centro. Calibre aqui.
MEIA_BITOLA = 0.8   # m do centro até a roda, para o lado
EIXO = 1.8          # m do centro até cada eixo, para frente e para trás

PISTAS = ["Melbourne", "Paul Ricard", "Xangai", "Bahrein", "Barcelona", "Mônaco", "Montreal",
          "Silverstone", "Hockenheim", "Hungaroring", "Spa", "Monza", "Singapura", "Suzuka",
          "Abu Dhabi", "Austin", "Interlagos", "Áustria", "Sochi", "México", "Baku",
          "Bahrein curto", "Silverstone curto", "Austin curto", "Suzuka curto", "Hanói",
          "Zandvoort", "Ímola", "Portimão", "Jeddah", "Miami", "Las Vegas", "Losail"]

# Formato UDP 2024 (especificação da EA). Cada pacote traz 22 carros.
HEADER = struct.Struct("<HBBBBBQfIIBB")  # 29 bytes
LAP = struct.Struct("<II12xf9xB3xB")     # lastLap, currentLap, lapDistance, currentLapNum, currentLapInvalid
TEL = struct.Struct("<HfffBb40xBBBB")    # speed, throttle, steer, brake, clutch, gear, surfaceType[RL, RR, FL, FR]
MOT = struct.Struct("<fff12xhhhhhh")     # worldPosition X Y Z, worldForwardDir X Y Z, worldRightDir X Y Z (int16 normalizado)
TAM_LAP, TAM_TEL, TAM_MOT = 57, 60, 60
CAMPOS = ("d", "t", "v", "acel", "freio", "marcha", "x", "z", "sup", "rx", "rz", "fx", "fz")
# Botões "UDP Action" (mapeados no jogo em Controles), no buttonStatus do evento BUTN.
BOTOES = {1: 0x00100000, 2: 0x00200000}  # 1: marca o ponto para o engenheiro explicar; 2: relatório da última volta


def nome_pista(i):
    return PISTAS[i] if 0 <= i < len(PISTAS) else f"Pista {i}"


# ---------- Recepção e gravação ----------

class Carro:
    def __init__(self, nome, i):
        self.nome = nome
        self.i = i         # índice do carro nos pacotes
        self.num = None    # número da volta atual
        self.lap = None    # (distância, tempo em ms) do último LapData
        self.inv = False
        self.pos = None    # (quadro, x, z, rx, rz, fx, fz): posição, vetores "direita" e "frente"
        self.tel = None    # (quadro, v, acel, freio, marcha, sup)
        self.quadro = None # último quadro amostrado
        self.t0 = None     # rival: sessionTime do começo da volta (o tempo que o jogo manda do fantasma é falso)
        self.marcas = []   # distâncias onde o piloto apertou UDP Action 1 nesta volta
        self.am = []       # amostras da volta atual


def amostrar(c):
    """Junta posição e telemetria do MESMO quadro do jogo (os pacotes chegam separados e em qualquer ordem)."""
    if not (c.pos and c.tel and c.lap) or c.pos[0] != c.tel[0] or c.tel[0] == c.quadro or c.lap[0] < 0:
        return  # falta um dos dois, quadros diferentes, já amostrado, ou antes da linha (out lap)
    c.quadro = c.tel[0]
    d, t = c.lap
    if c.am and c.am[-1][0] == round(d, 1):
        return  # parado (o fantasma do rival espera no fim da volta): vale a primeira amostra, com o tempo de quando chegou
    while c.am and c.am[-1][0] > d:  # flashback ou reinício: descarta o que "não aconteceu"
        c.am.pop()
    _, x, z, rx, rz, fx, fz = c.pos
    _, v, acel, freio, marcha, sup = c.tel
    c.am.append((round(d, 1), t, v, round(acel, 3), round(freio, 3), marcha,
                 round(x, 2), round(z, 2), sup, round(rx, 3), round(rz, 3), round(fx, 3), round(fz, 3)))


class Estado:
    def __init__(self):
        self.sessao = None
        self.pista = -1
        self.comp = 0      # comprimento da pista (m)
        self.rival = 255
        self.tipo = 0      # sessionType do jogo
        self.carros = {}
        self.nomes = {}    # índice -> nome do pacote Participants (gamertag, se o jogador liberou)
        self.avisou = False
        self.ultimo = 0.0  # hora do último pacote
        self.origem = None
        self.botoes = 0    # buttonStatus do último evento BUTN, para pegar só o aperto (não o soltar)
        self.apertos = {}  # ação -> hora do último aperto aceito
        self.fala = {"id": 0, "texto": ""}  # o que o Pi quer que a página fale (a página lê pelo /estado)

    def acao(self, n):  # botão "UDP Action n" do controle; trocado no self-check
        threading.Thread(target=responder_botao, args=(n,), daemon=True).start()


ESTADO = Estado()  # lido pelo /estado da página
SESSOES = {18: "Contra-Relógio"}  # ponytail: só o tipo que importa aqui; o resto aparece como número


def processar(data, e):
    if len(data) < HEADER.size:
        return
    fmt, _, _, _, _, pid, uid, tsess, _, quadro, eu, _ = HEADER.unpack_from(data)  # sessionTime, overallFrameIdentifier
    if fmt != 2024:
        if not e.avisou:
            print(f"Pacote no formato {fmt}: no jogo, coloque Formato UDP = 2024", flush=True)
            e.avisou = True
        return
    if uid != e.sessao:
        e.sessao, e.carros, e.rival, e.nomes = uid, {}, 255, {}
    if pid == 1:  # Session
        e.comp, e.tipo, e.pista = struct.unpack_from("<HBb", data, HEADER.size + 4)
        return
    if pid == 4:  # Participants: o nome de cada carro (a cada 5 s)
        passo = (len(data) - HEADER.size - 1) // 22  # 60 bytes no F1 24; calculado para não depender da versão
        for i in range(22):
            o = HEADER.size + 1 + i * passo + 7  # aiControlled, driverId, networkId, teamId, myTeam, raceNumber, nationality
            e.nomes[i] = data[o:o + 48].split(b"\0")[0].decode("utf-8", "replace").strip() or None
        return
    if pid == 3:  # Event
        if data[HEADER.size:HEADER.size + 4] == b"BUTN":
            status, = struct.unpack_from("<I", data, HEADER.size + 4)
            apertou, e.botoes = status & ~e.botoes, status
            for n, bit in BOTOES.items():
                if apertou & bit and time.monotonic() - e.apertos.get(n, -9) > 1:  # o jogo às vezes manda o aperto 2x
                    e.apertos[n] = time.monotonic()
                    e.acao(n)
            if outras := apertou & 0xFFF00000 & ~sum(BOTOES.values()):  # UDP Action 1..12 = bits 20..31
                print(f"UDP Action {outras.bit_length() - 20} apertada, mas sem função (só 1 e 2 têm)", flush=True)
        return

    seguir = {eu: "eu"}
    if e.rival not in (255, eu):
        seguir[e.rival] = "rival"

    if pid == 2:  # LapData
        if data[-1] != e.rival:  # timeTrialRivalCarIdx (255 = nenhum)
            print(f"Rival do Contra-Relógio: {'nenhum' if data[-1] == 255 else f'carro {data[-1]}'}", flush=True)
        e.rival = data[-1]
        for i, nome in seguir.items():
            ultima, atual, dist, num, inv = LAP.unpack_from(data, HEADER.size + i * TAM_LAP)
            c = e.carros.setdefault(i, Carro(nome, i))
            if c.num is not None and num != c.num:
                fechar(c, ultima, e)
            elif nome == "rival" and c.lap and e.comp and c.lap[0] > 0.9 * e.comp and 0 <= dist < 0.1 * e.comp:
                fechar(c, None, e)  # o fantasma do Contra-Relógio fica sempre na mesma volta: fecha pela distância
                c.t0 = tsess
            if nome == "rival":
                # O F1 24 manda tempo e velocidade do fantasma fora de escala (1:47 numa volta de 1:09 real, visto
                # em 14/11/2026); a posição é a real. O tempo vem do relógio da sessão e a velocidade, do trajeto.
                c.t0 = tsess if c.t0 is None else c.t0
                atual = round((tsess - c.t0) * 1000)
            c.num, c.lap, c.inv = num, (dist, atual), bool(inv)
    elif pid == 0:  # Motion
        for i in seguir:
            if c := e.carros.get(i):
                x, _, z, fx, _, fz, rx, _, rz = MOT.unpack_from(data, HEADER.size + i * TAM_MOT)
                c.pos = (quadro, x, z, rx / 32767, rz / 32767, fx / 32767, fz / 32767)
                amostrar(c)
    elif pid == 6:  # CarTelemetry
        for i in seguir:
            if c := e.carros.get(i):
                v, acel, _, freio, _, marcha, *sup = TEL.unpack_from(data, HEADER.size + i * TAM_TEL)
                c.tel = (quadro, v, acel, freio, marcha, sup)
                amostrar(c)


def volta_do_fantasma(am):
    """Rival: corta onde o fantasma parou na linha (o tempo da volta é o de quando chegou) e refaz a velocidade
    pelo trajeto, porque a do jogo vem fora de escala."""
    k = next(i for i, a in enumerate(am) if a[0] >= am[-1][0] - 1)
    am, n = am[:k + 1], k + 1
    out = []
    for i, a in enumerate(am):
        j0, j1 = max(0, i - 10), min(n - 1, i + 10)  # ±10 amostras (0,33 s a 60 Hz): a distância vem arredondada em 0,1 m
        dt = (am[j1][1] - am[j0][1]) / 1000
        v = (am[j1][0] - am[j0][0]) / dt * 3.6 if dt > 0 else a[2]
        out.append(a[:2] + (round(v),) + a[3:])
    return out


def fechar(c, tempo_ms, e):
    """tempo_ms=None: volta do rival, fechada pela distância; o tempo sai das amostras."""
    am, c.am = c.am, []
    marcas, c.marcas = c.marcas, []
    if tempo_ms is None:
        am = volta_do_fantasma(am) if am else am
        tempo_ms = am[-1][1] if am else 0
    motivo = ("sem tempo de volta" if tempo_ms <= 0 else
              f"só {len(am)} amostras" if len(am) < 50 else
              f"começou em {am[0][0]:.0f} m" if am[0][0] > 50 else
              f"parou em {am[-1][0]:.0f} m" if e.comp and am[-1][0] < 0.9 * e.comp else None)
    if motivo:
        if am:  # volta vazia (out lap) não merece log
            print(f"Volta descartada [{c.nome}]: {motivo}", flush=True)
        return
    volta = {"pista": e.pista, "nome_pista": nome_pista(e.pista), "carro": c.nome, "piloto": e.nomes.get(c.i),
             "tempo_ms": tempo_ms, "invalida": c.inv, "quando": time.strftime("%Y-%m-%d %H:%M:%S"), "marcas": marcas}
    volta.update({k: list(col) for k, col in zip(CAMPOS, zip(*am))})
    VOLTAS.mkdir(exist_ok=True)
    arq = VOLTAS / f"{e.pista:02d}_{time.strftime('%Y%m%d-%H%M%S')}_{c.nome}_v{c.num}.json"
    arq.write_text(json.dumps(volta))
    if marcas:  # o piloto marcou pontos: o engenheiro explica cada um no rádio
        threading.Thread(target=explicar_marcas, args=(arq.name, e.pista), daemon=True).start()
    print(f"Volta gravada: {volta['nome_pista']} {fmt_tempo(tempo_ms)}"
          f"{' (inválida)' if c.inv else ''} [{c.nome}: {volta['piloto']}] -> {arq.name}", flush=True)


def fmt_tempo(ms):
    return f"{ms // 60000}:{ms % 60000 / 1000:06.3f}"


def receptor():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", PORTA_UDP))
    e = ESTADO
    while True:
        data, origem = sock.recvfrom(2048)
        if time.time() - e.ultimo > 30:
            print(f"Recebendo pacotes de {origem[0]}", flush=True)
        e.ultimo, e.origem = time.time(), origem[0]
        try:
            processar(data, e)
        except struct.error:
            pass  # pacote truncado ou de outro formato


def estado():
    """Resumo ao vivo para a página: se está chegando dado, de onde, e o que está sendo gravado."""
    e, agora = ESTADO, time.time()
    return {
        "ha_s": round(agora - e.ultimo, 1) if e.ultimo else None, "origem": e.origem,
        "pista": nome_pista(e.pista) if e.pista >= 0 else None,
        "sessao": SESSOES.get(e.tipo, f"sessão tipo {e.tipo}") if e.ultimo else None,
        "rival": None if e.rival == 255 else e.rival,
        "carros": [{"nome": c.nome, "piloto": e.nomes.get(c.i), "volta": c.num, "dist": round(c.lap[0]) if c.lap else None, "amostras": len(c.am)}
                   for c in list(e.carros.values())],
        "n_voltas": sum(1 for _ in VOLTAS.glob("*.json")) if VOLTAS.exists() else 0,
        "fala": e.fala,
    }


# ---------- Análise ----------

def reamostrar(volta, grade):
    d = volta["d"]
    out = {}
    for k in ("t", "v", "acel", "freio", "x", "z", "sup"):
        if k not in volta:  # voltas antigas não têm superfície
            continue
        ys, col = volta[k], []
        for g in grade:
            j = bisect.bisect_left(d, g)
            if j <= 0:
                col.append(ys[0])
            elif j >= len(d):
                col.append(ys[-1])
            else:
                f = (g - d[j - 1]) / (d[j] - d[j - 1]) if d[j] > d[j - 1] else 0
                if k == "sup":  # categoria: pega a amostra mais próxima
                    col.append(ys[j - 1] if f < 0.5 else ys[j])
                else:
                    col.append(ys[j - 1] + f * (ys[j] - ys[j - 1]))
        out[k] = col
    return out


def pontos_roda(volta, tipos):
    """Posição (x, z) de cada roda que estava numa superfície de `tipos`, em grade de 0,5 m."""
    if "sup" not in volta:
        return []
    x, z, rx, rz, sup = (volta[k] for k in ("x", "z", "rx", "rz", "sup"))
    n, out = len(x), set()
    for i in range(n):
        if not tipos.intersection(sup[i]):
            continue
        if "fx" in volta:  # frente do carro, enviada pelo jogo (vale mesmo com o carro de lado)
            fx, fz, m = volta["fx"][i], volta["fz"][i], 1.0
        else:  # voltas antigas: frente = direção do movimento
            a, b = max(i - 1, 0), min(i + 1, n - 1)
            fx, fz = x[b] - x[a], z[b] - z[a]
            m = math.hypot(fx, fz) or 1
        for roda, s in enumerate(sup[i]):  # RL, RR, FL, FR
            if s in tipos:
                lado = MEIA_BITOLA if roda % 2 else -MEIA_BITOLA
                eixo = (EIXO if roda >= 2 else -EIXO) / m
                out.add((round((x[i] + rx[i] * lado + fx * eixo) * 2) / 2,
                         round((z[i] + rz[i] * lado + fz * eixo) * 2) / 2))
    return sorted(out)


RAIO_ZEBRA = 2.5  # m: contato de roda até essa distância da borda marca aquele trecho como zebra. Calibre aqui.


def zebras_na_borda(borda, pts):
    """Trechos [início, fim] (índices da borda) com contato de roda na zebra por perto."""
    grade = {}
    for px, pz in pts:
        grade.setdefault((int(px // 2), int(pz // 2)), []).append((px, pz))
    perto = lambda x, z: any((px - x) ** 2 + (pz - z) ** 2 <= RAIO_ZEBRA ** 2
                             for i in range(-2, 3) for j in range(-2, 3)
                             for px, pz in grade.get((int(x // 2) + i, int(z // 2) + j), ()))
    marca = [perto(x, z) for x, z in borda]
    trechos, ini = [], None
    for k, m in enumerate(marca + [False]):
        if m and ini is None:
            ini = k
        elif not m and ini is not None:
            if k - ini >= 3:  # ignora contato isolado de 1 ou 2 metros
                trechos.append([ini, k - 1])
            ini = None
    return trechos


def zebras(pista):
    """Zebras da pista: os pontos que as rodas tocaram em todas as voltas e, se a pista tem bordas,
    os trechos de borda onde ficam (zebra só existe na beira da pista)."""
    # ponytail: relê todas as voltas da pista a cada pedido; cachear se ficar lento com muitas voltas
    pts = set()
    for m in listar():
        if m["pista"] == pista:
            pts.update(pontos_roda(ler_volta(m["arquivo"]), ZEBRA))
    pts, arq = sorted(pts), PASTA / "bordas" / f"{pista}.json"
    if not (pts and arq.exists()):
        return {"pontos": pts, "borda": None}
    b = json.loads(arq.read_text())
    return {"pontos": pts, "borda": {lado: zebras_na_borda(b[lado], pts) for lado in ("esq", "dir")}}


def cantos(pista):
    """Curvas oficiais da pista (pistas.json). Lido a cada pedido, para editar sem reiniciar."""
    try:
        return json.loads((PASTA / "pistas.json").read_text(encoding="utf-8")).get(str(pista), [])
    except (OSError, ValueError):
        return []


def primeiro(serie, ini, cond):
    return next((k for k in range(ini, len(serie)) if cond(serie[k])), len(serie) - 1)


def comparar(a, r):
    """Compara a volta `a` com a referência `r`, alinhadas pela distância na volta."""
    fim = min(a["d"][-1], r["d"][-1])
    grade = [i * PASSO for i in range(int(fim // PASSO) + 1)]
    A, R = reamostrar(a, grade), reamostrar(r, grade)
    n = len(grade)
    delta = [(A["t"][i] - R["t"][i]) / 1000 for i in range(n)]

    # Direção da linha da referência e deslocamento lateral da volta em relação a ela.
    tx, tz = [], []
    for i in range(n):
        i0, i1 = max(i - 1, 0), min(i + 1, n - 1)
        dx, dz = R["x"][i1] - R["x"][i0], R["z"][i1] - R["z"][i0]
        m = math.hypot(dx, dz) or 1
        tx.append(dx / m)
        tz.append(dz / m)
    lado = [tx[i] * (A["z"][i] - R["z"][i]) - tz[i] * (A["x"][i] - R["x"][i]) for i in range(n)]
    giro = [tx[i] * tz[i + 1] - tz[i] * tx[i + 1] for i in range(n - 1)] + [0]

    # Lado de dentro em cada ponto, pela curvatura da referência suavizada em ±40 m.
    w = 8
    dentro = [lado[i] * (1 if sum(giro[max(0, i - w):i + w]) >= 0 else -1) for i in range(n)]

    # Rodas na zebra / fora da pista, por ponto (só se as duas voltas têm superfície gravada).
    tem_sup = "sup" in A and "sup" in R
    if tem_sup:
        Za, Zr = ([bool(ZEBRA.intersection(s)) for s in V["sup"]] for V in (A, R))
        Fa = [bool(FORA.intersection(s)) for s in A["sup"]]

    # Zonas de frenagem da referência (junta trechos separados por até 15 m).
    zonas = []
    for i in (i for i in range(n) if R["freio"][i] > 0.1):
        if zonas and i - zonas[-1][1] <= 3:
            zonas[-1][1] = i
        else:
            zonas.append([i, i])

    oficiais = cantos(a.get("pista"))
    curvas = []
    limite = 0  # a busca não volta para dentro da curva anterior
    for k, (i0, i1) in enumerate(zonas):
        saida_r = primeiro(R["acel"], i1, lambda x: x >= 0.95)
        apice_r = min(range(i0, saida_r + 1), key=R["v"].__getitem__)
        fa = next((j for j in range(max(limite, i0 - 30), min(n, apice_r + 10)) if A["freio"][j] > 0.1), None)
        ini = i0 if fa is None else fa
        apice_a = min(range(ini, min(n, saida_r + 20)), key=A["v"].__getitem__)
        saida_a = primeiro(A["acel"], apice_a, lambda x: x >= 0.95)
        limite = max(apice_r, apice_a) + 1
        s, f = max(0, min(i0, ini) - 4), min(n - 1, max(saida_r, saida_a) + 4)
        sinal = 1 if sum(giro[i0:saida_r]) >= 0 else -1

        # Fases da curva, cortadas onde a referência solta o freio, no ápice dela e onde as duas aceleram tudo.
        # A 4ª fase, a reta, vai até a freada da próxima curva (calculada depois do laço).
        b = min(i1, apice_r)
        partes = []  # (fase, frase)
        if abs(dc := A["v"][s] - R["v"][s]) >= LIM_VEL_KMH:  # a causa está na curva anterior
            partes.append(("freada", f"chegou {abs(dc):.0f} km/h mais {'lento' if dc < 0 else 'rápido'}"))
        if fa is None:
            partes.append(("freada", "não freou (só aliviou)"))
        elif abs(df := (fa - i0) * PASSO) >= LIM_FREIO_M:
            partes.append(("freada", f"freou {abs(df):.0f} m {'antes' if df < 0 else 'depois'}"))
        if fa is not None and b > i0:  # força média no mesmo tanto de freada, cada um a partir do seu ponto
            L = b - i0 + 1
            fa_m, fr_m = statistics.fmean(A["freio"][fa:fa + L]), statistics.fmean(R["freio"][i0:i0 + L])
            if abs(fa_m - fr_m) >= LIM_PEDAL:
                partes.append(("freada", f"freou mais {'forte' if fa_m > fr_m else 'leve'} ({fa_m:.0%} vs {fr_m:.0%})"))
        if not any(fs == "freada" for fs, _ in partes):  # mesmo ponto e mesmo pedal, mas perdeu velocidade demais
            if (deficit := min(A["v"][j] - R["v"][j] for j in range(s, b + 1))) <= -2 * LIM_VEL_KMH:
                partes.append(("freada", f"desacelerou demais: até {-deficit:.0f} km/h abaixo na freada"))
        if abs(dv := A["v"][apice_a] - R["v"][apice_r]) >= LIM_VEL_KMH:
            partes.append(("entrada", f"{abs(dv):.0f} km/h {'mais lento' if dv < 0 else 'mais rápido'} no ápice"))
        if abs(da := (saida_a - saida_r) * PASSO) >= LIM_ACEL_M:
            partes.append(("saida", f"acelerou tudo {abs(da):.0f} m {'depois' if da > 0 else 'antes'}"))
        for rot, j, fase in (("entrou", i0, "entrada"), ("ápice", apice_r, "entrada"), ("saiu", saida_r, "saida")):
            if abs(m := sinal * lado[j]) >= LIM_LADO_M:
                partes.append((fase, f"{rot} {abs(m):.1f}".replace(".", ",") + f" m mais por {'dentro' if m > 0 else 'fora'}"))
        if tem_sup:
            trechos = (("na entrada", range(i0, apice_r), "entrada"),
                       ("no ápice", range(max(i0, apice_r - 3), min(n, apice_r + 4)), "entrada"),
                       ("na saída", range(apice_r + 1, min(n, saida_r + 7)), "saida"))
            for nome, rg, fase in trechos:
                za, zr = any(Za[j] for j in rg), any(Zr[j] for j in rg)
                if za != zr:
                    partes.append((fase, f"{'usou' if za else 'não usou'} a zebra {nome} (a referência {'não' if za else 'usou'})"))
            if fora := next(((nome, fase) for nome, rg, fase in trechos if any(Fa[j] for j in rg)), None):
                partes.append((fora[1], f"saiu da pista {fora[0]}"))
        if abs(ds := A["v"][f] - R["v"][f]) >= LIM_VEL_KMH:  # é o que decide a reta
            partes.append(("reta", f"saiu da curva {abs(ds):.0f} km/h mais {'lento' if ds < 0 else 'rápido'}"))

        oficial = min(oficiais, key=lambda o: abs(o["d"] - grade[apice_r]), default=None)
        if oficial and abs(oficial["d"] - grade[apice_r]) > 120:
            oficial = None
        nome = oficial["nome"] if oficial else f"Curva a {grade[i0]:.0f} m"
        if any(c["nome"] == nome for c in curvas):  # duas frenagens na mesma curva (ex.: Ferradura)
            nome += " (2ª parte)"
        curvas.append({
            "n": k + 1, "dist": grade[i0], "_cortes": [s, b, apice_r, f], "_partes": partes,
            "t": oficial and oficial["t"], "nome": nome,
            "frenagem_m": None if fa is None else (fa - i0) * PASSO,
            "vel_apice": round(A["v"][apice_a]), "vel_apice_ref": round(R["v"][apice_r]),
            "dentro": [round(sinal * lado[j], 2) for j in (i0, apice_r, saida_r)],
            "pontos_r": [i0, apice_r, saida_r], "pontos_a": [ini, apice_a, saida_a],
        })

    # A volta inteira fica dividida entre as curvas: cada uma vai do começo da sua freada até o começo da
    # próxima (a reta depois da curva é dela, porque a velocidade de saída decide a reta; a reta dos boxes,
    # que cruza a linha, é da última). Assim a soma das curvas é exatamente a diferença da volta.
    total = ((a["tempo_ms"] - r["tempo_ms"]) / 1000 if "tempo_ms" in a and "tempo_ms" in r
             else delta[-1] - delta[0])
    cortes = []
    for c in curvas:
        for j in c["_cortes"]:
            cortes.append(max(j, cortes[-1]) if cortes else j)  # curvas coladas não podem se sobrepor
    for k, c in enumerate(curvas):
        s, b, ap, f = cortes[4 * k:4 * k + 4]
        ultima = k == len(curvas) - 1
        prox = cortes[0] if ultima else cortes[4 * k + 4]
        reta = total - delta[f] + delta[prox] if ultima else delta[prox] - delta[f]  # a última leva o resto da volta
        rg = list(range(f, n)) + list(range(0, prox + 1)) if ultima else list(range(f, prox + 1))
        partes = c.pop("_partes")
        c.pop("_cortes")
        if rg and abs(dm := max(A["v"][j] for j in rg) - max(R["v"][j] for j in rg)) >= LIM_VEL_KMH:
            partes.append(("reta", f"velocidade máxima {abs(dm):.0f} km/h {'menor' if dm < 0 else 'maior'}"))
        perdas = {"freada": delta[b] - delta[s], "entrada": delta[ap] - delta[b], "saida": delta[f] - delta[ap], "reta": reta}
        c["fases"] = [{"fase": k2, "perda": round(v, 3), "partes": [t for fs, t in partes if fs == k2]}
                      for k2, v in perdas.items()]
        # Marcas do piloto (UDP Action 1) que caem neste trecho da volta, com a fase onde caíram.
        dentro_trecho = (lambda m: m >= grade[s] or m < grade[prox]) if ultima else (lambda m: grade[s] <= m < grade[prox])
        fase_da = lambda m: "reta" if ultima and m < grade[s] else next(
            fs for fs, j in (("freada", b), ("entrada", ap), ("saida", f), ("reta", n - 1)) if m < grade[j] or fs == "reta")
        c["marcada"] = [fase_da(m) for m in a.get("marcas") or [] if dentro_trecho(m)]
        c["partes"] = [t for _, t in partes]
        c["perda"] = round(sum(perdas.values()), 3)
        c["texto"] = (f"{c['nome']}: {'perdeu' if c['perda'] > 0 else 'ganhou'} {abs(c['perda']):.2f} s".replace(".", ",")
                      + (": " + ", ".join(c["partes"]) if c["partes"] else ""))

    dicas = sorted((c for c in curvas if c["perda"] > 0.02), key=lambda c: -c["perda"])
    arred = lambda xs, nd=2: [round(x, nd) for x in xs]
    return {
        "grade": grade, "delta": arred(delta, 3), "dentro": arred(dentro),
        "a": {k: arred(A[k]) for k in ("v", "acel", "freio", "x", "z")},
        "r": {k: arred(R[k]) for k in ("v", "acel", "freio", "x", "z")},
        "curvas": curvas, "dicas": dicas, "fora_a": pontos_roda(a, FORA), "cantos": oficiais, "total": round(total, 3),
    }


# ---------- Engenheiro (Claude) ----------

MODELO_RADIO = "claude-haiku-5-5"   # rádio: precisa chegar rápido, enquanto ele ainda pilota
MODELO_IA = "claude-sonnet-5-5"     # análise na tela e vícios: mais lento, mais esperto
ENGENHEIRO = """Você é o engenheiro de pista de um piloto de F1 24 (videogame, Xbox, controle).
Recebe a comparação entre uma volta do piloto e a volta de referência, curva a curva, já medida pelo programa de telemetria.
Escreva em português do Brasil, como no rádio: direto, frases curtas, sem enrolação.

Regras:
- Use só os dados recebidos. Não invente curvas, números nem nomes de curva.
- "Linha" é sempre relativa à volta de referência, não à borda da pista.
- Frear antes nem sempre é erro: se ele freou antes e ganhou tempo, diga isso.
- Curva com "marcada_pelo_piloto": ele apertou o botão ali para pedir explicação. Explique essas PRIMEIRO, na ordem da volta:
  por que foi mais lento ou mais rápido que a referência ali, citando a fase e a causa. Se ele foi bem, diga que foi bem e por quê.
- Use as fases para dizer a CAUSA: o tempo foi perdido na fase com perda > 0, então a correção é o que aconteceu nessa fase.
  O que aconteceu numa fase que ganhou tempo não é erro. Se uma fase perde por causa da anterior (ex.: ápice lento -> acelera tarde), diga a cadeia.
- "chegou X km/h mais lento/rápido" na freada NÃO é erro desta curva: vem da saída e da reta da curva anterior. Nesse caso,
  a correção é na curva anterior (sair mais rápido dela), nunca "chegar mais devagar" nesta.
- Não dê aula de pilotagem genérica; ligue cada conselho a um número da volta."""

FORMATO_TEXTO = """
Formato (lido na tela):
- Uma frase de resumo da volta (onde o tempo foi perdido ou ganho).
- Até 3 prioridades, da que mais vale tempo para a que menos vale. Cada uma: a curva (C1, C4…), o que fazer de diferente e por quê, citando os números.
- Uma linha final com algo que o piloto fez bem.
- Tempo perdido se escreve com +, tempo ganho com − (ex.: "C10 +0,43 s", "C4 −0,07 s"), como no resto do app."""

FORMATO_RADIO = """
Formato (falado no rádio por voz sintética, enquanto o piloto já faz a próxima volta):
- No máximo 2 frases curtas, umas 30 palavras no total. Só a curva que mais custou e a ação nela; se sobrar espaço, a segunda.
- Imperativo, como engenheiro no rádio ("Curva dez: freia cinco metros antes e sai sem zebra.").
- Texto puro para ser lido em voz alta: sem markdown, sem listas, sem símbolos (+, −, /, km/h, s, m). Escreva "curva dez", "quatro décimos", "quilômetros por hora", "metros".
- Não fale o tempo da volta nem a diferença total: o app já falou.
- Se houver curvas marcadas pelo piloto, fale SÓ delas (uma frase curta por marca, dizendo se perdeu ou ganhou e por quê).
  Nesse caso pode passar das 30 palavras."""

_analises = {}  # (volta, referência, modo) -> texto; ponytail: cache só em memória, some ao reiniciar

def dados_para_ia(a, r, res):
    """Resumo compacto da comparação: só o que o engenheiro precisa, sem as séries ponto a ponto."""
    curvas = [{"curva": c["nome"]} | {k: c[k] for k in ("perda", "frenagem_m", "vel_apice", "vel_apice_ref", "fases")}
              | ({"marcada_pelo_piloto": c["marcada"]} if c.get("marcada") else {})
              for c in res["curvas"]]
    return json.dumps({
        "pista": a.get("nome_pista"),
        "volta": {"tempo": fmt_tempo(a["tempo_ms"]), "invalida": a.get("invalida")},
        "referencia": {"tempo": fmt_tempo(r["tempo_ms"]), "invalida": r.get("invalida"),
                       "quem": f"o rival ({r.get('piloto')})" if r.get("carro") == "rival" else "o próprio piloto"},
        "diferenca_total_s": round((a["tempo_ms"] - r["tempo_ms"]) / 1000, 3),
        "curvas": curvas,
        "rodas_fora_da_pista": len(res["fora_a"]),
        "legenda": "perda > 0 = perdeu tempo; as perdas das curvas somam exatamente a diferença total. "
                   "frenagem_m < 0 = freou antes da referência; vel_apice em km/h (volta vs referência). "
                   "fases: a perda da curva dividida em freada (até a referência soltar o freio), entrada (até o "
                   "ápice), saida (do ápice até acelerar tudo) e reta (até a freada da próxima curva), com o que "
                   "aconteceu em cada uma. marcada_pelo_piloto: ele apertou um botão ali pedindo explicação "
                   "(a lista diz em que fase apertou).",
    }, ensure_ascii=False)


def perguntar_claude(modelo, sistema, entrada):
    """Pergunta ao Claude pelo Claude Code em modo não interativo (`claude -p`), usando a assinatura
    em que o Claude Code do Pi está logado. Sem ferramentas: só lê os dados e escreve."""
    claude = shutil.which("claude") or next((str(p) for p in [Path.home() / ".local/bin/claude"] if p.exists()), None)
    if not claude:
        raise RuntimeError("O Claude Code não está instalado neste aparelho.")
    cmd = [claude, "-p", "--model", modelo, "--effort", "medium", "--tools", "",
           "--no-session-persistence", "--output-format", "text", "--system-prompt", sistema]
    try:
        p = subprocess.run(cmd, input=entrada, capture_output=True, text=True, encoding="utf-8", timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("O Claude demorou demais para responder (5 min).")
    if p.returncode != 0 or not p.stdout.strip():
        detalhe = (p.stderr or p.stdout).strip().splitlines()
        raise RuntimeError("O Claude Code falhou" + (f": {detalhe[-1]}" if detalhe else ".")
                           + " Ele está logado? (rode `claude` uma vez no aparelho)")
    return p.stdout.strip()


def analisar(nome_a, nome_b, radio=False):
    """Insights de uma volta. radio=True: versão curta, em texto puro, para ser falada (Haiku, rápido)."""
    chave = (nome_a, nome_b, radio)
    if chave in _analises:
        return _analises[chave]
    a, r = ler_volta(nome_a), ler_volta(nome_b)
    if a.get("invalida") and not a.get("marcas"):  # marcada: ele quer saber o que houve, mesmo inválida
        raise RuntimeError("Volta inválida: sem análise do engenheiro.")
    _analises[chave] = perguntar_claude(MODELO_RADIO if radio else MODELO_IA,
                                        ENGENHEIRO + (FORMATO_RADIO if radio else FORMATO_TEXTO),
                                        dados_para_ia(a, r, comparar(a, r)))
    return _analises[chave]


# ---------- Vícios: o que se repete em muitas voltas ----------

VICIOS = """Você é o engenheiro de pista de um piloto de F1 24 (videogame, Xbox, controle).
Recebe a estatística de várias voltas recentes dele, cada uma comparada com a MELHOR volta dele na pista, curva a curva.
Seu trabalho: achar os vícios e cacoetes de pilotagem, isto é, o que ele faz de errado DE FORMA REPETIDA, não o erro de uma volta só.
Escreva em português do Brasil, direto, frases curtas.

Como ler os dados:
- "padroes" diz em quantos % das voltas cada coisa aconteceu. Vício é o que aparece em boa parte das voltas (uns 50% ou mais).
- "desvio" alto = inconsistência: ele não repete o ponto de freio, a velocidade ou a linha. Inconsistência também é vício.
- Procure o que se repete entre curvas diferentes (ex.: freia cedo em todas as curvas lentas, acelera tarde nas curvas de baixa, sempre usa a zebra de saída). Isso vale mais que um vício de uma curva só.
- "perda_media_s" diz quanto cada curva custa por volta, em média. Ordene pelo que mais custa tempo.
- A referência é a melhor volta dele: a diferença sistemática para ela é o hábito a corrigir.
- Use só os dados recebidos. Não invente curvas, números nem nomes de curva. Não dê aula genérica: ligue cada vício a números.

Formato (lido na tela do celular):
- Uma frase de diagnóstico geral (o tipo de piloto que os números mostram).
- Até 4 vícios, do que mais custa tempo para o que menos custa. Cada um em uma linha começando com **nome curto do vício**: onde acontece (C1, C4…), com que frequência, quanto custa por volta, e um exercício concreto para treinar.
- Uma linha final com o ponto forte mais consistente.
- Tempo perdido se escreve com + (ex.: "+0,15 s por volta")."""

_vicios = {}  # (pista, referência, voltas) -> texto; ponytail: só em memória, como _analises


def sem_numeros(frase):
    """'freou 15 m antes' -> 'freou antes': junta a mesma dica de voltas diferentes."""
    return re.sub(r"\s+", " ", re.sub(r"\d+(,\d+)?\s*(km/h|m)\b", "", frase)).strip()


def estatistica_vicios(nomes, nome_ref):
    """Junta a comparação de várias voltas com a referência, curva a curva."""
    r = ler_volta(nome_ref)
    voltas = [ler_volta(n) for n in nomes]
    por_curva = {}
    for v in voltas:
        for c in comparar(v, r)["curvas"]:
            por_curva.setdefault(c["n"], []).append(c)
    media = lambda xs, nd=1: round(statistics.fmean(xs), nd) if xs else None
    desvio = lambda xs, nd=1: round(statistics.stdev(xs), nd) if len(xs) > 1 else None
    curvas = []
    for cs in por_curva.values():
        frenagens = [c["frenagem_m"] for c in cs if c["frenagem_m"] is not None]
        dv = [c["vel_apice"] - c["vel_apice_ref"] for c in cs]
        acel = [(c["pontos_a"][2] - c["pontos_r"][2]) * PASSO for c in cs]
        contagem = {}
        for c in cs:
            for frase in {sem_numeros(p) for p in c["partes"]}:
                contagem[frase] = contagem.get(frase, 0) + 1
        curvas.append({
            "curva": cs[0]["nome"],
            "voltas": len(cs),
            "perda_media_s": media([c["perda"] for c in cs], 3),
            "perda_media_por_fase_s": {f["fase"]: media([g["perda"] for c in cs for g in c["fases"] if g["fase"] == f["fase"]], 3)
                                       for f in cs[0]["fases"]},
            "frenagem_m": {"media": media(frenagens), "desvio": desvio(frenagens),
                           "voltas_sem_frear": len(cs) - len(frenagens)},
            "vel_apice_kmh_vs_ref": {"media": media(dv), "desvio": desvio(dv), "ref": cs[0]["vel_apice_ref"]},
            "acelerou_tudo_m_vs_ref": {"media": media(acel), "desvio": desvio(acel)},
            "linha_m_por_dentro": dict(zip(("entrada", "apice", "saida"),
                                           (media([c["dentro"][k] for c in cs]) for k in range(3)))),
            "padroes": {f: f"{100 * k // len(cs)}%" for f, k in sorted(contagem.items(), key=lambda x: -x[1])
                        if k / len(cs) >= 0.3},
        })
    curvas.sort(key=lambda c: -c["perda_media_s"])
    tempos = [v["tempo_ms"] / 1000 for v in voltas]
    return {
        "pista": r.get("nome_pista"),
        "voltas_analisadas": len(voltas),
        "periodo": [min(v["quando"] for v in voltas), max(v["quando"] for v in voltas)],
        "melhor_volta": fmt_tempo(r["tempo_ms"]),
        "tempo_medio_vs_melhor_s": round(statistics.fmean(tempos) - r["tempo_ms"] / 1000, 3),
        "desvio_tempo_s": desvio(tempos, 3),
        "legenda": "frenagem_m < 0 = freia antes da referência; vel_apice < 0 = mais lento no ápice; "
                   "acelerou_tudo_m > 0 = acelera tudo depois; linha > 0 = mais por dentro que a referência.",
        "curvas": curvas,
    }


def ultimas_validas(pista, piloto, n=20):
    """(referência, últimas `n` voltas válidas sem ela): a referência é a melhor volta válida da pista."""
    validas = [v for v in listar() if v["pista"] == pista and v["carro"] == "eu" and not v["invalida"]
               and v["piloto"] == piloto]
    if not validas:
        raise RuntimeError("Nenhuma volta válida nesta pista.")
    ref = min(validas, key=lambda v: v["tempo_ms"])["arquivo"]
    nomes = [v["arquivo"] for v in sorted(validas, key=lambda v: v["quando"], reverse=True) if v["arquivo"] != ref]
    return ref, nomes[:n]


def vicios(pista, piloto=None):
    """Vícios nas últimas 20 voltas válidas da pista, contra a melhor volta (Sonnet)."""
    ref, nomes = ultimas_validas(pista, piloto)
    if len(nomes) < 5:
        raise RuntimeError(f"Poucas voltas para achar vícios ({len(nomes)}): dê pelo menos 5 voltas válidas além da melhor.")
    chave = (pista, ref, tuple(nomes))
    if chave not in _vicios:
        _vicios[chave] = perguntar_claude(MODELO_IA, VICIOS,
                                          json.dumps(estatistica_vicios(nomes, ref), ensure_ascii=False))
    return {"texto": _vicios[chave], "voltas": len(nomes)}


# ---------- Botões do controle: 1 marca um ponto da volta, 2 pede o relatório da última volta ----------

def ultima_e_ref(pista):
    """(minha última volta nesta pista, a melhor volta válida do mesmo piloto)."""
    minhas = [v for v in listar() if v["pista"] == pista and v["carro"] == "eu"]
    if not minhas:
        raise RuntimeError("Ainda não tenho volta nesta pista.")
    ultima = max(minhas, key=lambda v: v["quando"])
    minhas = [v for v in minhas if v["piloto"] == ultima["piloto"]]  # quem está pilotando agora
    validas = [v for v in minhas if not v["invalida"]] or minhas
    return ultima, min(validas, key=lambda v: v["tempo_ms"])


def explicar_marcas(nome, pista):
    """Fim de uma volta com marcas: o engenheiro explica cada uma no rádio, contra o rival (se houver) ou a melhor."""
    time.sleep(0.5)  # a volta do rival fecha no mesmo pacote: espera ela ser gravada
    vs = listar()
    rivais = [v for v in vs if v["pista"] == pista and v["carro"] == "rival" and not v["invalida"]]
    try:
        ref = min(rivais, key=lambda v: v["tempo_ms"]) if rivais else ultima_e_ref(pista)[1]
        texto = ("Contra o rival. " if rivais else "Contra a sua melhor. ") + analisar(nome, ref["arquivo"], radio=True)
    except (RuntimeError, OSError, ValueError) as erro:
        texto = str(erro)
    ESTADO.fala = {"id": ESTADO.fala["id"] + 1, "texto": texto}
    print(f"Marcas explicadas: {texto}", flush=True)


def relatorio(pista):
    """Botão 2: tempo da última volta, diferença para a melhor e a dica curta do rádio (Haiku)."""
    ultima, ref = ultima_e_ref(pista)
    s = ultima["tempo_ms"] / 1000
    texto = f"Última volta, {int(s // 60)} minuto e {s % 60:.1f}.".replace(".", ",", 1)
    if ultima["invalida"]:
        return texto + " Volta inválida."
    if ultima["arquivo"] == ref["arquivo"]:
        return texto + " Sua melhor volta!"
    d = (ultima["tempo_ms"] - ref["tempo_ms"]) / 1000
    texto += f" {abs(d):.{1 if abs(d) >= 1 else 2}f}".replace(".", ",") + f" {'segundos' if abs(d) >= 2 else 'segundo'} " \
             f"{'mais lenta' if d > 0 else 'mais rápida'} que a melhor."
    try:
        return texto + " " + analisar(ultima["arquivo"], ref["arquivo"], radio=True)
    except RuntimeError:
        return texto


def responder_botao(n):
    """Monta a frase do botão e deixa no /estado; a página que estiver com o rádio ligado fala."""
    e = ESTADO
    eu = next((c for c in list(e.carros.values()) if c.nome == "eu"), None)
    try:
        if n == 1:  # marca o ponto; o engenheiro explica no fim da volta
            if not (eu and eu.lap and eu.lap[0] >= 0):
                raise RuntimeError("Sem volta em andamento para marcar.")
            eu.marcas.append(round(eu.lap[0]))
            texto = "Anotado."
        else:
            texto = relatorio(e.pista)
    except (RuntimeError, OSError, ValueError) as erro:
        texto = str(erro)
    e.fala = {"id": e.fala["id"] + 1, "texto": texto}
    print(f"Botão {n}: {texto}", flush=True)


# ---------- Web ----------

_meta = {}  # arquivo -> (mtime, metadados)


def ler_volta(nome):
    if Path(nome).name != nome or not nome.endswith(".json"):
        raise FileNotFoundError(nome)
    return json.loads((VOLTAS / nome).read_text())


def listar():
    out = []
    for arq in sorted(VOLTAS.glob("*.json"), reverse=True):
        mt = arq.stat().st_mtime
        if _meta.get(arq.name, (None,))[0] != mt:
            v = json.loads(arq.read_text())
            _meta[arq.name] = (mt, {k: v.get(k) for k in ("pista", "nome_pista", "carro", "piloto", "tempo_ms", "invalida", "quando", "marcas")}
                               | {"arquivo": arq.name})
        out.append(_meta[arq.name][1])
    return out


class Web(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self.enviar((PASTA / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/voltas":
                return self.json(listar())
            if u.path == "/comparar":
                return self.json(comparar(ler_volta(q["a"][0]), ler_volta(q["b"][0])))
            if u.path == "/voz":
                try:
                    return self.enviar(voz(q["texto"][0]), "audio/wav")
                except RuntimeError:
                    pass  # sem Piper: 404, e a página usa a voz do aparelho
            if u.path == "/analisar":
                try:
                    return self.json({"texto": analisar(q["a"][0], q["b"][0], radio="radio" in q)})
                except RuntimeError as erro:
                    return self.json({"erro": str(erro)})
            if u.path == "/vicios":
                try:
                    return self.json(vicios(int(q["pista"][0]), q.get("piloto", [""])[0] or None))
                except RuntimeError as erro:
                    return self.json({"erro": str(erro)})
            if u.path == "/estado":
                return self.json(estado())
            if u.path == "/bordas":  # gerado pelo bordas.py; pista sem bordas -> {}
                arq = PASTA / "bordas" / f"{int(q['pista'][0])}.json"
                return self.json(json.loads(arq.read_text()) if arq.exists() else {})
            if u.path == "/zebras":
                return self.json(zebras(int(q["pista"][0])))
        except (KeyError, FileNotFoundError, ValueError):
            pass
        self.send_error(404)

    def json(self, obj):
        self.enviar(json.dumps(obj).encode(), "application/json")

    def enviar(self, corpo, tipo):
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-cache")  # sem isso o Safari mostra a página velha depois de um deploy
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args):
        pass


# ---------- Voz do rádio (Piper, TTS neural local) ----------

PIPER = Path.home() / "piper" / "piper" / "piper"          # binário de github.com/rhasspy/piper
VOZ_MODELO = Path.home() / "piper" / "pt_BR-faber-medium.onnx"
VOZ_RITMO = 0.9  # length_scale do Piper: < 1 fala mais rápido. Calibre aqui.


@functools.lru_cache(maxsize=64)  # as frases das curvas se repetem a cada volta
def voz(texto):
    """Texto -> WAV (mono, 16 bits), falado pelo Piper. ~0,4 s de processamento por segundo de fala no Pi 4."""
    if not (PIPER.exists() and VOZ_MODELO.exists()):
        raise RuntimeError("Piper não instalado.")
    cfg = json.loads(VOZ_MODELO.with_suffix(".onnx.json").read_text())
    p = subprocess.run([str(PIPER), "--model", str(VOZ_MODELO), "--output-raw", "--length_scale", str(VOZ_RITMO)],
                       input=texto[:600].replace("\n", " ").encode(), capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("O Piper falhou.")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(cfg["audio"]["sample_rate"])
        w.writeframes(p.stdout)
    return buf.getvalue()


def ip_local():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # não envia nada, só escolhe a interface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


# ---------- Self-check ----------

def volta_sintetica(freio_ini, dentro_m, zebra=False):
    """Reta de 500 m, curva à esquerda de 90° (raio 50 m) e reta de 400 m.
    zebra=True: as rodas da esquerda (de dentro) passam na zebra entre 505 e 530 m."""
    R, ini_c = 50.0, 500.0
    fim_c = ini_c + R * math.pi / 2
    cols = {k: [] for k in CAMPOS}
    t = 0.0
    for i in range(1500):
        s = i * 0.7
        if s < ini_c:
            x, z, nx, nz = s, 0.0, 0.0, 1.0
        elif s < fim_c:
            th = (s - ini_c) / R
            x, z = ini_c + R * math.sin(th), R - R * math.cos(th)
            nx, nz = -math.sin(th), math.cos(th)  # aponta para o centro (dentro)
        else:
            x, z, nx, nz = ini_c + R, R + (s - fim_c), -1.0, 0.0
        rampa = max(0.0, min(1.0, (s - (ini_c - 50)) / 50, ((fim_c + 50) - s) / 50))
        x, z = x + nx * dentro_m * rampa, z + nz * dentro_m * rampa
        if s < freio_ini:
            v = 83.0
        elif s < ini_c:
            v = 83.0 - (83.0 - 28.0) * (s - freio_ini) / (ini_c - freio_ini)
        elif s < fim_c:
            v = 28.0
        else:
            v = min(83.0, 28.0 + (s - fim_c) * 0.3)
        freio = 1.0 if freio_ini <= s < ini_c else 0.0
        acel = 0.0 if freio_ini <= s < fim_c else 1.0
        sup = [1, 0, 1, 0] if zebra and 505 <= s < 530 else [0, 0, 0, 0]  # RL, RR, FL, FR
        # direita = -dentro; frente = dentro girado 90° (nz, -nx)
        for k, val in zip(CAMPOS, (s, t * 1000, v * 3.6, acel, freio, 5, x, z, sup, -nx, -nz, nz, -nx)):
            cols[k].append(val)
        t += 0.7 / v
    return cols


def teste():
    ref = volta_sintetica(freio_ini=380, dentro_m=0)
    a = volta_sintetica(freio_ini=365, dentro_m=2, zebra=True)
    res = comparar(a, ref)
    assert len(res["curvas"]) == 1, res["curvas"]
    c = res["curvas"][0]
    assert abs(c["frenagem_m"] + 15) <= PASSO, c                     # freou 15 m antes
    assert abs(c["dentro"][1] - 2) < 0.3, c                          # ápice 2 m por dentro
    assert c["perda"] > 0 and res["dicas"] == [c], c                 # frear antes custa tempo
    assert "freou 15 m antes" in c["texto"] and "ápice 2,0 m mais por dentro" in c["texto"], c["texto"]
    i = res["grade"].index(530.0)
    assert abs(res["dentro"][i] - 2) < 0.3, res["dentro"][i]         # gráfico: + = dentro

    # Zebra: a dica aponta, e as rodas ficam no lado de dentro (raio 50 - 2 - 0,8 m do centro da curva).
    assert "usou a zebra no ápice (a referência não)" in c["texto"], c["texto"]
    assert "saiu da pista" not in c["texto"] and res["fora_a"] == [], c["texto"]
    pts = pontos_roda(a, ZEBRA)
    assert pts and all(abs(math.hypot(x - 500, z - 50) - 47.2) < 0.6 for x, z in pts), pts[:5]

    # Espelhada (curva à direita): o "dentro" tem que continuar positivo.
    espelha = lambda v: {**v, **{k: [-z for z in v[k]] for k in ("z", "rz", "fz")}}
    c2 = comparar(espelha(a), espelha(ref))["curvas"][0]
    assert abs(c2["dentro"][1] - 2) < 0.3, c2

    # Sincronia: telemetria e posição só viram amostra quando são do mesmo quadro, chegando em qualquer ordem.
    e = Estado()
    pacote = lambda pid, quadro, corpo: HEADER.pack(2024, 24, 1, 0, 1, pid, 7, 0.0, quadro, quadro, 0, 255) + corpo
    processar(pacote(2, 11, LAP.pack(0, 1000, 100.0, 1, 0) + bytes(TAM_LAP * 22 - LAP.size + 2)[:-1] + b"\xff"), e)
    processar(pacote(6, 11, TEL.pack(200, 1.0, 0.0, 0.0, 0, 5, 1, 0, 0, 0) + bytes(TAM_TEL * 22 - TEL.size + 3)), e)
    mot = lambda x: MOT.pack(x, 0.0, 0.0, 32767, 0, 0, 0, 0, 32767) + bytes(TAM_MOT * 22 - MOT.size)
    processar(pacote(0, 10, mot(1.0)), e)  # posição do quadro anterior: não pode virar amostra
    assert e.carros[0].am == [], e.carros[0].am
    processar(pacote(0, 11, mot(5.0)), e)  # mesmo quadro da telemetria
    am = e.carros[0].am
    assert len(am) == 1 and am[0][6] == 5.0 and am[0][8] == [1, 0, 0, 0] and am[0][11] == 1.0, am
    # Parado na mesma distância (o fantasma espera no fim da volta): fica a 1ª amostra, com o tempo de quando chegou.
    processar(pacote(2, 12, LAP.pack(0, 2000, 100.0, 1, 0) + bytes(TAM_LAP * 22 - LAP.size + 2)[:-1] + b"\xff"), e)
    processar(pacote(6, 12, TEL.pack(0, 0.0, 0.0, 0.0, 0, 5, 1, 0, 0, 0) + bytes(TAM_TEL * 22 - TEL.size + 3)), e)
    processar(pacote(0, 12, mot(5.0)), e)
    assert len(e.carros[0].am) == 1 and e.carros[0].am[0][1] == 1000, e.carros[0].am

    # Zebra na borda: contato a 1 m de uma borda reta marca só aquele trecho.
    borda = [(float(k), 0.0) for k in range(101)]
    trechos = zebras_na_borda(borda, [(float(k), 1.0) for k in range(40, 61)])
    assert len(trechos) == 1 and trechos[0][0] <= 40 and trechos[0][1] >= 60 and trechos[0][0] >= 37, trechos
    # Botões: só o aperto conta (segurar ou soltar não repete), cada UDP Action vira sua ação,
    # e o mesmo botão de novo em menos de 1 s é o jogo repetindo o evento (visto com o F1 24 de verdade).
    e2, pedidos = Estado(), []
    e2.acao = pedidos.append
    for status in (BOTOES[1], BOTOES[1], 0, BOTOES[1] | BOTOES[2], 0x1):
        processar(pacote(3, 12, b"BUTN" + struct.pack("<I", status)), e2)
    assert pedidos == [1, 2], pedidos

    # Dica falada: freou 15 m antes e fez o ápice 2 m por dentro -> manda frear mais tarde e abrir o ápice.
    # Fases: somam a perda da curva, e frear 15 m antes custa tempo na freada.
    assert abs(sum(f["perda"] for f in c["fases"]) - c["perda"]) < 0.002, c["fases"]
    assert c["fases"][0]["perda"] > 0.05 and "freou 15 m antes" in c["fases"][0]["partes"], c["fases"]
    print("fases:", [(f["fase"], f["perda"]) for f in c["fases"]])
    # A curva leva a volta inteira (com a reta), então a soma das curvas é a diferença total.
    assert abs(sum(k["perda"] for k in res["curvas"]) - res["total"]) < 0.002, (res["total"], c["fases"])
    # Marca: apertou no ápice (~540 m) e na reta (900 m) -> as duas caem nesta curva, cada uma na sua fase.
    cm = comparar({**a, "marcas": [540, 900]}, ref)["curvas"][0]
    assert cm["marcada"] == ["entrada", "reta"] or cm["marcada"] == ["saida", "reta"], cm["marcada"]

    # Participants: o nome de cada carro sai do pacote 4 (60 bytes por carro no F1 24).
    corpo = bytearray(1 + 22 * 60)
    corpo[1 + 3 * 60 + 7:1 + 3 * 60 + 7 + 6] = "Árthur".encode()[:6]
    processar(pacote(4, 13, bytes(corpo)), e2)
    assert e2.nomes[3] == "Árthu" and e2.nomes[0] is None, e2.nomes  # UTF-8, e carro sem nome = None

    # Rival: corta onde o fantasma parou na linha e refaz a velocidade pelo trajeto.
    # Como no jogo: 1,5 m por quadro a 60 Hz (324 km/h), velocidade do jogo errada (226), parado na linha e um
    # último pulinho de 0,4 m quando o seu carro cruza.
    am = [(1.5 * i, round(i * 1000 / 60), 226, 1.0, 0.0, 8) for i in range(1, 401)]
    am += [(600.0, 7000 + k, 0, 0.0, 0.0, 8) for k in range(5)] + [(600.4, 7500, 0, 0.0, 0.0, 8)]
    f = volta_do_fantasma(am)
    assert f[-1][:2] == (600.0, 6667) and all(abs(a[2] - 324) <= 1 for a in f[10:-10]), (f[-1], f[200])

    # Vícios: a mesma dica com números diferentes vira um padrão só.
    assert sem_numeros("freou 15 m antes") == sem_numeros("freou 5 m antes") == "freou antes"
    assert sem_numeros("entrou 1,2 m mais por dentro") == "entrou mais por dentro"
    assert sem_numeros("3 km/h mais lento no ápice") == "mais lento no ápice"
    print("ok:", c["texto"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # acentos no terminal do Windows
    if "--teste" in sys.argv:
        teste()
    else:
        threading.Thread(target=receptor, daemon=True).start()
        ip = ip_local()
        print(f"Escutando UDP {PORTA_UDP}: no jogo, coloque IP = {ip}")
        print(f"Página: http://{ip}:{PORTA_HTTP}", flush=True)
        ThreadingHTTPServer(("", PORTA_HTTP), Web).serve_forever()
